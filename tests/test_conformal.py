"""Tests: conformal calibration of forecast intervals.

The point of conformal prediction here is that the band is *measured* on the
model's own backtest residuals instead of assumed normal. These tests pin the
properties that make that trustworthy: the width grows with the horizon, the
empirical coverage lands near the requested level, and a missing calibration
never silently becomes a zero-width band.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.evaluation.conformal import (
    BUCKETS,
    MAX_RELATIVE_HALF_WIDTH,
    MIN_CALIBRATION_POINTS,
    ConformalCalibration,
    apply_interval,
    bucket_for,
    calibrate,
    coverage_by,
)


def backtest_frame(
    *, points: int = 200, noise: float = 0.10, seed: int = 7
) -> pd.DataFrame:
    """A synthetic backtest whose error is proportional to the forecast."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(points):
        horizon = (i % 30) + 1
        yhat = 1000.0 + 10 * i
        actual = yhat * (1.0 + noise * rng.standard_normal())
        rows.append(
            {
                "forecast_origin": "2026-01-01",
                "target_date": "2026-01-02",
                "horizon": horizon,
                "yhat": yhat,
                "actual": max(actual, 0.0),
                "model_name": "calendar_profile",
                "entity_id": "FASHION",
            }
        )
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ buckets
def test_bucket_boundaries_are_inclusive():
    assert bucket_for(1) == (1, 7)
    assert bucket_for(7) == (1, 7)
    assert bucket_for(8) == (8, 14)
    assert bucket_for(14) == (8, 14)
    assert bucket_for(15) == (15, 30)
    assert bucket_for(30) == (15, 30)


def test_horizon_beyond_the_last_bucket_uses_the_widest():
    assert bucket_for(45) == BUCKETS[-1]


# -------------------------------------------------------------- calibration
def test_empty_frame_yields_empty_calibration():
    calibration = calibrate(pd.DataFrame())
    assert calibration.levels == {}
    assert calibration.half_width(1, 0.90) == 0.0


def test_missing_columns_are_rejected():
    with pytest.raises(ValueError):
        calibrate(pd.DataFrame({"horizon": [1], "yhat": [1.0]}))


def test_calibration_produces_a_width_per_bucket():
    calibration = calibrate(backtest_frame())
    table = calibration.levels[0.90]
    assert set(table) == set(BUCKETS)
    assert all(0.0 < width <= MAX_RELATIVE_HALF_WIDTH for width in table.values())


def test_ninety_percent_width_is_wider_than_eighty():
    calibration = calibrate(backtest_frame())
    for bucket in BUCKETS:
        assert calibration.levels[0.90][bucket] >= calibration.levels[0.80][bucket]


def test_width_is_capped():
    wild = backtest_frame(noise=5.0)
    calibration = calibrate(wild)
    assert all(
        width <= MAX_RELATIVE_HALF_WIDTH
        for table in calibration.levels.values()
        for width in table.values()
    )


def test_empirical_coverage_is_close_to_the_requested_level():
    """The whole point: a 90% band should contain about 90% of actuals."""
    frame = backtest_frame(points=3000, noise=0.10)
    calibration = calibrate(frame)
    coverage = coverage_by(frame, calibration, level=0.90)
    overall = (coverage["coverage"] * coverage["points"]).sum() / coverage["points"].sum()
    assert 0.85 <= overall <= 0.95


def test_coverage_can_be_split_by_entity():
    frame = backtest_frame(points=600)
    second = frame.copy()
    second["entity_id"] = "GOLD"
    second["yhat"] = second["yhat"] * 3
    second["actual"] = second["actual"] * 3
    combined = pd.concat([frame, second], ignore_index=True)
    calibration = calibrate(combined)
    coverage = coverage_by(combined, calibration, level=0.90, by="entity_id")
    assert set(coverage["entity_id"]) == {"FASHION", "GOLD"}
    assert (coverage["points"] > 0).all()


def test_small_bucket_falls_back_to_a_trusted_one():
    """A bucket with too few points must not produce a confident narrow band."""
    frame = backtest_frame(points=200)
    sparse = frame[frame["horizon"] <= 7].head(MIN_CALIBRATION_POINTS - 1)
    calibration = calibrate(sparse)
    # Only the first bucket exists and it is under the threshold, so there is
    # no trusted bucket; the width must still be the measured one, not zero.
    assert calibration.half_width(1, 0.90) > 0.0


def test_apply_interval_never_goes_negative():
    calibration = calibrate(backtest_frame())
    values = np.array([10.0, 100.0, 1000.0])
    lower, upper = apply_interval(values, calibration, level=0.90, horizon=1)
    assert (lower >= 0).all()
    assert (upper >= values).all()
    assert (lower <= values).all()


def test_apply_interval_widens_with_horizon():
    calibration = calibrate(backtest_frame())
    values = np.array([1000.0])
    near_lower, _ = apply_interval(values, calibration, level=0.90, horizon=1)
    far_lower, _ = apply_interval(values, calibration, level=0.90, horizon=30)
    assert far_lower[0] <= near_lower[0]


def test_calibration_serialises_for_the_api():
    calibration = calibrate(backtest_frame())
    payload = calibration.as_dict()
    assert "levels" in payload and "points" in payload
    assert "0.9" in payload["levels"]
    assert any(key.startswith("D+") for key in payload["levels"]["0.9"])


def test_zero_forecast_rows_are_ignored_not_divided_by_zero():
    frame = backtest_frame(points=50)
    frame.loc[0, "yhat"] = 0.0
    calibration = calibrate(frame)
    assert np.isfinite(calibration.half_width(1, 0.90))


def test_empty_calibration_returns_zero_width_not_an_error():
    empty = ConformalCalibration()
    lower, upper = apply_interval(np.array([5.0]), empty, level=0.90, horizon=3)
    assert lower[0] == 5.0 and upper[0] == 5.0
