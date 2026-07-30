"""Набор для замера должен ссылаться на существующие инструменты.

Иначе замер тихо деградирует: переименовали инструмент — и метрика падает
не потому, что модель хуже, а потому, что тест устарел.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "bench"))

from fakes.fake_1c_server import Fake1CServer
from perimeter_bridge1c.analytics import AnalyticsTools
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient
from perimeter_bridge1c.tools import Bridge1CTools
from tool_choice import CASES


def test_every_expected_tool_exists():
    with Fake1CServer() as srv:
        mapping = load_mapping("bp30")
        client = ODataClient(srv.base_url, "robot", "test", mapping=mapping)
        names = {s.name for s in Bridge1CTools(client, mapping).specs()
                 + AnalyticsTools(client, mapping).specs()}
    missing = {want for _, want, _ in CASES} - names
    assert not missing, f"в наборе инструменты, которых нет: {missing}"


def test_dataset_covers_every_tool():
    """Инструмент без вопроса в наборе — непроверенный инструмент."""
    with Fake1CServer() as srv:
        mapping = load_mapping("bp30")
        client = ODataClient(srv.base_url, "robot", "test", mapping=mapping)
        names = {s.name for s in Bridge1CTools(client, mapping).specs()
                 + AnalyticsTools(client, mapping).specs()}
    uncovered = names - {want for _, want, _ in CASES}
    assert not uncovered, f"нет вопросов для: {uncovered}"


def test_questions_are_unique():
    questions = [q for q, _, _ in CASES]
    assert len(questions) == len(set(questions))
