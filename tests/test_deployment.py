"""Tests: the scheduled job and cron line, without running a real forecast.

The parts worth pinning are the contract with cron (exit codes and job log)
and the cron line itself — a wrong cron line is silent, which is the failure
mode this phase exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.install_cron import MARKER, build_line


def test_cron_line_carries_the_marker_so_it_can_be_replaced():
    line = build_line(
        workbook="/w.xlsx", hour=6, minute=30, log="/tmp/cron.log",
        project_dir="/opt/app",
    )
    assert line.startswith("30 6 * * * ")
    assert line.endswith(MARKER)
    assert "scheduled_run.py" in line
    assert "--workbook /w.xlsx" in line


def test_cron_line_redirects_output_so_a_failure_is_recorded():
    line = build_line(
        workbook="/w.xlsx", hour=1, minute=5, log="/var/log/f.log",
        project_dir="/opt/app",
    )
    assert ">> /var/log/f.log 2>&1" in line


def test_cron_dry_run_prints_the_line_and_changes_nothing(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/install_cron.py", "--dry-run",
         "--workbook", str(tmp_path / "w.xlsx")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert MARKER in result.stdout


def test_cron_refuses_a_workbook_that_is_not_there(tmp_path):
    """A cron line pointing at a missing file fails silently every morning."""
    result = subprocess.run(
        [sys.executable, "scripts/install_cron.py",
         "--workbook", "/app/artifacts/web_runs/missing.xlsx",
         "--project-dir", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_cron_maps_the_container_path_to_the_host_before_checking(tmp_path):
    from scripts.install_cron import host_path

    assert host_path("/app/artifacts/w.xlsx", "/opt/app") == Path("/opt/app/artifacts/w.xlsx")
    assert host_path("/tmp/w.xlsx", "/opt/app") == Path("/tmp/w.xlsx")


def test_scheduled_run_reports_a_failure_with_exit_code_one(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path / "state"))
    import scripts.scheduled_run as scheduled

    def boom(*args, **kwargs):
        raise RuntimeError("workbook is unreadable")

    monkeypatch.setattr(scheduled, "run_pipeline", boom)
    code = scheduled.main([
        "--workbook", str(tmp_path / "missing.xlsx"),
        "--out", str(tmp_path / "out"),
        "--no-registry",
    ])
    assert code == 1
    log = json.loads((tmp_path / "state" / "monitoring" / "job_runs.json").read_text())
    assert log[-1]["status"] == "failed"
    assert "unreadable" in log[-1]["error"]


def test_scheduled_run_writes_outputs_and_registers_a_version(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path / "state"))
    import pandas as pd
    import scripts.scheduled_run as scheduled
    from src.pipeline import PipelineResult

    mart = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-11-14", "2026-11-15"] * 2),
            "entity_id": ["FASHION", "FASHION", "TOTAL", "TOTAL"],
            "successful_orders": [100, 110, 100, 110],
            "quality_flag": ["OBSERVED"] * 4,
        }
    )
    forecast = pd.DataFrame(
        {
            "entity_id": ["TOTAL"],
            "reconciled_yhat": [120.0],
            "horizon": [1],
        }
    )
    fake = PipelineResult(
        run_id="run_test",
        source_file="w.xlsx",
        date_start=pd.Timestamp("2026-11-14").date(),
        date_end=pd.Timestamp("2026-11-15").date(),
        horizon=30,
        model="calendar_profile",
        mart_rows=4,
        entities=["FASHION", "TOTAL"],
        unmapped_orders=0,
        total_last_actual=110,
        total_forecast={1: 120.0},
        backtest_ranking=[{"model": "calendar_profile", "wape": 0.12, "bias": 0.01}],
        forecast=forecast,
        mart=mart,
        backtest_rows=None,
    )
    monkeypatch.setattr(scheduled, "run_pipeline", lambda *a, **k: fake)

    code = scheduled.main([
        "--workbook", str(tmp_path / "w.xlsx"),
        "--out", str(tmp_path / "out"),
        "--reference", "2026-11-15",
    ])
    assert code == 0
    out = tmp_path / "out" / "run_test"
    assert (out / "summary.json").exists()
    assert (out / "forecast.parquet").exists()
    assert (out / "mart.parquet").exists()
    health = json.loads((out / "health.json").read_text())
    assert health["status"] in {"ok", "warn"}
    registry = json.loads(
        (tmp_path / "state" / "monitoring" / "model_registry.json").read_text()
    )
    assert registry["current"] is not None


def test_health_endpoints_expose_status_and_metrics(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    from src.serving.api import app

    client = TestClient(app)
    live = client.get("/healthz")
    assert live.status_code == 200 and live.json() == {"status": "ok"}

    full = client.get("/api/health")
    assert full.status_code == 200
    payload = full.json()
    assert payload["status"] in {"ok", "warn", "error"}
    assert "alerts" in payload and "registry" in payload and "job" in payload

    metrics = client.get("/api/health/metrics")
    assert metrics.status_code == 200
    assert "forecast_health_status" in metrics.text
