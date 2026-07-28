"""Навыки: папка skills/<имя>/SKILL.md (frontmatter + markdown-инструкция).

Паттерн openworker (прогрессивная подгрузка): в системный промпт попадает
только каталог «имя: описание» (экономия prefill), полный текст навыка
модель получает инструментом load_skill по мере надобности.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import REPO_ROOT
from .toolspec import ToolSpec

DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str


def load_skills(skills_dir: Path | None = None) -> dict[str, Skill]:
    base = skills_dir or DEFAULT_SKILLS_DIR
    skills: dict[str, Skill] = {}
    if not base.exists():
        return skills
    for md in sorted(base.glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        name, description = md.parent.name, ""
        m = _FRONTMATTER_RE.match(text)
        body = text[m.end():].strip() if m else text.strip()
        if m:
            for line in m.group(1).splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "name" and value.strip():
                    name = value.strip()
                elif key.strip() == "description":
                    description = value.strip()
        skills[name] = Skill(name=name, description=description, body=body)
    return skills


def catalog_text(skills: dict[str, Skill]) -> str:
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills.values()]
    return "Доступные навыки (инструкция — через load_skill):\n" + "\n".join(lines)


def make_load_skill_tool(skills: dict[str, Skill]) -> ToolSpec:
    def load_skill(name: str) -> str:
        skill = skills.get(name)
        if skill is None:
            return f"Нет навыка «{name}». Доступны: {', '.join(skills) or '—'}."
        return skill.body

    return ToolSpec(
        name="load_skill",
        description="Получить инструкцию навыка по имени из каталога.",
        parameters={"type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"]},
        func=load_skill,
    )
