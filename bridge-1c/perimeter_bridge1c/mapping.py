"""Загрузка маппинга сущностей 1С (mappings/*.yaml).

Единственный источник имён метаданных 1С — YAML-файлы; код оперирует
логическими именами (counterparty, sale, …).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from perimeter_core import REPO_ROOT  # noqa: F401  (vendor/ в sys.path)
import yaml

MAPPINGS_DIR = Path(__file__).parent / "mappings"


class MappingError(Exception):
    pass


@dataclass
class EntityMapping:
    logical_name: str
    entity_set: str
    fields: dict[str, str] = field(default_factory=dict)
    rows: str | None = None            # основная часть (для черновиков)
    # Все части со строками. В БП 3.0 у реализации их две — «Товары» и
    # «Услуги», и в базах с услугами первая пуста (живая база 2026-07-31).
    row_sections: list[str] = field(default_factory=list)
    # Колонки табличной части: логическое имя -> имя в 1С. Отдельно от fields,
    # потому что имена в шапке и в строках совпадать не обязаны.
    row_fields: dict[str, str] = field(default_factory=dict)


    def row_field(self, logical: str, default: str | None = None) -> str:
        """Имя колонки табличной части. Без него отчёты по строкам не строятся."""
        name = self.row_fields.get(logical, default)
        if not name:
            raise MappingError(
                f"колонка «{logical}» табличной части не описана в маппинге")
        return name

    def field_1c(self, logical: str) -> str:
        try:
            return self.fields[logical]
        except KeyError:
            raise MappingError(
                f"поле «{logical}» не описано в маппинге {self.logical_name}") from None


@dataclass
class ConfigurationMapping:
    configuration: str
    display_name: str
    entities: dict[str, EntityMapping]

    def entity(self, logical: str) -> EntityMapping:
        try:
            return self.entities[logical]
        except KeyError:
            raise MappingError(
                f"сущность «{logical}» не описана в маппинге {self.configuration}") from None


def load_mapping(configuration: str) -> ConfigurationMapping:
    path = MAPPINGS_DIR / f"{configuration}.yaml"
    if not path.exists():
        known = sorted(p.stem for p in MAPPINGS_DIR.glob("*.yaml"))
        raise MappingError(f"нет маппинга «{configuration}»; доступны: {known}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entities = {
        name: EntityMapping(
            logical_name=name,
            entity_set=spec["entity_set"],
            fields=spec.get("fields") or {},
            rows=(spec.get("rows") if isinstance(spec.get("rows"), str)
                  else (spec.get("rows") or [None])[0]),
            row_sections=([spec["rows"]] if isinstance(spec.get("rows"), str)
                          else list(spec.get("rows") or [])),
            row_fields=dict(spec.get("row_fields") or {}),
        )
        for name, spec in (raw.get("entities") or {}).items()
    }
    return ConfigurationMapping(
        configuration=raw["configuration"],
        display_name=raw.get("display_name", configuration),
        entities=entities,
    )
