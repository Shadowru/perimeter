"""Этап 6 — приемочные демо-сценарии (e2e против мок-1С).

Два режима:
1. CI (по умолчанию): «модель» — детерминированное скриптованное
   воспроизведение реалистичных ходов GLM-5.2 (FakeOpenAIServer).
   Проверяется весь продукт, кроме качества самой модели: цикл агента,
   инструменты, OData-клиент, мок-1С, guardrails, аудит.
   (Крошечная модель со случайными весами инструкциям следовать не может,
   а полная GLM-5.2 не помещается в CI — честное решение: playback.)
2. Живая модель (ручной прогон, в т.ч. полная GLM-5.2): задать
   PERIMETER_E2E_LLM_URL=http://127.0.0.1:8090 — те же сценарии идут в
   реальный сервер; проверяются эффекты инструментов и ключевые факты
   в ответе, а не дословный текст.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes.fake_1c_server import GUID_ROMASHKA, Fake1CServer
from fakes.fake_openai_server import FakeOpenAIServer, Scripted
from perimeter_bridge1c.mapping import load_mapping
from perimeter_bridge1c.odata import ODataClient
from perimeter_bridge1c.tools import Bridge1CTools
from perimeter_core.agent import Agent
from perimeter_core.audit import AuditLog
from perimeter_inference.client import InferenceClient

LIVE_LLM_URL = os.environ.get("PERIMETER_E2E_LLM_URL")


def build_agent(tmp_path, srv_1c, llm_url, script=None, confirm=lambda n, a: True):
    mapping = load_mapping("bp30")
    tools = Bridge1CTools(
        ODataClient(srv_1c.base_url, "robot", "test", mapping=mapping), mapping)
    return Agent(
        client=InferenceClient(llm_url, model="fake" if script is not None else "glm-5.2"),
        tool_specs=tools.specs(),
        audit=AuditLog(tmp_path / "audit.log"),
        confirm=confirm,
    )


# --- Сценарий 1: непроведённые реализации за июль по контрагенту ----------

SCRIPT_1 = [
    Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
    Scripted(tool_calls=[{"name": "find_document", "arguments": {
        "doc_type": "sale", "counterparty_key": GUID_ROMASHKA,
        "date_from": "2026-07-01T00:00:00", "date_to": "2026-07-31T23:59:59",
        "posted": False}}]),
    Scripted(content="Непроведённые реализации ООО «Ромашка» за июль 2026: "
                     "№РТ-0002 от 10.07.2026 на 45 000.50 руб.; "
                     "№РТ-0003 от 18.07.2026 на 78 000.00 руб."),
]


def test_scenario_1_unposted_sales_july(tmp_path):
    with Fake1CServer() as srv, FakeOpenAIServer(SCRIPT_1) as llm:
        agent = build_agent(tmp_path, srv, llm.base_url, SCRIPT_1)
        result = agent.run("Найди все непроведённые реализации за июль по контрагенту Ромашка")
        # Приёмка: оба непроведённых документа, ссылки «№ … от …», ничего лишнего.
        assert "РТ-0002" in result.text and "РТ-0003" in result.text
        assert "РТ-0001" not in result.text and "РТ-0005" not in result.text
        assert "№РТ-0002 от" in result.text
        # Инструмент реально получил из мок-1С ровно два документа:
        tool_out = next(m["content"] for m in agent.messages
                        if m["role"] == "tool" and m.get("name") == "find_document")
        assert tool_out.count("НЕ проведён") == 2


# --- Сценарий 2: сверка отгрузок и оплат ----------------------------------

SCRIPT_2 = [
    Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
    Scripted(tool_calls=[{"name": "ledger_report",
                          "arguments": {"counterparty_key": GUID_ROMASHKA}}]),
    Scripted(content="По ООО «Ромашка»: отгружено 219 000.00 руб. "
                     "(№РТ-0001 от 03.07.2026, №РТ-0005 от 25.06.2026), "
                     "оплачено 120 000.00 руб. (№ПС-0001 от 07.07.2026). "
                     "Не оплачено: 99 000.00 руб. — реализация №РТ-0005 от 25.06.2026."),
]


def test_scenario_2_reconciliation(tmp_path):
    with Fake1CServer() as srv, FakeOpenAIServer(SCRIPT_2) as llm:
        agent = build_agent(tmp_path, srv, llm.base_url, SCRIPT_2)
        result = agent.run('Сверка: что мы отгрузили ООО "Ромашка", но не получили оплату?')
        assert "99 000.00" in result.text
        tool_out = next(m["content"] for m in agent.messages
                        if m["role"] == "tool" and m.get("name") == "ledger_report")
        assert "сальдо (не оплачено) 99 000.00" in tool_out
        # Аудит содержит весь путь:
        events = [json.loads(line)["event"]
                  for line in (tmp_path / "audit.log").read_text().splitlines()]
        assert events[0] == "user_message" and "tool_call" in events


# --- Сценарий 3: черновик счёта на основании прошлого ---------------------

def script_3(invoice_key):
    return [
        Scripted(tool_calls=[{"name": "get_counterparty", "arguments": {"query": "ромашка"}}]),
        Scripted(tool_calls=[{"name": "find_document", "arguments": {
            "doc_type": "customer_invoice", "counterparty_key": GUID_ROMASHKA}}]),
        Scripted(tool_calls=[{"name": "create_draft_document", "arguments": {
            "doc_type": "customer_invoice", "counterparty_key": GUID_ROMASHKA,
            "based_on_key": invoice_key}}]),
        Scripted(content="Готово: создан черновик счёта на основании №СЧ-0101 от 20.06.2026 "
                         "на 99 000.00 руб. Документ не проведён — проверьте и проведите в 1С."),
    ]


def test_scenario_3_draft_invoice(tmp_path):
    with Fake1CServer() as srv:
        invoice_key = srv.dataset["Document_СчетНаОплатуПокупателю"][0]["Ref_Key"]
        script = script_3(invoice_key)
        confirmations = []
        with FakeOpenAIServer(script) as llm:
            agent = build_agent(tmp_path, srv, llm.base_url, script,
                                confirm=lambda n, a: confirmations.append((n, a)) or True)
            result = agent.run("Подготовь черновик счёта Ромашке на основании прошлого счёта")
            # Приёмка: подтверждение спрошено, создан ровно один ЧЕРНОВИК с
            # суммой из основания, проведение не выполнено.
            assert [c[0] for c in confirmations] == ["create_draft_document"]
            assert len(srv.created) == 1
            entity_set, row = srv.created[0]
            assert entity_set == "Document_СчетНаОплатуПокупателю"
            assert row["Posted"] is False
            assert row["СуммаДокумента"] == 99000.00
            assert "черновик" in result.text.lower()
            events = [json.loads(line)["event"]
                      for line in (tmp_path / "audit.log").read_text().splitlines()]
            assert "confirm_granted" in events


# --- Сценарий 3а: отказ человека останавливает запись ---------------------

def test_scenario_3_denied(tmp_path):
    with Fake1CServer() as srv:
        invoice_key = srv.dataset["Document_СчетНаОплатуПокупателю"][0]["Ref_Key"]
        script = script_3(invoice_key)[:3] + [
            Scripted(content="Создание отклонено пользователем; счёт не создан.")]
        with FakeOpenAIServer(script) as llm:
            agent = build_agent(tmp_path, srv, llm.base_url, script,
                                confirm=lambda n, a: False)
            agent.run("Подготовь черновик счёта Ромашке")
            assert srv.created == []
            events = [json.loads(line)["event"]
                      for line in (tmp_path / "audit.log").read_text().splitlines()]
            assert "confirm_denied" in events


# --- Живая модель (ручной прогон, полная GLM-5.2 или llama.cpp) -----------

@pytest.mark.skipif(not LIVE_LLM_URL, reason="задайте PERIMETER_E2E_LLM_URL для живого прогона")
def test_live_minimal_tool_loop(tmp_path):
    """Урезанный цикл: один инструмент, короткий промпт.

    Полный сценарий (4 схемы инструментов, ~760 токенов промпта) на машине
    с 32 ГБ RAM не проходит: prefill не укладывается в час, см.
    docs/hardware.md. Этот тест доказывает, что связка «живая модель →
    вызов инструмента → данные 1С → ответ» работает, при промпте, который
    такому железу по силам. На рекомендуемых 128 ГБ должен проходить
    полный сценарий ниже.
    """
    with Fake1CServer() as srv:
        mapping = load_mapping("bp30")
        tools = Bridge1CTools(
            ODataClient(srv.base_url, "robot", "test", mapping=mapping), mapping)
        only_counterparty = [s for s in tools.specs() if s.name == "get_counterparty"]
        agent = Agent(
            client=InferenceClient(LIVE_LLM_URL, model="glm-5.2", timeout_s=7200),
            tool_specs=only_counterparty,
            audit=AuditLog(tmp_path / "audit.log"),
            confirm=lambda n, a: False,
            max_iterations=3,
            max_tokens_per_call=96,
        )
        agent.system_prompt = (
            "Ты — ассистент по 1С. Используй инструмент get_counterparty, "
            "чтобы найти контрагента. Отвечай кратко по-русски."
        )
        result = agent.run("Найди контрагента Ромашка")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        assert tool_msgs, f"модель не вызвала инструмент; ответ: {result.text[:200]}"
        assert "Ромашка" in tool_msgs[0]["content"]


@pytest.mark.skipif(not LIVE_LLM_URL, reason="задайте PERIMETER_E2E_LLM_URL для живого прогона")
def test_live_model_scenario_1(tmp_path):
    with Fake1CServer() as srv:
        agent = build_agent(tmp_path, srv, LIVE_LLM_URL)
        result = agent.run("Найди все непроведённые реализации за июль 2026 по контрагенту Ромашка")
        # Живая модель: проверяем факты, не формулировки.
        assert "РТ-0002" in result.text and "РТ-0003" in result.text
