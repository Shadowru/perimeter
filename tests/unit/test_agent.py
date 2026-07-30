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
        # Бюджет ограничивает историю; системный промпт в него не входит —
        # он не сокращается, поэтому проверяется отдельно ниже.
        history_chars = sum(len(str(m.get("content"))) for m in outbound[1:])
        assert history_chars < 1600
        assert outbound[0]["role"] == "system"
        # Системный промпт уходит модели на каждом ходе: 2500 символов —
        # это ~700 токенов prefill, около секунды на стенде 16 ГБ.
        assert len(outbound[0]["content"]) < 2500
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


def test_streaming_tool_call_deltas_are_merged():
    """Потоковый вызов приходит кусками: имя, затем аргументы по частям."""
    from perimeter_core.agent import merge_tool_call_deltas
    acc = {}
    merge_tool_call_deltas(acc, [{"index": 0, "id": "c1", "type": "function",
                                  "function": {"name": "find_document", "arguments": ""}}])
    merge_tool_call_deltas(acc, [{"index": 0, "function": {"arguments": '{"doc_'}}])
    merge_tool_call_deltas(acc, [{"index": 0, "function": {"arguments": 'type":"sale"}'}}])
    assert len(acc) == 1, "фрагменты склеены в один вызов, а не размножены"
    call = acc[0]
    assert call["type"] == "function"          # без type сервер отвергает историю
    assert call["function"]["name"] == "find_document"
    assert json.loads(call["function"]["arguments"]) == {"doc_type": "sale"}


def test_streaming_path_produces_valid_tool_calls(tmp_path):
    """Сквозная проверка потокового пути — им пользуется веб-интерфейс."""
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content="Найден: ООО «Ромашка»."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        agent.run("найди ромашку", on_delta=lambda d: None)   # потоковый режим
        sent_back = [m for m in llm.requests[1]["messages"] if m.get("tool_calls")]
        assert sent_back, "история без вызовов инструментов"
        for call in sent_back[0]["tool_calls"]:
            assert call.get("type") == "function" and call.get("id")
            json.loads(call["function"]["arguments"])   # аргументы — валидный JSON
        llm.__exit__()


def test_system_prompt_states_today(tmp_path):
    """Без даты «за эту неделю» модель считает наугад (живое демо)."""
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, [], today="2026-07-30")
        assert "Сегодня 2026-07-30" in agent.system_prompt
        assert "эта неделя" in agent.system_prompt
        llm.__exit__()


def test_system_prompt_forbids_showing_keys(tmp_path):
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, [])
        assert "НИКОГДА не показывай пользователю" in agent.system_prompt
        llm.__exit__()


def test_invented_data_triggers_one_retry(tmp_path):
    """Ответ с выдуманным контрагентом отправляется на переписывание."""
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content='Клиенты: «Ромашка, ООО» и «ООО Вектор».'),
        Scripted(content='Найден один контрагент: «Ромашка, ООО».'),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Кто наши клиенты?")
        assert "Вектор" not in result.text
        assert result.grounded and result.steps == 3
        # Модели ушло указание переписать ответ с перечнем проблем:
        last = llm.requests[-1]["messages"][-1]
        assert "ООО Вектор" in last["content"]
        llm.__exit__()


def test_persistent_invention_is_flagged_to_user(tmp_path):
    """Если и после переписывания данные не подтверждаются — предупреждаем."""
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content='Долг «ООО Вектор» — 555 000.00 руб.'),
        Scripted(content='Долг «ООО Вектор» — 555 000.00 руб.'),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Сколько должен Вектор?")
        assert not result.grounded
        assert "не подтверждается" in result.text
        assert "ООО Вектор" in result.text and "555 000.00" in result.text
        llm.__exit__()


def test_grounded_answer_is_not_retried(tmp_path):
    """Корректный ответ уходит человеку сразу: лишний ход стоит секунд 20."""
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content='Контрагент «Ромашка, ООО», ИНН 7701234567.'),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Найди Ромашку")
        assert result.steps == 2 and result.grounded
        llm.__exit__()


def test_tool_budget_forces_an_answer(tmp_path):
    """Модель, ушедшая вызывать инструменты подряд, обязана всё же ответить.

    Живой прогон 2026-07-30: нужный отчёт пришёл первым ходом, после чего
    модель вызвала ещё одиннадцать инструментов и упёрлась в лимит шагов —
    330 секунд без ответа. Исчерпав бюджет, мы убираем инструменты из
    запроса, и модели остаётся только ответить по собранным данным.
    """
    script = ([Scripted(tool_calls=[{"name": "get_counterparty",
                                     "arguments": {"query": "ромашка"}}])] * 9
              + [Scripted(content="Контрагент «Ромашка, ООО», ИНН 7701234567.")])
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script, max_tool_calls_per_turn=3)
        result = agent.run("Кто такая Ромашка?")
        assert not result.stopped_by_limit
        assert "Ромашка" in result.text
        # Инструменты выполнены ровно по бюджету:
        assert sum(1 for m in agent.messages if m["role"] == "tool") == 3
        # На последнем запросе инструментов модели не предлагали:
        assert llm.requests[-1].get("tools") in (None, [])
        events = [json.loads(line)["event"]
                  for line in (tmp_path / "audit.log").read_text().splitlines()]
        assert "tool_budget_reached" in events
        llm.__exit__()


def test_tool_budget_does_not_disturb_normal_turns(tmp_path):
    """Обычный сценарий в бюджет укладывается — инструменты не отбираются."""
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content="Контрагент «Ромашка, ООО», ИНН 7701234567."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        agent.run("Найди Ромашку")
        assert all(r.get("tools") for r in llm.requests)
        llm.__exit__()


def test_distorted_name_is_repaired_from_data(tmp_path):
    """Живой прогон: модель дважды написала «ТехнSERVIC» вместо «ТехноСервис».

    Просить её переписать бесполезно — правим сами и говорим об этом вслух.
    """
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "техно"}}]),
        Scripted(content="Контрагент «ТехнSERVIC», ИНН 5047112233."),
        Scripted(content="Контрагент «ТехнSERVIC», ИНН 5047112233."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Найди ТехноСервис")
        body, _, note = result.text.partition("(Названия исправлены")
        assert "ТехнSERVIC" not in body      # в самом ответе искажения нет
        assert "ТехноСервис" in body
        assert "«ТехнSERVIC» -> «АО \"ТехноСервис\"»" in note  # правка названа
        assert result.grounded          # после правки расхождений не осталось
        assert "не подтверждается" not in result.text
        events = [json.loads(line)["event"]
                  for line in (tmp_path / "audit.log").read_text().splitlines()]
        assert "names_corrected" in events
        llm.__exit__()


def test_invented_amount_is_never_silently_replaced(tmp_path):
    """Число — не опечатка: подставлять своё нельзя, только предупредить."""
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content="Долг «Ромашка» — 777 000.00 руб."),
        Scripted(content="Долг «Ромашка» — 777 000.00 руб."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Сколько должна Ромашка?")
        assert "777 000.00" in result.text        # цифру не подменили
        assert "не подтверждается" in result.text  # но предупредили
        assert not result.grounded
        llm.__exit__()


def test_rewrite_step_does_not_offer_tools(tmp_path):
    """Переписывание — это правка текста, а не новый сбор данных.

    Живой прогон 2026-07-30: с инструментами на этом шаге модель вызывала
    тот же отчёт ещё четыре раза и упиралась в бюджет вместо исправления.
    """
    script = [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(content="Клиенты: «Ромашка, ООО» и «ООО Вектор»."),
        Scripted(content="Контрагент один: «Ромашка, ООО»."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Кто наши клиенты?")
        assert result.grounded and "Вектор" not in result.text
        assert llm.requests[-1].get("tools") in (None, [])   # правка шла без инструментов
        assert sum(1 for m in agent.messages if m["role"] == "tool") == 1
        llm.__exit__()


def test_backend_failure_does_not_lose_the_data(tmp_path):
    """Сбой модели после успешного обращения к 1С не должен терять отчёт.

    Живой прогон 2026-07-30: llama.cpp вернул 500 на исковерканном моделью
    названии, и корректно полученная кредиторка пропала вместе с ответом.
    """
    script = [Scripted(tool_calls=[{"name": "get_counterparty",
                                    "arguments": {"query": "ромашка"}}])]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)   # скрипт кончится -> 500
        result = agent.run("Найди Ромашку")
        assert result.model_failed
        assert GUID_ROMASHKA in result.text or "Ромашка" in result.text
        assert "не смогла сформулировать" in result.text
        events = [json.loads(line)["event"]
                  for line in (tmp_path / "audit.log").read_text().splitlines()]
        assert "model_error" in events
        llm.__exit__()


def test_backend_failure_without_data_still_raises(tmp_path):
    """Если данных нет вовсе, молчать об ошибке нельзя."""
    import pytest
    from perimeter_inference.client import InferenceError
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, [])
        with pytest.raises(InferenceError):
            agent.run("Привет")
        llm.__exit__()
