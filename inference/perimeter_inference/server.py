"""Супервизор локального inference-сервера.

Запускает вендореный OpenAI-совместимый сервер colibri
(vendor/colibri/c/openai_server.py, чистый stdlib) на loopback, либо
внешний llama.cpp `llama-server` как fallback — для core разницы нет:
оба говорят /v1/chat/completions. Наружу ничего не слушает и не ходит
(правило №0): host валидируется конфигом как loopback.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from perimeter_core import REPO_ROOT
from perimeter_core.config import InferenceConfig

COLIBRI_DIR = REPO_ROOT / "vendor" / "colibri" / "c"
LLAMACPP_DIR = REPO_ROOT / "vendor" / "llama.cpp"


class InferenceServerError(Exception):
    pass


class InferenceServer:
    def __init__(self, cfg: InferenceConfig, model_path: str | None = None):
        self.cfg = cfg
        raw_path = model_path or cfg.model_path
        # Сервер запускается с cwd движка, поэтому относительный путь к весам
        # там не разрешится — приводим к абсолютному от текущего каталога.
        self.model_path = str(Path(raw_path).resolve()) if raw_path else ""
        self.proc: subprocess.Popen[bytes] | None = None

    def _colibri_cmd(self) -> list[str]:
        server = COLIBRI_DIR / "openai_server.py"
        if not server.exists():
            raise InferenceServerError(f"vendored colibri server not found: {server}")
        return [
            sys.executable, str(server),
            "--host", self.cfg.host,
            "--port", str(self.cfg.port),
            "--model", self.model_path,
            "--model-id", self.cfg.model_id,
        ]

    def _llamacpp_cmd(self) -> list[str]:
        """Вендореный llama.cpp — основной бэкенд для рекомендуемых моделей."""
        binary = LLAMACPP_DIR / "build" / "bin" / "llama-server"
        if not binary.exists():
            raise InferenceServerError(
                f"llama-server не собран: {binary}. Запустите tools/install.sh")
        return [
            str(binary),
            "--model", self.model_path,
            "--host", self.cfg.host,
            "--port", str(self.cfg.port),
            "--alias", self.cfg.model_id,
            "--ctx-size", str(self.cfg.ctx_size),
            "--threads", str(self.cfg.threads or os.cpu_count() or 4),
            # --jinja: без него сервер не применяет шаблон чата модели и
            # не отдаёт tool-вызовы в формате OpenAI.
            "--jinja",
        ]

    def start(self, wait_ready_s: float = 600.0) -> None:
        if not self.model_path:
            raise InferenceServerError("model_path не задан (inference.model_path в perimeter.yaml)")
        cmd = self._colibri_cmd() if self.cfg.backend == "colibri" else self._llamacpp_cmd()
        env = dict(os.environ)
        env.setdefault("COLI_MODEL", self.model_path)
        self.proc = subprocess.Popen(cmd, cwd=str(COLIBRI_DIR), env=env)
        self._wait_ready(wait_ready_s)

    def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        url = f"{self.cfg.base_url}/health"
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise InferenceServerError(
                    f"inference-сервер завершился при старте (код {self.proc.returncode})")
            try:
                with urllib.request.urlopen(url, timeout=5):
                    return
            except (urllib.error.URLError, OSError):
                time.sleep(1.0)
        raise InferenceServerError(f"inference-сервер не поднялся за {timeout_s:.0f} с: {url}")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> "InferenceServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
