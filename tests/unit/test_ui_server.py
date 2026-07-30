import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer
from fakes.fake_openai_server import FakeOpenAIServer, Scripted
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient
from perimeter_bridge1c.tools import Bridge1CTools
from perimeter_core.agent import Agent
from perimeter_core.audit import AuditLog
from perimeter_inference.client import InferenceClient
from perimeter_ui.server import UIServer


def make_factory(tmp_path, srv_1c, llm):
    mapping = load_mapping("bp30")
    tools = Bridge1CTools(
        ODataClient(srv_1c.base_url, "robot", "test", mapping=mapping), mapping)

    def factory(confirm):
        return Agent(
            client=InferenceClient(llm.base_url, model="fake"),
            tool_specs=tools.specs(),
            audit=AuditLog(tmp_path / "audit.log"),
            confirm=confirm,
        )
    return factory


def sse_events(resp):
    events = []
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))
    return events


def test_page_served_localized(tmp_path):
    with Fake1CServer() as srv, FakeOpenAIServer([]) as llm:
        with UIServer("127.0.0.1", 0, make_factory(tmp_path, srv, llm)) as ui:
            html = urllib.request.urlopen(ui.base_url + "/").read().decode("utf-8")
            assert "Периметр" in html
            assert "{{" not in html  # все плейсхолдеры подставлены
            assert "http" not in html.lower().replace("http://127.0.0.1", "")  # без внешних ассетов


def test_chat_sse_flow(tmp_path):
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content="Найден: ООО «Ромашка»."),
    ]
    with Fake1CServer() as srv, FakeOpenAIServer(script) as llm:
        with UIServer("127.0.0.1", 0, make_factory(tmp_path, srv, llm)) as ui:
            req = urllib.request.Request(
                ui.base_url + "/api/chat",
                data=json.dumps({"message": "найди ромашку"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            events = sse_events(urllib.request.urlopen(req))
            done = [e for e in events if "done" in e]
            assert done and "Ромашка" in done[0]["done"]


def test_write_denied_without_checkbox(tmp_path):
    script = [
        Scripted(tool_calls=[{"name": "create_draft_document",
                              "arguments": {"doc_type": "customer_invoice",
                                            "counterparty_key": GUID_ROMASHKA}}]),
        Scripted(content="Нужно разрешение на запись."),
        Scripted(tool_calls=[{"name": "create_draft_document",
                              "arguments": {"doc_type": "customer_invoice",
                                            "counterparty_key": GUID_ROMASHKA}}]),
        Scripted(content="Черновик создан."),
    ]
    with Fake1CServer() as srv, FakeOpenAIServer(script) as llm:
        with UIServer("127.0.0.1", 0, make_factory(tmp_path, srv, llm)) as ui:
            def post(approve):
                req = urllib.request.Request(
                    ui.base_url + "/api/chat",
                    data=json.dumps({"message": "создай счёт",
                                     "approve_writes": approve}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                return sse_events(urllib.request.urlopen(req))

            post(False)
            assert srv.created == []      # без галочки запись отклонена
            post(True)
            assert len(srv.created) == 1  # с галочкой — черновик создан
            assert srv.created[0][1]["Posted"] is False


def test_reports_are_sent_to_the_browser_whole():
    """Таблица уходит в UI отдельным полем, а не через пересказ модели."""
    from perimeter_core.toolspec import ToolOutput

    class FakeAgent:
        def run(self, message, on_delta=None):
            from perimeter_core.agent import AgentResult
            if on_delta:
                on_delta("Нам должны 192 000.00 руб.")
            return AgentResult(
                text="Нам должны 192 000.00 руб.", steps=2,
                reports=[ToolOutput(display="контрагент | долг\nРомашка | 132 000.00",
                                    digest="итог", title="Дебиторка")])

    with UIServer("127.0.0.1", 0, lambda confirm: FakeAgent()) as ui:
        req = urllib.request.Request(
            ui.base_url + "/api/chat",
            data=json.dumps({"message": "кто должен?"}).encode(),
            headers={"Content-Type": "application/json"})
        done = next(e for e in sse_events(urllib.request.urlopen(req)) if "done" in e)
        assert done["reports"][0]["title"] == "Дебиторка"
        assert "Ромашка | 132 000.00" in done["reports"][0]["text"]
        assert done["grounded"] is True
