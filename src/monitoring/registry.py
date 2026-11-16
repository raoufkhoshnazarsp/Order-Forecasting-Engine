"""Model registry: what produced a forecast, and how to go back to the last good one.

A run is only reproducible if the thing that produced it can be named and
restored. This registry keeps an append-only log of model versions and a
``current`` pointer; rolling back moves the pointer to an earlier *successful*
version rather than deleting anything, so the bad version stays visible in the
history.

What is stored is metadata only: version id, backend, the metrics that made it
eligible, and the run it was promoted from. The fitted weights are not stored —
they can be refit from the same workbook, and storing them would silently pin a
model to old data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src import config

REGISTRY_FILE = "model_registry.json"


class RegistryError(ValueError):
    """Raised when a promotion or rollback would leave an invalid state."""


@dataclass(frozen=True)
class ModelVersion:
    version_id: str
    model_name: str
    backend: str
    created_at: str
    run_id: str
    metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"  # candidate | active | retired
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "model_name": self.model_name,
            "backend": self.backend,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "metrics": self.metrics,
            "status": self.status,
            "note": self.note,
        }


def registry_path() -> Path:
    override = os.environ.get("FORECAST_STATE_DIR")
    base = (Path(override) if override else config.PROJECT_ROOT / "artifacts") / "monitoring"
    return base / REGISTRY_FILE


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"current": None, "versions": []}
    if not isinstance(data, dict):
        return {"current": None, "versions": []}
    data.setdefault("current", None)
    data.setdefault("versions", [])
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _version_id(model_name: str, run_id: str, stamp: datetime) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in model_name)
    return f"{clean}_{stamp:%Y%m%d%H%M%S}_{run_id[-6:]}"


def register_version(
    *,
    model_name: str,
    backend: str,
    run_id: str,
    metrics: dict[str, Any] | None = None,
    note: str = "",
    path: Path | None = None,
    now: datetime | None = None,
) -> ModelVersion:
    """Append a candidate version. Registering never changes ``current``."""
    if not model_name:
        raise RegistryError("A model version needs a model name")
    target = path or registry_path()
    stamp = now or datetime.now()
    version = ModelVersion(
        version_id=_version_id(model_name, run_id, stamp),
        model_name=str(model_name),
        backend=str(backend),
        created_at=stamp.isoformat(timespec="seconds"),
        run_id=str(run_id),
        metrics=dict(metrics or {}),
        status="candidate",
        note=note,
    )
    data = _load(target)
    if any(v.get("version_id") == version.version_id for v in data["versions"]):
        raise RegistryError(f"Duplicate version id {version.version_id}")
    data["versions"].append(version.as_dict())
    _save(target, data)
    return version


def promote(
    version_id: str | None = None, *, path: Path | None = None
) -> ModelVersion:
    """Make a version current. Without an id, the newest candidate is promoted."""
    target = path or registry_path()
    data = _load(target)
    versions = data["versions"]
    if not versions:
        raise RegistryError("The registry holds no versions to promote")

    if version_id is None:
        candidates = [v for v in versions if v.get("status") == "candidate"]
        if not candidates:
            raise RegistryError("No candidate version to promote")
        chosen = candidates[-1]
    else:
        matches = [v for v in versions if v.get("version_id") == version_id]
        if not matches:
            raise RegistryError(f"Unknown version {version_id!r}")
        chosen = matches[0]

    for entry in versions:
        if entry.get("version_id") == chosen["version_id"]:
            entry["status"] = "active"
        elif entry.get("status") == "active":
            entry["status"] = "retired"
    data["current"] = chosen["version_id"]
    _save(target, data)
    return ModelVersion(**chosen)


def rollback(*, path: Path | None = None) -> ModelVersion:
    """Return to the newest version that was active before the current one.

    Only versions that were actually promoted qualify. Rolling back to a
    never-promoted candidate would swap one untested model for another, which is
    the opposite of what a rollback is for.
    """
    target = path or registry_path()
    data = _load(target)
    versions = data["versions"]
    current_id = data.get("current")
    if current_id is None:
        raise RegistryError("Nothing is active; there is nothing to roll back from")

    promoted = [
        v
        for v in versions
        if v.get("status") in {"active", "retired"}
        and v.get("version_id") != current_id
    ]
    if not promoted:
        raise RegistryError("No previously active version to roll back to")

    promoted.sort(key=lambda v: v.get("created_at", ""))
    previous = promoted[-1]
    for entry in versions:
        if entry.get("version_id") == previous["version_id"]:
            entry["status"] = "active"
        elif entry.get("version_id") == current_id:
            entry["status"] = "retired"
    data["current"] = previous["version_id"]
    _save(target, data)
    return ModelVersion(**previous)


def current_version(*, path: Path | None = None) -> dict | None:
    """The active version, plus a warning if there is none."""
    data = _load(path or registry_path())
    current_id = data.get("current")
    if current_id is None:
        return None
    for entry in data["versions"]:
        if entry.get("version_id") == current_id:
            return entry
    return None


def history(*, path: Path | None = None) -> list[dict]:
    data = _load(path or registry_path())
    return sorted(data["versions"], key=lambda v: v.get("created_at", ""), reverse=True)


def record_run_version(
    *,
    model_name: str,
    backend: str,
    run_id: str,
    metrics: dict[str, Any] | None = None,
    note: str = "",
    path: Path | None = None,
) -> dict:
    """Register a run's model and promote it if it is first or not worse.

    The rule is deliberately simple and stated in the note: a new version is
    promoted only when there is nothing active yet, or when its backtest WAPE is
    at least as good as the active version's. A regression stays a candidate so
    it is visible, but it does not silently become the model the dashboard shows.
    """
    version = register_version(
        model_name=model_name,
        backend=backend,
        run_id=run_id,
        metrics=metrics,
        note=note,
        path=path,
    )
    active = current_version(path=path)
    candidate_wape = (metrics or {}).get("wape")
    active_wape = (active or {}).get("metrics", {}).get("wape")

    promoted = False
    if active is None:
        promote(version.version_id, path=path)
        promoted = True
    elif candidate_wape is not None and active_wape is not None:
        if float(candidate_wape) <= float(active_wape):
            promote(version.version_id, path=path)
            promoted = True
    return {
        "version_id": version.version_id,
        "promoted": promoted,
        "active_version": (current_version(path=path) or {}).get("version_id"),
    }
