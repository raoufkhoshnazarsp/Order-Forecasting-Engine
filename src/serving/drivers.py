"""Why the forecast is what it is — for every model, not only the ML one.

The global model ships a per-feature attribution because it has features. The
baseline models do not: they read one series and extrapolate. Asking "which
features drove this number?" of them used to return nothing, which left step 5
of the flow empty for most runs.

This module answers the same business question in a way every model supports:
**which component of the forecast contributes how much**. The selected model's
path is compared with the three components a baseline can express:

* **level**   — the flat 7-day moving average (the recent business level);
* **trend**   — drift minus naive (how much the slope adds over the last value);
* **weekday** — calendar profile minus the flat level (the weekly shape);
* **model**   — everything the selected model adds or removes on top of those.

The four sum exactly to the selected model's forecast, so the shares are a
decomposition, not a guess. It is still a model attribution, not causality:
a component that moves the number is one the forecast *used*, not one that
*caused* the orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.models.baselines import calendar_profile, drift, moving_average, naive

FEATURE_LABELS = {
    "level": "سطح پایه (میانگین هفتگی اخیر)",
    "trend": "روند (شیب تاریخچه)",
    "weekday": "الگوی روز هفته",
    "model": "اثر مدل انتخابی و سایر عوامل",
}

MIN_DAYS = 21
MAX_DRIVERS = 8
MAX_ENTITIES = 24


@dataclass(frozen=True)
class Driver:
    feature: str
    label: str
    contribution: float
    share: float
    direction: str

    def as_dict(self) -> dict:
        return {
            "feature": self.feature,
            "label": self.label,
            "contribution": float(self.contribution),
            "share": float(self.share),
            "direction": self.direction,
        }


@dataclass(frozen=True)
class DriverReport:
    method: str
    is_causal: bool
    horizon: int
    total: float
    drivers: list[Driver]
    entities: list[dict] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "is_causal": self.is_causal,
            "horizon": self.horizon,
            "total": float(self.total),
            "drivers": [d.as_dict() for d in self.drivers],
            "entities": self.entities,
            "note": self.note,
        }


def _clean_series(series: pd.Series) -> pd.Series:
    series = series.dropna().astype("float64")
    series.index = pd.to_datetime(series.index)
    series = series.sort_index()
    series = series[~series.index.duplicated(keep="last")]
    if series.empty:
        return series
    return series.reindex(pd.date_range(series.index.min(), series.index.max(), freq="D"))


def _entity_series(mart: pd.DataFrame, entity_id: str) -> pd.Series:
    chunk = mart[mart["entity_id"].astype(str) == entity_id]
    if chunk.empty or "successful_orders" not in chunk.columns:
        return pd.Series(dtype="float64")
    return _clean_series(chunk.set_index("date")["successful_orders"])


def _path(forecaster, series: pd.Series, horizon: int) -> np.ndarray:
    clean = series.dropna()
    if clean.empty:
        return np.zeros(horizon)
    values = np.asarray(forecaster(clean, horizon).values, dtype="float64")
    if len(values) < horizon:
        values = np.concatenate([values, np.full(horizon - len(values), np.nan)])
    return np.nan_to_num(values[:horizon], nan=0.0)


def _report_from_paths(
    paths: dict[str, np.ndarray], horizon: int, note: str = ""
) -> DriverReport:
    contributions = {name: float(np.sum(values)) for name, values in paths.items()}
    total = float(sum(contributions.values()))
    magnitude = sum(abs(value) for value in contributions.values())
    drivers: list[Driver] = []
    if magnitude > 0:
        for name, value in contributions.items():
            share = abs(value) / magnitude
            drivers.append(
                Driver(
                    feature=name,
                    label=FEATURE_LABELS.get(name, name),
                    contribution=value,
                    share=share,
                    direction="increase" if value >= 0 else "decrease",
                )
            )
    drivers.sort(key=lambda d: -d.share)
    return DriverReport(
        method="component_decomposition",
        is_causal=False,
        horizon=horizon,
        total=total,
        drivers=drivers[:MAX_DRIVERS],
        note=note,
    )


def component_drivers(
    mart: pd.DataFrame,
    forecast: pd.DataFrame,
    *,
    horizon: int,
    model: str,
) -> dict:
    """Decompose the selected model's total forecast into interpretable parts."""
    if mart is None or forecast is None or forecast.empty or "TOTAL" not in set(
        mart["entity_id"].astype(str)
    ):
        return {}

    series = _entity_series(mart, "TOTAL")
    if len(series.dropna()) < MIN_DAYS:
        return {}

    total_rows = forecast[forecast["entity_id"].astype(str) == "TOTAL"]
    if total_rows.empty:
        return {}
    chosen = (
        total_rows.sort_values("horizon")["reconciled_yhat"]
        .astype("float64")
        .to_numpy()[:horizon]
    )
    if len(chosen) < horizon:
        chosen = np.concatenate([chosen, np.full(horizon - len(chosen), np.nan)])
    chosen = np.nan_to_num(chosen, nan=0.0)

    level = _path(moving_average, series, horizon)
    trend = _path(drift, series, horizon) - _path(naive, series, horizon)
    weekday = _path(calendar_profile, series, horizon) - _path(
        moving_average, series, horizon
    )
    # The residual keeps the four components adding up to the forecast exactly.
    model_effect = chosen - level - trend - weekday

    report = _report_from_paths(
        {"level": level, "trend": trend, "weekday": weekday, "model": model_effect},
        horizon,
        note="سهم هر جزء از جمع پیش‌بینی، با مدل انتخابی؛ تخصیص مدل است، نه رابطهٔ علّی.",
    ).as_dict()
    report["model"] = model

    # Composition: which vertical carries the forecast total.
    by_entity = (
        forecast.groupby("entity_id", as_index=False)["reconciled_yhat"].sum()
    )
    # TOTAL is the sum of the entities below it, so including it in the
    # denominator would halve every share. The composition is over the leaves.
    leaves = by_entity[by_entity["entity_id"].astype(str) != "TOTAL"]
    grand = float(leaves["reconciled_yhat"].sum())
    entities = [
        {
            "entity_id": str(row.entity_id),
            "orders": float(row.reconciled_yhat),
            "share": float(row.reconciled_yhat) / grand if grand else 0.0,
        }
        for row in leaves.itertuples(index=False)
    ]
    entities.sort(key=lambda r: -r["orders"])
    if len(entities) > MAX_ENTITIES:
        # Truncating would drop forecast from the composition; the tail is
        # folded into one row so the shares still sum to 1.
        head, tail = entities[: MAX_ENTITIES - 1], entities[MAX_ENTITIES - 1:]
        tail_orders = sum(row["orders"] for row in tail)
        entities = head + [
            {
                "entity_id": "سایر",
                "orders": tail_orders,
                "share": tail_orders / grand if grand else 0.0,
            }
        ]
    report["entities"] = entities
    return report
