"""Интеграционный smoke: вендореный движок + OpenAI-сервер + наш клиент.

Требует собранного бинаря (make portable) и фикстуры glm_tiny
(vendor/colibri/c/tools/make_glm_oracle.py + tokenizer). Без них — skip,
как в upstream: CI собирает бинарь, фикстура закоммичена.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from perimeter_core.config import InferenceConfig
from perimeter_inference.client import InferenceClient
from perimeter_inference.server import COLIBRI_DIR, InferenceServer

ENGINE = COLIBRI_DIR / "colibri"
TINY = COLIBRI_DIR / "glm_tiny"

pytestmark = pytest.mark.skipif(
    not (ENGINE.exists() and (TINY / "tokenizer.json").exists()),
    reason="нужен собранный движок (make portable) и фикстура glm_tiny",
)


@pytest.fixture(scope="module")
def server():
    cfg = InferenceConfig(port=18091)
    srv = InferenceServer(cfg, model_path=str(TINY))
    srv.start(wait_ready_s=120)
    yield cfg
    srv.stop()


def test_streaming_generation(server):
    client = InferenceClient(server.base_url, model=server.model_id)
    chunks = list(client.chat_stream(
        [{"role": "user", "content": "hi"}], max_tokens=8, temperature=0.0))
    usage = next((c.usage for c in chunks if c.usage), None)
    assert usage and usage["completion_tokens"] > 0
    assert any(c.finish_reason for c in chunks)


def test_non_streaming_generation(server):
    client = InferenceClient(server.base_url, model=server.model_id)
    result = client.chat([{"role": "user", "content": "hi"}], max_tokens=8, temperature=0.0)
    assert result.usage and result.usage["completion_tokens"] > 0


def test_health(server):
    assert InferenceClient(server.base_url).health()
