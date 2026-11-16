"""One upload, one session, many views.

The old flow asked the user to upload the same workbook twice — once for the
daily forecast and again for the monthly dashboard — and every endpoint re-read
and re-validated the file. A *run* fixes that: the workbook is saved once, read
once, checked once, and every later view (forecast, comparison, sensitivity,
delivery) answers from the same session.

What lives on disk under ``<state>/runs/<run_id>/``:

* ``run.json``       — metadata, quality report, the step that has completed
* ``<upload>.xlsx``  — the exact bytes the user reviewed (the quality token is
                       the hash of these bytes, so a different file can never
                       ride on an earlier approval)
* ``forecast.json``  — the forecast payload after the token was approved
* ``comparison.json``— the monthly dashboard payload
* ``package.xlsx``   — the delivery workbook, built once and downloaded later

Payloads are pruned before save (no pandas objects, no NaN) so a corrupt entry
degrades to "not computed yet" instead of a 500.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from src import config

RUNS_DIR = "runs"
MAX_RUNS = 40
STAMP_RE = re.compile(r"^\d{8}_\d{6}_")
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
STEPS = ("data", "quality", "forecast", "backtest", "sensitivity", "monthly", "package")


class RunError(LookupError):
    """The run does not exist or its directory was pruned."""


class RunStateError(ValueError):
    """The requested step is not valid for the run's current state."""


def runs_root() -> Path:
    override = os.environ.get("FORECAST_STATE_DIR")
    base = Path(override) if override else config.PROJECT_ROOT / "artifacts" / "web_runs"
    return base / RUNS_DIR


def _safe_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise RunError("شناسهٔ اجرا نامعتبر است.")
    return run_id


def run_dir(run_id: str) -> Path:
    return runs_root() / _safe_id(run_id)


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def _json_safe(value):
    """Make a payload storable: drop NaN, unwrap numpy, keep plain structures."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return _json_safe(value.item())
        except (ValueError, TypeError):
            return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def display_name(path: Path) -> str:
    """Undo the upload timestamp prefix so the UI shows the user's own name."""
    return STAMP_RE.sub("", path.name)


def create(workbook: Path, *, file_name: str, **meta) -> dict:
    """Open a run for an already-saved upload and prune old sessions."""
    run_id = new_run_id()
    target = runs_root() / run_id
    target.mkdir(parents=True, exist_ok=True)
    stored = target / workbook.name
    shutil.copyfile(workbook, stored)

    record = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "file": file_name,
        "stored": stored.name,
        "steps": {step: False for step in STEPS},
    }
    record.update(meta)
    _write_json(target / "run.json", record)
    _prune(keep=run_id)
    return record


def load(run_id: str) -> dict:
    record = _read_json(run_dir(run_id) / "run.json")
    if not isinstance(record, dict):
        raise RunError("این اجرا پیدا نشد یا حذف شده است.")
    return record


def update(run_id: str, **changes) -> dict:
    record = load(run_id)
    record.update(changes)
    _write_json(run_dir(run_id) / "run.json", record)
    return record


def mark(run_id: str, step: str) -> dict:
    record = load(run_id)
    steps = dict(record.get("steps") or {})
    steps[step] = True
    return update(run_id, steps=steps)


def workbook_path(record: dict) -> Path:
    path = run_dir(record["run_id"]) / record["stored"]
    if not path.exists():
        raise RunError("فایل این اجرا در دسترس نیست؛ دوباره بارگذاری کنید.")
    return path


def save_view(run_id: str, name: str, payload) -> None:
    _write_json(run_dir(run_id) / f"{name}.json", payload)


def load_view(run_id: str, name: str):
    return _read_json(run_dir(run_id) / f"{name}.json")


def save_package(run_id: str, data: bytes) -> Path:
    path = run_dir(run_id) / "package.xlsx"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def package_path(run_id: str) -> Path:
    path = run_dir(run_id) / "package.xlsx"
    if not path.exists():
        raise RunError("بستهٔ تحویل برای این اجرا ساخته نشده است.")
    return path


def list_runs(limit: int = 20) -> list[dict]:
    root = runs_root()
    if not root.is_dir():
        return []
    records = []
    for folder in root.iterdir():
        if not folder.is_dir() or not RUN_ID_RE.match(folder.name):
            continue
        record = _read_json(folder / "run.json")
        if not isinstance(record, dict):
            continue
        records.append(
            {
                "run_id": record.get("run_id"),
                "created_at": record.get("created_at"),
                "file": record.get("file"),
                "origin": record.get("origin"),
                "target": record.get("target"),
                "horizon": record.get("horizon"),
                "model": record.get("model"),
                "steps": record.get("steps") or {},
            }
        )
    records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return records[: max(1, limit)]


def session_payload(record: dict) -> dict:
    """What the UI needs to restore a run after a refresh."""
    run_id = record["run_id"]
    forecast = load_view(run_id, "forecast")
    comparison = load_view(run_id, "comparison")
    sensitivity = load_view(run_id, "sensitivity")
    return {
        "run": {
            "run_id": run_id,
            "created_at": record.get("created_at"),
            "file": record.get("file"),
            "origin": record.get("origin"),
            "target": record.get("target"),
            "horizon": record.get("horizon"),
            "model": record.get("model"),
            "steps": record.get("steps") or {},
        },
        "quality": record.get("quality"),
        "preview": record.get("preview"),
        "forecast": forecast,
        "comparison": comparison,
        "sensitivity": sensitivity,
        "has_package": (run_dir(run_id) / "package.xlsx").exists(),
    }


def _prune(keep: str) -> None:
    """Keep the newest sessions; a full disk must never break a live run."""
    root = runs_root()
    try:
        folders = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return
    folders.sort(key=lambda p: p.name, reverse=True)
    for folder in folders[MAX_RUNS:]:
        if folder.name == keep:
            continue
        shutil.rmtree(folder, ignore_errors=True)
