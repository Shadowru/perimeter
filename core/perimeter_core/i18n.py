"""i18n: все пользовательские строки — только через t().

ru — базовая локаль, en — вторичная. Отсутствующий ключ отдаёт значение
из ru, затем сам ключ (никогда не падаем из-за перевода).
"""

from __future__ import annotations

import json
from pathlib import Path

_LOCALES_DIR = Path(__file__).parent / "locales"
_BASE_LOCALE = "ru"

_cache: dict[str, dict[str, str]] = {}
_current = _BASE_LOCALE


def _load(locale: str) -> dict[str, str]:
    if locale not in _cache:
        path = _LOCALES_DIR / f"{locale}.json"
        _cache[locale] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _cache[locale]


def set_locale(locale: str) -> None:
    global _current
    _current = locale


def t(key: str, **kwargs: object) -> str:
    text = _load(_current).get(key) or _load(_BASE_LOCALE).get(key) or key
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError):
        return text
