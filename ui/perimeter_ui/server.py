"""Минимальный локальный веб-UI (stdlib, ноль внешних ассетов).

- GET /            — страница чата (inline HTML/CSS/JS, системные шрифты).
- POST /api/chat   — SSE: {"delta": …}* → {"done": …}.
  Поле approve_writes управляет подтверждением записывающих инструментов
  на этот запрос (человеческое подтверждение фиксируется в аудите).

Слушает только loopback (валидируется конфигом).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from perimeter_core.i18n import t

_STATIC = Path(__file__).parent / "static"


class UIServer:
    def __init__(self, host: str, port: int, agent_factory):
        """agent_factory(confirm) -> Agent — свой агент на каждую сессию UI."""
        outer = self
        self._agent = None
        self._agent_lock = threading.Lock()
        self._factory = agent_factory

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path not in ("/", "/index.html"):
                    self.send_error(404)
                    return
                page = (_STATIC / "index.html").read_text(encoding="utf-8")
                for key in ("ui.title", "ui.placeholder", "ui.send", "ui.working"):
                    page = page.replace("{{" + key + "}}", t(key))
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html;charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                if self.path != "/api/chat":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                message = str(req.get("message", "")).strip()
                approve_writes = bool(req.get("approve_writes", False))
                if not message:
                    self.send_error(400, "empty message")
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream;charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                def emit(obj: dict) -> None:
                    self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()

                with outer._agent_lock:
                    agent = outer._get_agent(approve_writes)
                    try:
                        result = agent.run(message,
                                           on_delta=lambda d: emit({"delta": d}))
                        emit({"done": result.text})
                    except Exception as e:  # noqa: BLE001 — ошибка уходит в UI
                        emit({"error": str(e)})

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _get_agent(self, approve_writes: bool):
        # Агент один на сессию UI (история сохраняется между запросами);
        # подтверждение записи — флаг текущего запроса.
        self._approve_writes = approve_writes
        if self._agent is None:
            self._agent = self._factory(lambda name, args: self._approve_writes)
        return self._agent

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "UIServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
