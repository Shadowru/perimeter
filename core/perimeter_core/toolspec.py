"""Спецификация инструмента агента (единая для bridge-1c, skills, ui)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolOutput:
    """Результат инструмента с разными лицами для человека и для модели.

    Зачем. Отчёт мы считаем точно, а портит его пересказ: модель перенабирает
    числа руками и ошибается — ставит верную сумму не в ту корзину, коверкает
    название, обрывает строку (живой прогон 2026-07-30). Лечить пересказ
    бесполезно, надёжнее его убрать: таблицу человек получает как есть, а
    модели уходит только выжимка — шапка, итог и оговорки.

    Строк таблицы модель не видит, поэтому исказить их не может физически.
    Сверка при этом идёт против ПОЛНОГО текста: если модель что-то
    придумает сверх выжимки, это всплывёт.
    """
    display: str   # что видит человек — полный отчёт
    digest: str    # что уходит модели — шапка, итоги, оговорки
    title: str = ""

    def __str__(self) -> str:      # на случай, если результат используют как текст
        return self.display


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., str]
    requires_approval: bool = False

    def openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}
