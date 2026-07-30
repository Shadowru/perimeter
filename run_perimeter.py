#!/usr/bin/env python3
"""Запуск «Периметра»: локальный inference + агент + веб-UI.

    python3 run_perimeter.py [--config config/perimeter.yaml] [--no-inference]

--no-inference: не поднимать inference-сервер (уже запущен отдельно,
например llama.cpp или colibri на другом порту из конфига).
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path[:0] = [str(REPO / "core"), str(REPO / "inference"),
                str(REPO / "bridge-1c"), str(REPO / "ui")]

from perimeter_core.app import build_agent  # noqa: E402
from perimeter_core.config import load_config  # noqa: E402
from perimeter_inference.server import InferenceServer  # noqa: E402
from perimeter_ui.server import UIServer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(REPO / "config" / "perimeter.yaml"))
    ap.add_argument("--no-inference", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    inference = None
    if not args.no_inference:
        inference = InferenceServer(cfg.inference)
        print(f"Поднимаю inference ({cfg.inference.backend}) на {cfg.inference.base_url} …")
        inference.start()

    def factory(confirm):
        agent, _ = build_agent(args.config, confirm)
        return agent

    print("Прогреваю кэш модели…")
    factory(lambda n, a: False).warmup()

    ui = UIServer(cfg.ui.host, cfg.ui.port, factory)
    ui.start()
    print(f"Периметр готов: {ui.base_url}")

    stop = []
    signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    try:
        while not stop:
            signal.pause()
    finally:
        ui.stop()
        if inference is not None:
            inference.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
