import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from jobwatch import main as jm
from jobwatch.filters import JobFilter
from jobwatch.models import Job
from jobwatch.notifier import build_digest
from jobwatch.parsers import dig, extract_from_html

REAL_CFG = json.load(open("config.json", encoding="utf-8"))
# для общих тестов механики — фиксированный конфиг, не зависящий от ваших настроек
CFG = {
    "grade": {"include": ["senior", "sr.", "lead", "staff", "старший", "ведущ"],
              "exclude": ["junior", "intern", "trainee", "стажер", "стажёр", "младший"]},
    "direction": {"include": ["python", "backend", "back-end", "бэкенд", "data engineer"], "exclude": []},
}


def J(title, loc=""):
    return Job("X", title, "https://x/" + title.replace(" ", "-"), loc)


def test_filter():
    f = JobFilter(CFG)
    assert f.matches(J("Senior Python Developer"))
    assert f.matches(J("Ведущий backend-разработчик"))
    assert f.matches(J("Lead Backend Engineer"))
    assert not f.matches(J("Junior Python Developer"))
    assert not f.matches(J("Senior Python Intern"))
    assert not f.matches(J("Senior Designer"))
    assert not f.matches(J("Python Developer"))          # нет грейда
    assert not f.matches(J("Misleading Python"))          # 'lead' внутри слова не считается


def test_location_filter():
    f = JobFilter({**CFG, "locations": ["remote", "london"]})
    assert f.matches(J("Senior Python Dev", "Remote, EU"))
    assert not f.matches(J("Senior Python Dev", "Berlin"))
    assert f.matches(J("Senior Python Dev", ""))          # локация неизвестна — не отсекаем


def test_dig():
    d = {"a": {"b": [{"c": 5}]}}
    assert dig(d, "a.b.0.c") == 5 and dig(d, "a.x.y") is None


HTML = """<ul>
<li class=v><h3>Senior Python Developer</h3><a href="/job/1?utm=1#top">more</a><span class=c>Remote</span></li>
<li class=v><h3>Junior Python Developer</h3><a href="/job/2">more</a></li>
<li class=v><h3>Lead Backend Engineer</h3></li></ul>
<a href="/careers/9">Principal Python Engineer</a><a href="#x">menu item</a>"""


def test_html_selectors_and_fallback():
    c = {"name": "X", "item_selector": "li.v", "title_selector": "h3", "location_selector": ".c",
         "strip_query": True}
    jobs = extract_from_html(HTML, c, "https://site.io/jobs")
    assert [j.title for j in jobs] == ["Senior Python Developer", "Junior Python Developer", "Lead Backend Engineer"]
    assert jobs[0].url == "https://site.io/job/1" and jobs[0].location == "Remote"
    assert jobs[2].key == "https://site.io/jobs#Lead Backend Engineer"
    auto = extract_from_html(HTML, {"name": "X"}, "https://site.io/jobs")
    assert [j.title for j in auto] == ["Principal Python Engineer"]  # короткие ("more") и якорные (#x) ссылки отброшены


def test_digest():
    s, t, h = build_digest({"A": [J("Senior Python")]}, [("B", 3, "boom")])
    assert "Новые вакансии: 1" in s and "https://x/Senior-Python" in t and "boom" in h


class Handler(BaseHTTPRequestHandler):
    page = {"html": HTML}

    def do_GET(self):
        if self.path.startswith("/broken"):
            self.send_response(500); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write(Handler.page["html"].encode())

    def log_message(self, *a): pass


def test_end_to_end(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    companies = [
        {"name": "Local", "type": "html", "url": base + "/jobs", "item_selector": "li.v", "title_selector": "h3",
         "link_selector": "a", "strip_query": True},
        {"name": "Broken", "type": "html", "url": base + "/broken"},
    ]
    (tmp_path / "c.json").write_text(json.dumps(companies))
    cfg = {**CFG, "delay_seconds": 0, "alert_after_failures": 2}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    sent = []
    monkeypatch.setattr(jm, "send_email", lambda s, t, h: sent.append((s, t)))
    monkeypatch.setattr("jobwatch.parsers.make_session", lambda: __import__("requests").Session())
    monkeypatch.setattr(jm, "make_session", lambda: __import__("requests").Session())
    argv = ["--companies", str(tmp_path / "c.json"), "--config", str(tmp_path / "cfg.json"),
            "--state", str(tmp_path / "s.json")]

    assert jm.main(argv) == 0
    assert len(sent) == 1 and "Senior Python Developer" in sent[0][1] and "Junior" not in sent[0][1]
    assert jm.main(argv) == 0                      # второй запуск: новых нет, но у Broken 2-й сбой → алерт
    assert len(sent) == 2 and "Broken" in sent[1][1]
    assert jm.main(argv) == 0 and len(sent) == 2   # третий: ни новых, ни повторного алерта

    Handler.page["html"] = HTML.replace("</ul>", '<li class=v><h3>Staff Python Engineer</h3><a href="/job/7">m</a></li></ul>')
    assert jm.main(argv) == 0
    assert len(sent) == 3 and "Staff Python Engineer" in sent[2][1] and "Senior Python Developer" not in sent[2][1]

    st = json.load(open(tmp_path / "s.json"))
    assert len(st["companies"]["Local"]) == 3      # Senior, Lead, Staff
    srv.shutdown()


def test_email_failure_keeps_state(tmp_path, monkeypatch):
    import pytest
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Handler.page["html"] = HTML
    c = [{"name": "L", "type": "html", "url": f"http://127.0.0.1:{srv.server_port}/", "item_selector": "li.v",
          "title_selector": "h3", "link_selector": "a"}]
    (tmp_path / "c.json").write_text(json.dumps(c))
    (tmp_path / "cfg.json").write_text(json.dumps({**CFG, "delay_seconds": 0}))

    def boom(*a): raise RuntimeError("smtp down")
    monkeypatch.setattr(jm, "send_email", boom)
    argv = ["--companies", str(tmp_path / "c.json"), "--config", str(tmp_path / "cfg.json"),
            "--state", str(tmp_path / "s.json")]
    with pytest.raises(RuntimeError):
        jm.main(argv)
    assert not (tmp_path / "s.json").exists()      # письмо не ушло → ничего не «забыто»
    srv.shutdown()


# ------------------------------------------------------------------ новые проверки
from jobwatch.parsers import jobs_from_json


def test_real_config_filter():
    f = JobFilter(REAL_CFG)
    ok = ["Стажер AI-автоматизации (Go + LLM)", "Junior Data Scientist", "Стажёр-аналитик данных",
          "ML Engineer (Junior/Middle)", "Data Engineer, начинающий специалист", "Младший специалист по данным",
          "Intern, Machine Learning", "Джун Data Scientist"]
    bad = ["Senior ML Engineer", "Middle+/Senior ML Engineer в Рекомендации", "Ведущий Data Scientist",
           "Продавец (Розничная сеть МТС) Без опыта", "Стажер-разработчик (PHP)", "Junior Frontend Developer",
           "Team Lead Data Engineering", "Data Scientist Middle", "Junior HTML верстальщик"]
    for t in ok:
        assert f.matches(J(t)), t
    for t in bad:
        assert not f.matches(J(t)), t


def test_card_text_context_and_url_regex():
    html = """<div class=list>
    <div class=card><a href="/vacancies/1000001"></a><a href="/vacancies/1000001"><img></a>
      <a href="/vacancies/1000001">Аналитик данных</a><span>Junior</span><span>SQL</span></div>
    <div class=card><a href="/vacancies/1000002">ML Engineer</a><span>Lead</span></div>
    <a href="/vacancies/skills/sql">SQL skill</a></div>"""
    c = {"name": "H", "item_selector": "a[href*='/vacancies/1000']", "url_regex": r"/vacancies/1000\d+$",
         "context": "card", "strip_query": True}
    jobs = extract_from_html(html, c, "https://h.ru/c/vacancies")
    assert [j.title for j in jobs] == ["Аналитик данных", "ML Engineer"]
    assert "Junior" in jobs[0].text and "Lead" not in jobs[0].text       # карточки не смешиваются
    assert "Lead" in jobs[1].text and "Junior" not in jobs[1].text
    f = JobFilter(REAL_CFG)
    assert f.matches(jobs[0]) and not f.matches(jobs[1])                   # уровень берётся из карточки


def test_json_sniffing():
    noise = ("https://s.ru/api/cities", [{"id": 1, "name": "Москва"}, {"id": 2, "name": "Казань"}] * 20)
    jobs_payload = {"data": {"items": [
        {"id": 11, "title": "Junior Data Scientist", "slug": "jds", "city": {"name": "Москва"},
         "grade": "Junior", "description": "x" * 500},
        {"id": 12, "title": "Senior Backend", "slug": "sb", "city": {"name": "Казань"}, "grade": "Senior",
         "description": "Работа с ML командой, " + "очень длинное описание " * 6},
        {"id": 13, "title": "Стажёр ML", "slug": "sml", "city": {"name": "СПб"}, "grade": "Intern"},
    ]}}
    c = {"name": "S", "url_template": "https://s.ru/vacancy/{slug}"}
    jobs = jobs_from_json([noise, ("https://s.ru/api/vacancies?page=1", jobs_payload)], c, "https://s.ru/jobs")
    assert [j.title for j in jobs] == ["Junior Data Scientist", "Senior Backend", "Стажёр ML"]
    assert jobs[0].url == "https://s.ru/vacancy/jds" and jobs[0].location == "Москва"
    assert "Junior" in jobs[0].text and "xxxx" not in jobs[0].text         # длинные описания не попадают в фильтр
    assert "ML" not in jobs[1].text                                         # описание Senior не даёт ложного совпадения
    f = JobFilter(REAL_CFG)
    assert [f.matches(j) for j in jobs] == [True, False, True]
    # две «страницы» одного эндпоинта объединяются; без url в JSON ключом служит id
    p2 = {"items": [{"id": 14, "title": "Junior ML Engineer", "dept": "AI", "city": "Омск"},
                    {"id": 15, "title": "Intern Data Engineer", "dept": "Data", "city": "Тула"}]}
    p1 = {"items": [{"id": 12, "title": "Junior NLP", "dept": "AI", "city": "Уфа"},
                    {"id": 13, "title": "Intern CV", "dept": "AI", "city": "Пермь"}]}
    jobs = jobs_from_json([("https://s.ru/api/v?page=1", p1), ("https://s.ru/api/v?page=2", p2)],
                          {"name": "S"}, "https://s.ru/jobs")
    assert len(jobs) == 4 and jobs[0].url == "https://s.ru/jobs" and jobs[0].key == "https://s.ru/jobs#12"
    assert jobs_from_json([noise], {"name": "S"}, "https://s.ru/jobs") == []   # справочники — не вакансии
