"""Tests: the backtest report shown in the dashboard."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.evaluation.report import Z_90, build_backtest_report


def frame(rows: int = 60, models=("naive", "seasonal_naive"), noise: float = 0.0):
    records = []
    for model in models:
        for i in range(rows):
            actual = 100.0
            yhat = 100.0 + (noise if i % 2 else -noise)
            records.append(
                {
                    "forecast_origin": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i // 10),
                    "target_date": pd.Timestamp("2026-02-01") + pd.Timedelta(days=i),
                    "horizon": i % 10 + 1,
                    "yhat": yhat,
                    "actual": actual,
                    "model_name": model,
                    "entity_id": "TOTAL",
                }
            )
    return pd.DataFrame(records)


def test_report_ranks_models_by_wape():
    report = build_backtest_report(frame())
    assert [m["model"] for m in report.models] == ["naive", "seasonal_naive"]
    assert all(m["wape"] == 0 for m in report.models)


def test_report_counts_windows_and_period():
    report = build_backtest_report(frame(rows=60))
    assert report.windows == 6
    assert report.points == 120
    assert report.test_start == "2026-02-01"
    assert report.test_end == "2026-04-01"


def test_perfect_forecast_has_full_coverage():
    report = build_backtest_report(frame(noise=0.0))
    assert report.models[0]["coverage_90"] == 1.0


def test_noisy_forecast_coverage_is_between_zero_and_one():
    report = build_backtest_report(frame(noise=40.0))
    coverage = report.models[0]["coverage_90"]
    assert 0.0 <= coverage <= 1.0


def test_coverage_uses_the_90_percent_multiplier():
    # Residuals of +/-10 with sigma ~10: the band is z*sigma, so all points fit.
    report = build_backtest_report(frame(noise=10.0))
    model = report.models[0]
    assert model["sigma"] > 0
    assert model["coverage_90"] == 1.0
    assert Z_90 > 1.6


def test_empty_backtest_is_reported_not_raised():
    report = build_backtest_report(pd.DataFrame())
    assert report.models == []
    assert report.windows == 0
    assert "کافی نبود" in report.note


def test_few_windows_adds_a_caveat():
    report = build_backtest_report(frame(rows=10))
    assert report.windows == 1
    assert "کم است" in report.note


def test_report_is_json_safe():
    import json

    payload = build_backtest_report(frame()).as_dict()
    json.dumps(payload)
    assert set(payload) == {
        "models",
        "windows",
        "test_start",
        "test_end",
        "points",
        "horizon",
        "note",
    }
