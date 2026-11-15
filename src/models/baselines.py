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


BASELINES = {
    "naive": lambda s, h: naive(s, h),
    "seasonal_naive": lambda s, h: seasonal_naive(s, h),
    "moving_average": lambda s, h: moving_average(s, h),
    "drift": lambda s, h: drift(s, h),
    "ets": lambda s, h: ets_simple(s, h),
}


def run_baselines(series: pd.Series, h: int) -> dict[str, ForecastResult]:
    return {name: fn(series, h) for name, fn in BASELINES.items()}
