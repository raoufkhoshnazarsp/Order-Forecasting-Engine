"""Forecast accuracy metrics (doc 13.3). WAPE is primary; MAPE is not."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _clean(actual, forecast) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(actual, dtype="float64")
    f = np.asarray(forecast, dtype="float64")
    mask = ~(np.isnan(a) | np.isnan(f))
    return a[mask], f[mask]


def wape(actual, forecast) -> float:
    a, f = _clean(actual, forecast)
    denom = np.abs(a).sum()
    if denom == 0:
        return float("nan")
    return float(np.abs(a - f).sum() / denom)


def bias(actual, forecast) -> float:
    a, f = _clean(actual, forecast)
    denom = np.abs(a).sum()
    if denom == 0:
        return float("nan")
    return float((f - a).sum() / denom)


def mae(actual, forecast) -> float:
    a, f = _clean(actual, forecast)
    return float(np.abs(a - f).mean()) if a.size else float("nan")


def rmse(actual, forecast) -> float:
    a, f = _clean(actual, forecast)
    return float(np.sqrt(((a - f) ** 2).mean())) if a.size else float("nan")


def mase(actual, forecast, insample: np.ndarray | None, seasonality: int = 7) -> float:
    a, f = _clean(actual, forecast)
    if not a.size:
        return float("nan")
    if insample is None or len(insample) <= seasonality:
        return float("nan")
    scale = np.abs(np.diff(np.asarray(insample, dtype="float64"), n=seasonality)).mean()
    if scale == 0:
        return float("nan")
    return float(np.abs(a - f).mean() / scale)


def rmsse(actual, forecast, insample: np.ndarray | None, seasonality: int = 7) -> float:
    a, f = _clean(actual, forecast)
    if not a.size or insample is None or len(insample) <= seasonality:
        return float("nan")
    scale = np.sqrt((np.diff(np.asarray(insample, dtype="float64"), n=seasonality) ** 2).mean())
    if scale == 0:
        return float("nan")
    return float(np.sqrt(((a - f) ** 2).mean()) / scale)


def score_frame(
    frame: pd.DataFrame,
    *,
    actual_col: str = "actual",
    forecast_col: str = "yhat",
    group_cols: tuple[str, ...] = ("entity_id",),
    horizon_col: str = "horizon",
) -> pd.DataFrame:
    """Return WAPE/Bias/MAE/RMSE per entity (and per horizon if present)."""
    cols = [c for c in group_cols if c in frame.columns]
    if horizon_col in frame.columns:
        cols = [*cols, horizon_col]
    if not cols:
        return pd.DataFrame(
            [
                {
                    "wape": wape(frame[actual_col], frame[forecast_col]),
                    "bias": bias(frame[actual_col], frame[forecast_col]),
                    "mae": mae(frame[actual_col], frame[forecast_col]),
                    "rmse": rmse(frame[actual_col], frame[forecast_col]),
                }
            ]
        )
    rows = []
    for keys, chunk in frame.groupby(cols):
        keys = keys if isinstance(keys, tuple) else (keys,)
        record = dict(zip(cols, keys))
        record.update(
            {
                "wape": wape(chunk[actual_col], chunk[forecast_col]),
                "bias": bias(chunk[actual_col], chunk[forecast_col]),
                "mae": mae(chunk[actual_col], chunk[forecast_col]),
                "rmse": rmse(chunk[actual_col], chunk[forecast_col]),
                "n_points": int(len(chunk)),
            }
        )
        rows.append(record)
    return pd.DataFrame(rows)
