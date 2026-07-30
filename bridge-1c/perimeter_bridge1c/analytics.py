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


def _period_label(date_from: str | None, date_to: str | None) -> str:
    """Явная подпись периода в выводе.

    Живой прогон 2026-07-30: без неё модель выдумывала период («за 2025 год»)
    и неверно пересказывала, к каким документам относятся суммы.
    """
    if not date_from and not date_to:
        return "за всё время"
    return f"с {(date_from or '...')[:10]} по {(date_to or '...')[:10]}"


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
            return (f"Проведённых реализаций {_period_label(date_from, date_to)} нет. "
                    "Если период не нужен — не указывайте даты, отчёт построится за всё время.")

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
                + f", {_period_label(date_from, date_to)} "
                + f"({len(ordered)} позиций, выручка {_fmt(grand)} руб.):")
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
            return (f"Данных о продажах {_period_label(date_from, date_to)} нет. "
                    "Без указания дат отчёт строится за всё время.")

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
        return (f"Себестоимость и маржа по брендам, {_period_label(date_from, date_to)}:\n"
                + "\n".join(out))

    # --- старение задолженности (дебиторка и кредиторка) --------------------

    def _aging(self, debt_entity: str, payment_entity: str, as_of: str | None,
               title: str, doc_word: str, empty_text: str) -> str:
        """Общий расчёт старения долга.

        Дебиторка и кредиторка устроены одинаково, меняются местами только
        стороны: «реализация минус приход денег» против «поступление минус
        расход денег». Считаем в одном месте, чтобы правило разнесения оплат
        не разъехалось между двумя отчётами.
        """
        debt = self.mapping.entity(debt_entity)
        pay = self.mapping.entity(payment_entity)
        cp_d, total_d = debt.field_1c("counterparty"), debt.field_1c("total")
        cp_p, total_p = pay.field_1c("counterparty"), pay.field_1c("total")

        base = [Cond("Posted", OP_EQ, True, KIND_BOOL)]
        docs_all = list(self.client.run(Query(entity_set=debt.entity_set, conditions=base)))
        pays = list(self.client.run(Query(entity_set=pay.entity_set, conditions=base)))
        if not docs_all:
            return f"Проведённых документов «{doc_word}» нет."

        paid: dict[str, float] = defaultdict(float)
        for p in pays:
            paid[p.get(cp_p, "")] += float(p.get(total_p) or 0)

        as_of_dt = datetime.fromisoformat(as_of) if as_of else datetime.now()
        buckets = ("0-30", "31-60", "61-90", "90+")
        by_cp: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(buckets, 0.0))
        docs_by_cp: dict[str, list[str]] = defaultdict(list)

        # Гасим оплаты старыми документами (FIFO) — как это делает бухгалтер.
        for cp in {d.get(cp_d, "") for d in docs_all}:
            docs = sorted((d for d in docs_all if d.get(cp_d) == cp),
                          key=lambda d: d.get("Date", ""))
            rest = paid.get(cp, 0.0)
            for d in docs:
                amount = float(d.get(total_d) or 0)
                covered = min(rest, amount)
                rest -= covered
                unpaid = amount - covered
                if unpaid <= 0.005:
                    continue
                days = (as_of_dt - datetime.fromisoformat(d["Date"])).days
                bucket = ("0-30" if days <= 30 else "31-60" if days <= 60
                          else "61-90" if days <= 90 else "90+")
                by_cp[cp][bucket] += unpaid
                # Конкретный документ: без него модель домысливает, к каким
                # отгрузкам относится долг (живой прогон 2026-07-30).
                docs_by_cp[cp].append(
                    f"№{d.get('Number')} от {str(d.get('Date'))[:10]} — "
                    f"не оплачено {_fmt(unpaid)} руб., возраст {days} дн. "
                    f"с даты документа")

        if not by_cp:
            return empty_text

        names = self._names("counterparty", set(by_cp))
        out = [f"{title} (дней с даты документа):",
               "контрагент | 0-30 | 31-60 | 61-90 | свыше 90 | итого"]
        totals = dict.fromkeys(buckets, 0.0)
        for cp, row in sorted(by_cp.items(), key=lambda kv: -sum(kv[1].values())):
            for b in buckets:
                totals[b] += row[b]
            out.append(f"{names.get(cp, '?')} | " + " | ".join(_fmt(row[b]) for b in buckets)
                       + f" | {_fmt(sum(row.values()))}")
            for line in docs_by_cp[cp]:
                out.append(f"    {line}")
        out.append("ИТОГО | " + " | ".join(_fmt(totals[b]) for b in buckets)
                   + f" | {_fmt(sum(totals.values()))}")
        out.append(f"Расчёт на {as_of_dt.date()}, оплаты разнесены по FIFO. "
                   "Возраст считается от даты документа; сроки оплаты по договорам "
                   "в данных отсутствуют, поэтому слово «просрочено» здесь неприменимо.")
        return "\n".join(out)

    def receivables_aging(self, as_of: str | None = None) -> str:
        """Сколько должны нам и как давно: 0–30, 31–60, 61–90, свыше 90 дней."""
        return self._aging(
            "sale", "incoming_payment", as_of,
            title="Дебиторская задолженность по срокам",
            doc_word="реализация",
            empty_text="Дебиторской задолженности нет — все отгрузки оплачены.")

    def payables_aging(self, as_of: str | None = None) -> str:
        """Сколько должны мы поставщикам и как давно."""
        return self._aging(
            "purchase", "outgoing_payment", as_of,
            title="Кредиторская задолженность перед поставщиками по срокам",
            doc_word="поступление товаров и услуг",
            empty_text="Кредиторской задолженности нет — все поступления оплачены.")

    # --- акт сверки взаиморасчётов -----------------------------------------

    def reconciliation_act(self, counterparty_key: str,
                           date_from: str | None = None,
                           date_to: str | None = None) -> str:
        """Акт сверки: сальдо на начало, обороты по документам, сальдо на конец.

        Первое, что бухгалтер делает перед закрытием периода. Форма привычная:
        отгрузки увеличивают долг контрагента, оплаты уменьшают.
        """
        sale = self.mapping.entity("sale")
        pay = self.mapping.entity("incoming_payment")
        cp_s, total_s = sale.field_1c("counterparty"), sale.field_1c("total")
        cp_p, total_p = pay.field_1c("counterparty"), pay.field_1c("total")

        base = [Cond("Posted", OP_EQ, True, KIND_BOOL),
                Cond(cp_s, OP_EQ, counterparty_key, KIND_GUID)]
        sales = list(self.client.run(Query(entity_set=sale.entity_set, conditions=base)))
        pay_conds = [Cond("Posted", OP_EQ, True, KIND_BOOL),
                     Cond(cp_p, OP_EQ, counterparty_key, KIND_GUID)]
        pays = list(self.client.run(Query(entity_set=pay.entity_set, conditions=pay_conds)))

        events = ([(str(d.get("Date")), "отгрузка", str(d.get("Number")),
                    float(d.get(total_s) or 0), 0.0) for d in sales]
                  + [(str(p.get("Date")), "оплата", str(p.get("Number")),
                      0.0, float(p.get(total_p) or 0)) for p in pays])
        if not events:
            return ("По этому контрагенту нет проведённых отгрузок и оплат. "
                    "Проверьте, того ли контрагента выбрали.")
        events.sort()

        names = self._names("counterparty", {counterparty_key})
        name = names.get(counterparty_key, "?")

        opening = 0.0
        rows, debit, credit = [], 0.0, 0.0
        for date, kind, number, sale_amt, pay_amt in events:
            if date_from and date < date_from:
                opening += sale_amt - pay_amt
                continue
            if date_to and date > date_to:
                continue
            debit += sale_amt
            credit += pay_amt
            balance = opening + debit - credit
            amount = sale_amt or pay_amt
            rows.append(f"{date[:10]} | {kind} №{number} | {_fmt(amount)} руб. | "
                        f"сальдо {_fmt(balance)}")

        closing = opening + debit - credit
        out = [f"Акт сверки с {name}, {_period_label(date_from, date_to)}:",
               f"Сальдо на начало периода: {_fmt(opening)} руб.",
               *rows,
               f"Обороты: отгружено {_fmt(debit)} руб., оплачено {_fmt(credit)} руб.",
               f"Сальдо на конец периода: {_fmt(closing)} руб. "
               + ("(долг контрагента перед нами)" if closing > 0.005
                  else "(наш долг / аванс контрагента)" if closing < -0.005
                  else "(взаиморасчёты закрыты)")]
        out.append("Учтены проведённые реализации и поступления на расчётный счёт. "
                   "Взаимозачёты и расчёты наличными в источник не входят.")
        return "\n".join(out)

    # --- движение денежных средств (ДДС) ------------------------------------

    def cash_flow(self, date_from: str | None = None,
                  date_to: str | None = None) -> str:
        """Поступления и списания по расчётному счёту помесячно."""
        inc = self.mapping.entity("incoming_payment")
        outg = self.mapping.entity("outgoing_payment")
        base = [Cond("Posted", OP_EQ, True, KIND_BOOL)]

        def load(ent, extra_conds):
            return list(self.client.run(Query(entity_set=ent.entity_set,
                                              conditions=base + extra_conds)))

        dates = _date_conds("Date", date_from, date_to)
        ins = load(inc, dates)
        outs = load(outg, dates)
        if not ins and not outs:
            return (f"Движений по расчётному счёту {_period_label(date_from, date_to)} нет.")

        total_f_in = inc.field_1c("total")
        total_f_out = outg.field_1c("total")
        by_month: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for d in ins:
            by_month[str(d.get("Date"))[:7]][0] += float(d.get(total_f_in) or 0)
        for d in outs:
            by_month[str(d.get("Date"))[:7]][1] += float(d.get(total_f_out) or 0)

        out = [f"Движение денежных средств, {_period_label(date_from, date_to)}:",
               "месяц | поступило | списано | чистый поток"]
        ti = to = 0.0
        for month in sorted(by_month):
            got, spent = by_month[month]
            ti += got
            to += spent
            out.append(f"{month} | {_fmt(got)} | {_fmt(spent)} | {_fmt(got - spent)}")
        out.append(f"ИТОГО | {_fmt(ti)} | {_fmt(to)} | {_fmt(ti - to)}")
        out.append("Источник — проведённые документы по расчётному счёту. "
                   "Остаток на счёте не показан: начальный остаток в этих "
                   "данных отсутствует, показан только оборот за период.")
        return "\n".join(out)

    # --- отчёт о прибылях и убытках (ОПиУ) ----------------------------------

    def pnl_report(self, date_from: str | None = None,
                   date_to: str | None = None) -> str:
        """Выручка, себестоимость и валовая прибыль за период."""
        try:
            ent = self.mapping.entity("sales_register")
        except Exception:
            return ("Регистр выручки и себестоимости не описан в маппинге этой "
                    "конфигурации — обратитесь к администратору.")
        rev_f, cost_f = ent.field_1c("revenue"), ent.field_1c("cost")
        period_f = ent.fields.get("period", "Period")
        rows = list(self.client.run(Query(entity_set=ent.entity_set,
                                          conditions=_date_conds(period_f, date_from, date_to))))
        if not rows:
            return f"Продаж {_period_label(date_from, date_to)} нет."

        by_month: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for r in rows:
            m = str(r.get(period_f))[:7]
            by_month[m][0] += float(r.get(rev_f) or 0)
            by_month[m][1] += float(r.get(cost_f) or 0)

        out = [f"Отчёт о прибылях и убытках (валовая прибыль), "
               f"{_period_label(date_from, date_to)}:",
               "месяц | выручка | себестоимость | валовая прибыль | рентабельность"]
        tr = tc = 0.0
        for month in sorted(by_month):
            rev, cost = by_month[month]
            tr += rev
            tc += cost
            gross = rev - cost
            out.append(f"{month} | {_fmt(rev)} | {_fmt(cost)} | {_fmt(gross)} | "
                       f"{gross / rev * 100 if rev else 0:.1f}%")
        gross = tr - tc
        out.append(f"ИТОГО | {_fmt(tr)} | {_fmt(tc)} | {_fmt(gross)} | "
                   f"{gross / tr * 100 if tr else 0:.1f}%")
        out.append("Это валовая прибыль. Коммерческие и управленческие расходы "
                   "(счета 26 и 44), налоги и проценты в расчёт не включены: "
                   "в подключённых данных их нет, поэтому чистая прибыль здесь "
                   "не выводится.")
        return "\n".join(out)

    # --- динамика продаж и средний чек --------------------------------------

    def sales_dynamics(self, date_from: str | None = None,
                       date_to: str | None = None) -> str:
        """Выручка, число отгрузок и средний чек помесячно."""
        ent = self.mapping.entity("sale")
        total_f = ent.field_1c("total")
        conds = [Cond("Posted", OP_EQ, True, KIND_BOOL)] + _date_conds("Date", date_from, date_to)
        docs = list(self.client.run(Query(entity_set=ent.entity_set, conditions=conds)))
        if not docs:
            return f"Проведённых реализаций {_period_label(date_from, date_to)} нет."

        by_month: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        for d in docs:
            m = str(d.get("Date"))[:7]
            by_month[m][0] += float(d.get(total_f) or 0)
            by_month[m][1] += 1

        months = sorted(by_month)
        out = [f"Динамика продаж, {_period_label(date_from, date_to)}:",
               "месяц | выручка | отгрузок | средний чек | к прошлому месяцу"]
        prev = None
        tr, tn = 0.0, 0
        for m in months:
            rev, cnt = by_month[m]
            tr += rev
            tn += int(cnt)
            change = "—" if prev is None else f"{(rev - prev) / prev * 100:+.1f}%" if prev else "—"
            out.append(f"{m} | {_fmt(rev)} | {int(cnt)} | {_fmt(rev / cnt)} | {change}")
            prev = rev
        out.append(f"ИТОГО | {_fmt(tr)} | {tn} | {_fmt(tr / tn)} | —")
        out.append("Средний чек — выручка, делённая на число проведённых реализаций.")
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
                "Сколько нам должны покупатели, по срокам долга.",
                {"type": "object", "properties": {"as_of": {"type": "string"}}},
                lambda **kw: self.receivables_aging(**kw),
            ),
            ToolSpec(
                "payables_aging",
                "Сколько мы должны поставщикам, по срокам долга.",
                {"type": "object", "properties": {"as_of": {"type": "string"}}},
                lambda **kw: self.payables_aging(**kw),
            ),
            ToolSpec(
                "reconciliation_act",
                "Акт сверки с контрагентом: сальдо и обороты по документам.",
                {"type": "object", "properties": {
                    "counterparty_key": {"type": "string"}, **dates},
                 "required": ["counterparty_key"]},
                lambda **kw: self.reconciliation_act(**kw),
            ),
            ToolSpec(
                "cash_flow",
                "Движение денег по расчётному счёту помесячно.",
                {"type": "object", "properties": dates},
                lambda **kw: self.cash_flow(**kw),
            ),
            ToolSpec(
                "pnl_report",
                "Прибыли и убытки: выручка, себестоимость, валовая прибыль.",
                {"type": "object", "properties": dates},
                lambda **kw: self.pnl_report(**kw),
            ),
            ToolSpec(
                "sales_dynamics",
                "Динамика продаж по месяцам и средний чек.",
                {"type": "object", "properties": dates},
                lambda **kw: self.sales_dynamics(**kw),
            ),
        ]
