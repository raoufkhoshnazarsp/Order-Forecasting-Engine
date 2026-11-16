"""Monitoring: is the forecast still trustworthy, and did the run actually happen?

Three questions this module answers, in the order a person would ask them:

1. **Is the data fresh?** A forecast built from a week-old mart is not a
   forecast, it is a historical reprint. Staleness is measured against the
   expected cadence (daily), not against a hardcoded date.
2. **Has the model drifted?** Once actuals arrive, the share of days inside the
   90% band and the recent WAPE tell you whether the calibration still holds.
   A band that used to cover 90% and now covers 60% is the earliest visible
   symptom of drift.
3. **Did the job run?** A scheduled job that silently stops is worse than one
   that fails loudly, so the last successful run is recorded and its age is
   checked against the schedule.

Nothing here raises on a bad state. Monitoring is advisory: it reports the
state so a human can act, and never blocks the pipeline it is watching.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd

from src import config

STALE_DAYS_WARN = 2
STALE_DAYS_FAIL = 4
COVERAGE_WARN = 0.80
DRIFT_WAPE_WARN = 0.30
JOB_MAX_AGE_HOURS = 36


@dataclass(frozen=True)
class Alert:
    level: str  # "info" | "warn" | "error"
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class HealthReport:
    as_of: str | None
    data: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    job: dict = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)

    @property
    def status(self) -> str:
        levels = {alert.level for alert in self.alerts}
        if "error" in levels:
            return "error"
        if "warn" in levels:
            return "warn"
        return "ok"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "data": self.data,
            "model": self.model,
            "job": self.job,
            "alerts": [alert.as_dict() for alert in self.alerts],
        }


# ------------------------------------------------------------- data freshness
def data_freshness(
    frame: pd.DataFrame,
    *,
    reference: object | None = None,
    warn_days: int = STALE_DAYS_WARN,
    fail_days: int = STALE_DAYS_FAIL,
) -> dict:
    """How many days behind is the newest observation?

    ``reference`` is "today" by default. It is a parameter so the check can be
    tested without freezing the clock, and so a backfill can be judged against
    its own target date rather than the wall clock.
    """
    if frame is None or frame.empty or "date" not in frame.columns:
        return {"stale_days": None, "latest": None, "state": "unknown"}
    dates = pd.to_datetime(frame["date"]).dropna()
    if dates.empty:
        return {"stale_days": None, "latest": None, "state": "unknown"}
    latest = dates.max().date()
    today = (
        pd.Timestamp(reference).date()
        if reference is not None
        else date.today()
    )
    stale = (today - latest).days
    if stale >= fail_days:
        state = "stale"
    elif stale >= warn_days:
        state = "late"
    else:
        state = "fresh"
    return {
        "stale_days": int(stale),
        "latest": latest.isoformat(),
        "state": state,
    }


def completeness(frame: pd.DataFrame) -> dict:
    """Share of expected leaf-days that actually have an observation.

    A gap in the mart is not a quiet day: it silently becomes a smaller total.
    This counts leaf-days between the first and last date that have no value.
    """
    if frame is None or frame.empty:
        return {"leaf_days": 0, "missing": 0, "missing_share": 0.0}
    required = {"date", "entity_id", "successful_orders"}
    if not required.issubset(frame.columns):
        return {"leaf_days": 0, "missing": 0, "missing_share": 0.0}
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"])
    leaves = work[work["entity_id"].astype(object).map(str) != "TOTAL"]
    span_days = work["date"].nunique()
    leaf_ids = leaves["entity_id"].nunique()
    expected = span_days * leaf_ids
    observed = leaves.dropna(subset=["successful_orders"]).groupby(
        ["date", "entity_id"]
    ).size().shape[0]
    missing = max(expected - observed, 0)
    return {
        "leaf_days": int(expected),
        "missing": int(missing),
        "missing_share": float(missing / expected) if expected else 0.0,
    }


# -------------------------------------------------------------- model drift
def coverage_drift(
    backtest_frame: pd.DataFrame,
    *,
    level: float = 0.90,
    recent_share: float = 0.30,
) -> dict:
    """Compare the interval coverage of recent origins to the full history.

    A stable model keeps roughly the same coverage over time. A drop means the
    calibration no longer describes the current regime, which the aggregate
    number over all origins hides.
    """
    if backtest_frame is None or backtest_frame.empty:
        return {"coverage": None, "recent_coverage": None, "state": "unknown"}
    required = {"forecast_origin", "actual", "yhat", "lower_90", "upper_90"}
    if not required.issubset(backtest_frame.columns):
        return {"coverage": None, "recent_coverage": None, "state": "unknown"}

    frame = backtest_frame.dropna(subset=["actual", "yhat"]).copy()
    if frame.empty:
        return {"coverage": None, "recent_coverage": None, "state": "unknown"}

    def _coverage(chunk: pd.DataFrame) -> float | None:
        if chunk.empty:
            return None
        inside = (chunk["actual"] >= chunk["lower_90"]) & (
            chunk["actual"] <= chunk["upper_90"]
        )
        return float(inside.mean())

    overall = _coverage(frame)
    origins = sorted(frame["forecast_origin"].unique())
    if len(origins) < 2:
        # With a single origin there is no "recent vs old" to compare; the
        # honest answer is the overall number, not a null that hides drift.
        recent = overall
    else:
        cutoff = max(int(len(origins) * (1.0 - recent_share)), 1)
        recent = _coverage(frame[frame["forecast_origin"].isin(origins[cutoff:])])

    state = "stable"
    if recent is not None and recent < COVERAGE_WARN:
        state = "undercovering"
    return {
        "coverage": overall,
        "recent_coverage": recent,
        "target": level,
        "state": state,
    }


def recent_error(
    backtest_frame: pd.DataFrame, *, horizon: int | None = None, recent_share: float = 0.30
) -> dict:
    """WAPE on the most recent origins, so a slow regression becomes visible."""
    if backtest_frame is None or backtest_frame.empty:
        return {"recent_wape": None, "wape": None, "state": "unknown"}
    required = {"forecast_origin", "actual", "yhat"}
    if not required.issubset(backtest_frame.columns):
        return {"recent_wape": None, "wape": None, "state": "unknown"}
    frame = backtest_frame.dropna(subset=["actual", "yhat"])
    if horizon is not None and "horizon" in frame.columns:
        frame = frame[frame["horizon"] <= horizon]
    if frame.empty:
        return {"recent_wape": None, "wape": None, "state": "unknown"}

    origins = sorted(frame["forecast_origin"].unique())
    if len(origins) < 2:
        recent = frame
    else:
        cutoff = max(int(len(origins) * (1.0 - recent_share)), 1)
        recent = frame[frame["forecast_origin"].isin(origins[cutoff:])]

    def _wape(chunk: pd.DataFrame) -> float | None:
        denom = float(chunk["actual"].abs().sum())
        if denom <= 0:
            return None
        return float((chunk["actual"] - chunk["yhat"]).abs().sum() / denom)

    recent_wape = _wape(recent) if not recent.empty else None
    state = "ok"
    if recent_wape is not None and recent_wape > DRIFT_WAPE_WARN:
        state = "degraded"
    return {"wape": _wape(frame), "recent_wape": recent_wape, "state": state}


# ---------------------------------------------------------------- job health
def job_state_path() -> Path:
    override = os.environ.get("FORECAST_STATE_DIR")
    base = (Path(override) if override else config.PROJECT_ROOT / "artifacts") / "monitoring"
    return base / "job_runs.json"


def record_job_run(payload: dict, *, path: Path | None = None) -> dict:
    """Append one run to the job log, keeping the last 200 entries."""
    entry = {"at": datetime.now().isoformat(timespec="seconds")}
    entry.update({k: v for k, v in payload.items() if v is not None})
    target = path or job_state_path()
    history: list[dict] = []
    try:
        if target.exists():
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = loaded
    except (OSError, ValueError):
        history = []
    history.append(entry)
    history = history[-200:]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass
    return entry


def job_health(*, now: datetime | None = None, max_age_hours: int = JOB_MAX_AGE_HOURS) -> dict:
    """Age of the newest successful run; a stopped job is a found bug."""
    path = job_state_path()
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_success": None, "age_hours": None, "state": "unknown"}
    if not isinstance(history, list) or not history:
        return {"last_success": None, "age_hours": None, "state": "unknown"}

    successes = [entry for entry in history if entry.get("status") == "success"]
    if not successes:
        return {"last_success": None, "age_hours": None, "state": "failing"}
    latest = successes[-1]
    try:
        stamp = datetime.fromisoformat(str(latest["at"]))
    except (KeyError, ValueError):
        return {"last_success": None, "age_hours": None, "state": "unknown"}
    current = now or datetime.now(stamp.tzinfo)
    age = (current - stamp).total_seconds() / 3600.0
    state = "ok" if age <= max_age_hours else "late"
    return {
        "last_success": str(latest["at"]),
        "age_hours": float(round(age, 1)),
        "state": state,
        "runs_logged": len(history),
    }


# ------------------------------------------------------------------- report
def health_report(
    mart: pd.DataFrame | None = None,
    *,
    backtest_frame: pd.DataFrame | None = None,
    reference: object | None = None,
    now: datetime | None = None,
) -> HealthReport:
    """Combine the three checks into one verdict with explicit alerts."""
    freshness = data_freshness(mart, reference=reference) if mart is not None else {
        "stale_days": None,
        "latest": None,
        "state": "unknown",
    }
    gaps = completeness(mart) if mart is not None else {
        "leaf_days": 0,
        "missing": 0,
        "missing_share": 0.0,
    }
    coverage = coverage_drift(backtest_frame) if backtest_frame is not None else {
        "coverage": None,
        "recent_coverage": None,
        "state": "unknown",
    }
    error = recent_error(backtest_frame) if backtest_frame is not None else {
        "recent_wape": None,
        "wape": None,
        "state": "unknown",
    }
    job = job_health(now=now)

    alerts: list[Alert] = []
    if freshness["state"] == "stale":
        alerts.append(
            Alert(
                "error",
                "data_stale",
                f"آخرین داده {freshness['stale_days']} روز قدیمی است "
                f"({freshness['latest']}). پیش‌بینی از دادهٔ کهنه ساخته می‌شود.",
            )
        )
    elif freshness["state"] == "late":
        alerts.append(
            Alert(
                "warn",
                "data_late",
                f"آخرین داده {freshness['stale_days']} روز عقب است ({freshness['latest']}).",
            )
        )
    if gaps["missing_share"] > 0.05:
        alerts.append(
            Alert(
                "warn",
                "data_gaps",
                f"{gaps['missing']} روز-موجودیت از {gaps['leaf_days']} خالی است "
                f"({gaps['missing_share'] * 100:.1f}٪).",
            )
        )
    if coverage["state"] == "undercovering":
        alerts.append(
            Alert(
                "warn",
                "coverage_drift",
                "پوشش بازهٔ ۹۰٪ در مبدأهای اخیر به "
                f"{coverage['recent_coverage'] * 100:.1f}٪ افت کرده است.",
            )
        )
    if error["state"] == "degraded":
        alerts.append(
            Alert(
                "warn",
                "error_drift",
                f"WAPE مبدأهای اخیر {error['recent_wape'] * 100:.1f}٪ است.",
            )
        )
    if job["state"] == "late":
        alerts.append(
            Alert(
                "warn",
                "job_late",
                f"آخرین اجرای موفق {job['age_hours']} ساعت پیش بوده است.",
            )
        )
    elif job["state"] == "failing":
        alerts.append(
            Alert("error", "job_failing", "هیچ اجرای موفقی در تاریخچهٔ کار ثبت نشده است.")
        )

    return HealthReport(
        as_of=(
            pd.Timestamp(reference).date().isoformat()
            if reference is not None
            else date.today().isoformat()
        ),
        data={"freshness": freshness, **gaps},
        model={"coverage": coverage, "error": error},
        job=job,
        alerts=alerts,
    )


def render_prometheus(report: HealthReport) -> str:
    """Minimal exposition format, so an external monitor can scrape it."""
    lines = [
        "# HELP forecast_health_status 0=ok 1=warn 2=error",
        "# TYPE forecast_health_status gauge",
        "forecast_health_status "
        + {"ok": "0", "warn": "1", "error": "2"}.get(report.status, "-1"),
        "# HELP forecast_data_stale_days days since the newest observation",
        "# TYPE forecast_data_stale_days gauge",
        "forecast_data_stale_days "
        + _num(report.data.get("freshness", {}).get("stale_days")),
        "# HELP forecast_recent_wape WAPE on the most recent origins",
        "# TYPE forecast_recent_wape gauge",
        "forecast_recent_wape " + _num(report.model.get("error", {}).get("recent_wape")),
        "# HELP forecast_recent_coverage share of actuals inside the 90%% band",
        "# TYPE forecast_recent_coverage gauge",
        "forecast_recent_coverage "
        + _num(report.model.get("coverage", {}).get("recent_coverage")),
        "# HELP forecast_job_age_hours hours since the last successful run",
        "# TYPE forecast_job_age_hours gauge",
        "forecast_job_age_hours " + _num(report.job.get("age_hours")),
    ]
    return "\n".join(lines) + "\n"


def _num(value) -> str:
    if value is None:
        return "NaN"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "NaN"
    return "NaN" if not np.isfinite(number) else f"{number:.6g}"
