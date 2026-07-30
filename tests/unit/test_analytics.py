import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer
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
        # Проведённые: Ромашка 120000+99000=219000, Василёк 15000 -> 93.6% / 6.4%
        assert "Ромашка" in out and "252 000.00" in out
        assert out.splitlines()[1].startswith("A |")   # крупнейший — группа A
        assert "Василёк" in out


def test_abc_by_nomenclature_uses_document_lines():
    with Fake1CServer() as srv:
        out = str(make(srv).abc_analysis("nomenclature"))
        # Ноутбук в проведённых: 90000 (РТ-0001) + 99000 (РТ-0005) = 189000
        assert "Ноутбук" in out and "189 000.00" in out
        # Кресло только в непроведённом документе -> в расчёт не попадает
        assert "Кресло" not in out


def test_abc_respects_period():
    with Fake1CServer() as srv:
        out = str(make(srv).abc_analysis("counterparty", date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59"))
        # июньская РТ-0005 (99000) исключена -> у Ромашки только 120000
        assert "120 000.00" in out and "219 000.00" not in out


def test_profit_by_brand():
    with Fake1CServer() as srv:
        out = str(make(srv).profit_by_brand())
        # Гамма: выручка 90000+30000+15000+99000=234000, себестоимость 159500
        assert "Гамма" in out
        assert "234 000.00" in out and "159 500.00" in out
        assert "ИТОГО" in out
        # маржа = 74 500 -> 31.8%
        assert "74 500.00" in out


def test_profit_by_brand_period_filter():
    with Fake1CServer() as srv:
        out = str(make(srv).profit_by_brand(date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59"))
        assert "135 000.00" in out  # 90000+30000+15000, июньская запись исключена


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
        # Июль: поступило 120 000 + 15 000, списано 50 000
        assert "2026-07 | 135 000.00 | 50 000.00 | 85 000.00" in out
        assert "ИТОГО | 135 000.00 | 200 000.00 | -65 000.00" in out


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
        # Июнь: выручка 99 000, себестоимость 67 000 -> 32 000
        assert "2026-06 | 99 000.00 | 67 000.00 | 32 000.00" in out
        # Июль: 90 000+30 000+15 000 = 135 000, себестоимость 61+21+10.5 = 92 500
        assert "2026-07 | 135 000.00 | 92 500.00 | 42 500.00" in out
        assert "ИТОГО | 234 000.00 | 159 500.00 | 74 500.00" in out


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
        # Май: 60 000 + 33 000 = 93 000 за 2 отгрузки -> средний чек 46 500
        assert "2026-05 | 93 000.00 | 2 | 46 500.00" in out
        # Июнь: одна отгрузка 99 000 -> средний чек равен сумме
        assert "2026-06 | 99 000.00 | 1 | 99 000.00" in out
        # Июль: 120 000 + 15 000 = 135 000 за 2 -> 67 500
        assert "2026-07 | 135 000.00 | 2 | 67 500.00" in out


def test_sales_dynamics_shows_month_over_month_change():
    with Fake1CServer() as srv:
        out = str(make(srv).sales_dynamics())
        june = next(l for l in out.splitlines() if l.startswith("2026-06"))
        assert "+6.5%" in june     # 99 000 против 93 000
        may = next(l for l in out.splitlines() if l.startswith("2026-05"))
        assert may.endswith("—")   # сравнивать не с чем


def test_sales_dynamics_excludes_unposted():
    """Непроведённые РТ-0002/РТ-0003 — не продажи."""
    with Fake1CServer() as srv:
        out = str(make(srv).sales_dynamics())
        assert "ИТОГО | 327 000.00 | 5" in out


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
            "pnl_report", "sales_dynamics"]
        assert all(not s.requires_approval for s in specs)  # только чтение
        size = len(json.dumps([s.openai_schema() for s in specs], ensure_ascii=False))
        # Схемы уходят модели на КАЖДОМ ходе, поэтому описания держим в одну
        # строку. 2400 символов ~ 700 токенов prefill — это около секунды на
        # стенде 16 ГБ; больше отдавать за список инструментов не готовы.
        assert size < 2400, f"схемы раздулись до {size} символов — это prefill на каждом ходе"


# --- разделение «человеку таблица, модели выжимка» ------------------------

def test_report_hides_rows_from_the_model():
    """Строк таблицы модель не получает — искажать нечего."""
    with Fake1CServer() as srv:
        rep = make(srv).receivables_aging(as_of="2026-07-31T00:00:00")
        assert "Ромашка" in rep.display and "120 000.00" in rep.display
        assert "Ромашка" not in rep.digest      # имён в выжимке нет
        assert "120 000.00" not in rep.digest   # построчных сумм тоже
        assert "строк показано пользователю" in rep.digest  # но знает, что она есть


def test_digest_keeps_what_the_model_needs():
    """Период, итог и оговорки остаются: из них модель пишет подводку."""
    with Fake1CServer() as srv:
        a = make(srv)
        aging = a.payables_aging(as_of="2026-07-31T00:00:00")
        assert "140 000.00" in aging.digest            # ИТОГО
        assert "Расчёт на 2026-07-31" in aging.digest  # оговорка
        pnl = a.pnl_report()
        assert "за всё время" in pnl.digest and "74 500.00" in pnl.digest
        assert "чистая прибыль здесь" in pnl.digest
        act = a.reconciliation_act(GUID_ROMASHKA)
        assert "Сальдо на конец периода: 132 000.00" in act.digest
        assert "РТ-0007" not in act.digest            # строк оборотов нет


def test_digest_is_much_smaller_than_the_report():
    """Выжимка уходит в prefill на каждом ходе — она должна быть короткой."""
    with Fake1CServer() as srv:
        rep = make(srv).receivables_aging(as_of="2026-07-31T00:00:00")
        assert len(rep.digest) < len(rep.display) / 2


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
