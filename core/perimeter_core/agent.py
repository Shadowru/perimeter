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
from .i18n import t

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


def load_system_prompt(locale: str = "ru") -> str:
    path = _PROMPTS_DIR / f"system.{locale}.md"
    if not path.exists():
        path = _PROMPTS_DIR / "system.ru.md"
    return path.read_text(encoding="utf-8").strip()


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


@dataclass
class Agent:
    client: Any                      # perimeter_inference.client.InferenceClient
    tool_specs: list[Any]            # perimeter_bridge1c.tools.ToolSpec (+ свои)
    audit: AuditLog
    confirm: ConfirmCallback         # (tool_name, args) -> bool; спрашивает человека
    locale: str = "ru"
    max_iterations: int = 12
    context_budget_chars: int = 24000   # ~ бюджет CTX 4096 токенов
    keep_recent: int = 8                # последних сообщений не сокращаем
    extra_system: str = ""              # напр., каталог навыков
    messages: list[Message] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.system_prompt = load_system_prompt(self.locale)
        if self.extra_system:
            self.system_prompt += "\n\n" + self.extra_system
        self._tools_by_name = {s.name: s for s in self.tool_specs}

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

    # --- основной цикл ----------------------------------------------------

    def run(self, user_text: str, on_delta: DeltaCallback | None = None) -> AgentResult:
        self.messages.append({"role": "user", "content": user_text})
        self.audit.write("user_message", text=user_text[:2000])
        schemas = [s.openai_schema() for s in self.tool_specs]

        for step in range(1, self.max_iterations + 1):
            text, tool_calls = self._call_model(schemas, on_delta)
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
                self.audit.write("assistant_message", text=(text or "")[:2000])
                return AgentResult(text=text, steps=step)

            for call in tool_calls:
                self._execute_call(call)

        limit_text = t("agent.turn_limit", limit=self.max_iterations)
        self.messages.append({"role": "assistant", "content": limit_text})
        self.audit.write("turn_limit", limit=self.max_iterations)
        return AgentResult(text=limit_text, steps=self.max_iterations, stopped_by_limit=True)

    def _call_model(self, schemas: list[dict[str, Any]],
                    on_delta: DeltaCallback | None) -> tuple[str, list[dict[str, Any]]]:
        outbound = self._outbound_messages()
        if on_delta is None:
            result = self.client.chat(outbound, tools=schemas)
            return result.content, list(result.tool_calls)
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for chunk in self.client.chat_stream(outbound, tools=schemas):
            if chunk.content:
                text_parts.append(chunk.content)
                on_delta(chunk.content)
            if chunk.tool_calls:
                tool_calls.extend(chunk.tool_calls)
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
        self._record_tool_result(call_id, name, str(result))

    def _record_tool_result(self, call_id: str, name: str, content: str) -> None:
        self.messages.append({
            "role": "tool", "tool_call_id": call_id, "name": name, "content": content,
        })
