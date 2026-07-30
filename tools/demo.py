#!/usr/bin/env python3
"""Демо-режим «Периметра»: веб-интерфейс на демонстрационной базе 1С.

Поднимает всё, что нужно, чтобы задать вопросы своими словами:
модель (llama.cpp), мок-1С с демонстрационными данными и веб-интерфейс.

    python3 tools/demo.py [--port 8091]

Живой 1С не требуется — данные вымышленные: три контрагента, реализации
и оплаты июля 2026, номенклатура с брендами, регистр себестоимости.
Полезно для показа заказчику и для проверки формулировок вопросов.

Интерфейс слушает только loopback (правило №0). Чтобы открыть его со
своего компьютера, пробросьте порт по SSH:

    ssh -L 8091:127.0.0.1:8091 root@<сервер>
    # затем откройте http://localhost:8091
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "core"), str(REPO / "inference"),
                str(REPO / "bridge-1c"), str(REPO / "ui"), str(REPO / "tests")]

from fakes.fake_1c_server import Fake1CServer  # noqa: E402
from perimeter_bridge1c.analytics import AnalyticsTools  # noqa: E402
from perimeter_bridge1c.mapping import load_mapping  # noqa: E402
from perimeter_bridge1c.odata import ODataClient  # noqa: E402
from perimeter_bridge1c.tools import Bridge1CTools  # noqa: E402
from perimeter_core.agent import Agent  # noqa: E402
from perimeter_core.audit import AuditLog  # noqa: E402
from perimeter_core.config import load_config  # noqa: E402
from perimeter_core.skills import catalog_text, load_skills, make_load_skill_tool  # noqa: E402
from perimeter_inference.client import InferenceClient  # noqa: E402
from perimeter_inference.server import InferenceServer  # noqa: E402
from perimeter_ui.server import UIServer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(REPO / "config" / "perimeter.yaml"))
    ap.add_argument("--port", type=int, default=None, help="порт веб-интерфейса")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ui_port = args.port or cfg.ui.port

    print("Поднимаю демонстрационную базу 1С…")
    mock = Fake1CServer()
    mock.__enter__()

    print(f"Поднимаю модель ({cfg.inference.model_id})…")
    inference = InferenceServer(cfg.inference)
    inference.start(wait_ready_s=900)

    mapping = load_mapping("bp30")
    backend = ODataClient(mock.base_url, "robot", "test", mapping=mapping)
    skills = load_skills()
    tool_specs = (Bridge1CTools(backend, mapping).specs()
                  + AnalyticsTools(backend, mapping).specs()
                  + [make_load_skill_tool(skills)])

    def factory(confirm):
        return Agent(
            client=InferenceClient(cfg.inference.base_url,
                                   model=cfg.inference.model_id, timeout_s=900),
            tool_specs=tool_specs,
            audit=AuditLog(REPO / "var" / "demo-audit.log"),
            confirm=confirm,
            extra_system=catalog_text(skills),
        )

    print("Прогреваю кэш модели (иначе первый вопрос ждёт дольше)…")
    warm = factory(lambda n, a: False).warmup()
    print(f"  прогрев занял {warm:.1f} с" if warm >= 0 else "  прогрев не удался")

    ui = UIServer(cfg.ui.host, ui_port, factory)
    ui.start()

    print(f"""
=== Демо готово: {ui.base_url} ===

Со своего компьютера откройте туннель и заходите на http://localhost:{ui_port}:
    ssh -L {ui_port}:127.0.0.1:{ui_port} root@<адрес сервера>

Что можно спросить (данные вымышленные):
  • Какие реализации не проведены за июль?
  • Что мы отгрузили Ромашке, но не получили оплату?
  • Сделай ABC-анализ по клиентам
  • Дай себестоимость и маржу по брендам за июль 2026
  • Кто нам должен и как давно?
  • Какие товары приносят больше всего выручки?
  • Подготовь черновик счёта Ромашке (нужна галочка «разрешить создание»)

Ctrl+C — остановить.""", flush=True)

    stop: list[int] = []
    signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    try:
        while not stop:
            signal.pause()
    finally:
        ui.stop()
        inference.stop()
        mock.__exit__()
    return 0


if __name__ == "__main__":
    sys.exit(main())
