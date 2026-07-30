import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient
from perimeter_bridge1c.tools import Bridge1CTools, execute_tool


def make_tools(srv):
    mapping = load_mapping("bp30")
    client = ODataClient(srv.base_url, "robot", "test", mapping=mapping)
    return Bridge1CTools(client, mapping)


def test_get_counterparty_by_name():
    with Fake1CServer() as srv:
        out = make_tools(srv).get_counterparty("ромашка")
        assert "Ромашка" in out and GUID_ROMASHKA in out and "7701234567" in out


def test_get_counterparty_by_inn():
    with Fake1CServer() as srv:
        out = make_tools(srv).get_counterparty("5047112233")
        assert "ТехноСервис" in out


def test_find_unposted_sales_july():
    with Fake1CServer() as srv:
        out = make_tools(srv).find_document(
            "sale", counterparty_key=GUID_ROMASHKA,
            date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59",
            posted=False)
        assert "РТ-0002" in out and "РТ-0003" in out
        assert "РТ-0001" not in out  # проведён
        assert "НЕ проведён" in out


def test_find_document_references_number_and_date():
    with Fake1CServer() as srv:
        out = make_tools(srv).find_document("sale", number="РТ-0001")
        assert "№РТ-0001 от 2026-07-03" in out


def test_ledger_report_unpaid_balance():
    with Fake1CServer() as srv:
        out = make_tools(srv).ledger_report(GUID_ROMASHKA)
        # Проведённые отгрузки Ромашки: 120000 (июль) + 99000 (июнь); оплата 120000.
        assert "сальдо (не оплачено) 132 000.00" in out


def test_create_draft_is_draft_only():
    with Fake1CServer() as srv:
        out = make_tools(srv).create_draft_document(
            "customer_invoice", GUID_ROMASHKA, total=45000.5)
        assert "ЧЕРНОВИК" in out
        entity_set, row = srv.created[0]
        assert entity_set == "Document_СчетНаОплатуПокупателю"
        assert row["Posted"] is False


def test_create_draft_based_on():
    with Fake1CServer() as srv:
        tools = make_tools(srv)
        src_key = "\n".join(
            line for line in tools.find_document("customer_invoice", number="СЧ-0101").splitlines()
        ).split("key=")[1]
        out = tools.create_draft_document(
            "customer_invoice", GUID_ROMASHKA, based_on_key=src_key)
        assert "ЧЕРНОВИК" in out
        _, row = srv.created[0]
        assert row["СуммаДокумента"] == 99000.00  # скопировано из основания


def test_specs_and_execute():
    with Fake1CServer() as srv:
        tools = make_tools(srv)
        specs = tools.specs()
        assert [s.name for s in specs] == [
            "get_counterparty", "list_counterparties", "find_document",
            "ledger_report", "create_draft_document"]
        draft = next(s for s in specs if s.name == "create_draft_document")
        assert draft.requires_approval
        out, spec = execute_tool(specs, "get_counterparty", json.dumps({"query": "василёк"}))
        assert "Василёк" in out and spec.name == "get_counterparty"
        out, spec = execute_tool(specs, "nope", "{}")
        assert "неизвестный инструмент" in out and spec is None


def test_execute_bad_json_is_soft_error():
    with Fake1CServer() as srv:
        specs = make_tools(srv).specs()
        out, spec = execute_tool(specs, "find_document", "{broken")
        assert "Ошибка" in out and spec is not None


# --- Поиск контрагента: как его пишет живой пользователь -------------------

def test_normalizes_user_written_counterparty_names():
    """Найдено живым прогоном модели: «ООО «Ромашка»» не находилось."""
    from perimeter_bridge1c.tools import normalize_counterparty_query as norm
    assert norm('ООО «Ромашка»') == "ромашка"
    assert norm('ООО "Ромашка"') == "ромашка"
    assert norm("Ромашка") == "ромашка"
    assert norm('АО «ТехноСервис»') == "техносервис"
    assert norm("ИП Иванов") == "иванов"
    assert norm('ООО «Торговый дом»') == "торговый дом"
    # Пустой результат недопустим — возвращаем исходное
    assert norm("ООО") == "ооо"


def test_counterparty_search_with_quotes_and_legal_form():
    with Fake1CServer() as srv:
        tools = make_tools(srv)
        for written_as in ('ООО «Ромашка»', 'ООО "Ромашка"', "  РОМАШКА  ", "ромашка"):
            out = tools.get_counterparty(written_as)
            assert "Ромашка" in out, f"не найдено при написании {written_as!r}: {out}"


def test_list_counterparties_returns_real_records():
    """Без этого инструмента модель выдумывает контрагентов (живое демо)."""
    with Fake1CServer() as srv:
        out = make_tools(srv).list_counterparties()
        assert "Ромашка" in out and "Василёк" in out and "ТехноСервис" in out
        assert "7701234567" in out          # ИНН на месте
        assert out.count("key=") == 3       # ровно три записи базы


def test_list_counterparties_marks_truncation():
    with Fake1CServer() as srv:
        out = make_tools(srv).list_counterparties(limit=2)
        assert "показаны первые" in out
        assert out.count("key=") == 2
