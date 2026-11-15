"""Baseline forecasters (doc 10.1). Pure pandas/numpy, no heavy deps.

Each forecaster takes a per-entity series indexed by an unbroken daily date
range and returns an h-step forecast path.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ForecastResult:
    model_name: str
    values: np.ndarray

    def as_series(self, start: pd.Timestamp, freq: str = "D") -> pd.Series:
        idx = pd.date_range(start=start, periods=len(self.values), freq=freq)
        return pd.Series(self.values, index=idx, name=self.model_name)


def _as_float(series: pd.Series) -> np.ndarray:
    return series.astype("float64").to_numpy()


def naive(series: pd.Series, h: int) -> ForecastResult:
    value = float(_as_float(series)[-1])
    return ForecastResult("naive", np.full(h, value))


def seasonal_naive(series: pd.Series, h: int, seasonality: int = 7) -> ForecastResult:
    y = _as_float(series)
    if len(y) < seasonality:
        return ForecastResult("seasonal_naive", np.full(h, float(y[-1])))
    last_season = y[-seasonality:]
    values = np.array([last_season[i % seasonality] for i in range(h)])
    return ForecastResult("seasonal_naive", values)


def moving_average(series: pd.Series, h: int, window: int = 7) -> ForecastResult:
    y = _as_float(series)
    window = min(window, len(y))
    value = float(y[-window:].mean())
    return ForecastResult("moving_average", np.full(h, value))


def drift(series: pd.Series, h: int) -> ForecastResult:
    """Random walk with drift — a cheap trend baseline."""
    y = _as_float(series)
    if len(y) < 2:
        return ForecastResult("drift", np.full(h, float(y[-1])))
    slope = (y[-1] - y[0]) / (len(y) - 1)
    values = y[-1] + slope * np.arange(1, h + 1)
    return ForecastResult("drift", np.maximum(values, 0.0))


def ets_simple(series: pd.Series, h: int, alpha: float = 0.3, beta: float = 0.1) -> ForecastResult:
    """Holt's linear method with fixed smoothing — a light ETS stand-in.

    ``statsforecast`` supplies the production ETS/AutoARIMA in Phase 3; this
    keeps the baseline layer testable without heavy dependencies.
    """
    y = _as_float(series)
    if len(y) < 2:
        return ForecastResult("ets", np.full(h, float(y[-1])))
    level = y[0]
    trend = y[1] - y[0]
    for value in y[1:]:
        prev_level = level
        level = alpha * value + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
    values = np.array([level + (i + 1) * trend for i in range(h)])
    return ForecastResult("ets", np.maximum(values, 0.0))


def calendar_profile(
    series: pd.Series, h: int, lookback: int = 56, seasonality: int = 7
) -> ForecastResult:
    """Weekday-profile model driven by the Persian calendar dimension.

    The level comes from the last ``seasonality`` days; the shape comes from
    each Persian weekday's average ratio over the last ``lookback`` days. The
    weekday of every target date is read from the ``dim_date`` table, so the
    Iranian weekend (Friday) and Persian month boundaries are honoured without
    any observed data for the future dates.
    """
    y = series.dropna().astype("float64")
    if len(y) < seasonality * 2:
        return ForecastResult("calendar_profile", np.full(h, float(y.iloc[-1]) if len(y) else 0.0))

    recent = y.iloc[-lookback:]
    try:
        from src.features.persian_calendar import calendar_features

        cal = calendar_features(recent.index)
        weekdays = cal["weekday"].to_numpy()
        future = pd.date_range(
            start=y.index[-1] + pd.Timedelta(days=1), periods=h, freq="D"
        )
        future_weekdays = calendar_features(future)["weekday"].to_numpy()
    except Exception:  # noqa: BLE001 - calendar is optional, fall back to Gregorian
        weekdays = recent.index.day_of_week.to_numpy()
        future = pd.date_range(
            start=y.index[-1] + pd.Timedelta(days=1), periods=h, freq="D"
        )
        future_weekdays = future.day_of_week.to_numpy()

    # Detrend before profiling: each day's ratio to its centred 7-day mean
    # isolates the weekday shape from the level and the trend.
    rolling = recent.rolling(seasonality, center=True, min_periods=seasonality // 2 + 1).mean()
    ratio = (recent / rolling).replace([np.inf, -np.inf], np.nan).dropna()
    if ratio.empty:
        return ForecastResult("calendar_profile", np.full(h, float(y.iloc[-1])))
    ratio_weekdays = pd.Series(weekdays, index=recent.index).loc[ratio.index]
    profile = ratio.groupby(ratio_weekdays.to_numpy()).mean().to_dict()

    # Level and trend are read from the deseasonalised series, so the Friday
    # dip is not counted twice (once in the level, once in the profile).
    recent_ratio = pd.Series(
        [profile.get(int(w), 1.0) for w in weekdays], index=recent.index
    )
    deseason = (recent / recent_ratio).replace([np.inf, -np.inf], np.nan).dropna()
    if deseason.empty:
        return ForecastResult("calendar_profile", np.full(h, float(y.iloc[-1])))
    level = float(deseason.iloc[-seasonality:].mean())
    ratios = np.array([profile.get(int(w), 1.0) for w in future_weekdays], dtype="float64")
    return ForecastResult("calendar_profile", np.maximum(level * ratios, 0.0))


BASELINES = {
    "naive": lambda s, h: naive(s, h),
    "seasonal_naive": lambda s, h: seasonal_naive(s, h),
    "moving_average": lambda s, h: moving_average(s, h),
    "drift": lambda s, h: drift(s, h),
    "ets": lambda s, h: ets_simple(s, h),
    "calendar_profile": lambda s, h: calendar_profile(s, h),
}


def run_baselines(series: pd.Series, h: int) -> dict[str, ForecastResult]:
    return {name: fn(series, h) for name, fn in BASELINES.items()}
