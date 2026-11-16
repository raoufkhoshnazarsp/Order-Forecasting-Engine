"""Tests: changepoint detection and structural regime features.

What must hold for this to be safe in a forecast:

* a clean level shift is found, a stable noisy series produces nothing,
* detection only ever looks at data up to the requested date,
* regime features agree with the changepoints they came from.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.lifecycle.changepoints import (
    ChangepointError,
    changepoints,
    default_penalty,
    detect_changepoints,
    recent_regimes,
)
from src.lifecycle.regime import RegimeError, regime_features, regime_report


def series(values, start="2026-01-01") -> pd.Series:
    return pd.Series(
        values, index=pd.date_range(start, periods=len(values), freq="D"), dtype="float64"
    )


def stepped(n=120, at=70, before=100.0, after=180.0, noise=1.0, seed=3) -> pd.Series:
    rng = np.random.default_rng(seed)
    values = np.where(
        np.arange(n) < at, before, after
    ) + noise * rng.standard_normal(n)
    return series(values)


# ------------------------------------------------------------- detection
def test_a_clear_level_shift_is_found_once():
    breaks = detect_changepoints(stepped())
    assert len(breaks) == 1
    assert abs(breaks[0] - 70) <= 3


def test_a_stable_noisy_series_produces_no_changepoints():
    rng = np.random.default_rng(5)
    values = 100 + 3 * rng.standard_normal(120)
    assert detect_changepoints(values) == []


def test_a_trend_without_a_level_shift_is_not_a_changepoint():
    values = np.linspace(100, 160, 120) + np.random.default_rng(2).standard_normal(120)
    assert detect_changepoints(values) == []


def test_too_short_a_series_has_no_changepoints():
    assert detect_changepoints([1.0, 2.0, 3.0]) == []


def test_nan_input_is_rejected():
    with pytest.raises(ChangepointError):
        detect_changepoints(np.array([1.0, np.nan, 3.0, 4.0]))


def test_negative_penalty_is_rejected():
    with pytest.raises(ChangepointError):
        detect_changepoints(stepped().to_numpy(), penalty=-1.0)


def test_two_shifts_are_both_found():
    values = np.concatenate(
        [np.full(60, 100.0), np.full(60, 170.0), np.full(60, 110.0)]
    )
    breaks = detect_changepoints(values)
    assert len(breaks) == 2
    assert abs(breaks[0] - 60) <= 3
    assert abs(breaks[1] - 120) <= 3


def test_default_penalty_is_positive_and_finite():
    penalty = default_penalty(stepped().to_numpy())
    assert np.isfinite(penalty) and penalty > 0


def test_changepoints_report_dates_and_shift_direction():
    points = changepoints(stepped())
    assert len(points) == 1
    assert points[0].shift > 0
    assert points[0].after_mean > points[0].before_mean
    assert hasattr(points[0].date, "year")


def test_recent_regimes_ignores_the_future():
    """A step that happens after as_of must not be visible."""
    data = stepped(at=100, before=100.0, after=260.0)
    cutoff = data.index[80]
    early = recent_regimes(data, as_of=cutoff)
    assert all(p.index <= 80 for p in early)
    whole = recent_regimes(data)
    assert any(95 <= p.index <= 105 for p in whole)


# --------------------------------------------------------------- regimes
def test_regime_length_matches_days_since_the_shift():
    data = stepped(at=70)
    features = regime_features(data)
    assert features.changepoints
    assert features.days_since_changepoint == pytest.approx(len(data) - 1 - 70, abs=3)
    assert features.regime_length == pytest.approx(features.days_since_changepoint + 1, abs=1)


def test_level_ratio_reflects_the_new_level():
    data = stepped(before=100.0, after=200.0, noise=1.0)
    features = regime_features(data)
    # The last regime sits well above the full-history mean.
    assert features.level_ratio > 1.2


def test_recent_shift_is_relative_and_signed():
    up = regime_features(stepped(before=100.0, after=200.0))
    down = regime_features(stepped(before=200.0, after=100.0))
    assert up.recent_shift > 0.5
    assert down.recent_shift < -0.3


def test_no_changepoint_means_the_whole_series_is_one_regime():
    rng = np.random.default_rng(9)
    data = series(100 + 2 * rng.standard_normal(120))
    features = regime_features(data)
    assert features.changepoints == []
    assert features.recent_shift == 0.0
    assert features.regime_length == 120


def test_regime_features_are_numeric_columns():
    features = regime_features(stepped())
    columns = features.columns()
    assert set(columns) == {
        "days_since_changepoint",
        "regime_length",
        "level_ratio",
        "recent_shift",
        "regime_volatility",
    }
    assert all(isinstance(v, float) for v in columns.values())


def test_empty_series_is_refused():
    with pytest.raises(RegimeError):
        regime_features(pd.Series(dtype="float64"))


def test_regime_features_do_not_look_past_as_of():
    data = stepped(at=100, before=100.0, after=300.0)
    cutoff = data.index[80]
    early = regime_features(data, as_of=cutoff)
    assert early.changepoints == []
    late = regime_features(data)
    assert late.changepoints


# --------------------------------------------------------------- report
def test_regime_report_covers_each_entity_separately():
    left = stepped(seed=1)
    right = series(np.full(120, 50.0))
    frame = pd.concat(
        [
            pd.DataFrame(
                {
                    "date": left.index,
                    "entity_id": "FASHION",
                    "successful_orders": left.to_numpy(),
                }
            ),
            pd.DataFrame(
                {
                    "date": right.index,
                    "entity_id": "GOLD",
                    "successful_orders": right.to_numpy(),
                }
            ),
        ],
        ignore_index=True,
    )
    report = regime_report(frame)
    ids = {row["entity_id"] for row in report["entities"]}
    assert ids == {"FASHION", "GOLD"}
    fashion = next(r for r in report["entities"] if r["entity_id"] == "FASHION")
    gold = next(r for r in report["entities"] if r["entity_id"] == "GOLD")
    assert fashion["changepoints"]
    assert gold["changepoints"] == []
    assert "علت" not in report["note"]


def test_regime_report_on_empty_frame_is_empty():
    report = regime_report(pd.DataFrame())
    assert report["entities"] == []


def test_regime_report_requires_known_columns():
    with pytest.raises(RegimeError):
        regime_report(
            pd.DataFrame(
                {"date": ["2026-01-01"], "entity_id": ["FASHION"]}
            )
        )
