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


# --- контрагент по названию, а не только по ключу -------------------------
# Живой прогон 2026-07-30: модель сочиняла ключи вида «key_ООО_Ромашка».

def test_tools_accept_counterparty_name_instead_of_key():
    with Fake1CServer() as srv:
        t = make_tools(srv)
        out = t.ledger_report("Ромашка")
        assert "сальдо (не оплачено) 132 000.00" in out
        assert "РТ-0001" in t.find_document("sale", counterparty_key='ООО "Ромашка"')


def test_invented_key_gives_a_useful_message_not_a_wrong_report():
    with Fake1CServer() as srv:
        out = make_tools(srv).ledger_report("key_ООО_Ромашка")
        assert "не найден" in out and "list_counterparties" in out


def test_ambiguous_name_asks_to_clarify():
    with Fake1CServer() as srv:
        out = make_tools(srv).ledger_report("ООО")
        assert "несколько контрагентов" in out


def test_guid_still_works():
    with Fake1CServer() as srv:
        assert "132 000.00" in make_tools(srv).ledger_report(GUID_ROMASHKA)


def test_absurd_limit_is_clamped():
    """Модель передала limit=1000000000000000 (замер 30.07) — не выкачиваем базу."""
    from perimeter_bridge1c.tools import MAX_ROWS, _sane_limit
    assert _sane_limit(10 ** 15) == MAX_ROWS
    assert _sane_limit(0) == 1 and _sane_limit(-5) == 1
    assert _sane_limit(None) == 20 and _sane_limit("много") == 20
    assert _sane_limit(15) == 15
    with Fake1CServer() as srv:
        out = make_tools(srv).find_document("sale", limit=10 ** 15)
        assert "РТ-0001" in out          # запрос всё равно отработал


def test_doc_type_descriptions_distinguish_goods_from_money():
    """«Поступление» — это и товар, и деньги; модель их путала."""
    with Fake1CServer() as srv:
        spec = next(s for s in make_tools(srv).specs() if s.name == "find_document")
        assert "поступление товаров" in spec.description
        assert "приход денег" in spec.description


# --- выборка называет свои условия ----------------------------------------
# Модель теряла период («за июль» уходило без дат), и ни промпт, ни описание
# параметра этого не исправили. Тогда пусть об этом говорит сам результат.

def test_selection_states_that_no_period_was_given():
    with Fake1CServer() as srv:
        out = make_tools(srv).find_document("sale", posted=False)
        assert out.startswith("Выборка: реализации, за всё время (период не задан)")
        assert "только непроведённые" in out.splitlines()[0]


def test_selection_states_the_period_it_used():
    with Fake1CServer() as srv:
        out = make_tools(srv).find_document(
            "sale", date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59")
        assert "с 2026-07-01 по 2026-07-31" in out.splitlines()[0]


def test_empty_result_still_says_what_was_searched():
    """«Ничего не найдено» без условий — бесполезный ответ."""
    with Fake1CServer() as srv:
        out = make_tools(srv).find_document("sale", number="НЕТ-ТАКОГО")
        assert "Выборка:" in out and "номер НЕТ-ТАКОГО" in out
        assert "Документы не найдены" in out


def test_draft_reports_its_amount():
    """Без суммы в ответе инструмента фразу «счёт на 50 000» нечем подтвердить."""
    with Fake1CServer() as srv:
        out = make_tools(srv).create_draft_document(
            "customer_invoice", GUID_ROMASHKA, total=50000)
        assert "50 000.00" in out and "ЧЕРНОВИК" in out
        assert "от  |" not in out      # пустой даты быть не должно
