"""Агентный цикл «Периметра» — компактная адаптация архитектуры openworker.

Перенесённые паттерны (см. docs/research.md §2): канонический OpenAI-формат
истории; единственный шов выдачи истории модели (_outbound_messages) с
компактизацией; батч tool-вызовов с записью результатов в историю; salvage
tool-вызовов, пришедших текстом; лимит итераций.

Отличия под локальную модель (colibri, CTX 4096, медленный prefill):
- детерминированная компактизация истории (без вызова модели: на 1 tok/s
  суммаризация моделью дороже самой задачи);
- инструменты обязаны отдавать компактный вывод (одна строка на документ);
- guardrail: инструмент с requires_approval исполняется только после
  подтверждения человека (confirm-callback); отказ уходит модели как
  результат-ошибка;
- каждый шаг — в append-only аудит.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audit import AuditLog
from .grounding import (CORRECTION_PROMPT, NAME_FIX_NOTE, WARNING_SUFFIX,
                        apply_name_corrections, check_grounding)
from .i18n import t
from .toolspec import ToolOutput

Message = dict[str, Any]
ConfirmCallback = Callable[[str, dict[str, Any]], bool]
DeltaCallback = Callable[[str], None]

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Салваж tool-вызовов, пришедших текстом (паттерн openworker; формат GLM):
_TOOL_CALL_TEXT_RE = re.compile(
    r"<tool_call>\s*(?P<name>[\w.-]+)"
    r"(?P<args>(?:\s*<arg_key>[^<]*</arg_key>\s*<arg_value>[^<]*</arg_value>)*)"
    r"\s*</tool_call>", re.S)
_ARG_RE = re.compile(r"<arg_key>([^<]*)</arg_key>\s*<arg_value>([^<]*)</arg_value>", re.S)

# Модель, у которой отобрали инструменты (шаг переписывания), пытается
# выразить вызов текстом — и JSON утекает в ответ человеку. Замер 2026-07-30:
# в ответе оказалось {"name": "abc_analysis", "arguments": {...}}.
_TOOL_JSON_RE = re.compile(
    r"\{[^{}]*\"name\"\s*:\s*\"[\w.-]+\"[^{}]*\"arguments\"\s*:\s*\{[^{}]*\}[^{}]*\}", re.S)


def strip_tool_call_text(text: str) -> str:
    """Убирает из ответа человеку то, что модель написала как вызов инструмента."""
    cleaned = _TOOL_JSON_RE.sub("", text or "")
    cleaned = _TOOL_CALL_TEXT_RE.sub("", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def load_system_prompt(locale: str = "ru") -> str:
    path = _PROMPTS_DIR / f"system.{locale}.md"
    if not path.exists():
        path = _PROMPTS_DIR / "system.ru.md"
    return path.read_text(encoding="utf-8").strip()


def merge_tool_call_deltas(acc: dict[int, dict[str, Any]],
                           deltas: list[dict[str, Any]]) -> None:
    """Склейка потоковых фрагментов вызова инструмента.

    В потоке вызов приходит по частям: сперва имя, затем аргументы кусками,
    каждый — со своим `index`. Складывать фрагменты списком нельзя: получится
    несколько «вызовов» с обрывками аргументов и без поля `type`, и сервер
    отвергнет такую историю («Missing tool call type»). Найдено в живом
    интерфейсе 2026-07-30: неструйный путь (и все тесты) работал, а
    единственный путь, которым пользуется человек, — нет.
    """
    for delta in deltas:
        index = delta.get("index", 0)
        slot = acc.setdefault(index, {
            "id": "", "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        if delta.get("id"):
            slot["id"] = delta["id"]
        if delta.get("type"):
            slot["type"] = delta["type"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def salvage_tool_calls(text: str, known_tools: set[str]) -> list[dict[str, Any]]:
    """Достаёт tool-вызовы из текста, если бэкенд не распарсил их сам
    (например, llama.cpp-fallback не знает маркеры GLM)."""
    calls = []
    for i, m in enumerate(_TOOL_CALL_TEXT_RE.finditer(text)):
        if m.group("name") not in known_tools:
            continue
        args = {k.strip(): v for k, v in _ARG_RE.findall(m.group("args") or "")}
        calls.append({
            "id": f"salvaged_{i}",
            "type": "function",
            "function": {"name": m.group("name"),
                         "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


@dataclass
class AgentResult:
    text: str
    steps: int
    stopped_by_limit: bool = False
    grounded: bool = True   # False -> в ответе есть неподтверждённые данные
    model_failed: bool = False  # True -> ответ собран из данных в обход модели
    # Отчёты, которые показываются человеку целиком (модель их не пересказывает)
    reports: list[ToolOutput] = field(default_factory=list)


@dataclass
class Agent:
    client: Any                      # perimeter_inference.client.InferenceClient
    tool_specs: list[Any]            # perimeter_bridge1c.tools.ToolSpec (+ свои)
    audit: AuditLog
    confirm: ConfirmCallback         # (tool_name, args) -> bool; спрашивает человека
    locale: str = "ru"
    max_iterations: int = 12
    # Потолок на один ответ модели. Локальная модель на дисковом стриминге
    # выдаёт ~0,09 ток/с (замер 2026-07-30), поэтому 512 токенов — это полтора
    # часа на один ход. Вызов инструмента укладывается в ~80 токенов, деловой
    # ответ — в ~150, так что 192 хватает, а лишние рассуждения отсекаются.
    max_tokens_per_call: int = 192
    # Потолок на итоговый ответ человеку. Вызов инструмента укладывается в
    # ~80 токенов, а вот ответ по отчёту — нет: на живом прогоне 2026-07-30
    # ответы обрывались посреди числа («Всего: 135 0»), что хуже короткого
    # ответа. Больший потолок включается только после того, как инструменты
    # отработали, поэтому на выбор инструмента он не влияет.
    max_answer_tokens: int = 448
    # Сколько вызовов инструментов даём модели на один вопрос. Реальным
    # сценариям хватает трёх (найти контрагента -> найти документы -> ответ);
    # запас взят на уточнения. Всё сверх этого — почти всегда зацикливание.
    max_tool_calls_per_turn: int = 5
    # Выбор инструмента должен быть воспроизводимым. При температуре по
    # умолчанию (0.6) модель на одном и том же вопросе то вызывает
    # abc_analysis, то ищет контрагента «клиенты» (замер на стенде 16 ГБ,
    # 2026-07-30). Для деловых отчётов разброс недопустим.
    temperature: float = 0.1
    today: str | None = None            # ISO-дата; None = системная дата
    context_budget_chars: int = 24000   # ~ бюджет CTX 4096 токенов
    keep_recent: int = 8                # последних сообщений не сокращаем
    extra_system: str = ""              # напр., каталог навыков
    messages: list[Message] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.system_prompt = load_system_prompt(self.locale)
        # Без сегодняшней даты модель считает «за эту неделю» наугад
        # (живое демо 2026-07-30: выдала произвольный диапазон).
        from datetime import date
        today = self.today or date.today().isoformat()
        self.system_prompt += f"\n\nСегодня {today}. От этой даты считай «сегодня», «эта неделя», «этот месяц», «этот год»."
        if self.extra_system:
            self.system_prompt += "\n\n" + self.extra_system
        self._tools_by_name = {s.name: s for s in self.tool_specs}
        self.reports: list[ToolOutput] = []
        self._full_outputs: list[str] = []

    # --- история → модель (единственный шов; здесь же компактизация) -----

    def _outbound_messages(self) -> list[Message]:
        history = [self._compact(m, i) for i, m in enumerate(self.messages)]
        history = self._enforce_budget(history)
        return [{"role": "system", "content": self.system_prompt}, *history]

    def _compact(self, msg: Message, index: int) -> Message:
        """Старые сообщения ужимаются; последние keep_recent — как есть."""
        if index >= len(self.messages) - self.keep_recent:
            return msg
        out = dict(msg)
        content = out.get("content") or ""
        if out.get("role") == "tool" and len(content) > 160:
            first = content.splitlines()[0]
            out["content"] = first[:160] + " …[сокращено]"
        elif out.get("role") == "assistant" and len(content) > 300:
            out["content"] = content[:300] + " …[сокращено]"
        elif out.get("role") == "user" and len(content) > 500:
            out["content"] = content[:500] + " …[сокращено]"
        return out

    def _enforce_budget(self, history: list[Message]) -> list[Message]:
        def size(msgs: list[Message]) -> int:
            return sum(len(str(m.get("content") or "")) + 60 for m in msgs)

        if size(history) <= self.context_budget_chars:
            return history
        # Отбрасываем самые старые целыми ходами, но не последние keep_recent.
        dropped = 0
        while len(history) > self.keep_recent and size(history) > self.context_budget_chars:
            msg = history.pop(0)
            dropped += 1
            # tool-результаты не должны осиротеть без вызвавшего assistant
            while history and history[0].get("role") == "tool":
                history.pop(0)
                dropped += 1
        if dropped:
            history.insert(0, {"role": "user", "content":
                               f"[история сокращена: удалено {dropped} старых сообщений]"})
        return history

    def warmup(self) -> float:
        """Прогрев кэша префикса: один холостой запрос при запуске.

        Системный промпт и схемы инструментов одинаковы во всех обращениях,
        и движок кэширует их разбор. Без прогрева за это платит первый
        пользователь: замер на стенде — 14,8 с против 1,2 с на последующих
        вопросах. Вызывается при старте сервиса, ответ отбрасывается.
        """
        import time
        t0 = time.monotonic()
        try:
            self.client.chat(
                [{"role": "system", "content": self.system_prompt},
                 {"role": "user", "content": "готов?"}],
                tools=[s.openai_schema() for s in self.tool_specs],
                max_tokens=1, temperature=self.temperature)
        except Exception:  # noqa: BLE001 — прогрев не должен ронять запуск
            return -1.0
        return time.monotonic() - t0

    # --- основной цикл ----------------------------------------------------

    def run(self, user_text: str, on_delta: DeltaCallback | None = None) -> AgentResult:
        turn_start = len(self.messages)
        self.reports = []          # отчёты этого хода
        self._full_outputs = []
        self.messages.append({"role": "user", "content": user_text})
        self.audit.write("user_message", text=user_text[:2000])
        schemas = [s.openai_schema() for s in self.tool_specs]
        corrected = False
        forced_answer = False
        rewriting = False

        for step in range(1, self.max_iterations + 1):
            # Исчерпав бюджет инструментов, забираем их из запроса: модели
            # физически нечего вызвать, и она отвечает по уже собранным
            # данным. Иначе получается худший исход — «достигнут лимит
            # шагов» после пяти минут работы (живой прогон 2026-07-30:
            # нужный отчёт пришёл первым ходом, а модель ушла вызывать все
            # инструменты подряд и ответа так и не дала).
            calls_made = sum(1 for m in self.messages[turn_start:]
                             if m.get("role") == "tool")
            # На переписывании инструменты не нужны: данные уже собраны,
            # просят исправить текст. Оставишь их — модель вызывает тот же
            # отчёт заново (живой прогон 2026-07-30: четыре лишних вызова
            # payables_aging и упор в бюджет вместо исправления названия).
            offered = (None if rewriting or calls_made >= self.max_tool_calls_per_turn
                       else schemas)
            if offered is None and not forced_answer:
                forced_answer = True
                self.audit.write("tool_budget_reached", calls=calls_made)
            budget = (self.max_answer_tokens if calls_made
                      else self.max_tokens_per_call)
            try:
                text, tool_calls = self._call_model(offered, on_delta, max_tokens=budget)
            except Exception as e:  # noqa: BLE001 — сбой бэкенда не должен стоить хода
                # Данные из 1С уже получены и они верны; терять их из-за того,
                # что модель не смогла сформулировать ответ, незачем. Живой
                # прогон 2026-07-30: llama.cpp вернул 500 на исковерканном
                # моделью названии, и весь отчёт пропал вместе с ответом.
                collected = self._turn_tool_outputs(turn_start)
                self.audit.write("model_error", error=str(e)[:300],
                                 tool_results=len(collected))
                if not collected:
                    raise
                fallback = t("agent.model_failed_with_data",
                             error=str(e)[:120], data=collected[-1])
                self.messages.append({"role": "assistant", "content": fallback})
                if on_delta:
                    on_delta("\n\n" + fallback)
                return AgentResult(text=fallback, steps=step, model_failed=True,
                                   reports=list(self.reports))
            if offered is None and tool_calls:
                # Инструментов не предлагали, а модель всё равно их запросила.
                # Не исполняем: бюджет на то и бюджет.
                self.audit.write("tool_call_after_budget",
                                 tools=[c.get("function", {}).get("name") for c in tool_calls])
                tool_calls = []
                if not (text or "").strip():
                    continue
            if not tool_calls:
                salvaged = salvage_tool_calls(text, set(self._tools_by_name))
                if salvaged:
                    tool_calls = salvaged
                    text = _TOOL_CALL_TEXT_RE.sub("", text).strip()
            assistant_msg: Message = {"role": "assistant", "content": text or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.messages.append(assistant_msg)

            if not tool_calls:
                text = strip_tool_call_text(text)
                grounding = check_grounding(text, self._turn_tool_outputs(turn_start),
                                            question=user_text)
                if not grounding.ok and grounding.only_fixable_names:
                    text, fixes = apply_name_corrections(text, grounding)
                    self.audit.write("names_corrected", fixes=fixes)
                    # Пересверяем ДО добавления пометки: сама пометка цитирует
                    # исходное искажение и иначе ловилась бы как ошибка.
                    grounding = check_grounding(
                        text, self._turn_tool_outputs(turn_start), question=user_text)
                    note = NAME_FIX_NOTE.format(fixes=", ".join(fixes))
                    text += note
                    self.messages[-1]["content"] = text
                    if on_delta:
                        on_delta(note)
                if not grounding.ok and not corrected:
                    corrected = True
                    self.audit.write("grounding_retry", details=grounding.describe())
                    self.messages.append({
                        "role": "user",
                        "content": CORRECTION_PROMPT.format(details=grounding.describe()),
                    })
                    rewriting = True
                    if on_delta:
                        on_delta("\n\n[сверяю с данными 1С…]\n\n")
                    continue
                if not grounding.ok:
                    # Модель не исправилась сама. Искажённые названия чиним
                    # по данным (живой прогон 2026-07-30: «ТехноСервис» она
                    # коверкала и после прямой подсказки), остальное —
                    # предупреждением человеку.
                    text, fixes = apply_name_corrections(text or "", grounding)
                    suffix = ""
                    if fixes:
                        suffix += NAME_FIX_NOTE.format(fixes=", ".join(fixes))
                        self.audit.write("names_corrected", fixes=fixes)
                        grounding = check_grounding(
                            text, self._turn_tool_outputs(turn_start),
                            question=user_text)
                    if not grounding.ok:
                        details = grounding.describe()
                        self.audit.write("grounding_failed", details=details)
                        suffix += WARNING_SUFFIX.format(details=details)
                    text += suffix
                    self.messages[-1]["content"] = text
                    if on_delta and suffix:
                        on_delta(suffix)
                self.audit.write("assistant_message", text=(text or "")[:2000])
                return AgentResult(text=text, steps=step, grounded=grounding.ok,
                                   reports=list(self.reports))

            for call in tool_calls:
                self._execute_call(call)

        limit_text = t("agent.turn_limit", limit=self.max_iterations)
        self.messages.append({"role": "assistant", "content": limit_text})
        self.audit.write("turn_limit", limit=self.max_iterations)
        return AgentResult(text=limit_text, steps=self.max_iterations,
                           stopped_by_limit=True, reports=list(self.reports))

    def _call_model(self, schemas: list[dict[str, Any]] | None,
                    on_delta: DeltaCallback | None,
                    max_tokens: int | None = None) -> tuple[str, list[dict[str, Any]]]:
        outbound = self._outbound_messages()
        max_tokens = max_tokens or self.max_tokens_per_call
        if on_delta is None:
            result = self.client.chat(outbound, tools=schemas,
                                      max_tokens=max_tokens,
                                      temperature=self.temperature)
            return result.content, list(result.tool_calls)
        text_parts: list[str] = []
        partial: dict[int, dict[str, Any]] = {}
        for chunk in self.client.chat_stream(outbound, tools=schemas,
                                             max_tokens=max_tokens,
                                             temperature=self.temperature):
            if chunk.content:
                text_parts.append(chunk.content)
                on_delta(chunk.content)
            if chunk.tool_calls:
                merge_tool_call_deltas(partial, chunk.tool_calls)
        tool_calls = []
        for i, call in sorted(partial.items()):
            call.setdefault("type", "function")
            if not call.get("id"):
                call["id"] = f"call_{i}"
            tool_calls.append(call)
        return "".join(text_parts), tool_calls

    # --- исполнение инструментов -----------------------------------------

    def _execute_call(self, call: dict[str, Any]) -> None:
        name = call.get("function", {}).get("name", "?")
        args_json = call.get("function", {}).get("arguments", "{}")
        call_id = call.get("id", "")
        try:
            args = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError:
            args = {"_raw": args_json}

        spec = self._tools_by_name.get(name)
        if spec is None:
            self._record_tool_result(call_id, name, f"Ошибка: неизвестный инструмент {name}.")
            return

        if getattr(spec, "requires_approval", False):
            approved = self.confirm(name, args)
            self.audit.write("confirm_granted" if approved else "confirm_denied",
                             tool=name, args=args)
            if not approved:
                self._record_tool_result(call_id, name, t("agent.action_denied"))
                return

        try:
            result = spec.func(**args)
        except Exception as e:  # noqa: BLE001 — ошибка уходит модели
            result = f"Ошибка выполнения: {e}"
        self.audit.write("tool_call", tool=name, args=args, result=str(result)[:500])
        if isinstance(result, ToolOutput):
            # Человек получает отчёт целиком, модель — только выжимку.
            self.reports.append(result)
            self._record_tool_result(call_id, name, result.digest)
            self._full_outputs.append(result.display)
        else:
            self._record_tool_result(call_id, name, str(result))

    def _turn_tool_outputs(self, turn_start: int) -> list[str]:
        """База для сверки ответа: то, что инструменты дали на этом ходе.

        Для отчётов берём ПОЛНЫЙ текст, а не выжимку, которую видела модель:
        если она напишет что-то сверх выжимки и это окажется правдой из
        таблицы — придираться не за что; а выдумку поймаем по-прежнему.
        """
        return ([str(m.get("content") or "")
                 for m in self.messages[turn_start:] if m.get("role") == "tool"]
                + list(self._full_outputs))

    def _record_tool_result(self, call_id: str, name: str, content: str) -> None:
        self.messages.append({
            "role": "tool", "tool_call_id": call_id, "name": name, "content": content,
        })
