"""OpenAI-совместимый клиент на stdlib (urllib): /v1/chat/completions.

Работает с любым локальным бэкендом (colibri, llama.cpp). Стриминг —
разбор SSE-потока `data: {...}`. Никаких внешних адресов: base_url
приходит из конфига, где host валидирован как loopback.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator


class InferenceError(Exception):
    pass


@dataclass
class ChatChunk:
    """Инкремент стрима: дельта текста/рассуждений либо финал с tool_calls."""
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


class InferenceClient:
    def __init__(self, base_url: str, model: str = "glm-5.2", timeout_s: float = 3600.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
             temperature: float = 0.6, max_tokens: int | None = None) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "temperature": temperature, "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            with urllib.request.urlopen(self._request(payload), timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise InferenceError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:500]}") from e
        except urllib.error.URLError as e:
            raise InferenceError(str(e)) from e
        msg = data["choices"][0]["message"]
        return ChatResult(
            content=msg.get("content") or "",
            tool_calls=msg.get("tool_calls") or [],
            finish_reason=data["choices"][0].get("finish_reason"),
            usage=data.get("usage"),
        )

    def chat_stream(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                    temperature: float = 0.6, max_tokens: int | None = None) -> Iterator[ChatChunk]:
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "temperature": temperature,
            "stream": True, "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            resp = urllib.request.urlopen(self._request(payload), timeout=self.timeout_s)
        except urllib.error.HTTPError as e:
            raise InferenceError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:500]}") from e
        except urllib.error.URLError as e:
            raise InferenceError(str(e)) from e
        with resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    return
                event = json.loads(body)
                usage = event.get("usage")
                if not event.get("choices"):
                    if usage:
                        yield ChatChunk(usage=usage)
                    continue
                choice = event["choices"][0]
                delta = choice.get("delta") or {}
                yield ChatChunk(
                    content=delta.get("content") or "",
                    reasoning=delta.get("reasoning_content") or "",
                    tool_calls=delta.get("tool_calls"),
                    finish_reason=choice.get("finish_reason"),
                    usage=usage,
                )

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise InferenceError(f"health check failed: {e}") from e
