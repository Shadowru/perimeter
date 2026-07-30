"""Инструменты агента поверх 1С OData.

Принципы:
- Компактный вывод: локальная модель медленно читает контекст (prefill),
  каждый лишний токен — секунды ожидания. Одна строка на документ.
- Ссылки на документы всегда «№ … от …» — требование продукта.
- Запись: только create_draft_document, и только черновик (Posted=false).
  Проведение документов агентом невозможно на уровне клиента (нет метода).
  requires_approval=True — ядро обязано получить подтверждение человека
  ДО вызова (guardrail Этапа 5).
"""

from __future__ import annotations

import json
from typing import Any

from perimeter_core.toolspec import ToolSpec

from .backend import (KIND_BOOL, KIND_DATETIME, KIND_GUID, OP_CONTAINS, OP_EQ,
                      OP_GE, OP_LE, Backend, Cond, Query)
from .mapping import ConfigurationMapping

DOC_TYPES = ("sale", "purchase", "incoming_payment", "customer_invoice")

# Потолок выборки. Модель однажды передала limit=1000000000000000 (замер
# 2026-07-30) — без потолка это выкачивание всей базы в контекст.
MAX_ROWS = 200


DOC_TYPE_NAMES = {
    "sale": "реализации",
    "purchase": "поступления товаров и услуг",
    "incoming_payment": "поступления на расчётный счёт",
    "customer_invoice": "счета покупателям",
}


def _selection_label(doc_type: str, date_from: str | None, date_to: str | None,
                     posted: bool | None, number: str | None) -> str:
    """Строка условий выборки — она идёт первой в выводе.

    Модель регулярно теряла период: «за июль» уходило в инструмент без дат,
    и выборка молча шла за всё время. Ни правило в промпте, ни описание
    параметра этого не исправили (замеры 2026-07-30). Поэтому выборка сама
    сообщает, за что она построена: расхождение с вопросом становится видно.
    """
    parts = [DOC_TYPE_NAMES.get(doc_type, doc_type)]
    if date_from or date_to:
        parts.append(f"с {(date_from or '...')[:10]} по {(date_to or '...')[:10]}")
    else:
        parts.append("за всё время (период не задан)")
    if posted is True:
        parts.append("только проведённые")
    elif posted is False:
        parts.append("только непроведённые")
    if number:
        parts.append(f"номер {number}")
    return "Выборка: " + ", ".join(parts) + "."


def _sane_limit(limit: int | None) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 20
    return max(1, min(value, MAX_ROWS))


def _date_conditions(date_from: str | None, date_to: str | None) -> list[Cond]:
    conds = []
    if date_from:
        conds.append(Cond("Date", OP_GE, date_from, KIND_DATETIME))
    if date_to:
        conds.append(Cond("Date", OP_LE, date_to, KIND_DATETIME))
    return conds


def _fmt_money(v: Any) -> str:
    try:
        return f"{float(v):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _fmt_date(iso: str) -> str:
    return (iso or "")[:10]


from .counterparty import (CounterpartyNotResolved,  # noqa: F401 — публичный ре-экспорт
                           normalize_counterparty_query, resolve_counterparty_key)


class Bridge1CTools:
    def __init__(self, client: Backend, mapping: ConfigurationMapping):
        self.client = client
        self.mapping = mapping

    # --- инструменты ------------------------------------------------------

    def get_counterparty(self, query: str) -> str:
        ent = self.mapping.entity("counterparty")
        name_f = ent.field_1c("name")
        needle = normalize_counterparty_query(query)
        rows = list(self.client.run(Query(
            entity_set=ent.entity_set,
            conditions=[Cond(name_f, OP_CONTAINS, needle)],
            select=["Ref_Key", "Code", name_f] + [f for f in (ent.fields.get("inn"),) if f],
            top=10,
        )))
        if not rows and query.strip().isdigit():
            inn_f = ent.fields.get("inn")
            if inn_f:
                rows = list(self.client.run(Query(
                    entity_set=ent.entity_set,
                    conditions=[Cond(inn_f, OP_EQ, query.strip())],
                    top=10,
                )))
        if not rows:
            return f"Контрагент по запросу «{query}» не найден."
        inn_f = ent.fields.get("inn")
        lines = [
            f"{r.get(name_f, '?')} | ИНН {r.get(inn_f, '—')} | key={r['Ref_Key']}"
            if inn_f else f"{r.get(name_f, '?')} | key={r['Ref_Key']}"
            for r in rows
        ]
        return "\n".join(lines)

    def list_counterparties(self, limit: int = 20) -> str:
        """Список контрагентов из базы.

        Без этого инструмента модель на вопрос «список контрагентов»
        выдумывает названия — проверено на живом демо 2026-07-30.
        """
        limit = _sane_limit(limit)
        ent = self.mapping.entity("counterparty")
        name_f = ent.field_1c("name")
        inn_f = ent.fields.get("inn")
        select = ["Ref_Key", name_f] + ([inn_f] if inn_f else [])
        rows = list(self.client.run(Query(entity_set=ent.entity_set,
                                          select=select, order_by=name_f,
                                          top=limit + 1)))
        if not rows:
            return "В справочнике контрагентов нет записей."
        shown = rows[:limit]
        lines = [f"{r.get(name_f, '?')}"
                 + (f" | ИНН {r.get(inn_f)}" if inn_f and r.get(inn_f) else "")
                 + f" | key={r['Ref_Key']}"
                 for r in shown]
        head = f"Контрагенты ({len(shown)}"
        head += " из большего числа, показаны первые):" if len(rows) > limit else "):"
        return head + "\n" + "\n".join(lines)

    def _key(self, value: str) -> str:
        """Ключ контрагента по ключу или названию (см. counterparty.py)."""
        return resolve_counterparty_key(self.client, self.mapping, value)

    def find_document(self, doc_type: str, counterparty_key: str | None = None,
                      date_from: str | None = None, date_to: str | None = None,
                      posted: bool | None = None, number: str | None = None,
                      limit: int = 20) -> str:
        limit = _sane_limit(limit)
        if doc_type not in DOC_TYPES:
            return f"Неизвестный тип документа «{doc_type}». Доступны: {', '.join(DOC_TYPES)}."
        ent = self.mapping.entity(doc_type)
        cp_f = ent.field_1c("counterparty")
        total_f = ent.field_1c("total")
        conds: list[Cond] = []
        if counterparty_key:
            try:
                counterparty_key = self._key(counterparty_key)
            except CounterpartyNotResolved as e:
                return str(e)
            conds.append(Cond(cp_f, OP_EQ, counterparty_key, KIND_GUID))
        conds += _date_conditions(date_from, date_to)
        if posted is not None:
            conds.append(Cond("Posted", OP_EQ, posted, KIND_BOOL))
        if number:
            conds.append(Cond("Number", OP_EQ, number))
        rows = list(self.client.run(Query(
            entity_set=ent.entity_set,
            conditions=conds,
            select=["Ref_Key", "Number", "Date", "Posted", cp_f, total_f],
            order_by="Date",
            top=limit,
        )))
        head = _selection_label(doc_type, date_from, date_to, posted, number)
        if not rows:
            return f"{head}\nДокументы не найдены."
        lines = [
            f"№{r['Number']} от {_fmt_date(r['Date'])} | {_fmt_money(r.get(total_f))} руб. | "
            f"{'проведён' if r.get('Posted') else 'НЕ проведён'} | key={r['Ref_Key']}"
            for r in rows
        ]
        return "\n".join([head, *lines])

    def ledger_report(self, counterparty_key: str,
                      date_from: str | None = None, date_to: str | None = None) -> str:
        """Сверка по документам: отгрузки (реализации) против оплат.

        MVP: расчёт по проведённым документам, а не по регистру взаиморасчётов.
        TODO(1С): точная сверка требует AccumulationRegister расчётов с
        покупателями — имя регистра различается по конфигурациям, добавить в
        маппинг после верификации на живой базе.
        """
        try:
            counterparty_key = self._key(counterparty_key)
        except CounterpartyNotResolved as e:
            return str(e)
        out = []
        totals = {}
        for logical, title in (("sale", "Отгрузки"), ("incoming_payment", "Оплаты")):
            ent = self.mapping.entity(logical)
            cp_f = ent.field_1c("counterparty")
            total_f = ent.field_1c("total")
            conds = [Cond(cp_f, OP_EQ, counterparty_key, KIND_GUID),
                     Cond("Posted", OP_EQ, True, KIND_BOOL)]
            conds += _date_conditions(date_from, date_to)
            rows = list(self.client.run(Query(
                entity_set=ent.entity_set, conditions=conds,
                select=["Number", "Date", total_f], order_by="Date")))
            subtotal = sum(float(r.get(total_f) or 0) for r in rows)
            totals[logical] = subtotal
            out.append(f"{title} ({len(rows)}): " + "; ".join(
                f"№{r['Number']} от {_fmt_date(r['Date'])} на {_fmt_money(r.get(total_f))}"
                for r in rows) if rows else f"{title}: нет")
        diff = totals.get("sale", 0.0) - totals.get("incoming_payment", 0.0)
        out.append(
            f"Итого отгружено {_fmt_money(totals.get('sale', 0))} руб., "
            f"оплачено {_fmt_money(totals.get('incoming_payment', 0))} руб., "
            f"сальдо (не оплачено) {_fmt_money(diff)} руб.")
        out.append("Примечание: сверка по проведённым документам (без регистра взаиморасчётов).")
        return "\n".join(out)

    def create_draft_document(self, doc_type: str, counterparty_key: str,
                              total: float | None = None, comment: str | None = None,
                              based_on_key: str | None = None) -> str:
        if doc_type not in ("customer_invoice", "sale"):
            return f"Создание черновиков поддержано для: customer_invoice, sale. Получено: {doc_type}."
        try:
            counterparty_key = self._key(counterparty_key)
        except CounterpartyNotResolved as e:
            return str(e)
        ent = self.mapping.entity(doc_type)
        payload: dict[str, Any] = {ent.field_1c("counterparty"): counterparty_key}
        if based_on_key:
            src = self.client.get(ent.entity_set, based_on_key)
            for logical in ("total", "comment"):
                f1c = ent.fields.get(logical)
                if f1c and f1c in src:
                    payload[f1c] = src[f1c]
            rows_name = ent.rows
            if rows_name and rows_name in src:
                payload[rows_name] = src[rows_name]
        if total is not None:
            payload[ent.field_1c("total")] = total
        if comment is not None and ent.fields.get("comment"):
            payload[ent.field_1c("comment")] = comment
        created = self.client.create_draft(ent.entity_set, payload)
        when = _fmt_date(created.get("Date", ""))
        amount = created.get(ent.field_1c("total"))
        parts = [f"Создан ЧЕРНОВИК (не проведён): №{created.get('Number', '?')}"]
        parts.append(f"от {when}" if when else "дату присвоит 1С")
        if amount is not None:
            parts.append(f"на {_fmt_money(amount)} руб.")
        parts.append(f"key={created['Ref_Key']}")
        return (" | ".join(parts)
                + ". Проведение — только вручную человеком в 1С.")

    # --- спецификации для агента -----------------------------------------

    def specs(self) -> list[ToolSpec]:
        """Схемы инструментов.

        Каждый токен схемы оплачивается временем prefill на каждом ходе
        агента: на стенде 2026-07-29 это ~9 с/токен, а схемы составляли
        две трети промпта. Поэтому описания предельно короткие, а формат
        дат вынесен в системный промпт (он в контексте всё равно есть).
        """
        dates = {"date_from": {"type": "string"}, "date_to": {"type": "string"}}
        return [
            ToolSpec(
                "get_counterparty",
                "Контрагент по названию/ИНН -> key.",
                {"type": "object", "properties": {"query": {"type": "string"}},
                 "required": ["query"]},
                lambda **kw: self.get_counterparty(**kw),
            ),
            ToolSpec(
                "list_counterparties",
                "Список контрагентов из базы (когда конкретное имя неизвестно).",
                {"type": "object", "properties": {"limit": {"type": "integer"}}},
                lambda **kw: self.list_counterparties(**kw),
            ),
            ToolSpec(
                "find_document",
                "Документы: sale — реализация (отгрузка), purchase — поступление "
                "товаров и услуг, incoming_payment — приход денег на счёт, "
                "customer_invoice — счёт покупателю.",
                {"type": "object", "properties": {
                    "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
                    "counterparty_key": {"type": "string"},
                    # Правило про период есть в системном промпте, но именно
                    # здесь модель его теряла и после правки промпта: «за июль»
                    # уходило без дат, и выборка шла за всё время.
                    "date_from": {"type": "string",
                                  "description": "начало периода, ISO; назван месяц — заполни"},
                    "date_to": {"type": "string",
                                "description": "конец периода, ISO; назван месяц — заполни"},
                    "posted": {"type": "boolean"},
                    "number": {"type": "string"},
                    "limit": {"type": "integer"},
                }, "required": ["doc_type"]},
                lambda **kw: self.find_document(**kw),
            ),
            ToolSpec(
                "ledger_report",
                "Что отгружено контрагенту и не оплачено: отгрузки против оплат, сальдо.",
                {"type": "object", "properties": {
                    "counterparty_key": {"type": "string"}, **dates,
                }, "required": ["counterparty_key"]},
                lambda **kw: self.ledger_report(**kw),
            ),
            ToolSpec(
                "create_draft_document",
                "ЧЕРНОВИК документа (не проводится, нужно подтверждение).",
                {"type": "object", "properties": {
                    "doc_type": {"type": "string", "enum": ["customer_invoice", "sale"]},
                    "counterparty_key": {"type": "string"},
                    "total": {"type": "number"},
                    "comment": {"type": "string"},
                    "based_on_key": {"type": "string"},
                }, "required": ["doc_type", "counterparty_key"]},
                lambda **kw: self.create_draft_document(**kw),
                requires_approval=True,
            ),
        ]


def execute_tool(specs: list[ToolSpec], name: str, arguments_json: str) -> tuple[str, ToolSpec | None]:
    """Выполнить инструмент по имени с JSON-аргументами (для ядра агента)."""
    spec = next((s for s in specs if s.name == name), None)
    if spec is None:
        return f"Ошибка: неизвестный инструмент {name}.", None
    try:
        kwargs = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return f"Ошибка: некорректные аргументы ({e}).", spec
    try:
        return spec.func(**kwargs), spec
    except TypeError as e:
        return f"Ошибка аргументов инструмента: {e}", spec
    except Exception as e:  # noqa: BLE001 — ошибка уходит модели, не падаем
        return f"Ошибка выполнения: {e}", spec
