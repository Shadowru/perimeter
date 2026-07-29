"""TTFT должен считаться по первому токену ответа, а не по keepalive.

Сервер colibri каждые 10 с шлёт точку в reasoning_content, чтобы клиент не
отвалился на длинном prefill. Если считать её первым токеном, замер покажет
ровные 10 с вместо реальных минут — и эта цифра уедет в маркетинг.
"""

import importlib.util
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench", REPO / "tools" / "bench_inference.py")
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["bench"]
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    finally:
        sys.argv = argv
    return mod


class _Chunk:
    def __init__(self, content="", reasoning="", usage=None):
        self.content = content
        self.reasoning = reasoning
        self.usage = usage
        self.finish_reason = None


class _SlowClient:
    """Имитирует долгий prefill: keepalive-точки, затем настоящий ответ."""

    def chat_stream(self, messages, **kw):
        for _ in range(3):
            time.sleep(0.05)
            yield _Chunk(reasoning=".")      # keepalive, не ответ
        time.sleep(0.05)
        yield _Chunk(content="Ответ")        # вот теперь первый токен
        yield _Chunk(usage={"completion_tokens": 1})


def test_keepalive_not_counted_as_first_token():
    bench = _load_bench()
    result = bench.run_once(_SlowClient(), [{"role": "user", "content": "x"}], 8)
    # keepalive идут первые ~0.15 с; настоящий токен — позже
    assert result["ttft_s"] >= 0.15, f"keepalive засчитан за первый токен: {result}"
    assert result["completion_tokens"] == 1
