"""Tests: leakage prevention for lag, rolling and calendar features."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.features.calendar import calendar_features
from src.features.lags import add_time_series_features, lag_features, rolling_features


def series_frame(n: int = 120, entity: str = "FASHION") -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": list(dates.date),
            "entity_id": entity,
            "successful_orders": np.arange(n, dtype="int64") + 1,
        }
    )


def test_lag_matches_manual_values():
    frame = series_frame()
    out = lag_features(frame)
    assert out["lag_1"].iloc[1] == 1
    assert out["lag_7"].iloc[7] == 1
    assert out["lag_7"].iloc[10] == 4
    assert pd.isna(out["lag_1"].iloc[0])


def test_rolling_excludes_target_day():
    frame = series_frame()
    out = rolling_features(frame)
    # row index 10 -> date t; rolling_mean_7 must be mean(y[3..9]) = 4..10 -> 7
    assert out["rolling_mean_7"].iloc[10] == pytest.approx(7.0)
    # a value from the target day itself would push it to 8
    assert out["rolling_mean_7"].iloc[10] != 8


def test_rolling_never_uses_future_values():
    frame = series_frame(60)
    out = rolling_features(frame)
    # Perturb every value at and after index 30; earlier feature rows must be unchanged.
    perturbed = frame.copy()
    perturbed.loc[30:, "successful_orders"] = -999
    out2 = rolling_features(perturbed)
    before = out.iloc[:30].drop(columns=["successful_orders"])
    after = out2.iloc[:30].drop(columns=["successful_orders"])
    pd.testing.assert_frame_equal(before, after)


def test_lag_never_uses_future_values():
    frame = series_frame(60)
    out = lag_features(frame)
    perturbed = frame.copy()
    perturbed.loc[25:, "successful_orders"] = -1
    out2 = lag_features(perturbed)
    pd.testing.assert_frame_equal(
        out.iloc[:25].drop(columns=["successful_orders"]),
        out2.iloc[:25].drop(columns=["successful_orders"]),
    )


def test_features_are_per_entity():
    a = series_frame(40, entity="A")
    b = series_frame(40, entity="B")
    b["successful_orders"] = b["successful_orders"] * 100
    out = lag_features(pd.concat([a, b], ignore_index=True))
    row = out[(out["entity_id"] == "B") & (out["date"] == date(2025, 1, 10))]
    assert row["lag_1"].iloc[0] == b["successful_orders"].iloc[8]


def test_calendar_features_are_correct():
    dates = [date(2026, 3, 20), date(2026, 3, 21)]
    out = calendar_features(dates)
    assert out["weekday"].tolist() == [4, 5]  # Friday, Saturday
    assert out["is_weekend"].tolist() == [1, 0]
    # 2026-03-21 is 1 Farvardin 1405 (Nowruz day boundary).
    assert out["persian_month"].iloc[1] == 1
    assert out["persian_day"].iloc[1] == 1
    assert out["gregorian_year"].tolist() == [2026, 2026]


def test_month_flags():
    out = calendar_features([date(2026, 2, 1), date(2026, 2, 15), date(2026, 2, 28)])
    assert out["month_start_flag"].tolist() == [1, 0, 0]
    assert out["month_end_flag"].tolist() == [0, 0, 1]
    assert out["days_to_month_end"].tolist() == [27, 13, 0]


def test_holiday_flag_marks_only_holidays():
    out = calendar_features([date(2026, 3, 20), date(2026, 3, 21)], holidays={date(2026, 3, 21)})
    assert out["holiday_flag"].tolist() == [0, 1]


def test_feature_schema_is_stable():
    out = add_time_series_features(series_frame(40))
    for col in ["lag_1", "lag_90", "rolling_mean_7", "rolling_std_30", "rolling_max_7"]:
        assert col in out.columns
    # No target-derived column may be named like the target itself.
    assert "successful_orders" in out.columns  # target stays intact
    assert not any(c.startswith("future_") for c in out.columns)
