import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer
from fakes.fake_openai_server import FakeOpenAIServer, Scripted
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient
from perimeter_bridge1c.tools import Bridge1CTools
from perimeter_core.agent import Agent, salvage_tool_calls
from perimeter_core.audit import AuditLog
from perimeter_inference.client import InferenceClient


def make_agent(tmp_path, srv_1c, script, confirm=lambda name, args: True, **kw):
    mapping = load_mapping("bp30")
    tools = Bridge1CTools(
        ODataClient(srv_1c.base_url, "robot", "test", mapping=mapping), mapping)
    fake_llm = FakeOpenAIServer(script)
    fake_llm.__enter__()
    agent = Agent(
        client=InferenceClient(fake_llm.base_url, model="fake"),
        tool_specs=tools.specs(),
        audit=AuditLog(tmp_path / "audit.log"),
        confirm=confirm,
        **kw,
    )
    return agent, fake_llm


def test_tool_loop_and_final_answer(tmp_path):
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content='Контрагент ООО "Ромашка", ИНН 7701234567.'),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Найди контрагента Ромашка")
        assert "Ромашка" in result.text and result.steps == 2
        # Результат инструмента дошёл до модели вторым запросом:
        second_request = llm.requests[1]
        tool_msgs = [m for m in second_request["messages"] if m["role"] == "tool"]
        assert tool_msgs and GUID_ROMASHKA in tool_msgs[0]["content"]
        llm.__exit__()


def test_approval_denied_blocks_write(tmp_path):
    script = [
        Scripted(tool_calls=[{"name": "create_draft_document",
                              "arguments": {"doc_type": "customer_invoice",
                                            "counterparty_key": GUID_ROMASHKA}}]),
        Scripted(content="Действие отклонено, счёт не создан."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script, confirm=lambda n, a: False)
        agent.run("Создай счёт Ромашке")
        assert srv.created == []  # ничего не создано
        tool_msg = next(m for m in agent.messages if m["role"] == "tool")
        assert "отклонено" in tool_msg["content"].lower()
        llm.__exit__()


def test_approval_granted_creates_draft_only(tmp_path):
    script = [
        Scripted(tool_calls=[{"name": "create_draft_document",
                              "arguments": {"doc_type": "customer_invoice",
                                            "counterparty_key": GUID_ROMASHKA,
                                            "total": 500.0}}]),
        Scripted(content="Черновик счёта создан."),
    ]
    with Fake1CServer() as srv:
        asked = []
        agent, llm = make_agent(tmp_path, srv, script,
                                confirm=lambda n, a: asked.append(n) or True)
        agent.run("Создай счёт Ромашке на 500")
        assert asked == ["create_draft_document"]
        assert len(srv.created) == 1 and srv.created[0][1]["Posted"] is False
        llm.__exit__()


def test_salvage_text_tool_call(tmp_path):
    text = ("Сейчас найду. <tool_call>get_counterparty"
            "<arg_key>query</arg_key><arg_value>ромашка</arg_value></tool_call>")
    script = [Scripted(content=text), Scripted(content="Готово: ООО «Ромашка».")]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Найди Ромашку")
        assert result.steps == 2  # текстовый вызов распознан и исполнен
        assert any(m["role"] == "tool" for m in agent.messages)
        llm.__exit__()


def test_salvage_parser_unit():
    calls = salvage_tool_calls(
        "<tool_call>find_document<arg_key>doc_type</arg_key><arg_value>sale</arg_value></tool_call>",
        {"find_document"})
    assert calls[0]["function"]["name"] == "find_document"
    assert json.loads(calls[0]["function"]["arguments"]) == {"doc_type": "sale"}
    assert salvage_tool_calls("обычный текст", {"find_document"}) == []


def test_turn_limit(tmp_path):
    script = [Scripted(tool_calls=[{"name": "get_counterparty",
                                    "arguments": {"query": "х"}}])] * 3
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script, max_iterations=3)
        result = agent.run("зациклись")
        assert result.stopped_by_limit
        llm.__exit__()


def test_compaction_budget(tmp_path):
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, [], context_budget_chars=1500, keep_recent=4)
        # Наполняем историю искусственно длинными ходами:
        for i in range(20):
            agent.messages.append({"role": "user", "content": f"вопрос {i} " + "х" * 300})
            agent.messages.append({"role": "assistant", "content": f"ответ {i} " + "у" * 300})
        outbound = agent._outbound_messages()
        chars = sum(len(str(m.get("content"))) for m in outbound)
        assert chars < 3000  # бюджет соблюдён
        assert outbound[0]["role"] == "system"
        assert "история сокращена" in outbound[1]["content"]
        # Последние сообщения не тронуты:
        assert outbound[-1]["content"].startswith("ответ 19")
        llm.__exit__()


def test_old_tool_output_truncated(tmp_path):
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, [], keep_recent=2)
        agent.messages.append({"role": "tool", "tool_call_id": "1", "name": "x",
                               "content": "строка1\n" + "длинная " * 100})
        agent.messages.extend({"role": "user", "content": f"{i}"} for i in range(3))
        outbound = agent._outbound_messages()
        tool_msg = next(m for m in outbound if m["role"] == "tool")
        assert "…[сокращено]" in tool_msg["content"] and len(tool_msg["content"]) < 200
        llm.__exit__()


def test_audit_append_only(tmp_path):
    path = tmp_path / "audit.log"
    with AuditLog(path) as log:
        log.write("tool_call", tool="find_document")
    with AuditLog(path) as log:
        log.write("assistant_message", text="ок")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # второй запуск не затёр первый
    events = [json.loads(line)["event"] for line in lines]
    assert events == ["tool_call", "assistant_message"]
    assert all("ts" in json.loads(line) for line in lines)


def test_agent_uses_low_temperature(tmp_path):
    """Выбор инструмента должен быть воспроизводимым, а не творческим."""
    script = [Scripted(content="готово")]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        agent.run("привет")
        assert llm.requests[0]["temperature"] <= 0.2, (
            f"агент вызвал модель с температурой {llm.requests[0]['temperature']} — "
            "выбор инструмента станет случайным")
        llm.__exit__()


def test_warmup_sends_full_prompt_and_tools(tmp_path):
    """Прогрев должен слать тот же префикс, что и рабочие запросы."""
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, [Scripted(content="ок")])
        elapsed = agent.warmup()
        assert elapsed >= 0
        req = llm.requests[0]
        assert req["messages"][0]["role"] == "system"
        assert req["messages"][0]["content"] == agent.system_prompt
        assert len(req["tools"]) == len(agent.tool_specs)
        assert agent.messages == []  # история не засоряется
        llm.__exit__()
