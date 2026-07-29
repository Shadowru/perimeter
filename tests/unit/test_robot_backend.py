import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, default_dataset
from fakes.fake_robot import FakeRobot
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.robot import RobotBackend, RobotError, RobotGateway
from perimeter_bridge1c.tools import Bridge1CTools

TOKEN = "test-token"


@pytest.fixture
def robot_stack():
    gw = RobotGateway(host="127.0.0.1", port=0, token=TOKEN)
    gw.start()
    mapping = load_mapping("bp30")
    backend = RobotBackend(gw, mapping=mapping, timeout_s=15)
    robot = FakeRobot(gw.base_url, token=TOKEN)
    robot.__enter__()
    try:
        yield gw, backend, robot, Bridge1CTools(backend, mapping)
    finally:
        robot.__exit__()
        gw.stop()


def test_find_documents_through_robot(robot_stack):
    _, _, _, tools = robot_stack
    out = tools.find_document(
        "sale", counterparty_key=GUID_ROMASHKA,
        date_from="2026-07-01T00:00:00", date_to="2026-07-31T23:59:59", posted=False)
    assert "РТ-0002" in out and "РТ-0003" in out
    assert "РТ-0001" not in out


def test_counterparty_search_through_robot(robot_stack):
    _, _, _, tools = robot_stack
    out = tools.get_counterparty("ромашка")
    assert "Ромашка" in out and GUID_ROMASHKA in out


def test_ledger_report_through_robot(robot_stack):
    _, _, _, tools = robot_stack
    out = tools.ledger_report(GUID_ROMASHKA)
    assert "сальдо (не оплачено) 99 000.00" in out


def test_draft_is_created_unposted(robot_stack):
    _, _, robot, tools = robot_stack
    out = tools.create_draft_document("customer_invoice", GUID_ROMASHKA, total=500.0)
    assert "ЧЕРНОВИК" in out
    assert len(robot.created) == 1
    entity, row = robot.created[0]
    assert entity == "Document_СчетНаОплатуПокупателю"
    assert row["Posted"] is False


def test_mapping_validation_through_robot(robot_stack):
    _, backend, _, _ = robot_stack
    assert backend.validate_mapping() == []


def test_mapping_problem_detected(robot_stack):
    gw, backend, robot, _ = robot_stack
    del robot.dataset["Document_СчетНаОплатуПокупателю"]
    problems = backend.validate_mapping()
    assert any("СчетНаОплату" in p for p in problems)


def test_timeout_when_robot_absent():
    gw = RobotGateway(host="127.0.0.1", port=0)
    gw.start()
    try:
        backend = RobotBackend(gw, timeout_s=1)
        with pytest.raises(RobotError) as e:
            list(backend.run(__import__("perimeter_bridge1c.backend", fromlist=["Query"])
                             .Query(entity_set="Catalog_Контрагенты")))
        assert "не ответил" in str(e.value)
    finally:
        gw.stop()


def test_bad_token_rejected():
    gw = RobotGateway(host="127.0.0.1", port=0, token="right")
    gw.start()
    try:
        with FakeRobot(gw.base_url, token="wrong") as robot:
            backend = RobotBackend(gw, timeout_s=2)
            with pytest.raises(RobotError):
                list(backend.run(__import__("perimeter_bridge1c.backend", fromlist=["Query"])
                                 .Query(entity_set="Catalog_Контрагенты")))
            assert robot.handled == 0  # робот с чужим токеном заданий не получил
    finally:
        gw.stop()


def test_missing_object_surfaces_as_error(robot_stack):
    _, backend, _, _ = robot_stack
    with pytest.raises(RobotError) as e:
        backend.get("Catalog_Контрагенты", "00000000-0000-0000-0000-000000000000")
    assert "не найден" in str(e.value)


def test_failure_inside_1c_surfaces(robot_stack):
    """Ошибка выполнения на стороне 1С должна доходить до агента текстом."""
    _, backend, robot, _ = robot_stack
    original = robot._execute
    robot._execute = lambda task: {"id": task.get("id"), "ok": False,
                                   "error": "Ошибка запроса: поле не найдено"}
    try:
        from perimeter_bridge1c.backend import Query
        with pytest.raises(RobotError) as e:
            list(backend.run(Query(entity_set="Catalog_Контрагенты")))
        assert "поле не найдено" in str(e.value)
    finally:
        robot._execute = original
