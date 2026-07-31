"""Прогон отчётных сценариев на живой модели.

Отличие от `tool_choice.py`: там меряется выбор инструмента, здесь смотрят
глазами на весь ответ целиком — формулировки, оговорки, читаемость. Такие
вещи автотестом не ловятся, а на живом прогоне видны сразу: именно так
нашлись обрыв ответа посреди числа, искажённое название контрагента и
ход без ответа за 330 секунд.

Запуск (сервер модели должен быть поднят):
    .venv/bin/python tests/bench/live_reports.py http://127.0.0.1:8090 gigachat
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for pkg in ("core", "bridge-1c", "inference", "tests"):
    sys.path.insert(0, str(ROOT / pkg))

from fakes.fake_1c_server import Fake1CServer            # noqa: E402
from perimeter_bridge1c.analytics import AnalyticsTools  # noqa: E402
from perimeter_bridge1c.mapping import load_mapping      # noqa: E402
from perimeter_bridge1c.odata import ODataClient         # noqa: E402
from perimeter_bridge1c.tools import Bridge1CTools       # noqa: E402
from perimeter_core.agent import Agent                   # noqa: E402
from perimeter_core.audit import AuditLog                # noqa: E402
from perimeter_inference.client import InferenceClient   # noqa: E402

QUESTIONS = [
    "Покажи прибыли и убытки",
    "Что с деньгами на расчётном счёте?",
    "Сколько мы должны поставщикам?",
    "Как идут продажи и какой средний чек?",
    "Сделай акт сверки с Ромашкой",
    "Кто нам должен и сколько?",
    "Кто наши крупнейшие клиенты?",
]


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
    model = sys.argv[2] if len(sys.argv) > 2 else "gigachat"
    today = sys.argv[3] if len(sys.argv) > 3 else None

    with Fake1CServer() as srv:
        mapping = load_mapping("bp30")
        backend = ODataClient(srv.base_url, "robot", "test", mapping=mapping)
        specs = (Bridge1CTools(backend, mapping).specs()
                 + AnalyticsTools(backend, mapping).specs())
        print(f"инструментов: {len(specs)}\n")
        for question in QUESTIONS:
            agent = Agent(
                client=InferenceClient(url, model=model, timeout_s=600),
                tool_specs=specs, audit=AuditLog(Path("/tmp/live_audit.log")),
                confirm=lambda n, a: True, today=today)
            t0 = time.monotonic()
            try:
                result = agent.run(question)
            except Exception as e:  # noqa: BLE001 — сбой модели тоже результат
                print(f"### {question}\n  ОШИБКА: {e}\n")
                continue
            elapsed = time.monotonic() - t0
            tools = [m.get("name") for m in agent.messages if m.get("role") == "tool"]
            print(f"### {question}")
            print(f"  {elapsed:.1f} c | ходов {result.steps} | "
                  f"сверка {'OK' if result.grounded else 'НЕ ПРОШЛА'} | "
                  f"инструменты: {', '.join(tools) or '—'}")
            print("  " + (result.text or "").replace("\n", "\n  "))
            for report in result.reports:
                # Таблицу человек видит целиком — её модель не пересказывает.
                print(f"  [таблица «{report.title}»]")
                print("  " + report.display.replace("\n", "\n  "))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
