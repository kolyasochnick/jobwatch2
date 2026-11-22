from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Job:
    company: str
    title: str
    url: str
    location: str = ""
    id: str = ""  # стабильный ключ, если у вакансии нет собственной ссылки
    text: str = field(default="", compare=False)  # доп. текст карточки (теги, уровень) — только для фильтра

    @property
    def key(self) -> str:
        return self.id or self.url
