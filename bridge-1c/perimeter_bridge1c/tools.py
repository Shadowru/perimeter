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

from .mapping import ConfigurationMapping
from .odata import ODataClient, f_and, f_date_range, f_eq_bool, f_eq_guid, f_eq_str

DOC_TYPES = ("sale", "purchase", "incoming_payment", "customer_invoice")


def _fmt_money(v: Any) -> str:
    try:
        return f"{float(v):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _fmt_date(iso: str) -> str:
    return (iso or "")[:10]


class Bridge1CTools:
    def __init__(self, client: ODataClient, mapping: ConfigurationMapping):
        self.client = client
        self.mapping = mapping

    # --- инструменты ------------------------------------------------------

    def get_counterparty(self, query: str) -> str:
        ent = self.mapping.entity("counterparty")
        name_f = ent.field_1c("name")
        rows = list(self.client.query(
            ent.entity_set,
            filter_=f"substringof('{query.lower()}', {name_f})",
            select=["Ref_Key", "Code", name_f] + [f for f in (ent.fields.get("inn"),) if f],
            top=10,
        ))
        if not rows and query.strip().isdigit():
            inn_f = ent.fields.get("inn")
            if inn_f:
                rows = list(self.client.query(
                    ent.entity_set, filter_=f_eq_str(inn_f, query.strip()), top=10))
        if not rows:
            return f"Контрагент по запросу «{query}» не найден."
        inn_f = ent.fields.get("inn")
        lines = [
            f"{r.get(name_f, '?')} | ИНН {r.get(inn_f, '—')} | key={r['Ref_Key']}"
            if inn_f else f"{r.get(name_f, '?')} | key={r['Ref_Key']}"
            for r in rows
        ]
        return "\n".join(lines)

    def find_document(self, doc_type: str, counterparty_key: str | None = None,
                      date_from: str | None = None, date_to: str | None = None,
                      posted: bool | None = None, number: str | None = None,
                      limit: int = 20) -> str:
        if doc_type not in DOC_TYPES:
            return f"Неизвестный тип документа «{doc_type}». Доступны: {', '.join(DOC_TYPES)}."
        ent = self.mapping.entity(doc_type)
        cp_f = ent.field_1c("counterparty")
        total_f = ent.field_1c("total")
        parts: list[str] = []
        if counterparty_key:
            parts.append(f_eq_guid(cp_f, counterparty_key))
        parts += f_date_range("Date", date_from, date_to)
        if posted is not None:
            parts.append(f_eq_bool("Posted", posted))
        if number:
            parts.append(f_eq_str("Number", number))
        rows = list(self.client.query(
            ent.entity_set,
            filter_=f_and(parts) or None,
            select=["Ref_Key", "Number", "Date", "Posted", cp_f, total_f],
            order_by="Date",
            top=limit,
        ))
        if not rows:
            return "Документы не найдены."
        lines = [
            f"№{r['Number']} от {_fmt_date(r['Date'])} | {_fmt_money(r.get(total_f))} руб. | "
            f"{'проведён' if r.get('Posted') else 'НЕ проведён'} | key={r['Ref_Key']}"
            for r in rows
        ]
        return "\n".join(lines)

    def ledger_report(self, counterparty_key: str,
                      date_from: str | None = None, date_to: str | None = None) -> str:
        """Сверка по документам: отгрузки (реализации) против оплат.

        MVP: расчёт по проведённым документам, а не по регистру взаиморасчётов.
        TODO(1С): точная сверка требует AccumulationRegister расчётов с
        покупателями — имя регистра различается по конфигурациям, добавить в
        маппинг после верификации на живой базе.
        """
        out = []
        totals = {}
        for logical, title in (("sale", "Отгрузки"), ("incoming_payment", "Оплаты")):
            ent = self.mapping.entity(logical)
            cp_f = ent.field_1c("counterparty")
            total_f = ent.field_1c("total")
            parts = [f_eq_guid(cp_f, counterparty_key), f_eq_bool("Posted", True)]
            parts += f_date_range("Date", date_from, date_to)
            rows = list(self.client.query(
                ent.entity_set, filter_=f_and(parts),
                select=["Number", "Date", total_f], order_by="Date"))
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
        return (f"Создан ЧЕРНОВИК (не проведён): №{created.get('Number', '?')} "
                f"от {_fmt_date(created.get('Date', ''))} | key={created['Ref_Key']}. "
                f"Проведение — только вручную человеком в 1С.")

    # --- спецификации для агента -----------------------------------------

    def specs(self) -> list[ToolSpec]:
        date_props = {
            "date_from": {"type": "string", "description": "ISO, напр. 2026-07-01T00:00:00"},
            "date_to": {"type": "string", "description": "ISO, напр. 2026-07-31T23:59:59"},
        }
        return [
            ToolSpec(
                "get_counterparty",
                "Найти контрагента по названию или ИНН; вернёт key для других инструментов.",
                {"type": "object",
                 "properties": {"query": {"type": "string", "description": "часть названия или ИНН"}},
                 "required": ["query"]},
                lambda **kw: self.get_counterparty(**kw),
            ),
            ToolSpec(
                "find_document",
                "Найти документы 1С по типу, контрагенту, датам, признаку проведения.",
                {"type": "object", "properties": {
                    "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
                    "counterparty_key": {"type": "string"},
                    **date_props,
                    "posted": {"type": "boolean"},
                    "number": {"type": "string"},
                    "limit": {"type": "integer"},
                }, "required": ["doc_type"]},
                lambda **kw: self.find_document(**kw),
            ),
            ToolSpec(
                "ledger_report",
                "Сверка по контрагенту: отгрузки против оплат, сальдо.",
                {"type": "object", "properties": {
                    "counterparty_key": {"type": "string"}, **date_props,
                }, "required": ["counterparty_key"]},
                lambda **kw: self.ledger_report(**kw),
            ),
            ToolSpec(
                "create_draft_document",
                "Создать ЧЕРНОВИК документа (не проводится). Требует подтверждения человека.",
                {"type": "object", "properties": {
                    "doc_type": {"type": "string", "enum": ["customer_invoice", "sale"]},
                    "counterparty_key": {"type": "string"},
                    "total": {"type": "number"},
                    "comment": {"type": "string"},
                    "based_on_key": {"type": "string", "description": "key документа-основания"},
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
