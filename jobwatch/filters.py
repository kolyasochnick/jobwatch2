from __future__ import annotations

import re

from .models import Job


def _compile(words) -> re.Pattern | None:
    parts = [r"(?<!\w)" + re.escape(w.strip()) for w in (words or []) if w and w.strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


class JobFilter:
    """Заголовок должен содержать грейд И направление и не содержать исключений."""

    def __init__(self, cfg: dict):
        grade = cfg.get("grade", {})
        direction = cfg.get("direction", {})
        self.grade_inc = _compile(grade.get("include"))
        self.dir_inc = _compile(direction.get("include"))
        self.exclude = _compile(
            list(grade.get("exclude", []))
            + list(direction.get("exclude", []))
            + list(cfg.get("exclude_any", []))
        )
        self.locations = _compile(cfg.get("locations"))

    def matches(self, job: Job) -> bool:
        title = f"{job.title} {job.text}"  # заголовок + текст карточки (теги, уровень, отдел)
        if self.exclude and self.exclude.search(title):
            return False
        if self.grade_inc and not self.grade_inc.search(title):
            return False
        if self.dir_inc and not self.dir_inc.search(title):
            return False
        # Локация проверяется только если сайт её отдаёт
        if self.locations and job.location and not self.locations.search(job.location):
            return False
        return True
