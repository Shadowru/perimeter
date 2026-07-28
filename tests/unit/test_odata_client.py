import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer, default_dataset
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient, ODataError, f_and, f_eq_bool, f_eq_guid


def make_client(srv, **kw):
    return ODataClient(srv.base_url, "robot", "test", mapping=load_mapping("bp30"), **kw)


def test_query_reads_catalog():
    with Fake1CServer() as srv:
        rows = list(make_client(srv).query("Catalog_Контрагенты"))
        assert len(rows) == 3
        assert any("Ромашка" in r["Description"] for r in rows)


def test_auth_required():
    with Fake1CServer() as srv:
        bad = ODataClient(srv.base_url, "robot", "wrong")
        with pytest.raises(ODataError) as e:
            list(bad.query("Catalog_Контрагенты"))
        assert e.value.status == 401


def test_filter_and_pagination():
    with Fake1CServer() as srv:
        client = make_client(srv, page_size=2)  # 3 документа Ромашки → 2 страницы
        rows = list(client.query(
            "Document_РеализацияТоваровУслуг",
            filter_=f_and([f_eq_guid("Контрагент_Key", GUID_ROMASHKA),
                           f_eq_bool("Posted", False)]),
        ))
        assert [r["Number"] for r in rows] == ["РТ-0002", "РТ-0003"]


def test_pagination_multiple_pages():
    with Fake1CServer() as srv:
        client = make_client(srv, page_size=2)
        rows = list(client.query("Document_РеализацияТоваровУслуг"))
        assert len(rows) == 5  # больше одной страницы


def test_retries_on_5xx():
    with Fake1CServer(fail_first_n=2) as srv:
        client = make_client(srv, retries=3)
        rows = list(client.query("Catalog_Контрагенты", top=1))
        assert rows


def test_retries_exhausted():
    with Fake1CServer(fail_first_n=10) as srv:
        client = make_client(srv, retries=1)
        with pytest.raises(ODataError):
            list(client.query("Catalog_Контрагенты"))


def test_get_by_guid():
    with Fake1CServer() as srv:
        row = make_client(srv).get("Catalog_Контрагенты", GUID_ROMASHKA)
        assert row["ИНН"] == "7701234567"


def test_create_draft_forces_posted_false():
    with Fake1CServer() as srv:
        client = make_client(srv)
        created = client.create_draft(
            "Document_СчетНаОплатуПокупателю",
            {"Контрагент_Key": GUID_ROMASHKA, "СуммаДокумента": 500, "Posted": True})
        assert created["Posted"] is False  # черновик принудительно
        assert srv.created[0][1]["Posted"] is False


def test_validate_mapping_ok():
    with Fake1CServer() as srv:
        assert make_client(srv).validate_mapping() == []


def test_validate_mapping_detects_missing():
    ds = default_dataset()
    del ds["Document_СчетНаОплатуПокупателю"]
    with Fake1CServer(dataset=ds) as srv:
        problems = make_client(srv).validate_mapping()
        assert any("СчетНаОплату" in p for p in problems)
