"""Сборка агента из config/perimeter.yaml (production-обвязка).

Порядок: конфиг → netguard (правило №0) → клиент inference → клиент 1С
(+ валидация маппинга) → инструменты + навыки → Agent с аудитом.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from . import REPO_ROOT, netguard
from .agent import Agent
from .audit import AuditLog
from .config import PerimeterConfig, load_config
from .i18n import set_locale
from .skills import catalog_text, load_skills, make_load_skill_tool

# компоненты-пакеты проекта (без установки)
for component in ("inference", "bridge-1c"):
    p = str(REPO_ROOT / component)
    if p not in sys.path:
        sys.path.insert(0, p)

from perimeter_bridge1c.mapping import load_mapping  # noqa: E402
from perimeter_bridge1c.odata import ODataClient  # noqa: E402
from perimeter_bridge1c.tools import Bridge1CTools  # noqa: E402
from perimeter_inference.client import InferenceClient  # noqa: E402


def build_agent(config_path: str | Path,
                confirm: Callable[[str, dict], bool]) -> tuple[Agent, PerimeterConfig]:
    cfg = load_config(config_path)
    set_locale(cfg.locale)

    audit = AuditLog(REPO_ROOT / cfg.audit_log_path)
    netguard.install(cfg.allowed_hosts,
                     on_violation=lambda host: audit.write("network_violation", host=host))

    mapping = load_mapping(cfg.bridge_1c.configuration)
    odata = ODataClient(
        cfg.bridge_1c.base_url, cfg.bridge_1c.username,
        cfg.bridge_1c.resolve_password(),
        timeout_s=cfg.bridge_1c.timeout_s, retries=cfg.bridge_1c.retries,
        page_size=cfg.bridge_1c.page_size, mapping=mapping,
    )
    problems = odata.validate_mapping()
    if problems:
        audit.write("mapping_problems", problems=problems)

    skills = load_skills()
    tool_specs = Bridge1CTools(odata, mapping).specs() + [make_load_skill_tool(skills)]

    agent = Agent(
        client=InferenceClient(cfg.inference.base_url, model=cfg.inference.model_id),
        tool_specs=tool_specs,
        audit=audit,
        confirm=confirm,
        locale=cfg.locale,
        extra_system=catalog_text(skills),
    )
    return agent, cfg
