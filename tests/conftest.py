"""Пути импорта для тестов без установки пакета (editable-режим)."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for component in ("core", "inference", "bridge-1c", "ui", "vendor"):
    p = str(REPO / component)
    if p not in sys.path:
        sys.path.insert(0, p)
