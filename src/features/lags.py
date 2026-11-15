"""Lag and rolling features with strict no-leakage semantics (doc 8.2-8.3)."""

from __future__ import annotations

import pandas as pd

DEFAULT_LAGS = (1, 2, 3, 7, 14, 21, 28, 30, 56, 90, 364)
ROLLING_MEAN = (7, 14, 28, 30, 90)
ROLLING_STD = (7, 14, 30)
ROLLING_MIN = (7,)
ROLLING_MAX = (7,)


def _series_by_entity(frame: pd.DataFrame, entity_id: str, target: str) -> pd.Series:
    entity = frame[frame["entity_id"] == entity_id].copy()
    entity = entity.sort_values("date")
    entity["date"] = pd.to_datetime(entity["date"])
    return entity.set_index("date")[target].astype("float64")


def lag_features(
    frame: pd.DataFrame, *, target: str = "successful_orders", lags=DEFAULT_LAGS
) -> pd.DataFrame:
    """shift(n) per entity so a row for date t only sees t-1 and earlier."""
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    out = out.sort_values(["entity_id", "date"])
    grouped = out.groupby("entity_id", sort=False)[target]
    for lag in lags:
        out[f"lag_{lag}"] = grouped.shift(lag)
    return out


def rolling_features(
    frame: pd.DataFrame,
    *,
    target: str = "successful_orders",
    means=ROLLING_MEAN,
    stds=ROLLING_STD,
    mins=ROLLING_MIN,
    maxs=ROLLING_MAX,
) -> pd.DataFrame:
    """Rolling stats over strictly past values: for row t use window (t-n, t-1].

    Implemented by shifting the series by one *before* rolling, which is the
    invariant the leakage tests enforce.
    """
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values(["entity_id", "date"])
    grouped = out.groupby("entity_id", sort=False)[target]
    past = grouped.shift(1)

    past_frame = past.groupby(out["entity_id"], sort=False)
    for n in means:
        out[f"rolling_mean_{n}"] = past_frame.rolling(n, min_periods=n).mean().reset_index(level=0, drop=True)
    for n in stds:
        out[f"rolling_std_{n}"] = past_frame.rolling(n, min_periods=n).std().reset_index(level=0, drop=True)
    for n in mins:
        out[f"rolling_min_{n}"] = past_frame.rolling(n, min_periods=n).min().reset_index(level=0, drop=True)
    for n in maxs:
        out[f"rolling_max_{n}"] = past_frame.rolling(n, min_periods=n).max().reset_index(level=0, drop=True)
    return out


def add_time_series_features(
    frame: pd.DataFrame, *, target: str = "successful_orders", lags=DEFAULT_LAGS
) -> pd.DataFrame:
    """Convenience wrapper: lags then rolling stats."""
    with_lags = lag_features(frame, target=target, lags=lags)
    return rolling_features(with_lags, target=target)
