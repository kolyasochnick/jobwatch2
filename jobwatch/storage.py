from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone


class State:
    """Файл state/seen.json: какие вакансии уже отправлялись + здоровье источников."""

    def __init__(self, path: str):
        self.path = path
        self.data = {"companies": {}, "health": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            self.data["companies"] = loaded.get("companies", {})
            self.data["health"] = loaded.get("health", {})

    # --- вакансии ---
    def is_new_company(self, name: str) -> bool:
        return name not in self.data["companies"]

    def known(self, name: str) -> dict:
        return self.data["companies"].get(name, {})

    def add(self, name: str, jobs, now: str) -> None:
        comp = self.data["companies"].setdefault(name, {})
        for j in jobs:
            entry = comp.get(j.key)
            if entry:
                entry["last_seen"] = now
            else:
                comp[j.key] = {
                    "title": j.title,
                    "url": j.url,
                    "location": j.location,
                    "first_seen": now,
                    "last_seen": now,
                }

    def prune(self, days: int) -> None:
        limit = datetime.now(timezone.utc) - timedelta(days=days)
        for comp in self.data["companies"].values():
            for key in [k for k, v in comp.items()
                        if datetime.fromisoformat(v["last_seen"]) < limit]:
                del comp[key]

    # --- здоровье источников ---
    def record_failure(self, name: str, error: str) -> int:
        h = self.data["health"].setdefault(name, {"fails": 0, "error": ""})
        h["fails"] += 1
        h["error"] = error[:300]
        return h["fails"]

    def record_success(self, name: str) -> None:
        self.data["health"].pop(name, None)

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)
