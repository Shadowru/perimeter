"""Управленческая аналитика поверх данных 1С.

Отличие от инструментов поиска: здесь агент не пересказывает документы, а
считает агрегаты — ABC-анализ, рентабельность по брендам, старение
дебиторки. Считаем МЫ, а не модель: локальная модель складывает числа
ненадёжно, а ошибка в отчёте директору дороже, чем медленный ответ.

Вывод компактный: каждая строка — одна позиция. Это и экономит prefill,
и удобно читать в мессенджере.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .backend import (KIND_BOOL, KIND_DATETIME, KIND_GUID, OP_EQ, OP_GE, OP_LE,
                      Backend, Cond, Query)
from .mapping import ConfigurationMapping

# Границы групп ABC по накопленной доле выручки (классика 80/15/5).
ABC_A, ABC_B = 0.80, 0.95


def _fmt(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ")


def _date_conds(field: str, date_from: str | None, date_to: str | None) -> list[Cond]:
    conds = []
    if date_from:
        conds.append(Cond(field, OP_GE, date_from, KIND_DATETIME))
    if date_to:
        conds.append(Cond(field, OP_LE, date_to, KIND_DATETIME))
    return conds


def _abc_group(share_before: float) -> str:
    """Группа позиции по накопленной доле ПЕРЕД ней.

    Классическое правило: позиция относится к A, если до неё накоплено
    меньше 80% выручки — то есть позиция, которая сама пересекает границу,
    остаётся в A. Если считать по доле ПОСЛЕ, единственный крупный клиент
    с долей 93% попадёт в B, что бессмысленно (поймано тестом).
    """
    if share_before < ABC_A:
        return "A"
    return "B" if share_before < ABC_B else "C"


class AnalyticsTools:
    def __init__(self, client: Backend, mapping: ConfigurationMapping):
        self.client = client
        self.mapping = mapping

    # --- справочные данные -------------------------------------------------

    def _names(self, logical_entity: str, keys: set[str]) -> dict[str, str]:
        """key -> наименование, одним запросом на весь справочник."""
        ent = self.mapping.entity(logical_entity)
        name_f = ent.field_1c("name")
        rows = self.client.run(Query(entity_set=ent.entity_set,
                                     select=["Ref_Key", name_f]))
        return {r["Ref_Key"]: r.get(name_f, "?") for r in rows if r["Ref_Key"] in keys}

    def _brands(self) -> dict[str, str]:
        """key номенклатуры -> бренд (реквизит из маппинга)."""
        ent = self.mapping.entity("nomenclature")
        brand_f = ent.fields.get("brand")
        name_f = ent.field_1c("name")
        if not brand_f:
            return {}
        rows = self.client.run(Query(entity_set=ent.entity_set,
                                     select=["Ref_Key", name_f, brand_f]))
        return {r["Ref_Key"]: (r.get(brand_f) or "без бренда") for r in rows}

    # --- ABC-анализ --------------------------------------------------------

    def abc_analysis(self, dimension: str = "counterparty",
                     date_from: str | None = None, date_to: str | None = None,
                     limit: int = 15) -> str:
        """ABC по выручке: A — до 80% выручки, B — до 95%, C — остальные."""
        if dimension not in ("counterparty", "nomenclature"):
            return "dimension должен быть counterparty или nomenclature."

        ent = self.mapping.entity("sale")
        cp_f = ent.field_1c("counterparty")
        total_f = ent.field_1c("total")
        rows_field = ent.rows

        conds = [Cond("Posted", OP_EQ, True, KIND_BOOL)] + _date_conds("Date", date_from, date_to)
        docs = list(self.client.run(Query(entity_set=ent.entity_set, conditions=conds)))
        if not docs:
            return "За период нет проведённых реализаций."

        totals: dict[str, float] = defaultdict(float)
        if dimension == "counterparty":
            for d in docs:
                totals[d.get(cp_f, "")] += float(d.get(total_f) or 0)
            names = self._names("counterparty", set(totals))
        else:
            if not rows_field:
                return "Товарный состав документов не описан в маппинге (rows)."
            for d in docs:
                for line in d.get(rows_field) or []:
                    totals[line.get("Номенклатура_Key", "")] += float(line.get("Сумма") or 0)
            names = self._names("nomenclature", set(totals))

        grand = sum(totals.values())
        if grand <= 0:
            return "Выручка за период нулевая."

        ordered = sorted(totals.items(), key=lambda kv: -kv[1])
        out, cum = [], 0.0
        counts: dict[str, int] = defaultdict(int)
        for key, amount in ordered[:limit]:
            group = _abc_group(cum / grand)   # доля ДО этой позиции
            cum += amount
            counts[group] += 1
            out.append(f"{group} | {names.get(key, '?')} | {_fmt(amount)} руб. | "
                       f"{amount / grand * 100:.1f}% | нарастающим {cum / grand * 100:.1f}%")

        head = ("ABC-анализ по выручке, "
                + ("контрагенты" if dimension == "counterparty" else "номенклатура")
                + f" ({len(ordered)} позиций, выручка {_fmt(grand)} руб.):")
        tail = ("Группы: " + ", ".join(f"{g} — {counts[g]}" for g in ("A", "B", "C") if counts[g])
                + ". Расчёт по проведённым реализациям.")
        return "\n".join([head, *out, tail])

    # --- себестоимость и рентабельность по брендам -------------------------

    def profit_by_brand(self, date_from: str | None = None,
                        date_to: str | None = None) -> str:
        """Выручка, себестоимость и маржа по брендам — из регистра продаж."""
        try:
            ent = self.mapping.entity("sales_register")
        except Exception:
            return ("Регистр выручки и себестоимости не описан в маппинге этой "
                    "конфигурации — обратитесь к администратору.")

        nom_f = ent.field_1c("nomenclature")
        rev_f = ent.field_1c("revenue")
        cost_f = ent.field_1c("cost")
        period_f = ent.fields.get("period", "Period")

        conds = _date_conds(period_f, date_from, date_to)
        rows = list(self.client.run(Query(entity_set=ent.entity_set, conditions=conds)))
        if not rows:
            return "За период нет данных о продажах в регистре."

        brands = self._brands()
        agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for r in rows:
            brand = brands.get(r.get(nom_f, ""), "без бренда")
            agg[brand][0] += float(r.get(rev_f) or 0)
            agg[brand][1] += float(r.get(cost_f) or 0)

        out = []
        tr = tc = 0.0
        for brand, (rev, cost) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
            margin = rev - cost
            pct = margin / rev * 100 if rev else 0.0
            tr += rev
            tc += cost
            out.append(f"{brand} | выручка {_fmt(rev)} | себестоимость {_fmt(cost)} | "
                       f"маржа {_fmt(margin)} ({pct:.1f}%)")
        total_margin = tr - tc
        out.append(f"ИТОГО | выручка {_fmt(tr)} | себестоимость {_fmt(tc)} | "
                   f"маржа {_fmt(total_margin)} "
                   f"({total_margin / tr * 100 if tr else 0:.1f}%)")
        return "Себестоимость и маржа по брендам:\n" + "\n".join(out)

    # --- старение дебиторской задолженности --------------------------------

    def receivables_aging(self, as_of: str | None = None) -> str:
        """Сколько должны и как давно: 0–30, 31–60, 61–90, свыше 90 дней."""
        sale = self.mapping.entity("sale")
        pay = self.mapping.entity("incoming_payment")
        cp_s, total_s = sale.field_1c("counterparty"), sale.field_1c("total")
        cp_p, total_p = pay.field_1c("counterparty"), pay.field_1c("total")

        base = [Cond("Posted", OP_EQ, True, KIND_BOOL)]
        sales = list(self.client.run(Query(entity_set=sale.entity_set, conditions=base)))
        pays = list(self.client.run(Query(entity_set=pay.entity_set, conditions=base)))
        if not sales:
            return "Проведённых реализаций нет."

        paid: dict[str, float] = defaultdict(float)
        for p in pays:
            paid[p.get(cp_p, "")] += float(p.get(total_p) or 0)

        as_of_dt = datetime.fromisoformat(as_of) if as_of else datetime(2026, 7, 31)
        buckets = ("0-30", "31-60", "61-90", "90+")
        by_cp: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(buckets, 0.0))

        # Гасим оплаты старыми отгрузками (FIFO) — как это делает бухгалтер.
        for cp in {s.get(cp_s, "") for s in sales}:
            docs = sorted((s for s in sales if s.get(cp_s) == cp), key=lambda s: s.get("Date", ""))
            rest = paid.get(cp, 0.0)
            for d in docs:
                amount = float(d.get(total_s) or 0)
                covered = min(rest, amount)
                rest -= covered
                unpaid = amount - covered
                if unpaid <= 0.005:
                    continue
                days = (as_of_dt - datetime.fromisoformat(d["Date"])).days
                bucket = ("0-30" if days <= 30 else "31-60" if days <= 60
                          else "61-90" if days <= 90 else "90+")
                by_cp[cp][bucket] += unpaid

        if not by_cp:
            return "Просроченной дебиторской задолженности нет — всё оплачено."

        names = self._names("counterparty", set(by_cp))
        out = ["Дебиторская задолженность по срокам (дней с даты отгрузки):",
               "контрагент | 0-30 | 31-60 | 61-90 | свыше 90 | итого"]
        totals = dict.fromkeys(buckets, 0.0)
        for cp, row in sorted(by_cp.items(), key=lambda kv: -sum(kv[1].values())):
            for b in buckets:
                totals[b] += row[b]
            out.append(f"{names.get(cp, '?')} | " + " | ".join(_fmt(row[b]) for b in buckets)
                       + f" | {_fmt(sum(row.values()))}")
        out.append("ИТОГО | " + " | ".join(_fmt(totals[b]) for b in buckets)
                   + f" | {_fmt(sum(totals.values()))}")
        out.append(f"Расчёт на {as_of_dt.date()}, оплаты разнесены по FIFO.")
        return "\n".join(out)

    # --- спецификации для агента -------------------------------------------

    def specs(self) -> list[Any]:
        from perimeter_core.toolspec import ToolSpec
        dates = {"date_from": {"type": "string"}, "date_to": {"type": "string"}}
        return [
            ToolSpec(
                "abc_analysis",
                "ABC-анализ выручки по контрагентам или номенклатуре.",
                {"type": "object", "properties": {
                    "dimension": {"type": "string", "enum": ["counterparty", "nomenclature"]},
                    **dates, "limit": {"type": "integer"}}, "required": ["dimension"]},
                lambda **kw: self.abc_analysis(**kw),
            ),
            ToolSpec(
                "profit_by_brand",
                "Выручка, себестоимость и маржа по брендам за период.",
                {"type": "object", "properties": dates},
                lambda **kw: self.profit_by_brand(**kw),
            ),
            ToolSpec(
                "receivables_aging",
                "Дебиторка по срокам: 0-30, 31-60, 61-90, свыше 90 дней.",
                {"type": "object", "properties": {"as_of": {"type": "string"}}},
                lambda **kw: self.receivables_aging(**kw),
            ),
        ]
