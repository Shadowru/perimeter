#!/usr/bin/env python3
"""Замер честных цифр inference на текущем железе (Этап 3).

Запускать на целевой машине с полными весами GLM-5.2:
  python3 tools/bench_inference.py --model /nvme/glm52_i4 [--port 18092]

Меряет: TTFT (холодный и тёплый), tok/s декодирования, пиковый RSS
сервера+движка, размер модели на диске. Результаты — в stdout в
markdown-виде для вставки в docs/hardware.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "core"), str(REPO / "inference")]

from perimeter_core.config import InferenceConfig  # noqa: E402
from perimeter_inference.client import InferenceClient  # noqa: E402
from perimeter_inference.server import InferenceServer  # noqa: E402

PROMPT_SHORT = [{"role": "user", "content": "Перечисли три формы бухгалтерской отчётности."}]


def _descendants(pid: int) -> list[int]:
    out: list[int] = []
    try:
        for task in Path(f"/proc/{pid}/task").iterdir():
            out += [int(c) for c in (task / "children").read_text().split()]
    except OSError:
        return out
    for child in list(out):
        out += _descendants(child)
    return out


def peak_rss_gb(pid: int) -> float:
    """Пиковый RSS процесса и всех потомков (VmHWM из /proc, Linux).

    Читается пока процессы живы: getrusage(RUSAGE_CHILDREN) учитывает
    только завершённых потомков и на работающем сервере даёт ноль.
    """
    total_kb = 0
    for p in [pid, *_descendants(pid)]:
        try:
            for line in Path(f"/proc/{p}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    total_kb += int(line.split()[1])
                    break
        except OSError:
            continue
    return total_kb / 1e6


def run_once(client: InferenceClient, messages, max_tokens: int) -> dict:
    t0 = time.monotonic()
    ttft = None
    n_content = 0
    usage = None
    for chunk in client.chat_stream(messages, max_tokens=max_tokens, temperature=0.0):
        # Только content: сервер каждые 10 с шлёт keepalive-точку в
        # reasoning_content, чтобы клиент не отвалился на длинном prefill.
        # Считать её первым токеном — значит получить красивые, но ложные 10 с.
        if ttft is None and chunk.content:
            ttft = time.monotonic() - t0
        n_content += len(chunk.content)
        if chunk.usage:
            usage = chunk.usage
    total = time.monotonic() - t0
    completion = (usage or {}).get("completion_tokens", 0)
    decode_s = total - (ttft or 0.0)
    return {
        "ttft_s": round(ttft or total, 2),
        "total_s": round(total, 2),
        "completion_tokens": completion,
        "tok_s": round(completion / decode_s, 2) if decode_s > 0 and completion else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="каталог весов (int4-gs64)")
    ap.add_argument("--port", type=int, default=18092)
    ap.add_argument("--max-tokens", type=int, default=128)
    args = ap.parse_args()

    model_dir = Path(args.model)
    disk_gb = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file()) / 1e9

    cfg = InferenceConfig(port=args.port)
    srv = InferenceServer(cfg, model_path=args.model)
    print(f"Запуск сервера (модель {disk_gb:.0f} ГБ на диске)…", file=sys.stderr)
    t_load = time.monotonic()
    srv.start(wait_ready_s=3600)
    load_s = time.monotonic() - t_load
    client = InferenceClient(cfg.base_url, model=cfg.model_id)
    try:
        cold = run_once(client, PROMPT_SHORT, args.max_tokens)
        warm = run_once(client, PROMPT_SHORT, args.max_tokens)
        # Пока сервер жив — иначе /proc уже не прочитать.
        peak_gb = peak_rss_gb(srv.proc.pid) if srv.proc else 0.0
        print("\n### Результаты замера\n")
        print(f"| Метрика | Значение |\n|---|---|")
        print(f"| Модель на диске | {disk_gb:.1f} ГБ |")
        print(f"| Загрузка до READY | {load_s:.0f} с |")
        print(f"| TTFT холодный (до первого токена ответа) | {cold['ttft_s']} с |")
        print(f"| TTFT тёплый | {warm['ttft_s']} с |")
        print(f"| Декодирование | {warm['tok_s'] or cold['tok_s']} tok/s |")
        print(f"| Пиковый RSS (сервер+движок) | {peak_gb:.1f} ГБ |")
        print(f"\nСырые данные: cold={json.dumps(cold)} warm={json.dumps(warm)}")
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
