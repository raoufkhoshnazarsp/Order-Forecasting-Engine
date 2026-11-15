"""Last-run memory shared by the pages.

The three pages (run, comparison, static report) each open with their own
upload, so without this they would silently disagree about what was computed.
Every successful run records a small, non-sensitive summary here; the pages
read it back so one page can tell the user what the other page last produced.

Only metadata is stored — file name, origin, target, horizon, model, time.
No uploaded rows, no tokens.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from src import config

FILE_NAME = "last_run.json"


def state_path() -> Path:
    override = os.environ.get("FORECAST_STATE_DIR")
    if override:
        return Path(override) / FILE_NAME
    return config.PROJECT_ROOT / "artifacts" / "web_runs" / FILE_NAME


def record_run(kind: str, payload: dict) -> dict:
    """Persist the summary of a run. Never raises: a read-only disk must not
    break the forecast the user is waiting for."""
    entry = {"kind": kind, "at": datetime.now().isoformat(timespec="seconds")}
    entry.update({k: v for k, v in payload.items() if v is not None})
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return entry
    return entry


def last_run() -> dict | None:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
