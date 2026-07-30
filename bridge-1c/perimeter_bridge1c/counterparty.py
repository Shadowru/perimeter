"""Разбор того, как пользователь и модель называют контрагента.

Отдельный модуль, потому что этим пользуются и инструменты поиска, и
аналитика: правило должно быть одно.
"""

from __future__ import annotations

import re

from .backend import OP_CONTAINS, Backend, Cond, Query
from .mapping import ConfigurationMapping

# Организационно-правовые формы, которые пользователь пишет, а в наименовании
# справочника они могут стоять иначе (или отсутствовать).
_LEGAL_FORMS = ("ооо", "оао", "зао", "пао", "ао", "ип", "нко", "ано", "гуп", "муп")
_QUOTES = "«»\"'“”„‟‘’`"

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_counterparty_query(query: str) -> str:
    """«ООО «Ромашка»» -> «ромашка».

    Живой пользователь пишет контрагента как угодно: с кавычками-ёлочками,
    с формой собственности, в любом регистре. Поиск в 1С идёт подстрокой,
    поэтому ищем по ядру наименования (найдено живым прогоном 2026-07-30:
    модель спрашивала уточнение, потому что «ООО «Ромашка»» не совпало).
    """
    text = query.strip().lower()
    for ch in _QUOTES:
        text = text.replace(ch, " ")
    words = [w for w in text.split() if w and w.strip(".") not in _LEGAL_FORMS]
    return " ".join(words).strip() or query.strip().lower()


def is_guid(value: str) -> bool:
    return bool(_GUID_RE.match(value.strip()))


class CounterpartyNotResolved(Exception):
    """Название не удалось однозначно превратить в ключ справочника."""


def resolve_counterparty_key(client: Backend, mapping: ConfigurationMapping,
                             value: str) -> str:
    """Ключ контрагента по ключу или по названию.

    Модель регулярно передаёт в инструмент название вместо ключа, а то и
    сочиняет ключ вида «key_ООО_Ромашка» (живой прогон 2026-07-30: акт
    сверки строился по трём выдуманным ключам подряд). Требовать от неё
    предварительного вызова get_counterparty бесполезно — надёжнее принять
    название и найти ключ самим.
    """
    value = (value or "").strip()
    if not value:
        raise CounterpartyNotResolved("Не указан контрагент.")
    if is_guid(value):
        return value

    ent = mapping.entity("counterparty")
    name_f = ent.field_1c("name")
    needle = normalize_counterparty_query(value)
    rows = list(client.run(Query(
        entity_set=ent.entity_set,
        conditions=[Cond(name_f, OP_CONTAINS, needle)],
        select=["Ref_Key", name_f],
        top=10,
    )))
    if not rows:
        raise CounterpartyNotResolved(
            f"Контрагент «{value}» в базе не найден. "
            "Проверьте название или посмотрите список через list_counterparties.")
    if len(rows) > 1:
        names = ", ".join(f"«{r.get(name_f, '?')}»" for r in rows[:5])
        raise CounterpartyNotResolved(
            f"По запросу «{value}» найдено несколько контрагентов: {names}. "
            "Уточните, какой именно нужен.")
    return rows[0]["Ref_Key"]
