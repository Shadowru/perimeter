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
        # Системный промпт уходит модели на каждом ходе. 3200 символов —
        # это ~900 токенов prefill, около секунды на стенде 16 ГБ. Выросло
        # осознанно: готовые границы периодов (≈600 символов) убирают у
        # модели арифметику дат, на которой она ошибалась целыми кварталами.
        assert len(outbound[0]["content"]) < 3200
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
    ]   # второго ответа не нужно: правим сразу, без переписывания
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Найди ТехноСервис")
        body, _, note = result.text.partition("(Названия исправлены")
        assert "ТехнSERVIC" not in body      # в самом ответе искажения нет
        assert "ТехноСервис" in body
        assert "«ТехнSERVIC» -> «АО \"ТехноСервис\"»" in note  # правка названа
        assert result.steps == 2   # лишнего хода модели не было
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


# --- отчёты идут человеку напрямую, минуя пересказ ------------------------

def make_full_agent(tmp_path, srv_1c, script, **kw):
    from perimeter_bridge1c.analytics import AnalyticsTools
    mapping = load_mapping("bp30")
    client = ODataClient(srv_1c.base_url, "robot", "test", mapping=mapping)
    fake_llm = FakeOpenAIServer(script)
    fake_llm.__enter__()
    agent = Agent(
        client=InferenceClient(fake_llm.base_url, model="fake"),
        tool_specs=Bridge1CTools(client, mapping).specs()
                   + AnalyticsTools(client, mapping).specs(),
        audit=AuditLog(tmp_path / "audit.log"), confirm=lambda n, a: True, **kw)
    return agent, fake_llm


def test_report_reaches_the_user_whole(tmp_path):
    """Таблица приходит человеку целиком, а модель её не пересказывает."""
    script = [
        Scripted(tool_calls=[{"name": "receivables_aging", "arguments": {}}]),
        Scripted(content="Нам должны 192 000.00 руб., подробности в таблице."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_full_agent(tmp_path, srv, script)
        result = agent.run("Кто нам должен?")
        assert len(result.reports) == 1
        report = result.reports[0]
        assert "Ромашка" in report.display and "120 000.00" in report.display
        # Модели ушла выжимка, а не весь отчёт: документы-основания скрыты.
        tool_msg = next(m for m in agent.messages if m["role"] == "tool")
        assert "РТ-0005" not in tool_msg["content"]
        assert len(tool_msg["content"]) < len(report.display)
        assert result.grounded
        llm.__exit__()


def test_model_inventing_beyond_the_digest_is_still_caught(tmp_path):
    """Выдумка сверх выжимки ловится."""
    script = [
        Scripted(tool_calls=[{"name": "receivables_aging", "arguments": {}}]),
        Scripted(content="Больше всех должно «ООО Вектор» — 999 000.00 руб."),
        Scripted(content="Больше всех должно «ООО Вектор» — 999 000.00 руб."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_full_agent(tmp_path, srv, script)
        result = agent.run("Кто нам должен?")
        assert not result.grounded
        assert "999 000.00" in result.text and "не подтверждается" in result.text
        llm.__exit__()


def test_hidden_row_guess_is_not_accepted(tmp_path):
    """Про скрытую строку модель может только гадать — и это ловится.

    Документы-основания в выжимку не попадают, поэтому названная по ним
    сумма взята ниоткуда: сегодня совпадёт, завтра нет, а выглядит одинаково.
    """
    script = [
        Scripted(tool_calls=[{"name": "receivables_aging", "arguments": {}}]),
        Scripted(content="Не оплачен счёт «Ромашка» на 39 000.00 руб. по №РТ-0006."),
        Scripted(content="Всего нам должны 186 000.00 руб., детали в таблице."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_full_agent(tmp_path, srv, script)
        result = agent.run("Кто нам должен?")
        assert "РТ-0006" not in result.text        # догадку переписали
        assert result.grounded and result.steps == 3
        llm.__exit__()


def test_top_row_may_be_named(tmp_path):
    """Верхнюю строку модель видит — называть её можно без предупреждений."""
    script = [
        Scripted(tool_calls=[{"name": "receivables_aging", "arguments": {}}]),
        Scripted(content="Больше всех должна «Ромашка» — 132 000.00 руб."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_full_agent(tmp_path, srv, script)
        result = agent.run("Кто нам должен?")
        assert result.grounded and result.steps == 2
        llm.__exit__()


def test_total_from_the_digest_passes(tmp_path):
    """Итог модель видит — называть его можно и нужно."""
    script = [
        Scripted(tool_calls=[{"name": "receivables_aging", "arguments": {}}]),
        Scripted(content="Нам должны 186 000.00 руб., подробности в таблице."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_full_agent(tmp_path, srv, script)
        result = agent.run("Кто нам должен?")
        assert result.grounded and result.steps == 2
        llm.__exit__()


def test_tool_call_json_never_reaches_the_user():
    """Модель без инструментов пишет вызов текстом — в ответ он попасть не должен.

    Замер 30.07: на шаге переписывания в ответе оказался
    {"name": "abc_analysis", "arguments": {...}} — человек видел служебный JSON.
    """
    from perimeter_core.agent import strip_tool_call_text
    text = ('Крупнейшие клиенты:\n'
            '{"name": "abc_analysis", "arguments": {"dimension": "counterparty"}}\n'
            'подробности в таблице.')
    cleaned = strip_tool_call_text(text)
    assert "abc_analysis" not in cleaned and "arguments" not in cleaned
    assert "Крупнейшие клиенты:" in cleaned and "подробности в таблице." in cleaned


def test_ordinary_braces_survive_cleanup():
    """Обычный текст с фигурными скобками ломать нельзя."""
    from perimeter_core.agent import strip_tool_call_text
    text = 'Долг 100 000 руб. {примечание: без НДС}'
    assert strip_tool_call_text(text) == text


def test_prompt_demands_dates_when_a_period_is_named():
    """Все три модели теряли период: «за июль» уходило без date_from/date_to.

    Отчёт при этом строился за всё время и молча отвечал не на тот вопрос.
    """
    from perimeter_core.agent import load_system_prompt
    prompt = load_system_prompt()
    assert "ОБЯЗАТЕЛЬНО передай date_from и date_to" in prompt
    assert "не придумывай" in prompt      # обратное правило осталось


def test_special_token_never_reaches_the_user():
    """Живой прогон 31.07: в ответе оказался один «<|function_call|>»."""
    from perimeter_core.agent import strip_tool_call_text
    assert strip_tool_call_text("<|function_call|>") == ""
    assert strip_tool_call_text("Долг 100.00 <|im_end|>") == "Долг 100.00"
    # Обычный текст с угловыми скобками ломать нельзя
    assert strip_tool_call_text("Сумма < 100 и > 10") == "Сумма < 100 и > 10"


def test_empty_answer_is_retried_not_shown(tmp_path):
    """Пустой ответ — это не ответ: просим модель ещё раз."""
    script = [
        Scripted(content="<|function_call|>"),
        Scripted(content="Готово: контрагентов в базе три."),
    ]
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script)
        result = agent.run("Сколько контрагентов?")
        assert result.text == "Готово: контрагентов в базе три."
        assert "function_call" not in result.text
        events = [json.loads(line)["event"]
                  for line in (tmp_path / "audit.log").read_text().splitlines()]
        assert "empty_answer_retry" in events
        llm.__exit__()


def test_persistent_empty_answer_does_not_loop_forever(tmp_path):
    script = [Scripted(content="<|function_call|>")] * 8
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, script, max_iterations=6)
        result = agent.run("Сколько контрагентов?")
        assert "function_call" not in result.text
        assert "не смогла сформулировать ответ" in result.text
        assert result.model_failed
        llm.__exit__()


# --- границы периодов считаем мы, а не модель ------------------------------
# Живой прогон 31.07: на «сколько заработали за год» модель передала в отчёт
# 2025-01-01…2026-07-31 — девятнадцать месяцев. Цифра верная для диапазона и
# бесполезная для вопроса.

def test_period_hints_cover_the_usual_periods():
    from perimeter_core.agent import period_hints
    t = period_hints("2026-07-31")
    assert "этот год: 2026-01-01T00:00:00 … 2026-12-31T23:59:59" in t
    assert "прошлый год: 2025-01-01T00:00:00 … 2025-12-31T23:59:59" in t
    assert "этот месяц: 2026-07-01T00:00:00 … 2026-07-31T23:59:59" in t
    assert "прошлый месяц: 2026-06-01T00:00:00 … 2026-06-30T23:59:59" in t
    assert "этот квартал: 2026-07-01T00:00:00" in t
    assert "«За год» без уточнения — это ЭТОТ год" in t


def test_rolling_year_is_twelve_months_not_thirteen():
    from perimeter_core.agent import period_hints
    assert "последние 12 месяцев: 2025-08-01T00:00:00 … 2026-07-31" in period_hints("2026-07-31")
    # Январь: год назад — февраль предыдущего года, а не январь.
    assert "последние 12 месяцев: 2025-02-01T00:00:00" in period_hints("2026-01-15")


def test_period_hints_survive_leap_day():
    from perimeter_core.agent import period_hints
    t = period_hints("2024-02-29")
    assert "прошлый месяц: 2024-01-01T00:00:00 … 2024-01-31" in t
    assert "последние 12 месяцев: 2023-03-01T00:00:00 … 2024-02-29" in t


def test_prompt_carries_the_boundaries(tmp_path):
    with Fake1CServer() as srv:
        agent, llm = make_agent(tmp_path, srv, [], today="2026-07-31")
        assert "этот год: 2026-01-01" in agent.system_prompt
        assert "сам не вычисляй" in agent.system_prompt
        llm.__exit__()


def test_fallback_shows_the_first_tool_result_not_the_last(tmp_path):
    """Первый инструмент — выбор модели по вопросу, последний — перебор.

    Живой прогон 31.07: на «сколько заработали за год» модель вызвала пять
    отчётов и не ответила, а человек увидел движение денежных средств —
    последний из перебранных, а не прибыль, о которой спрашивал.
    """
    script = ([Scripted(tool_calls=[{"name": "receivables_aging", "arguments": {}}]),
               Scripted(tool_calls=[{"name": "cash_flow", "arguments": {}}])]
              + [Scripted(content="<|function_call|>")] * 5)
    with Fake1CServer() as srv:
        agent, llm = make_full_agent(tmp_path, srv, script, max_tool_calls_per_turn=2)
        result = agent.run("Кто нам должен?")
        assert result.model_failed
        assert "Дебиторская задолженность" in result.text     # первый вызов
        assert "Движение денежных средств" not in result.text  # не последний
        llm.__exit__()
