"""Backtest report: what the user sees to trust the forecast.

The pipeline already runs a rolling-origin backtest to pick a model, but the
evidence was thrown away after ranking. This module turns the raw backtest
frame into a report the dashboard can show:

* WAPE and Bias per model (the ranking the engine already uses);
* how many forecast origins (windows) were evaluated and over which period;
* the empirical coverage of a 90% interval built from the model's own
  residuals — the honest way to say "the band is calibrated", rather than
  assuming normality.

Nothing here changes the forecast; it only describes how well it did.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.evaluation.metrics import bias, wape

Z_90 = 1.6449


@dataclass(frozen=True)
class BacktestReport:
    models: list[dict]
    windows: int
    test_start: str | None
    test_end: str | None
    points: int
    horizon: int
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "models": self.models,
            "windows": self.windows,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "points": self.points,
            "horizon": self.horizon,
            "note": self.note,
        }


def _coverage(actual: np.ndarray, yhat: np.ndarray, sigma: float) -> float:
    """Share of actuals inside yhat +/- z*sigma (sigma in absolute units)."""
    if len(actual) == 0:
        return float("nan")
    half = Z_90 * sigma
    inside = np.abs(actual - yhat) <= half
    return float(inside.mean())


def build_backtest_report(
    backtest_frame: pd.DataFrame, *, horizon: int = 30
) -> BacktestReport:
    """Summarise a rolling-origin backtest frame for display."""
    if backtest_frame is None or backtest_frame.empty:
        return BacktestReport(
            models=[],
            windows=0,
            test_start=None,
            test_end=None,
            points=0,
            horizon=horizon,
            note="تاریخچه برای بک‌تست کافی نبود؛ اعتبار مدل اندازه‌گیری نشد.",
        )

    frame = backtest_frame.copy()
    frame["target_date"] = pd.to_datetime(frame["target_date"])
    frame["forecast_origin"] = pd.to_datetime(frame["forecast_origin"])

    models: list[dict] = []
    for name, chunk in frame.groupby("model_name"):
        actual = chunk["actual"].to_numpy(dtype="float64")
        yhat = chunk["yhat"].to_numpy(dtype="float64")
        residual = actual - yhat
        sigma = float(np.std(residual, ddof=1)) if len(residual) > 1 else 0.0
        models.append(
            {
                "model": str(name),
                "wape": float(wape(chunk["actual"], chunk["yhat"])),
                "bias": float(bias(chunk["actual"], chunk["yhat"])),
                "coverage_90": _coverage(actual, yhat, sigma),
                "sigma": sigma,
                "points": int(len(chunk)),
                "windows": int(chunk["forecast_origin"].nunique()),
            }
        )

    models.sort(key=lambda m: (np.isnan(m["wape"]), m["wape"]))
    note = ""
    if models and models[0]["windows"] < 3:
        note = "تعداد پنجره‌های بک‌تست کم است؛ اعتبار برآورد محدود است."

    return BacktestReport(
        models=models,
        windows=int(frame["forecast_origin"].nunique()),
        test_start=frame["target_date"].min().date().isoformat(),
        test_end=frame["target_date"].max().date().isoformat(),
        points=int(len(frame)),
        horizon=horizon,
        note=note,
    )
