"""Шов между инструментами агента и способом доступа к 1С.

Инструменты (find_document, ledger_report, …) формулируют запрос
структурно — сущность, условия, поля, сортировка, лимит. Как это
превратится в обращение к базе, решает бэкенд:

- ODataClient       — стандартный интерфейс OData (требует публикации ИБ);
- RobotBackend      — обратное подключение: обработка-«робот» внутри сеанса
                      1С сама опрашивает наш локальный шлюз (публикация не
                      нужна, работает и на базовой версии).

Логическая схема имён — общая для обоих: `Catalog_Контрагенты`,
`Document_РеализацияТоваровУслуг`, поля `Ref_Key`, `Number`, `Date`,
`Posted`, `Контрагент_Key`. Робот переводит их в термины встроенного
языка сам (см. bridge-1c/robot1c/README.md), поэтому маппинги и
инструменты одинаковы для обоих каналов.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable

# Виды значений: определяют и синтаксис OData, и тип параметра запроса 1С.
KIND_STR = "str"
KIND_GUID = "guid"
KIND_BOOL = "bool"
KIND_DATETIME = "datetime"
KIND_NUMBER = "number"

OP_EQ = "eq"
OP_GE = "ge"
OP_LE = "le"
OP_CONTAINS = "contains"


@dataclass(frozen=True)
class Cond:
    """Одно условие отбора. Поле — имя в логической схеме (как в OData)."""
    field: str
    op: str
    value: Any
    kind: str = KIND_STR


@dataclass
class Query:
    entity_set: str
    conditions: list[Cond] = field(default_factory=list)
    select: list[str] | None = None
    order_by: str | None = None
    top: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Сериализация для передачи роботу 1С."""
        return {
            "entity": self.entity_set,
            "conditions": [
                {"field": c.field, "op": c.op, "value": c.value, "kind": c.kind}
                for c in self.conditions
            ],
            "select": self.select,
            "order_by": self.order_by,
            "top": self.top,
        }


@runtime_checkable
class Backend(Protocol):
    """Минимальный контракт доступа к данным 1С."""

    def run(self, query: Query) -> Iterator[dict[str, Any]]:
        """Выборка строк по структурному запросу."""

    def get(self, entity_set: str, ref_key: str,
            select: list[str] | None = None) -> dict[str, Any]:
        """Один объект по ссылке."""

    def create_draft(self, entity_set: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Создание ЧЕРНОВИКА документа. Проведение недоступно by design."""

    def validate_mapping(self) -> list[str]:
        """Сверка маппинга с фактическими метаданными базы; [] — всё сходится."""
