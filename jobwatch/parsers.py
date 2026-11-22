from __future__ import annotations

import logging
import os
import re
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Job

log = logging.getLogger("jobwatch.parsers")

UA = "Mozilla/5.0 (compatible; JobWatch/1.0; personal job alert)"
TIMEOUT = 30
PARSERS: dict = {}


def register(name):
    def deco(fn):
        PARSERS[name] = fn
        return fn
    return deco


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    return s


def fetch_jobs(company: dict, session: requests.Session) -> list[Job]:
    kind = company.get("type", "html")
    if kind not in PARSERS:
        raise ValueError(f"Неизвестный type '{kind}' (доступны: {', '.join(PARSERS)})")
    return PARSERS[kind](company, session)


def normalize_url(url: str, strip_query: bool = False) -> str:
    url, _ = urldefrag(url.strip())
    if strip_query:
        p = urlsplit(url)
        url = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    return url


def dig(obj, path: str):
    """dig(data, 'a.b.0.c') — достать значение по точечному пути."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


# ---------------------------------------------------------------- ATS с API
@register("greenhouse")
def greenhouse(c, s):
    r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs", timeout=TIMEOUT)
    r.raise_for_status()
    return [
        Job(c["name"], j["title"], j["absolute_url"], (j.get("location") or {}).get("name", ""))
        for j in r.json().get("jobs", [])
    ]


@register("lever")
def lever(c, s):
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    r = s.get(f"https://{host}/v0/postings/{c['board']}", params={"mode": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        Job(c["name"], j["text"], j["hostedUrl"], (j.get("categories") or {}).get("location") or "")
        for j in r.json()
    ]


@register("workable")
def workable(c, s):
    acc = c["account"]
    url = f"https://apply.workable.com/api/v3/accounts/{acc}/jobs"
    body = {"query": c.get("query", ""), "location": [], "department": [], "worktype": [], "remote": []}
    jobs, token = [], None
    for _ in range(20):
        if token:
            body["token"] = token
        r = s.post(url, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for j in data.get("results", []):
            loc = j.get("location") or {}
            jobs.append(Job(
                c["name"], j["title"],
                f"https://apply.workable.com/{acc}/j/{j['shortcode']}/",
                ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
            ))
        token = data.get("nextPage")
        if not token:
            break
    return jobs


@register("hh")
def hh(c, s):
    contact = os.getenv("MAIL_TO") or "contact@example.com"
    headers = {"HH-User-Agent": f"JobWatch/1.0 ({contact})"}
    if os.getenv("HH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['HH_TOKEN']}"
    params = {"per_page": 100, "page": 0, **c.get("params", {})}
    if c.get("employer_id"):
        params["employer_id"] = c["employer_id"]
    jobs = []
    for page in range(20):
        params["page"] = page
        r = s.get("https://api.hh.ru/vacancies", params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for j in data.get("items", []):
            jobs.append(Job(c["name"], j["name"], normalize_url(j["alternate_url"], True),
                            (j.get("area") or {}).get("name", "")))
        if page + 1 >= data.get("pages", 1):
            break
    return jobs


@register("json")
def json_api(c, s):
    r = s.request(c.get("method", "GET"), c["url"], json=c.get("body"), params=c.get("params"),
                  headers=c.get("headers"), timeout=TIMEOUT)
    r.raise_for_status()
    items = dig(r.json(), c.get("items_path", "")) or []
    jobs = []
    for it in items:
        title = dig(it, c["title_field"])
        url = dig(it, c["url_field"]) if c.get("url_field") else c["url_template"].format_map(it)
        if title and url:
            url = urljoin(c["url"], str(url))
            jobs.append(Job(c["name"], str(title), url, str(dig(it, c.get("location_field", "")) or "")))
    return jobs


# ---------------------------------------------------------------- HTML
def _find_link(el, title_el, c):
    if c.get("link_selector"):
        return el.select_one(c["link_selector"])
    for cand in (el, title_el):
        if cand is not None and cand.name == "a" and cand.get("href"):
            return cand
    return el.find("a", href=True)


def _card_text(el, sel: str, limit: int = 3000) -> str:
    """Текст всей карточки вакансии: поднимаемся вверх, пока внутри только ссылка на эту вакансию."""
    node = el
    while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
        if len({a.get("href") for a in node.parent.select(sel)}) > 1:
            break
        node = node.parent
    return node.get_text(" ", strip=True)[:limit]


def extract_from_html(html: str, c: dict, base_url: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    strip_q = c.get("strip_query", False)
    rx = re.compile(c["url_regex"]) if c.get("url_regex") else None
    jobs: list[Job] = []

    sel = c.get("item_selector")
    if sel:
        for el in soup.select(sel):
            title_el = el.select_one(c["title_selector"]) if c.get("title_selector") else el
            if title_el is None:
                continue
            title = title_el.get_text(" ", strip=True)
            if not title:
                continue
            link = _find_link(el, title_el, c)
            loc_el = el.select_one(c["location_selector"]) if c.get("location_selector") else None
            location = loc_el.get_text(" ", strip=True) if loc_el else ""
            if c.get("context") == "card":
                text = _card_text(el, sel)
            else:
                text = el.get_text(" ", strip=True)[:3000] if c.get("title_selector") else ""
            if link is not None and link.get("href"):
                url = normalize_url(urljoin(base_url, link["href"]), strip_q)
                if rx and not rx.search(url):
                    continue
                jobs.append(Job(c["name"], title, url, location, text=text))
            else:  # у вакансии нет своей ссылки — ключом будет заголовок
                jobs.append(Job(c["name"], title, base_url, location, id=f"{base_url}#{title}", text=text))
        return jobs

    # Без селекторов: все ссылки страницы, дальше работает фильтр по ключевым словам
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = a.get_text(" ", strip=True)
        if 5 <= len(text) <= 200:
            url = normalize_url(urljoin(base_url, href), strip_q)
            if not rx or rx.search(url):
                jobs.append(Job(c["name"], text, url))
    return jobs


@register("html")
def html_page(c, s):
    url_tpl = c["url"]
    paged = "{page}" in url_tpl
    seen: dict[str, Job] = {}
    start = c.get("page_start", 1)
    for page in range(start, start + (c.get("max_pages", 10) if paged else 1)):
        url = url_tpl.format(page=page) if paged else url_tpl
        r = s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        if r.encoding and r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        before = len(seen)
        for j in extract_from_html(r.text, c, url):
            seen.setdefault(j.key, j)
        if paged and len(seen) == before:  # новая страница ничего не добавила — конец
            break
    return list(seen.values())


# ---------------------------------------------------------------- Автоопределение JSON (для SPA-сайтов)
TITLE_KEYS = ("title", "vacancyName", "vacancy_name", "jobTitle", "job_title", "position",
              "profession", "name", "header")
STRONG_TITLE_KEYS = ("title", "vacancyName", "vacancy_name", "jobTitle", "job_title", "position", "profession")
ID_KEYS = ("id", "uuid", "slug", "code", "vacancyId", "vacancy_id", "externalId", "external_id")
URL_KEYS = ("url", "link", "href", "absolute_url", "alternate_url", "hostedUrl", "detailUrl", "path")
LOC_KEYS = ("city", "location", "region", "address", "area", "office")


def _qualifying_lists(obj):
    """Все списки словарей, найденные где угодно внутри JSON."""
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            if len(x) >= 2 and all(isinstance(i, dict) for i in x[:5]):
                yield x
            stack.extend(i for i in x if isinstance(i, (dict, list)))
        elif isinstance(x, dict):
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))


def _title_key(items, c):
    keys = [c["title_field"]] if c.get("title_field") else list(TITLE_KEYS)
    for k in keys:
        n = sum(1 for i in items if isinstance(i, dict) and isinstance(i.get(k), str) and len(i[k].strip()) >= 3)
        if n >= max(2, 0.6 * len(items)):
            return k
    return None


def _score(items, tk) -> float:
    avg_keys = sum(len(i) for i in items if isinstance(i, dict)) / len(items)
    if avg_keys < 3:  # {id, name} — это справочники (города, категории), а не вакансии
        return 0.0
    return len(items) * avg_keys * (2 if tk in STRONG_TITLE_KEYS else 1)


def _short_text(obj, depth=0, out=None) -> str:
    """Короткие строковые значения карточки (уровень, отдел, теги); длинные описания пропускаем."""
    top = out is None
    out = [] if top else out
    if isinstance(obj, str):
        if len(obj) <= 80:
            out.append(obj)
    elif isinstance(obj, dict) and depth < 3:
        for v in obj.values():
            _short_text(v, depth + 1, out)
    elif isinstance(obj, list) and depth < 3:
        for v in obj[:30]:
            _short_text(v, depth + 1, out)
    return " ".join(out)[:1500] if top else ""


def _loc_of(it) -> str:
    for k in LOC_KEYS:
        v = it.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "title", "city"):
                if isinstance(v.get(kk), str):
                    return v[kk]
    return ""


def _items_to_jobs(items, tk, c, page_url) -> list[Job]:
    jobs, seen = [], set()
    strip_q = c.get("strip_query", False)
    for it in items:
        title = it.get(tk)
        if not isinstance(title, str) or not title.strip():
            continue
        title = title.strip()
        ident = next((str(it[k]) for k in ID_KEYS if it.get(k) not in (None, "")), "")
        url = None
        if c.get("url_field"):
            url = dig(it, c["url_field"])
        else:
            url = next((it[k] for k in URL_KEYS if isinstance(it.get(k), str) and it[k].strip()), None)
        if not url and c.get("url_template"):
            try:
                url = c["url_template"].format_map(it)
            except (KeyError, IndexError):
                url = None
        text = _short_text(it)
        if url:
            job = Job(c["name"], title, normalize_url(urljoin(page_url, str(url)), strip_q), _loc_of(it), text=text)
        else:  # ссылки в JSON нет: ключом будет id, ссылка в письме поведёт на страницу списка
            job = Job(c["name"], title, page_url, _loc_of(it),
                      id=f"{page_url}#{ident or title}", text=text)
        if job.key not in seen:
            seen.add(job.key)
            jobs.append(job)
    return jobs


def jobs_from_json(captured, c, page_url) -> list[Job]:
    """captured: [(url_ответа, json)]. Выбираем эндпоинт, чей ответ больше всего похож на список вакансий."""
    groups: dict = {}
    for url, payload in captured:
        best = None
        for items in _qualifying_lists(payload):
            tk = _title_key(items, c)
            if not tk:
                continue
            sc = _score(items, tk)
            if sc and (best is None or sc > best[0]):
                best = (sc, items, tk)
        if best:
            p = urlsplit(url)
            key = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
            g = groups.setdefault(key, {"score": 0.0, "items": [], "tk": best[2]})
            g["score"] += best[0]
            g["items"] += best[1]
    if c.get("api_filter"):
        groups = {k: v for k, v in groups.items() if c["api_filter"] in k}
    if not groups:
        return []
    for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["score"]):
        log.debug("%s: кандидат API %s — %d элементов, поле заголовка '%s', оценка %.0f",
                  c["name"], k, len(v["items"]), v["tk"], v["score"])
    api, g = max(groups.items(), key=lambda kv: kv[1]["score"])
    log.info("%s: выбран API %s (%d элементов, заголовок в поле '%s')", c["name"], api, len(g["items"]), g["tk"])
    return _items_to_jobs(g["items"], g["tk"], c, page_url)


# ---------------------------------------------------------------- Браузер
@register("playwright")
def playwright_page(c, s):
    """Рендерит страницу в браузере. Есть item_selector → разбор DOM. Нет → автопоиск JSON-API сайта,
    а если и его нет — все ссылки страницы."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    captured: list = []

    def on_response(resp):
        try:
            if resp.status == 200 and "json" in (resp.headers.get("content-type") or ""):
                captured.append((resp.url, resp.json()))
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(locale=c.get("locale", "ru-RU"), user_agent=c.get("user_agent"))
            page = ctx.new_page()
            page.on("response", on_response)
            page.goto(c["url"], wait_until="domcontentloaded", timeout=60000)
            try:
                if c.get("wait_for"):
                    page.wait_for_selector(c["wait_for"], timeout=30000)
                else:
                    page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                log.warning("%s: не дождались загрузки, берём что есть", c["name"])
            for _ in range(c.get("load_more_clicks", 0)):
                btn = page.query_selector(c["load_more_selector"]) if c.get("load_more_selector") else None
                if not btn or not btn.is_visible():
                    break
                btn.click()
                page.wait_for_timeout(1500)
            last, same = -1, 0
            for _ in range(c.get("scroll", 0)):  # прокручиваем, пока страница растёт
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(c.get("scroll_pause_ms", 1200))
                h = page.evaluate("document.body.scrollHeight")
                same = same + 1 if h == last else 0
                last = h
                if same >= 2:
                    break
            html = page.content()
        finally:
            browser.close()

    if c.get("item_selector"):
        return extract_from_html(html, c, c["url"])
    jobs = jobs_from_json(captured, c, c["url"])
    if jobs:
        return jobs
    log.warning("%s: JSON-API со списком вакансий не найден (перехвачено ответов: %d), беру ссылки со страницы",
                c["name"], len(captured))
    return extract_from_html(html, c, c["url"])
