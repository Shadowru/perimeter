"""Загрузка config/perimeter.yaml — единственного источника сетевых разрешений.

Пароль 1С в конфиге не хранится: только переменная окружения
PERIMETER_1C_PASSWORD или локальный keyring (secret-tool). Валидация:
хост из bridge_1c.base_url обязан входить в allowed_hosts.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import REPO_ROOT  # noqa: F401  (подключает vendor/ к sys.path)
import yaml

from .i18n import t

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
PASSWORD_ENV = "PERIMETER_1C_PASSWORD"
PASSWORD_PLACEHOLDER = "__FROM_ENV_OR_KEYRING__"


class ConfigError(Exception):
    pass


@dataclass
class InferenceConfig:
    host: str = "127.0.0.1"
    port: int = 8090
    backend: str = "colibri"
    model_path: str = ""
    ci_model_path: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class Bridge1CConfig:
    base_url: str = ""
    username: str = ""
    password: str = PASSWORD_PLACEHOLDER
    configuration: str = "bp30"
    timeout_s: int = 30
    retries: int = 3
    page_size: int = 200

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    def resolve_password(self) -> str:
        """Пароль: env → keyring (secret-tool) → ошибка. Конфиг — никогда."""
        env = os.environ.get(PASSWORD_ENV)
        if env:
            return env
        if self.password and self.password != PASSWORD_PLACEHOLDER:
            raise ConfigError(
                "Пароль в perimeter.yaml запрещён; используйте "
                f"{PASSWORD_ENV} или keyring (правило проекта о секретах)."
            )
        try:
            out = subprocess.run(
                ["secret-tool", "lookup", "service", "perimeter-1c", "user", self.username],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout:
                return out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise ConfigError(f"Пароль 1С не найден: задайте {PASSWORD_ENV} или secret-tool.")


@dataclass
class UIConfig:
    host: str = "127.0.0.1"
    port: int = 8091


@dataclass
class PerimeterConfig:
    allowed_hosts: list[str] = field(default_factory=list)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    bridge_1c: Bridge1CConfig = field(default_factory=Bridge1CConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    audit_log_path: str = "var/audit.log"
    locale: str = "ru"

    def validate(self) -> None:
        for section, host in (("inference", self.inference.host), ("ui", self.ui.host)):
            if host not in LOOPBACK_NAMES:
                raise ConfigError(f"{section}.host обязан быть loopback, получено: {host}")
        bridge_host = self.bridge_1c.host
        if bridge_host and bridge_host not in LOOPBACK_NAMES and bridge_host not in self.allowed_hosts:
            raise ConfigError(t("error.bridge_host_not_allowed", host=bridge_host))


def load_config(path: str | os.PathLike[str]) -> PerimeterConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(t("error.config_missing", path=str(p)))
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    cfg = PerimeterConfig(
        allowed_hosts=list(raw.get("allowed_hosts") or []),
        inference=InferenceConfig(**(raw.get("inference") or {})),
        bridge_1c=Bridge1CConfig(**(raw.get("bridge_1c") or {})),
        ui=UIConfig(**(raw.get("ui") or {})),
        audit_log_path=(raw.get("audit") or {}).get("log_path", "var/audit.log"),
        locale=raw.get("locale", "ru"),
    )
    cfg.validate()
    return cfg
