"""Append-only журнал действий агента (фича продукта для ИБ).

JSONL, файл открывается в режиме O_APPEND — дозапись атомарна на уровне
ОС; метода перезаписи/усечения в API нет намеренно. Каждая запись:
ts (UTC ISO), event, payload. Секреты в журнал не пишутся.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND: даже при конкурентной записи строки не перемешиваются.
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)

    def write(self, event: str, **payload: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        os.write(self._fd, line.encode("utf-8"))
        os.fsync(self._fd)

    def close(self) -> None:
        os.close(self._fd)

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
