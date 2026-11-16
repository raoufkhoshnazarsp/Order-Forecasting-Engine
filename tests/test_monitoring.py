"""Tests: monitoring signals and the model registry.

Monitoring that is wrong is worse than no monitoring: a false "ok" hides a
stale mart, and a rollback that lands on a never-promoted candidate swaps one
untested model for another. These tests pin the states that matter.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from src.monitoring.health import (
    DRIFT_WAPE_WARN,
    JOB_MAX_AGE_HOURS,
    completeness,
    coverage_drift,
    data_freshness,
    health_report,
    job_health,
    recent_error,
    record_job_run,
    render_prometheus,
)
from src.monitoring.registry import (
    RegistryError,
    current_version,
    history,
    promote,
    record_run_version,
    register_version,
    rollback,
)


# --------------------------------------------------------------- freshness
def mart(days: int = 10, end: str = "2026-11-15") -> pd.DataFrame:
    rows = []
    last = pd.Timestamp(end)
    for offset in range(days):
        day = last - pd.Timedelta(days=days - 1 - offset)
        for entity_id in ("FASHION", "GOLD"):
            rows.append(
                {"date": day, "entity_id": entity_id, "successful_orders": 100 + offset}
            )
    return pd.DataFrame(rows)


def test_fresh_data_is_fresh():
    result = data_freshness(mart(), reference=date(2026, 11, 15))
    assert result["stale_days"] == 0
    assert result["state"] == "fresh"
    assert result["latest"] == "2026-11-15"


def test_late_data_warns_before_it_fails():
    assert data_freshness(mart(), reference=date(2026, 11, 17))["state"] == "late"
    assert data_freshness(mart(), reference=date(2026, 11, 20))["state"] == "stale"


def test_empty_mart_is_unknown_not_fresh():
    assert data_freshness(pd.DataFrame())["state"] == "unknown"


def test_missing_column_is_unknown():
    assert data_freshness(pd.DataFrame({"when": []}))["state"] == "unknown"


def test_completeness_counts_missing_leaf_days():
    frame = mart(days=4)
    # Drop two of the eight leaf-days.
    gap = frame.drop(index=[0, 3])
    result = completeness(gap)
    assert result["leaf_days"] == 8
    assert result["missing"] == 2
    assert result["missing_share"] == pytest.approx(0.25)


def test_a_complete_mart_has_no_gaps():
    assert completeness(mart())["missing"] == 0


# ------------------------------------------------------------------ drift
def backtest(origins: int = 5, inside: float = 0.9) -> pd.DataFrame:
    rows = []
    for origin_index in range(origins):
        origin = date(2026, 1, 1) + timedelta(days=30 * origin_index)
        for i in range(20):
            yhat = 100.0
            # `inside` of the points land within ±10%, the rest far outside.
            actual = 105.0 if (i / 20) < inside else 300.0
            rows.append(
                {
                    "forecast_origin": origin,
                    "target_date": origin + timedelta(days=1),
                    "horizon": i % 30 + 1,
                    "yhat": yhat,
                    "actual": actual,
                    "lower_90": 90.0,
                    "upper_90": 110.0,
                }
            )
    return pd.DataFrame(rows)


def test_coverage_matches_the_fraction_inside_the_band():
    result = coverage_drift(backtest())
    assert result["coverage"] == pytest.approx(0.90)
    assert result["state"] == "stable"


def test_recent_undercoverage_is_flagged():
    frame = backtest()
    origins = sorted(frame["forecast_origin"].unique())
    # Make the newest origin badly under-cover.
    newest = frame[frame["forecast_origin"] == origins[-1]].index
    frame.loc[newest, "actual"] = 300.0
    result = coverage_drift(frame)
    assert result["recent_coverage"] < result["coverage"]
    assert result["state"] == "undercovering"


def test_single_origin_uses_the_overall_number_instead_of_null():
    (result,) = [coverage_drift(backtest(origins=1))]
    assert result["recent_coverage"] is not None


def test_missing_bounds_is_unknown():
    frame = backtest().drop(columns=["lower_90", "upper_90"])
    assert coverage_drift(frame)["state"] == "unknown"


def test_recent_error_reports_wape_and_flags_degradation():
    frame = backtest()
    result = recent_error(frame)
    assert result["recent_wape"] is not None
    origins = sorted(frame["forecast_origin"].unique())
    newest = frame[frame["forecast_origin"] == origins[-1]].index
    frame.loc[newest, "actual"] = 1000.0
    degraded = recent_error(frame)
    assert degraded["recent_wape"] > DRIFT_WAPE_WARN
    assert degraded["state"] == "degraded"


def test_error_on_empty_frame_is_unknown():
    assert recent_error(pd.DataFrame())["state"] == "unknown"


# -------------------------------------------------------------------- job
def test_job_health_reads_the_newest_success(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    path = tmp_path / "monitoring" / "job_runs.json"
    record_job_run({"status": "failed", "error": "boom"}, path=path)
    record_job_run({"status": "success", "run_id": "a"}, path=path)
    result = job_health()
    assert result["state"] == "ok"
    assert result["runs_logged"] == 2


def test_job_health_without_a_log_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path / "empty"))
    assert job_health()["state"] == "unknown"


def test_job_health_ignores_failed_runs_when_dating_the_last_success(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    path = tmp_path / "monitoring" / "job_runs.json"
    record_job_run({"status": "success"}, path=path)
    old = (datetime.now() - timedelta(hours=JOB_MAX_AGE_HOURS + 5)).isoformat(
        timespec="seconds"
    )
    history = json.loads(path.read_text())
    history[0]["at"] = old
    path.write_text(json.dumps(history))
    result = job_health()
    assert result["state"] == "late"
    assert result["age_hours"] > JOB_MAX_AGE_HOURS


def test_job_with_only_failures_is_failing(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    path = tmp_path / "monitoring" / "job_runs.json"
    record_job_run({"status": "failed"}, path=path)
    assert job_health()["state"] == "failing"


def test_a_run_that_completed_with_bad_data_proves_the_scheduler_works(
    monkeypatch, tmp_path
):
    """A dead scheduler and stale data need different fixes; do not conflate them."""
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    path = tmp_path / "monitoring" / "job_runs.json"
    record_job_run({"status": "unhealthy", "health": "error"}, path=path)
    result = job_health()
    assert result["state"] == "ok"
    assert result["last_status"] == "unhealthy"


# ----------------------------------------------------------------- report
def test_health_report_is_ok_when_everything_is_in_order(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    record_job_run({"status": "success"}, path=tmp_path / "monitoring" / "job_runs.json")
    report = health_report(mart(), backtest_frame=backtest(), reference=date(2026, 11, 15))
    assert report.status in {"ok", "warn"}
    assert all(alert.level != "error" for alert in report.alerts)


def test_an_empty_system_is_not_reported_ok(monkeypatch, tmp_path):
    """Nothing has ever run; "ok" would be a claim nothing supports."""
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path / "empty"))
    report = health_report()
    assert report.status == "warn"
    codes = {alert.code for alert in report.alerts}
    assert {"data_unknown", "job_unknown"} <= codes


def test_stale_data_makes_the_report_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    report = health_report(mart(), reference=date(2026, 12, 1))
    assert report.status == "error"
    assert any(alert.code == "data_stale" for alert in report.alerts)


def test_alerts_are_readable_persian(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    report = health_report(mart(), reference=date(2026, 12, 1))
    payload = report.as_dict()
    assert all(isinstance(a["message"], str) and a["message"] for a in payload["alerts"])
    assert all(ord(a["message"][0]) > 127 for a in payload["alerts"])


def test_report_is_json_serialisable(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    report = health_report(mart(), backtest_frame=backtest(), reference=date(2026, 11, 15))
    json.dumps(report.as_dict())


def test_prometheus_output_has_the_expected_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    report = health_report(mart(), backtest_frame=backtest(), reference=date(2026, 11, 15))
    text = render_prometheus(report)
    assert "forecast_health_status" in text
    assert "forecast_data_stale_days 0" in text
    assert "forecast_recent_wape" in text


# --------------------------------------------------------------- registry
def test_register_does_not_change_the_active_version(tmp_path):
    path = tmp_path / "registry.json"
    first = register_version(model_name="m", backend="ridge", run_id="r1", path=path)
    promote(first.version_id, path=path)
    second = register_version(model_name="m", backend="ridge", run_id="r2", path=path)
    assert current_version(path=path)["version_id"] == first.version_id
    assert second.status == "candidate"


def test_registering_needs_a_model_name(tmp_path):
    with pytest.raises(RegistryError):
        register_version(model_name="", backend="ridge", run_id="r", path=tmp_path / "x.json")


def test_duplicate_version_id_is_refused(tmp_path):
    path = tmp_path / "registry.json"
    stamp = datetime(2026, 11, 1, 10, 0, 0)
    register_version(model_name="m", backend="ridge", run_id="r1", path=path, now=stamp)
    with pytest.raises(RegistryError):
        register_version(model_name="m", backend="ridge", run_id="r1", path=path, now=stamp)


def test_promote_retires_the_previous_active_version(tmp_path):
    path = tmp_path / "registry.json"
    first = register_version(model_name="m", backend="ridge", run_id="r1", path=path,
                             now=datetime(2026, 11, 1, 10))
    second = register_version(model_name="m", backend="ridge", run_id="r2", path=path,
                              now=datetime(2026, 11, 2, 10))
    promote(first.version_id, path=path)
    promote(second.version_id, path=path)
    statuses = {v["version_id"]: v["status"] for v in history(path=path)}
    assert statuses[second.version_id] == "active"
    assert statuses[first.version_id] == "retired"


def test_promote_without_versions_is_refused(tmp_path):
    with pytest.raises(RegistryError):
        promote(path=tmp_path / "registry.json")


def test_rollback_returns_to_the_previous_active_version(tmp_path):
    path = tmp_path / "registry.json"
    first = register_version(model_name="m", backend="ridge", run_id="r1", path=path,
                             now=datetime(2026, 11, 1, 10))
    second = register_version(model_name="m", backend="ridge", run_id="r2", path=path,
                              now=datetime(2026, 11, 2, 10))
    promote(first.version_id, path=path)
    promote(second.version_id, path=path)
    restored = rollback(path=path)
    assert restored.version_id == first.version_id
    assert current_version(path=path)["version_id"] == first.version_id


def test_rollback_never_lands_on_a_never_promoted_candidate(tmp_path):
    """A candidate has never been checked; rolling back to it is not a rollback."""
    path = tmp_path / "registry.json"
    active = register_version(model_name="m", backend="ridge", run_id="r1", path=path,
                              now=datetime(2026, 11, 1, 10))
    register_version(model_name="m", backend="ridge", run_id="r2", path=path,
                     now=datetime(2026, 11, 2, 10))
    promote(active.version_id, path=path)
    with pytest.raises(RegistryError):
        rollback(path=path)


def test_rollback_with_nothing_active_is_refused(tmp_path):
    with pytest.raises(RegistryError):
        rollback(path=tmp_path / "registry.json")


def test_better_wape_is_promoted_and_worse_is_not(tmp_path):
    path = tmp_path / "registry.json"
    first = record_run_version(
        model_name="m", backend="baseline", run_id="r1", metrics={"wape": 0.15}, path=path
    )
    assert first["promoted"] is True
    better = record_run_version(
        model_name="m", backend="baseline", run_id="r2", metrics={"wape": 0.12}, path=path
    )
    assert better["promoted"] is True
    worse = record_run_version(
        model_name="m", backend="baseline", run_id="r3", metrics={"wape": 0.30}, path=path
    )
    assert worse["promoted"] is False
    assert current_version(path=path)["run_id"] == "r2"


def test_missing_metrics_never_promote_over_a_measured_version(tmp_path):
    path = tmp_path / "registry.json"
    record_run_version(model_name="m", backend="b", run_id="r1", metrics={"wape": 0.12}, path=path)
    entry = record_run_version(model_name="m", backend="b", run_id="r2", metrics={}, path=path)
    assert entry["promoted"] is False
