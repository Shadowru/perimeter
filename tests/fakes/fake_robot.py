"""Фальшивый робот 1С: опрашивает шлюз и отвечает из мок-датасета.

Повторяет поведение внешней обработки (bridge-1c/robot1c): забирает
задание длинным опросом, выполняет его над данными «базы», возвращает
результат. Позволяет тестировать обратное подключение целиком, без 1С.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any

from .fake_1c_server import default_dataset


def _match(row: dict[str, Any], cond: dict[str, Any]) -> bool:
    actual = row.get(cond["field"])
    value = cond["value"]
    op = cond["op"]
    if op == "contains":
        return str(value).lower() in str(actual or "").lower()
    if actual is None:
        return False
    if op == "eq":
        return actual == value
    if op == "ge":
        return str(actual) >= str(value)
    if op == "le":
        return str(actual) <= str(value)
    return False


class FakeRobot:
    """Крутится в потоке, пока не остановят."""

    def __init__(self, base_url: str, token: str = "",
                 dataset: dict[str, list[dict[str, Any]]] | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.dataset = dataset if dataset is not None else default_dataset()
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.handled = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Robot-Token"] = self.token
        return h

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/robot/poll?wait=1", headers=self._headers())
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 204:
                        continue
                    task = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                if self._stop.wait(0.2):
                    return
                continue
            result = self._execute(task)
            self.handled += 1
            try:
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                urllib.request.urlopen(urllib.request.Request(
                    f"{self.base_url}/robot/result", data=body,
                    headers=self._headers(), method="POST"), timeout=10).close()
            except (urllib.error.URLError, OSError):
                pass

    def _execute(self, task: dict[str, Any]) -> dict[str, Any]:
        op = task.get("op")
        tid = task.get("id")
        try:
            if op == "query":
                return {"id": tid, "ok": True, "rows": self._query(task)}
            if op == "get":
                rows = [r for r in self.dataset.get(task["entity"], [])
                        if r["Ref_Key"] == task["ref_key"]]
                return {"id": tid, "ok": True, "row": rows[0] if rows else None}
            if op == "create_draft":
                return {"id": tid, "ok": True, "row": self._create(task)}
            if op == "metadata":
                return {"id": tid, "ok": True, "entities": {
                    name: sorted({k for row in rows for k in row})
                    for name, rows in self.dataset.items()}}
            return {"id": tid, "ok": False, "error": f"неизвестная операция {op}"}
        except Exception as e:  # noqa: BLE001 — ошибка уходит агенту, робот живёт
            return {"id": tid, "ok": False, "error": str(e)}

    def _query(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        rows = list(self.dataset.get(task["entity"], []))
        for cond in task.get("conditions") or []:
            rows = [r for r in rows if _match(r, cond)]
        if task.get("order_by"):
            rows.sort(key=lambda r: str(r.get(task["order_by"], "")))
        if task.get("top"):
            rows = rows[: int(task["top"])]
        if task.get("select"):
            rows = [{k: r[k] for k in task["select"] if k in r} for r in rows]
        return rows

    def _create(self, task: dict[str, Any]) -> dict[str, Any]:
        row = dict(task.get("payload") or {})
        row.setdefault("Ref_Key", str(uuid.uuid4()))
        row.setdefault("Number", f"АГ-{len(self.created) + 1:04d}")
        row.setdefault("Date", "2026-07-29T00:00:00")
        row["Posted"] = False  # робот тоже не проводит документы
        self.dataset.setdefault(task["entity"], []).append(row)
        self.created.append((task["entity"], row))
        return row

    def __enter__(self) -> "FakeRobot":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
