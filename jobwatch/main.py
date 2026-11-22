from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .filters import JobFilter
from .notifier import build_digest, send_email
from .parsers import fetch_jobs, make_session
from .storage import State

log = logging.getLogger("jobwatch")
ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run(args) -> int:
    cfg = load_json(args.config)
    companies = [c for c in load_json(args.companies) if c.get("enabled", True)]
    if args.company:
        companies = [c for c in companies if c["name"] == args.company]
    if not companies:
        log.error("Нет активных компаний в %s", args.companies)
        return 1

    state = State(args.state)
    jf = JobFilter(cfg)
    session = make_session()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alert_after = cfg.get("alert_after_failures", 3)

    matched_by_company: dict = {}
    digest: dict = {}
    alerts: list = []
    failed = 0

    for i, company in enumerate(companies):
        name = company["name"]
        if i:
            time.sleep(cfg.get("delay_seconds", 1))
        try:
            jobs = fetch_jobs(company, session)
        except Exception as e:  # один сломавшийся сайт не должен валить остальные
            failed += 1
            err = f"{type(e).__name__}: {e}"
            fails = state.record_failure(name, err)
            log.error("%s: ошибка (%d подряд) — %s", name, fails, err)
            if fails == alert_after:
                alerts.append((name, fails, err))
            continue

        state.record_success(name)
        unique = {j.key: j for j in jobs}
        matched = [j for j in unique.values() if jf.matches(j)]
        if args.show_all:
            print(f"\n=== {name}: {len(unique)} вакансий (✓ = проходит фильтр) ===")
            for j in unique.values():
                print(f"{'✓' if jf.matches(j) else ' '} {j.title[:100]} | {j.url}")
        known = state.known(name)
        new = [j for j in matched if j.key not in known]
        first_run = state.is_new_company(name)
        log.info("%s: всего %d, подходит %d, новых %d%s", name, len(unique), len(matched), len(new),
                 " (первый запуск)" if first_run else "")
        if not jobs:
            log.warning("%s: 0 вакансий — возможно, сломались селекторы", name)

        matched_by_company[name] = matched
        silent = first_run and cfg.get("on_new_company", "notify") == "baseline"
        if new and not silent:
            digest[name] = new

    if digest or alerts:
        subject, text, html = build_digest(digest, alerts)
        if args.dry_run:
            print(f"Тема: {subject}\n\n{text}")
        else:
            send_email(subject, text, html)  # если упало — состояние не сохраняем, повторим в след. раз
            log.info("Письмо отправлено: %s", subject)
    else:
        log.info("Новых подходящих вакансий нет")

    if not args.dry_run:
        for name, matched in matched_by_company.items():
            state.add(name, matched, now)
        state.prune(cfg.get("forget_after_days", 90))
        state.save()

    return 2 if failed == len(companies) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jobwatch", description="Мониторинг новых вакансий")
    ap.add_argument("--companies", default=str(ROOT / "companies.json"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--state", default=str(ROOT / "state" / "seen.json"))
    ap.add_argument("--company", help="проверить только одну компанию (по name)")
    ap.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    ap.add_argument("--show-all", action="store_true", help="напечатать все найденные вакансии (для отладки)")
    ap.add_argument("--test-email", action="store_true", help="отправить тестовое письмо и выйти")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    load_env(ROOT / ".env")

    if args.test_email:
        send_email("JobWatch: тестовое письмо", "Если вы это читаете — почта настроена верно.",
                   "<p>Если вы это читаете — почта настроена верно.</p>")
        log.info("Тестовое письмо отправлено")
        return 0
    return run(args)
