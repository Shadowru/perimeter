"""Ядро «Периметра».

Импорт этого пакета подключает vendor/ к sys.path — все сторонние
зависимости живут только там (правило проекта: вендорим всё).
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = REPO_ROOT / "vendor"

if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))
