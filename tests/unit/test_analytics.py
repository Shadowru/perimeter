import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import Fake1CServer
from perimeter_bridge1c.analytics import AnalyticsTools
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient


def make(srv):
    mapping = load_mapping("bp30")
    return AnalyticsTools(
        ODataClient(srv.base_url, "robot", "test", mapping=mapping), mapping)


def test_abc_by_counterparty():
    with Fake1CServer() as srv:
        out = make(srv).abc_analysis("counterparty")
        # Проведённые: Ромашка 120000+99000=219000, Василёк 15000 -> 93.6% / 6.4%
        assert "Ромашка" in out and "219 000.00" in out
        assert out.splitlines()[1].startswith("A |")   # крупнейший — группа A
        assert "Василёк" in out


def test_abc_by_nomenclature_uses_document_lines():
    with Fake1CServer() as srv:
        out = make(srv).abc_analysis("nomenclature")
        # Ноутбук в проведённых: 90000 (РТ-0001) + 99000 (РТ-0005) = 189000
        assert "Ноутбук" in out and "189 000.00" in out
        # Кресло только в непроведённом документе -> в расчёт не попадает
        assert "Кресло" not in out


def test_abc_respects_period():
    with Fake1CServer() as srv:
        out = make(srv).abc_analysis("counterparty",
                                     date_from="2026-07-01T00:00:00",
                                     date_to="2026-07-31T23:59:59")
        # июньская РТ-0005 (99000) исключена -> у Ромашки только 120000
        assert "120 000.00" in out and "219 000.00" not in out


def test_profit_by_brand():
    with Fake1CServer() as srv:
        out = make(srv).profit_by_brand()
        # Гамма: выручка 90000+30000+15000+99000=234000, себестоимость 159500
        assert "Гамма" in out
        assert "234 000.00" in out and "159 500.00" in out
        assert "ИТОГО" in out
        # маржа = 74 500 -> 31.8%
        assert "74 500.00" in out


def test_profit_by_brand_period_filter():
    with Fake1CServer() as srv:
        out = make(srv).profit_by_brand(date_from="2026-07-01T00:00:00",
                                        date_to="2026-07-31T23:59:59")
        assert "135 000.00" in out  # 90000+30000+15000, июньская запись исключена


def test_receivables_aging():
    with Fake1CServer() as srv:
        out = make(srv).receivables_aging(as_of="2026-07-31T00:00:00")
        # Ромашка: отгрузки 99000 (25.06) + 120000 (03.07), оплата 120000
        # FIFO гасит июньскую -> остаётся 120000 от 03.07 (28 дней) в 0-30
        assert "Ромашка" in out
        assert "0-30" in out and "ИТОГО" in out
        # Василёк оплатил полностью -> его в отчёте нет
        assert "Василёк" not in out


def test_analytics_specs_are_compact():
    import json
    with Fake1CServer() as srv:
        specs = make(srv).specs()
        assert [s.name for s in specs] == [
            "abc_analysis", "profit_by_brand", "receivables_aging"]
        assert all(not s.requires_approval for s in specs)  # только чтение
        size = len(json.dumps([s.openai_schema() for s in specs], ensure_ascii=False))
        assert size < 900, f"схемы раздулись до {size} символов — это prefill на каждом ходе"
