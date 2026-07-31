import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer, default_dataset
from perimeter_bridge1c.analytics import AnalyticsTools
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient


def make(srv):
    mapping = load_mapping("bp30")
    return AnalyticsTools(
        ODataClient(srv.base_url, "robot", "test", mapping=mapping), mapping)


def test_abc_by_counterparty():
    with Fake1CServer() as srv:
        out = str(make(srv).abc_analysis("counterparty"))
        # Выручка БЕЗ НДС: Ромашка 252 000 / 1.2 = 210 000;
        # Василёк 75 000 / 1.2 = 62 500 минус возврат 6 000 / 1.2 = 5 000.
        assert "Ромашка" in out and "210 000.00" in out
        assert "57 500.00" in out
        assert out.splitlines()[1].startswith("A |")   # крупнейший — группа A
        assert "Василёк" in out


def test_abc_by_nomenclature_uses_document_lines():
    with Fake1CServer() as srv:
        out = str(make(srv).abc_analysis("nomenclature"))
        # Ноутбук: (90 000 + 99 000) без НДС = 157 500
        assert "Ноутбук" in out and "157 500.00" in out
        # Кресло только в непроведённом документе -> в расчёт не попадает
        assert "Кресло" not in out


def test_abc_respects_period():
    with Fake1CServer() as srv:
        out = str(make(srv).abc_analysis("counterparty", date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59"))
        # июньская РТ-0005 исключена -> у Ромашки только 120 000 с НДС = 100 000
        assert "100 000.00" in out and "210 000.00" not in out


def test_profit_by_brand():
    with Fake1CServer() as srv:
        out = str(make(srv).profit_by_brand())
        # Источник — строки реализаций (регистра «ВыручкаИСебестоимостьПродаж»
        # в реальной БП 3.0 нет, проверено 31.07). Ноутбук и Монитор — «Гамма»,
        # услуги без бренда. Себестоимость в фикстуре 2/3 от суммы без НДС.
        assert "Гамма" in out
        assert "240 000.00" in out and "160 000.00" in out
        assert "без бренда" in out and "27 500.00" in out
        assert "ИТОГО | выручка 267 500.00" in out


def test_profit_by_brand_period_filter():
    with Fake1CServer() as srv:
        out = str(make(srv).profit_by_brand(date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59"))
        # Июль без НДС: 112 500 минус возврат 5 000 = 107 500
        assert "ИТОГО | выручка 107 500.00" in out and "267 500.00" not in out


def test_receivables_aging():
    with Fake1CServer() as srv:
        out = str(make(srv).receivables_aging(as_of="2026-07-31T00:00:00"))
        # Ромашка: отгрузки 33000 (28.05) + 99000 (25.06) + 120000 (03.07),
        # оплата 120000 -> FIFO гасит майскую и июньскую (12000 остаток),
        # непогашено 132 000
        assert "Ромашка" in out
        assert "0-30" in out and "ИТОГО" in out
        assert "132 000.00" in out
        # Василёк: отгрузки 15000 (июль) + 60000 (май), оплата 15000 -> должен
        assert "Василёк" in out


# --- кредиторская задолженность -------------------------------------------

def test_payables_aging():
    with Fake1CServer() as srv:
        out = str(make(srv).payables_aging(as_of="2026-07-31T00:00:00"))
        # ТехноСервис: приход ПТ-0001 300 000 (05.07), оплачено 150 000 + 50 000
        # -> непогашено 100 000, возраст 25 дн. -> корзина 0-30
        assert "ТехноСервис" in out and "100 000.00" in out
        # Василёк: приход ПТ-0002 40 000 от 10.03, оплат поставщику не было
        # -> 142 дня -> корзина 90+
        assert "40 000.00" in out
        assert "ИТОГО | 100 000.00 | 0.00 | 0.00 | 40 000.00 | 140 000.00" in out


def test_payables_and_receivables_do_not_mix():
    """Кредиторка считается по поступлениям, а не по нашим отгрузкам."""
    with Fake1CServer() as srv:
        out = str(make(srv).payables_aging(as_of="2026-07-31T00:00:00"))
        assert "РТ-0001" not in out and "ПТ-0001" in out


# --- акт сверки ------------------------------------------------------------

def test_reconciliation_act_balances():
    with Fake1CServer() as srv:
        out = str(make(srv).reconciliation_act(GUID_ROMASHKA))
        # Проведённые: отгрузки 33 000 + 99 000 + 120 000 = 252 000,
        # оплата ПС-0001 120 000 -> сальдо 132 000 в нашу пользу
        assert "Сальдо на начало периода: 0.00" in out
        assert "отгружено 252 000.00 руб., оплачено 120 000.00 руб." in out
        assert "Сальдо на конец периода: 132 000.00" in out
        assert "долг контрагента перед нами" in out


def test_reconciliation_act_opening_balance():
    """Документы до начала периода сворачиваются во входящее сальдо."""
    with Fake1CServer() as srv:
        out = str(make(srv).reconciliation_act(GUID_ROMASHKA, date_from="2026-07-01T00:00:00"))
        # До июля: отгрузки 33 000 (май) + 99 000 (июнь) = 132 000, оплат не было
        assert "Сальдо на начало периода: 132 000.00" in out
        assert "РТ-0005" not in out          # июньский документ в обороты не попал
        assert "Сальдо на конец периода: 132 000.00" in out


def test_reconciliation_act_unknown_counterparty():
    with Fake1CServer() as srv:
        out = str(make(srv).reconciliation_act("00000000-0000-0000-0000-000000000000"))
        assert "нет проведённых отгрузок и оплат" in out


# --- движение денежных средств --------------------------------------------

def test_cash_flow_by_month():
    with Fake1CServer() as srv:
        out = str(make(srv).cash_flow())
        # Июнь: списание 150 000, поступлений нет
        assert "2026-06 | 0.00 | 150 000.00 | -150 000.00" in out
        # Июль: поступило 120 000 + 15 000 + аванс 20 000, списано 50 000
        assert "2026-07 | 155 000.00 | 50 000.00 | 105 000.00" in out
        assert "ИТОГО | 155 000.00 | 200 000.00 | -45 000.00" in out


def test_cash_flow_states_missing_opening_balance():
    """Остаток на счёте мы не знаем и не должны его придумывать."""
    with Fake1CServer() as srv:
        assert "Остаток на счёте не показан" in str(make(srv).cash_flow())


def test_cash_flow_respects_period():
    with Fake1CServer() as srv:
        out = str(make(srv).cash_flow(date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59"))
        assert "2026-06" not in out and "2026-07" in out


# --- отчёт о прибылях и убытках -------------------------------------------

def test_pnl_report():
    with Fake1CServer() as srv:
        out = str(make(srv).pnl_report())
        # Всё без НДС, себестоимость в фикстуре 2/3 от суммы без НДС.
        assert "2026-06 | 82 500.00 | 55 000.00 | 27 500.00" in out
        assert "2026-07 | 107 500.00 | 71 666.67 | 35 833.33" in out
        assert "ИТОГО | 267 500.00 | 178 333.33 | 89 166.67" in out


def test_pnl_declares_what_is_not_included():
    """Чистую прибыль без счетов 26/44 считать нельзя — и мы об этом говорим."""
    with Fake1CServer() as srv:
        out = str(make(srv).pnl_report())
        assert "валовая прибыль" in out.lower()
        assert "чистая прибыль здесь" in out and "26 и 44" in out


# --- динамика продаж и средний чек ----------------------------------------

def test_sales_dynamics_and_average_check():
    with Fake1CServer() as srv:
        out = str(make(srv).sales_dynamics())
        # Всё без НДС. Май: (60 000 + 33 000)/1.2 = 77 500 за 2 отгрузки
        assert "2026-05 | 77 500.00 | 2 | 38 750.00" in out
        # Июнь: 99 000/1.2 = 82 500, одна отгрузка
        assert "2026-06 | 82 500.00 | 1 | 82 500.00" in out
        # Июль: (120 000 + 15 000)/1.2 = 112 500 минус возврат 5 000 = 107 500
        assert "2026-07 | 107 500.00 | 2 | 53 750.00" in out


def test_sales_dynamics_shows_month_over_month_change():
    with Fake1CServer() as srv:
        out = str(make(srv).sales_dynamics())
        june = next(l for l in out.splitlines() if l.startswith("2026-06"))
        assert "+6.5%" in june     # 82 500 против 77 500
        may = next(l for l in out.splitlines() if l.startswith("2026-05"))
        assert may.endswith("—")   # сравнивать не с чем


def test_sales_dynamics_excludes_unposted():
    """Непроведённые РТ-0002/РТ-0003 — не продажи."""
    with Fake1CServer() as srv:
        out = str(make(srv).sales_dynamics())
        assert "ИТОГО | 267 500.00 | 5" in out


# --- общие требования к набору инструментов -------------------------------

def test_every_report_states_its_period():
    """Модель выдумывала период, если отчёт о нём молчал."""
    with Fake1CServer() as srv:
        a = make(srv)
        for out in (a.abc_analysis("counterparty"), a.profit_by_brand(),
                    a.cash_flow(), a.pnl_report(), a.sales_dynamics()):
            assert "за всё время" in str(out)
        for out in (a.receivables_aging(as_of="2026-07-31T00:00:00"),
                    a.payables_aging(as_of="2026-07-31T00:00:00")):
            assert "Расчёт на 2026-07-31" in str(out)


def test_analytics_specs_are_compact():
    import json
    with Fake1CServer() as srv:
        specs = make(srv).specs()
        assert [s.name for s in specs] == [
            "abc_analysis", "profit_by_brand", "receivables_aging",
            "payables_aging", "reconciliation_act", "cash_flow",
            "pnl_report", "cost_report", "sales_dynamics"]
        assert all(not s.requires_approval for s in specs)  # только чтение
        size = len(json.dumps([s.openai_schema() for s in specs], ensure_ascii=False))
        # Схемы уходят модели на КАЖДОМ ходе, поэтому описания держим в одну
        # строку. 2400 символов ~ 700 токенов prefill — это около секунды на
        # стенде 16 ГБ; больше отдавать за список инструментов не готовы.
        assert size < 2400, f"схемы раздулись до {size} символов — это prefill на каждом ходе"


# --- разделение «человеку таблица, модели выжимка» ------------------------

def test_model_sees_the_top_but_not_the_tail():
    """Верхушку модель копирует, хвост не видит и потому не перечисляет."""
    with Fake1CServer() as srv:
        rep = make(srv).receivables_aging(as_of="2026-07-31T00:00:00")
        assert "Ромашка" in rep.digest              # верхнюю строку назвать можно
        assert "РТ-0005" not in rep.digest          # документы-основания скрыты
        assert "ещё" in rep.digest                  # и модель знает, что скрыты


def test_digest_keeps_what_the_model_needs():
    """Период, итог и оговорки остаются: из них модель пишет подводку."""
    with Fake1CServer() as srv:
        a = make(srv)
        aging = a.payables_aging(as_of="2026-07-31T00:00:00")
        assert "140 000.00" in aging.digest            # ИТОГО
        assert "Расчёт на 2026-07-31" in aging.digest  # оговорка
        pnl = a.pnl_report()
        assert "за всё время" in pnl.digest and "89 166.67" in pnl.digest
        assert "чистая прибыль здесь" in pnl.digest
        act = a.reconciliation_act(GUID_ROMASHKA)
        assert "Сальдо на конец периода: 132 000.00" in act.digest


def test_digest_stays_shorter_than_the_report():
    """Выжимка уходит в prefill на каждом ходе — она не должна равняться отчёту."""
    with Fake1CServer() as srv:
        rep = make(srv).receivables_aging(as_of="2026-07-31T00:00:00")
        assert len(rep.digest) < len(rep.display) * 0.8


def test_reports_have_titles():
    with Fake1CServer() as srv:
        a = make(srv)
        assert a.cash_flow().title == "Движение денежных средств"
        assert "Ромашка" in a.reconciliation_act(GUID_ROMASHKA).title


def test_digest_marks_the_hidden_rows_briefly():
    """Маркер уходит в prefill с каждым отчётом — он должен быть коротким."""
    with Fake1CServer() as srv:
        digest = make(srv).payables_aging(as_of="2026-07-31T00:00:00").digest
        marker = next(l for l in digest.splitlines() if l.startswith("["))
        assert "строк показано пользователю" in marker and len(marker) < 60
        assert "140 000.00" in digest    # итог назвать можно и нужно


# --- НДС и возвраты -------------------------------------------------------
# «Деньги любят точность»: выручка с НДС завышена на ставку налога, а маржа
# завышена дважды, потому что себестоимость идёт без НДС.

def test_revenue_excludes_vat_but_debt_does_not():
    """Выручка — без НДС, долг — с НДС: контрагент должен полную сумму."""
    with Fake1CServer() as srv:
        a = make(srv)
        abc = str(a.abc_analysis("counterparty"))
        assert "210 000.00" in abc          # 252 000 с НДС -> 210 000 без НДС
        aging = str(a.receivables_aging(as_of="2026-07-31T00:00:00"))
        assert "132 000.00" in aging        # долг остаётся полным, с НДС
        assert "110 000.00" not in aging    # без НДС здесь было бы неверно


def test_returns_reduce_revenue():
    with Fake1CServer() as srv:
        a = make(srv)
        july = str(a.abc_analysis("counterparty",
                                  date_from="2026-07-01T00:00:00",
                                  date_to="2026-07-31T23:59:59"))
        # Василёк: 15 000 с НДС = 12 500, возврат 6 000 с НДС = 5 000 -> 7 500
        assert "7 500.00" in july


def test_both_abc_cuts_agree_on_total():
    """Разрезка по клиентам и по товарам обязана дать одну выручку.

    Если они разойдутся, доверять нельзя ни одной: это первое, что заметит
    бухгалтер.
    """
    with Fake1CServer() as srv:
        a = make(srv)
        by_cp = str(a.abc_analysis("counterparty"))
        by_nom = str(a.abc_analysis("nomenclature"))
        assert "выручка 267 500.00 руб." in by_cp
        assert "выручка 267 500.00 руб." in by_nom


def test_report_declares_its_basis():
    """Отчёт обязан сказать, что в нём с НДС, а что без, и учтены ли возвраты."""
    with Fake1CServer() as srv:
        out = str(make(srv).abc_analysis("counterparty"))
        assert "Выручка без НДС." in out
        assert "за вычетом возвратов" in out


def test_missing_vat_field_is_declared_not_hidden():
    """Нет НДС в описании строк — отчёт предупреждает, а не молчит.

    НДС живёт в строках документа: реквизита в шапке у БП 3.0 нет
    (проверено роботом на живой базе 31.07).
    """
    mapping = load_mapping("bp30")
    mapping.entity("sale").row_fields.pop("vat", None)
    with Fake1CServer() as srv:
        tools = AnalyticsTools(
            ODataClient(srv.base_url, "robot", "test", mapping=mapping), mapping)
        out = str(tools.abc_analysis("counterparty"))
        assert "реквизит НДС не описан" in out
        assert "252 000.00" in out      # тогда суммы честно с НДС


def test_pnl_agrees_with_the_other_reports():
    """Один источник — одна выручка. Раньше ОПиУ давал 234 000 против 267 500.

    Расхождение шло от регистра «ВыручкаИСебестоимостьПродаж», которого в
    реальной БП 3.0 не существует (проверено на живой базе 31.07).
    """
    with Fake1CServer() as srv:
        a = make(srv)
        out = str(a.pnl_report())
        assert "Источник: строки проведённых реализаций" in out
        assert "Выручка по шапкам документов — 267 500.00" in out
        assert "Расхождение со строками" not in out      # источник один
        assert "ИТОГО | 267 500.00" in out
        assert "выручка 267 500.00 руб." in str(a.abc_analysis("counterparty"))
        assert "ИТОГО | 267 500.00" in str(a.sales_dynamics())


def test_document_based_reports_agree_with_each_other():
    """ABC и динамика продаж считают выручку одинаково — иначе доверия нет."""
    with Fake1CServer() as srv:
        a = make(srv)
        assert "выручка 267 500.00 руб." in str(a.abc_analysis("counterparty"))
        assert "ИТОГО | 267 500.00" in str(a.sales_dynamics())


def test_returns_reduce_the_debt():
    """Товар вернулся — требовать за него деньги нельзя."""
    with Fake1CServer() as srv:
        out = str(make(srv).receivables_aging(as_of="2026-07-31T00:00:00"))
        # Василёк: было 60 000, возврат 6 000 -> 54 000
        assert "54 000.00" in out
        assert "Возвраты от покупателей уменьшают долг" in out


def test_advance_is_shown_not_swallowed():
    """Переплата — это аванс, а не ноль. Раньше остаток молча отбрасывался."""
    with Fake1CServer() as srv:
        out = str(make(srv).receivables_aging(as_of="2026-07-31T00:00:00"))
        assert "АВАНС" in out and "ТехноСервис" in out and "20 000.00" in out
        assert "это не долг нам" in out


def test_advance_does_not_leak_into_the_debt_total():
    """Аванс не должен ни увеличивать, ни уменьшать сумму долга."""
    with Fake1CServer() as srv:
        out = str(make(srv).receivables_aging(as_of="2026-07-31T00:00:00"))
        total = next(l for l in out.splitlines() if l.startswith("ИТОГО"))
        assert total.endswith("186 000.00")


def test_tool_descriptions_use_the_words_accountants_use():
    """Замер 30.07: «производители» уходили в поиск документов, «обороты» — тоже.

    Оба раза причина была в нашем словаре, а не в модели.
    """
    with Fake1CServer() as srv:
        by_name = {s.name: s.description for s in make(srv).specs()}
        assert "производител" in by_name["profit_by_brand"]
        assert "боротов" in by_name["reconciliation_act"] or \
               "Обороты" in by_name["reconciliation_act"]


def test_digest_shows_top_rows_so_the_model_can_name_them():
    """Ответ на «раздели клиентов на группы» обязан называть клиентов.

    Не видя ни одной строки, модель их выдумывала — на замере 31.07 так
    помечались четыре ответа из 61. Верхушку показываем: её модель копирует,
    и искажение ловится сверкой, потому что источник у неё тот же.
    """
    with Fake1CServer() as srv:
        rep = make(srv).abc_analysis("counterparty")
        assert "Ромашка" in rep.digest and "210 000.00" in rep.digest


def test_digest_hides_the_tail_and_says_so():
    with Fake1CServer() as srv:
        rep = make(srv).receivables_aging(as_of="2026-07-31T00:00:00")
        # Документы-основания (строки с отступом) модель не видит никогда.
        assert "РТ-0005" not in rep.digest
        assert "ещё" in rep.digest and "показано пользователю таблицей" in rep.digest


def test_column_header_does_not_eat_the_row_budget():
    """Шапка «контрагент | 0-30 | …» — не строка данных."""
    with Fake1CServer() as srv:
        rep = make(srv).receivables_aging(as_of="2026-07-31T00:00:00")
        assert "контрагент | 0-30" in rep.digest
        assert "Ромашка" in rep.digest and "Василёк" in rep.digest


def test_missing_cost_is_declared_not_shown_as_full_margin():
    """Себестоимость в БП считается при проведении/закрытии месяца.

    Пока она не заполнена, маржа равна выручке — показывать это как прибыль
    нельзя, поэтому отчёт предупреждает.
    """
    ds = default_dataset()
    for doc in ds["Document_РеализацияТоваровУслуг"]:
        for row in doc.get("Товары", []):
            row["Себестоимость"] = 0
    with Fake1CServer(dataset=ds) as srv:
        out = str(make(srv).profit_by_brand())
        assert "себестоимость в строках документов не заполнена" in out
        assert "завышена" in out


def test_mapping_has_no_unverified_entity_names():
    """Имена сверены с живой БП 3.0.111 — догадок в маппинге остаться не должно."""
    from pathlib import Path
    text = Path("bridge-1c/perimeter_bridge1c/mappings/bp30.yaml").read_text(encoding="utf-8")
    todos = [l.strip() for l in text.splitlines()
             if "TODO(verify)" in l and not l.strip().startswith("#")]
    assert not todos, f"остались непроверенные имена: {todos}"


def test_vat_comes_from_lines_not_from_the_document_header():
    """У БП 3.0 нет реквизита СуммаНДС в шапке — только в строках.

    Проверка живой базы 31.07 показала, что прежний расчёт по шапке молча
    вычитал ноль и выдавал сумму с НДС за сумму без НДС.
    """
    ds = default_dataset()
    for doc in ds["Document_РеализацияТоваровУслуг"]:
        doc.pop("СуммаНДС", None)          # как в настоящей БП 3.0
    with Fake1CServer(dataset=ds) as srv:
        out = str(make(srv).abc_analysis("counterparty"))
        assert "210 000.00" in out          # 252 000 с НДС -> 210 000 без НДС
        assert "252 000.00" not in out


# --- потоварный ABC и расчёт себестоимости --------------------------------

def test_item_abc_shows_quantity_and_margin():
    """Классический потоварный ABC — это не только выручка."""
    with Fake1CServer() as srv:
        out = str(make(srv).abc_analysis("nomenclature"))
        assert "продано | маржа" in out
        # Ноутбук: 4 шт, выручка 157 500, себестоимость 105 000 -> маржа 52 500
        assert "| 4 | 52 500.00 (33.3%)" in out
        # По контрагентам этих колонок быть не должно — там нет ни штук, ни себестоимости
        assert "продано | маржа" not in str(make(srv).abc_analysis("counterparty"))


def test_cost_report_uses_1c_figures_when_present():
    with Fake1CServer() as srv:
        out = str(make(srv).cost_report())
        assert "Ноутбук ProBook 14 | 4 | 157 500.00 | 105 000.00 | 1С" in out
        итого = next(l for l in out.splitlines() if l.startswith("ИТОГО"))
        assert "267 500.00" in итого and "178 333.33" in итого and "89 166.67" in итого


def test_cost_report_estimates_from_purchases_when_1c_has_not_calculated():
    """До закрытия месяца себестоимости в строках нет — оцениваем по закупкам.

    Показывать в этом случае маржу, равную выручке, нельзя: это читается как
    прибыль. Оценка помечается в каждой строке и отдельным предупреждением.
    """
    ds = default_dataset()
    for doc in ds["Document_РеализацияТоваровУслуг"]:
        for row in doc.get("Товары", []):
            row["Себестоимость"] = 0
    with Fake1CServer(dataset=ds) as srv:
        out = str(make(srv).cost_report())
        assert "оценка по закупкам" in out
        assert "себестоимость оценена, а не взята из учёта" in out
        # Ноутбук закуплен 3 шт на 180 000 с НДС = 150 000 без НДС -> 50 000/шт;
        # продано 4 шт -> оценка 200 000
        assert "200 000.00" in out


def test_cost_report_marks_positions_without_any_cost_source():
    """Не продавали и не покупали — честное «нет данных», а не ноль."""
    ds = default_dataset()
    for doc in ds["Document_РеализацияТоваровУслуг"]:
        for row in doc.get("Товары", []):
            row["Себестоимость"] = 0
    ds["Document_ПоступлениеТоваровУслуг"] = []
    with Fake1CServer(dataset=ds) as srv:
        out = str(make(srv).cost_report())
        assert "нет данных" in out


def test_cost_report_is_registered_as_a_tool():
    with Fake1CServer() as srv:
        names = [s.name for s in make(srv).specs()]
        assert "cost_report" in names


def test_unknown_cost_does_not_print_a_hundred_percent_margin():
    """Живая база 31.07: по услугам себестоимости нет, а отчёт показывал 100%.

    Директор читает это как прибыль. Без себестоимости маржа не выводится.
    """
    ds = default_dataset()
    for doc in ds["Document_РеализацияТоваровУслуг"]:
        for row in doc.get("Товары", []):
            row["Себестоимость"] = 0
    ds["Document_ПоступлениеТоваровУслуг"] = []      # и оценить не по чему
    with Fake1CServer(dataset=ds) as srv:
        out = str(make(srv).cost_report())
        assert "100.0%" not in out
        assert "нет данных" in out
        итого = next(l for l in out.splitlines() if l.startswith("ИТОГО"))
        assert итого.rstrip().endswith("—")
        assert "маржа по ним и по отчёту в целом не выводится" in out
