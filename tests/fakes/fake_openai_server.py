"""Скриптованный OpenAI-совместимый сервер для тестов (stdlib).

Отдаёт заранее заданные ответы (текст и/или tool-вызовы) по очереди —
детерминированная «модель» для юнит- и e2e-тестов без весов и GPU.
Поддерживает stream=true (SSE, чанк на слово) и stream=false.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Scripted:
    """Один ход «модели»: текст и/или tool-вызовы."""
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.get("id", f"call_{i}"),
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                    },
                }
                for i, tc in enumerate(self.tool_calls)
            ]
        return msg


class FakeOpenAIServer:
    def __init__(self, script: list[Scripted]):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []  # что реально прислали (для ассертов)
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/health":
                    body = json.dumps({"status": "ok", "fake": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with outer._lock:
                    outer.requests.append(payload)
                    if not outer.script:
                        self.send_error(500, "fake model script exhausted")
                        return
                    step = outer.script.pop(0)
                if payload.get("stream"):
                    self._stream(step)
                else:
                    self._complete(step)

            def _complete(self, step: Scripted) -> None:
                body = json.dumps({
                    "id": "fake", "object": "chat.completion", "model": "fake",
                    "choices": [{
                        "index": 0,
                        "message": step.as_message(),
                        "finish_reason": "tool_calls" if step.tool_calls else "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sse(self, obj: dict[str, Any]) -> None:
                self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))

            def _stream(self, step: Scripted) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                base = {"id": "fake", "object": "chat.completion.chunk", "model": "fake"}
                words = step.content.split(" ") if step.content else []
                for i, w in enumerate(words):
                    text = w if i == len(words) - 1 else w + " "
                    self._sse({**base, "choices": [{"index": 0, "delta": {"content": text}}]})
                final_delta: dict[str, Any] = {}
                if step.tool_calls:
                    final_delta["tool_calls"] = step.as_message()["tool_calls"]
                self._sse({**base, "choices": [{
                    "index": 0, "delta": final_delta,
                    "finish_reason": "tool_calls" if step.tool_calls else "stop",
                }]})
                self._sse({**base, "choices": [],
                           "usage": {"prompt_tokens": 1, "completion_tokens": len(words), "total_tokens": 1 + len(words)}})
                self.wfile.write(b"data: [DONE]\n\n")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FakeOpenAIServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
