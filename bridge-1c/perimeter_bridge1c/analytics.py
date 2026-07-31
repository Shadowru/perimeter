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
from .counterparty import CounterpartyNotResolved, resolve_counterparty_key
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


def _net_revenue(doc: dict, ent) -> float:
    """Выручка по документу БЕЗ НДС — суммой по строкам табличной части.

    В БП 3.0 `СуммаДокумента` — сумма С НДС: контрагент должен именно её.
    Управленческая выручка считается без НДС, иначе она завышена на ставку
    налога, а маржа завышена дважды (себестоимость идёт без НДС).

    Раньше мы вычитали реквизит `СуммаНДС` из ШАПКИ документа. На живой
    БП 3.0.111 такого реквизита в шапке НЕТ — НДС есть только в строках
    (робот перечислил метаданные изнутри базы, 2026-07-31). Прежний код
    молча вычитал ноль и выдавал сумму с НДС за сумму без НДС: цифра
    выглядела правильной и была неверна ровно на ставку налога.

    Строк нет или НДС строки не описан — возвращаем полную сумму, и отчёт
    об этом честно пишет.
    """
    rows = doc.get(ent.rows) if ent.rows else None
    if rows and ent.row_fields.get("amount"):
        amount_f = ent.row_field("amount")
        vat_f = ent.row_fields.get("vat")
        total = sum(float(r.get(amount_f) or 0) for r in rows)
        if vat_f:
            total -= sum(float(r.get(vat_f) or 0) for r in rows)
        return total
    return float(doc.get(ent.field_1c("total")) or 0)


def _line_net(line: dict, ent=None) -> float:
    """Строка табличной части без НДС. Имена колонок — из маппинга."""
    amount_f = ent.row_field("amount") if ent else "Сумма"
    vat_f = (ent.row_fields.get("vat") if ent else "СуммаНДС") or None
    net = float(line.get(amount_f) or 0)
    if vat_f:
        net -= float(line.get(vat_f) or 0)
    return net


def _vat_known(ent) -> bool:
    return bool(ent.rows and ent.row_fields.get("vat"))


def _vat_note(ent) -> str:
    return ("Выручка без НДС." if _vat_known(ent)
            else "ВНИМАНИЕ: реквизит НДС не описан в маппинге, суммы приведены "
                 "С НДС — управленческая выручка будет завышена на ставку налога.")


# Сколько строк отчёта отдаём модели. Ноль отдавать нельзя: на вопрос
# «раздели клиентов на группы» ответ обязан назвать клиентов, а не видя их,
# модель их выдумывала — на замере 31.07 так помечались четыре ответа из
# 61. Но и всю таблицу отдавать незачем: чем больше строк, тем больше шансов
# ошибиться при переписывании. Первые строки — это верхушка любого нашего
# отчёта (он отсортирован по убыванию), и именно её называют в ответе.
#
# Важно: показанные строки модель не угадывает, а копирует, и сверка ловит
# искажение — источник у неё тот же. Скрытые строки не подтверждаются
# принципиально, поэтому их и просят не перечислять.
TOP_ROWS_FOR_MODEL = 3


def _report(text: str, title: str):
    """Оборачивает готовый отчёт: человеку — таблица, модели — выжимка.

    Модель получает шапку с периодом, несколько верхних строк, итог и
    оговорки. Остальные строки скрыты: пересказывая их, она путала корзины
    и приближала суммы (155 000 вместо 157 500 — живой замер 30.07).
    """
    from perimeter_core.toolspec import ToolOutput
    kept, hidden, shown = [], 0, 0
    for i, line in enumerate(text.splitlines()):
        is_row = (line.count("|") >= 2 or line.startswith("    ")) \
            and not line.startswith("ИТОГО")
        if i == 0 or not is_row:
            kept.append(line)
        elif line.count("|") >= 2 and not any(c.isdigit() for c in line):
            # Шапка таблицы: цифр в ней нет. Она нужна для понимания колонок
            # и в лимит строк не идёт.
            kept.append(line)
        elif shown < TOP_ROWS_FOR_MODEL and line.count("|") >= 2:
            # Детализацию с отступом не показываем никогда — она построчная.
            kept.append(line)
            shown += 1
        else:
            hidden += 1
    if hidden:
        # Маркер короткий: он уходит в prefill с каждым отчётом. Сам запрет
        # перечислять скрытое стоит один раз в системном промпте.
        kept.insert(1 + shown, f"[ещё {hidden} строк показано пользователю таблицей]")
    return ToolOutput(display=text, digest="\n".join(kept), title=title)


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

    def _returns(self, date_from: str | None, date_to: str | None) -> list[dict]:
        """Проведённые возвраты от покупателей за период.

        Возврат уменьшает и выручку, и долг покупателя. Без него отчёт
        завышает продажи на сумму товара, который вернулся на склад.
        Если возвраты не описаны в маппинге конкретной конфигурации —
        считаем, что их нет, и отчёт об этом предупреждает.
        """
        try:
            ent = self.mapping.entity("sales_return")
        except Exception:
            return []
        conds = [Cond("Posted", OP_EQ, True, KIND_BOOL)] + _date_conds("Date", date_from, date_to)
        return list(self.client.run(Query(entity_set=ent.entity_set, conditions=conds)))

    def _has_returns(self) -> bool:
        try:
            self.mapping.entity("sales_return")
            return True
        except Exception:
            return False

    def _returns_note(self) -> str:
        return ("Реализации за вычетом возвратов." if self._has_returns()
                else "Возвраты от покупателей в маппинге не описаны и в расчёт "
                     "не вошли — выручка может быть завышена.")

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

        returns = self._returns(date_from, date_to)
        ret_ent = self.mapping.entity("sales_return") if self._has_returns() else None

        totals: dict[str, float] = defaultdict(float)
        # По товарам классический ABC смотрят не только на выручку: сколько
        # штук продано и сколько на этом заработано — [количество, себестоимость].
        extra: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        if dimension == "counterparty":
            for d in docs:
                totals[d.get(cp_f, "")] += _net_revenue(d, ent)
            for r in returns:
                totals[r.get(ret_ent.field_1c("counterparty"), "")] -= _net_revenue(r, ret_ent)
            names = self._names("counterparty", set(totals))
        else:
            if not rows_field:
                return "Товарный состав документов не описан в маппинге (rows)."
            qty_f = ent.row_fields.get("quantity")
            cost_f = ent.row_fields.get("cost")
            for d in docs:
                for line in d.get(rows_field) or []:
                    key = line.get(ent.row_field("nomenclature"), "")
                    totals[key] += _line_net(line, ent)
                    extra[key][0] += float(line.get(qty_f) or 0) if qty_f else 0.0
                    extra[key][1] += float(line.get(cost_f) or 0) if cost_f else 0.0
            for r in returns:
                for line in r.get(ret_ent.rows) or []:
                    key = line.get(ret_ent.row_field("nomenclature"), "")
                    totals[key] -= _line_net(line, ret_ent)
                    rq = ret_ent.row_fields.get("quantity")
                    rc = ret_ent.row_fields.get("cost")
                    extra[key][0] -= float(line.get(rq) or 0) if rq else 0.0
                    extra[key][1] -= float(line.get(rc) or 0) if rc else 0.0
            names = self._names("nomenclature", set(totals))
        totals = {k: v for k, v in totals.items() if abs(v) > 0.005}

        grand = sum(totals.values())
        if grand <= 0:
            return "Выручка за период нулевая."

        ordered = sorted(totals.items(), key=lambda kv: -kv[1])
        by_goods = dimension == "nomenclature"
        out, cum = [], 0.0
        counts: dict[str, int] = defaultdict(int)
        if by_goods:
            out.append("группа | позиция | выручка без НДС | доля | нарастающим | "
                       "продано | маржа")
        for key, amount in ordered[:limit]:
            group = _abc_group(cum / grand)   # доля ДО этой позиции
            cum += amount
            counts[group] += 1
            line = (f"{group} | {names.get(key, '?')} | {_fmt(amount)} руб. | "
                    f"{amount / grand * 100:.1f}% | нарастающим {cum / grand * 100:.1f}%")
            if by_goods:
                qty, cost = extra[key]
                margin = amount - cost
                line += (f" | {qty:g} | " + (_fmt(margin) if cost else "нет себестоимости")
                         + (f" ({margin / amount * 100:.1f}%)" if cost and amount else ""))
            out.append(line)

        head = ("ABC-анализ по выручке, "
                + ("контрагенты" if dimension == "counterparty" else "номенклатура")
                + f", {_period_label(date_from, date_to)} "
                + f"({len(ordered)} позиций, выручка {_fmt(grand)} руб.):")
        tail = ("Группы: " + ", ".join(f"{g} — {counts[g]}" for g in ("A", "B", "C") if counts[g])
                + ". Источник: проведённые реализации. "
                + _vat_note(ent) + " " + self._returns_note())
        return _report("\n".join([head, *out, tail]), "ABC-анализ")

    def _sale_lines(self, date_from: str | None, date_to: str | None):
        """Строки проведённых реализаций минус возвраты: (номенклатура, месяц, выручка, себестоимость).

        Источник — табличная часть документа, а НЕ регистр. Регистр
        «ВыручкаИСебестоимостьПродаж» мы предполагали по аналогии с УТ, но на
        живой БП 3.0.111 его нет вовсе (проверено 2026-07-31). Зато у строк
        реализации есть колонки «Сумма», «СуммаНДС» и «Себестоимость» — этого
        достаточно и для выручки без НДС, и для маржи.

        Побочная польза: выручка теперь считается из одного источника во всех
        отчётах, и ОПиУ сходится с ABC без оговорок.
        """
        def lines(ent, docs, sign):
            nom_f = ent.row_field("nomenclature")
            amt_f = ent.row_field("amount")
            vat_f = ent.row_fields.get("vat")
            cost_f = ent.row_fields.get("cost")
            for d in docs:
                month = str(d.get("Date"))[:7]
                for row in d.get(ent.rows) or []:
                    net = float(row.get(amt_f) or 0)
                    if vat_f:
                        net -= float(row.get(vat_f) or 0)
                    cost = float(row.get(cost_f) or 0) if cost_f else 0.0
                    yield row.get(nom_f, ""), month, sign * net, sign * cost

        sale = self.mapping.entity("sale")
        conds = [Cond("Posted", OP_EQ, True, KIND_BOOL)] + _date_conds("Date", date_from, date_to)
        docs = list(self.client.run(Query(entity_set=sale.entity_set, conditions=conds)))
        yield from lines(sale, docs, 1)
        if self._has_returns():
            ret = self.mapping.entity("sales_return")
            yield from lines(ret, self._returns(date_from, date_to), -1)

    def _avg_purchase_cost(self, date_to: str | None = None) -> dict[str, float]:
        """Средневзвешенная цена закупки за единицу, без НДС.

        Нужна, когда 1С ещё не рассчитала себестоимость: в «Бухгалтерии» она
        появляется в строках реализации при проведении или при закрытии
        месяца. До этого маржа равна выручке, что читается как прибыль.
        Средняя по приходам — не замена расчёту 1С, а оценка, и отчёт это
        называет прямо.

        Период закупок не ограничиваем снизу: средняя считается по всей
        доступной истории поступлений, иначе товар, купленный раньше начала
        периода, остался бы без цены.
        """
        ent = self.mapping.entity("purchase")
        if not (ent.rows and ent.row_fields.get("quantity")):
            return {}
        conds = [Cond("Posted", OP_EQ, True, KIND_BOOL)] + _date_conds("Date", None, date_to)
        docs = self.client.run(Query(entity_set=ent.entity_set, conditions=conds))
        nom_f = ent.row_field("nomenclature")
        qty_f = ent.row_field("quantity")
        sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for d in docs:
            for row in d.get(ent.rows) or []:
                qty = float(row.get(qty_f) or 0)
                if qty <= 0:
                    continue
                sums[row.get(nom_f, "")][0] += _line_net(row, ent)
                sums[row.get(nom_f, "")][1] += qty
        return {nom: cost / qty for nom, (cost, qty) in sums.items() if qty > 0}

    def cost_report(self, date_from: str | None = None,
                    date_to: str | None = None) -> str:
        """Себестоимость проданного по номенклатуре: сколько, почём, с какой маржой."""
        sale = self.mapping.entity("sale")
        conds = [Cond("Posted", OP_EQ, True, KIND_BOOL)] + _date_conds("Date", date_from, date_to)
        docs = list(self.client.run(Query(entity_set=sale.entity_set, conditions=conds)))
        if not docs:
            return f"Проведённых реализаций {_period_label(date_from, date_to)} нет."

        nom_f = sale.row_field("nomenclature")
        qty_f = sale.row_fields.get("quantity")
        cost_f = sale.row_fields.get("cost")
        # [выручка без НДС, количество, себестоимость из 1С]
        agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
        for d in docs:
            for row in d.get(sale.rows) or []:
                key = row.get(nom_f, "")
                agg[key][0] += _line_net(row, sale)
                agg[key][1] += float(row.get(qty_f) or 0) if qty_f else 0.0
                agg[key][2] += float(row.get(cost_f) or 0) if cost_f else 0.0
        if self._has_returns():
            ret = self.mapping.entity("sales_return")
            rqty = ret.row_fields.get("quantity")
            rcost = ret.row_fields.get("cost")
            for r in self._returns(date_from, date_to):
                for row in r.get(ret.rows) or []:
                    key = row.get(ret.row_field("nomenclature"), "")
                    agg[key][0] -= _line_net(row, ret)
                    agg[key][1] -= float(row.get(rqty) or 0) if rqty else 0.0
                    agg[key][2] -= float(row.get(rcost) or 0) if rcost else 0.0

        names = self._names("nomenclature", set(agg))
        averages = self._avg_purchase_cost(date_to)
        out = ["позиция | продано | выручка без НДС | себестоимость | источник | маржа"]
        tr = tc = 0.0
        estimated = 0
        for key, (revenue, qty, cost) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
            if abs(revenue) < 0.005 and abs(qty) < 0.005:
                continue
            source = "1С"
            if cost <= 0.005 and qty > 0 and key in averages:
                cost = averages[key] * qty
                source = "оценка по закупкам"
                estimated += 1
            elif cost <= 0.005:
                source = "нет данных"
            margin = revenue - cost
            tr += revenue
            tc += cost
            out.append(f"{names.get(key, '?')} | {qty:g} | {_fmt(revenue)} | {_fmt(cost)} | "
                       f"{source} | {_fmt(margin)}"
                       + (f" ({margin / revenue * 100:.1f}%)" if revenue else ""))
        out.append(f"ИТОГО | | {_fmt(tr)} | {_fmt(tc)} | | {_fmt(tr - tc)}"
                   + (f" ({(tr - tc) / tr * 100:.1f}%)" if tr else ""))
        out.append(f"Себестоимость проданного, {_period_label(date_from, date_to)}. "
                   "Источник «1С» — значение из строки документа; «оценка по "
                   "закупкам» — средневзвешенная цена поступлений, потому что "
                   "1С ещё не рассчитала себестоимость (обычно до закрытия месяца).")
        if estimated:
            out.append(f"ВНИМАНИЕ: по {estimated} позициям себестоимость оценена, "
                       "а не взята из учёта. Для отчётности дождитесь закрытия месяца.")
        return _report("\n".join(out), "Себестоимость проданного")

    def _cost_note(self, cost_total: float, revenue_total: float) -> str:
        if cost_total > 0.005:
            return ""
        return (" ВНИМАНИЕ: себестоимость в строках документов не заполнена — "
                "в «Бухгалтерии» она рассчитывается при проведении или при "
                "закрытии месяца. Маржа показана без себестоимости и завышена.")

    # --- себестоимость и рентабельность по брендам -------------------------

    def profit_by_brand(self, date_from: str | None = None,
                        date_to: str | None = None) -> str:
        """Выручка, себестоимость и маржа по брендам — из строк реализаций."""
        lines = list(self._sale_lines(date_from, date_to))
        if not lines:
            return (f"Данных о продажах {_period_label(date_from, date_to)} нет. "
                    "Без указания дат отчёт строится за всё время.")

        brands = self._brands()
        agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for nom, _month, net, cost in lines:
            brand = brands.get(nom, "без бренда")
            agg[brand][0] += net
            agg[brand][1] += cost

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
        out.append("Источник: строки проведённых реализаций за вычетом возвратов. "
                   "Выручка без НДС." + self._cost_note(tc, tr))
        return _report(
            f"Себестоимость и маржа по брендам, {_period_label(date_from, date_to)}:\n"
            + "\n".join(out), "Маржа по брендам")

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
        # Возврат гасит долг так же, как оплата: товар вернулся, платить не за
        # что. Без этого мы требуем денег за то, что уже приняли обратно.
        returns_used = False
        if debt_entity == "sale" and self._has_returns():
            ret = self.mapping.entity("sales_return")
            for r in self._returns(None, None):
                paid[r.get(ret.field_1c("counterparty"), "")] += float(
                    r.get(ret.field_1c("total")) or 0)
            returns_used = True

        as_of_dt = datetime.fromisoformat(as_of) if as_of else datetime.now()
        buckets = ("0-30", "31-60", "61-90", "90+")
        by_cp: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(buckets, 0.0))
        docs_by_cp: dict[str, list[str]] = defaultdict(list)
        advances: dict[str, float] = {}

        # Гасим оплаты старыми документами (FIFO) — как это делает бухгалтер.
        # Идём по объединению: контрагент, который заплатил, но которому мы
        # ничего не отгружали, — это чистая предоплата, и она должна быть
        # видна. Раньше такой плательщик не попадал в отчёт вообще.
        for cp in {d.get(cp_d, "") for d in docs_all} | set(paid):
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
            if rest > 0.005:
                # Денег пришло больше, чем отгружено: это аванс. Раньше остаток
                # молча отбрасывался, и аванс не был виден нигде.
                advances[cp] = rest

        if not by_cp and not advances:
            return empty_text

        names = self._names("counterparty", set(by_cp) | set(advances))
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
        for cp, amount in sorted(advances.items(), key=lambda kv: -kv[1]):
            out.append(f"АВАНС | {names.get(cp, '?')} | {_fmt(amount)} — "
                       f"оплачено больше, чем отгружено; это не долг нам")
        if returns_used:
            out.append("Возвраты от покупателей уменьшают долг наравне с оплатой.")
        out.append(f"Расчёт на {as_of_dt.date()}, оплаты разнесены по FIFO. "
                   "Возраст считается от даты документа; сроки оплаты по договорам "
                   "в данных отсутствуют, поэтому слово «просрочено» здесь неприменимо.")
        return _report("\n".join(out), title)

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
        try:
            counterparty_key = resolve_counterparty_key(
                self.client, self.mapping, counterparty_key)
        except CounterpartyNotResolved as e:
            return str(e)

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
        return _report("\n".join(out), f"Акт сверки с {name}")

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
        return _report("\n".join(out), "Движение денежных средств")

    # --- отчёт о прибылях и убытках (ОПиУ) ----------------------------------

    def _doc_revenue(self, date_from: str | None, date_to: str | None) -> float:
        """Выручка по документам без НДС — для сверки с регистром."""
        ent = self.mapping.entity("sale")
        conds = [Cond("Posted", OP_EQ, True, KIND_BOOL)] + _date_conds("Date", date_from, date_to)
        docs = self.client.run(Query(entity_set=ent.entity_set, conditions=conds))
        total = sum(_net_revenue(d, ent) for d in docs)
        if self._has_returns():
            ret = self.mapping.entity("sales_return")
            total -= sum(_net_revenue(r, ret) for r in self._returns(date_from, date_to))
        return total

    def pnl_report(self, date_from: str | None = None,
                   date_to: str | None = None) -> str:
        """Выручка, себестоимость и валовая прибыль за период."""
        lines = list(self._sale_lines(date_from, date_to))
        if not lines:
            return f"Продаж {_period_label(date_from, date_to)} нет."

        by_month: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for _nom, month, net, cost in lines:
            by_month[month][0] += net
            by_month[month][1] += cost

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
        # Регистр и документы — разные источники, и они законно расходятся
        # (регистр может не покрывать услуги и незакрытые периоды). Показываем
        # расхождение сами: иначе бухгалтер найдёт его первым и перестанет
        # доверять обоим отчётам.
        # Источник теперь один и тот же во всех отчётах — расхождению взяться
        # неоткуда. Сверку с шапками документов оставляем: она ловит случай,
        # когда часть суммы документа не разнесена по строкам (например, услуги
        # лежат в отдельной табличной части).
        by_docs = self._doc_revenue(date_from, date_to)
        gap = by_docs - tr
        out.append("Источник: строки проведённых реализаций за вычетом возвратов. "
                   f"Выручка по шапкам документов — {_fmt(by_docs)} руб."
                   + self._cost_note(tc, tr))
        if abs(gap) > 0.005:
            out.append(f"Расхождение со строками {_fmt(gap)} руб. — часть суммы "
                       "документов не разнесена по товарным строкам (услуги, "
                       "агентские). Сверьте в 1С до принятия решений.")
        out.append("Это валовая прибыль. Коммерческие и управленческие расходы "
                   "(счета 26 и 44), налоги и проценты в расчёт не включены: "
                   "в подключённых данных их нет, поэтому чистая прибыль здесь "
                   "не выводится.")
        return _report("\n".join(out), "Прибыли и убытки")

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

        ret_ent = self.mapping.entity("sales_return") if self._has_returns() else None
        by_month: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        for d in docs:
            m = str(d.get("Date"))[:7]
            by_month[m][0] += _net_revenue(d, ent)
            by_month[m][1] += 1
        for r in self._returns(date_from, date_to):
            # Возврат уменьшает выручку месяца, но отгрузкой не был —
            # число отгрузок и средний чек он не меняет.
            by_month[str(r.get("Date"))[:7]][0] -= _net_revenue(r, ret_ent)

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
        out.append("Средний чек — выручка, делённая на число проведённых реализаций. "
                   "Источник: проведённые реализации. " + _vat_note(ent) + " "
                   + self._returns_note())
        return _report("\n".join(out), "Динамика продаж")

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
                "Выручка, себестоимость, маржа по брендам (производителям).",
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
                "Обороты и сальдо по контрагенту: акт сверки взаиморасчётов.",
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
                "cost_report",
                "Себестоимость проданного по товарам: сколько, почём, маржа.",
                {"type": "object", "properties": dates},
                lambda **kw: self.cost_report(**kw),
            ),
            ToolSpec(
                "sales_dynamics",
                "Динамика продаж по месяцам и средний чек.",
                {"type": "object", "properties": dates},
                lambda **kw: self.sales_dynamics(**kw),
            ),
        ]
