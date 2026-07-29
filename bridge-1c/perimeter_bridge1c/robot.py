"""Обратное подключение к 1С: шлюз заданий + бэкенд поверх него.

Зачем: OData требует публикации информационной базы на веб-сервере. Там,
где это запрещено службой ИБ или невозможно (базовая версия платформы),
направление переворачивается — 1С сама ходит к нам:

    инструмент агента → RobotBackend → очередь заданий в RobotGateway
                                             ↑ HTTP-опрос (long polling)
                              внешняя обработка «робот» в сеансе 1С
                                             ↓ результат
                        RobotGateway → RobotBackend → инструмент агента

Конфигурация 1С при этом не меняется: обработка внешняя (файл .epf),
как внешний отчёт. Исходники обработки — в bridge-1c/robot1c/.

Правило №0: шлюз слушает только loopback или адрес внутренней сети
(проверяется в конфиге), наружу не ходит. Доступ — по общему токену.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

from .backend import Query
from .mapping import ConfigurationMapping


class RobotError(Exception):
    pass


@dataclass
class _Task:
    id: str
    payload: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class RobotGateway:
    """Очередь заданий для робота 1С + HTTP-эндпоинты его опроса."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8092, token: str = ""):
        self._queue: list[_Task] = []
        self._by_id: dict[str, _Task] = {}
        self._lock = threading.Lock()
        self._new_task = threading.Condition(self._lock)
        self._token = token
        self.last_seen_robot: float | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _authorized(self) -> bool:
                if outer._token and self.headers.get("X-Robot-Token") != outer._token:
                    self._send(403, b'{"error":"bad token"}')
                    return False
                return True

            def _send(self, code: int, body: bytes = b"") -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json;charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self) -> None:
                split = urlsplit(self.path)
                if split.path == "/robot/hello":
                    if not self._authorized():
                        return
                    self._send(200, json.dumps(
                        {"service": "perimeter-robot-gateway", "version": 1},
                        ensure_ascii=False).encode("utf-8"))
                    return
                if split.path != "/robot/poll":
                    self._send(404, b'{"error":"not found"}')
                    return
                if not self._authorized():
                    return
                wait_s = float((parse_qs(split.query).get("wait") or ["25"])[0])
                task = outer._take_task(min(wait_s, 60.0))
                if task is None:
                    self._send(204)
                    return
                self._send(200, json.dumps(
                    {"id": task.id, **task.payload}, ensure_ascii=False).encode("utf-8"))

            def do_POST(self) -> None:
                if urlsplit(self.path).path != "/robot/result":
                    self._send(404, b'{"error":"not found"}')
                    return
                if not self._authorized():
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    self._send(400, json.dumps({"error": str(e)}).encode())
                    return
                outer._complete(data)
                self._send(204)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # --- сторона агента ---------------------------------------------------

    def submit(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        task = _Task(id=uuid.uuid4().hex, payload=payload)
        with self._new_task:
            self._queue.append(task)
            self._by_id[task.id] = task
            self._new_task.notify()
        if not task.done.wait(timeout_s):
            with self._lock:
                self._by_id.pop(task.id, None)
                if task in self._queue:
                    self._queue.remove(task)
            raise RobotError(
                f"робот 1С не ответил за {timeout_s:.0f} с — проверьте, запущена ли "
                f"обработка «Периметр: робот» в сеансе 1С")
        assert task.result is not None
        if not task.result.get("ok", False):
            raise RobotError(f"1С вернула ошибку: {task.result.get('error', 'без описания')}")
        return task.result

    # --- сторона робота ---------------------------------------------------

    def _take_task(self, wait_s: float) -> _Task | None:
        import time
        with self._new_task:
            self.last_seen_robot = time.time()
            if not self._queue:
                self._new_task.wait(wait_s)
            return self._queue.pop(0) if self._queue else None

    def _complete(self, data: dict[str, Any]) -> None:
        with self._lock:
            task = self._by_id.pop(str(data.get("id", "")), None)
        if task is not None:
            task.result = data
            task.done.set()

    # --- жизненный цикл ---------------------------------------------------

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "RobotGateway":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class RobotBackend:
    """Backend поверх шлюза: те же операции, что у ODataClient."""

    def __init__(self, gateway: RobotGateway, mapping: ConfigurationMapping | None = None,
                 timeout_s: float = 120.0):
        self.gateway = gateway
        self.mapping = mapping
        self.timeout_s = timeout_s

    def run(self, query: Query) -> Iterator[dict[str, Any]]:
        result = self.gateway.submit({"op": "query", **query.as_dict()}, self.timeout_s)
        yield from result.get("rows", [])

    def get(self, entity_set: str, ref_key: str,
            select: list[str] | None = None) -> dict[str, Any]:
        result = self.gateway.submit(
            {"op": "get", "entity": entity_set, "ref_key": ref_key, "select": select},
            self.timeout_s)
        row = result.get("row")
        if row is None:
            raise RobotError(f"объект {entity_set} {ref_key} не найден")
        return row

    def create_draft(self, entity_set: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body["Posted"] = False  # тот же guardrail, что и в OData-клиенте
        result = self.gateway.submit(
            {"op": "create_draft", "entity": entity_set, "payload": body}, self.timeout_s)
        return result.get("row", {})

    def validate_mapping(self) -> list[str]:
        """Сверка маппинга с метаданными, которые перечислил робот."""
        if self.mapping is None:
            return ["маппинг не задан"]
        try:
            result = self.gateway.submit({"op": "metadata"}, self.timeout_s)
        except RobotError as e:
            return [str(e)]
        known: dict[str, list[str]] = result.get("entities", {})
        problems = []
        for ent in self.mapping.entities.values():
            fields = known.get(ent.entity_set)
            if fields is None:
                problems.append(f"сущность {ent.entity_set} отсутствует в конфигурации 1С")
                continue
            for logical, name_1c in ent.fields.items():
                if name_1c not in fields:
                    problems.append(
                        f"{ent.entity_set}: поле {name_1c} (={logical}) не найдено")
        return problems
