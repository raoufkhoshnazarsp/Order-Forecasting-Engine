"""Entity Master: hierarchy loaded from config, never hardcoded."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src import config

ENTITY_TYPES = ("TOTAL", "VERTICAL", "VENTURE")


class EntityMasterError(ValueError):
    """Raised when the entity master is inconsistent."""


@dataclass(frozen=True)
class Entity:
    entity_id: str
    entity_name: str
    entity_type: str
    parent_entity_id: str | None
    is_active: bool
    valid_from: str | None
    valid_to: str | None


class EntityMaster:
    """Validated view over the entity master CSV."""

    def __init__(self, frame: pd.DataFrame):
        self._frame = frame
        self._entities = self._parse(frame)
        self._validate()

    @classmethod
    def load(cls, path: str | Path | None = None) -> "EntityMaster":
        hierarchy = config.load_yaml("hierarchy.yaml")
        csv_path = Path(path or hierarchy["entity_master_path"])
        if not csv_path.is_absolute():
            csv_path = config.PROJECT_ROOT / csv_path
        if not csv_path.exists():
            raise EntityMasterError(f"Entity master not found: {csv_path}")
        frame = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        frame.columns = [c.strip() for c in frame.columns]
        return cls(frame)

    @staticmethod
    def _parse(frame: pd.DataFrame) -> list[Entity]:
        required = {
            "entity_id",
            "entity_name",
            "entity_type",
            "parent_entity_id",
            "is_active",
            "valid_from",
            "valid_to",
        }
        missing = required - set(frame.columns)
        if missing:
            raise EntityMasterError(f"Entity master missing columns: {sorted(missing)}")
        entities: list[Entity] = []
        for row in frame.to_dict("records"):
            entities.append(
                Entity(
                    entity_id=str(row["entity_id"]).strip(),
                    entity_name=str(row["entity_name"]).strip(),
                    entity_type=str(row["entity_type"]).strip().upper(),
                    parent_entity_id=(str(row["parent_entity_id"]).strip() or None),
                    is_active=str(row["is_active"]).strip().lower() in {"true", "1", "yes"},
                    valid_from=str(row["valid_from"]).strip() or None,
                    valid_to=str(row["valid_to"]).strip() or None,
                )
            )
        return entities

    def _validate(self) -> None:
        ids = [e.entity_id for e in self._entities]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise EntityMasterError(f"Duplicate entity_id: {sorted(duplicates)}")
        known = set(ids)
        for entity in self._entities:
            if not entity.entity_id:
                raise EntityMasterError("Empty entity_id in entity master")
            if entity.entity_type not in ENTITY_TYPES:
                raise EntityMasterError(
                    f"Invalid entity_type {entity.entity_type!r} for {entity.entity_id}"
                )
            if entity.entity_type == "TOTAL":
                if entity.parent_entity_id is not None:
                    raise EntityMasterError("TOTAL entity must have no parent")
            else:
                if entity.parent_entity_id not in known:
                    raise EntityMasterError(
                        f"Unknown parent {entity.parent_entity_id!r} for {entity.entity_id}"
                    )
        if not self.totals():
            raise EntityMasterError("Entity master must contain exactly one TOTAL entity")
        leaves = {e.entity_id for e in self.leaves()}
        if len(leaves) != len(self.leaves()):
            raise EntityMasterError("Duplicate leaf entity")

    # -- accessors ------------------------------------------------------
    @property
    def entities(self) -> list[Entity]:
        return list(self._entities)

    def totals(self) -> list[Entity]:
        return [e for e in self._entities if e.entity_type == "TOTAL"]

    def leaves(self, active_only: bool = True) -> list[Entity]:
        return [
            e
            for e in self._entities
            if e.entity_type in ("VERTICAL", "VENTURE") and (e.is_active or not active_only)
        ]

    def by_id(self, entity_id: str) -> Entity | None:
        return next((e for e in self._entities if e.entity_id == entity_id), None)

    def total_id(self) -> str:
        totals = self.totals()
        if len(totals) != 1:
            raise EntityMasterError("Exactly one TOTAL entity is required")
        return totals[0].entity_id

    def leaf_ids(self, active_only: bool = True) -> list[str]:
        return [e.entity_id for e in self.leaves(active_only=active_only)]
