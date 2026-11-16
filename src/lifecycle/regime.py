"""Structural-regime features derived from changepoints.

Every feature here is computed from data up to the forecast origin only. They
describe the series' current state, not the future, so they can be added to a
model without a leakage risk — which is why they live in their own module and
are tested against a truncation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.lifecycle.changepoints import changepoints


class RegimeError(ValueError):
    """Raised when regime features cannot be built."""


@dataclass(frozen=True)
class RegimeFeatures:
    days_since_changepoint: float
    regime_length: float
    level_ratio: float  # current regime mean / full-history mean
    recent_shift: float  # last changepoint's relative shift
    regime_volatility: float  # coefficient of variation inside the regime
    changepoints: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "days_since_changepoint": self.days_since_changepoint,
            "regime_length": self.regime_length,
            "level_ratio": self.level_ratio,
            "recent_shift": self.recent_shift,
            "regime_volatility": self.regime_volatility,
            "changepoints": list(self.changepoints),
        }

    def columns(self) -> dict[str, float]:
        """Flat, numeric-only view for a feature matrix."""
        return {
            "days_since_changepoint": self.days_since_changepoint,
            "regime_length": self.regime_length,
            "level_ratio": self.level_ratio,
            "recent_shift": self.recent_shift,
            "regime_volatility": self.regime_volatility,
        }


def regime_features(series: pd.Series, *, as_of: object | None = None) -> RegimeFeatures:
    """Regime state of the series at ``as_of`` (default: its last observation)."""
    clean = series.dropna().astype("float64").sort_index()
    if as_of is not None:
        clean = clean[clean.index <= pd.Timestamp(as_of)]
    if clean.empty:
        raise RegimeError("Regime features need at least one observation")

    points = changepoints(clean)
    values = clean.to_numpy()
    history_mean = float(values.mean())
    last = len(values) - 1

    if points:
        last_point = points[-1]
        start = max(0, int(last_point.index))
        regime = values[start:]
        days_since = float(last - start)
        shift = (
            float(last_point.shift / last_point.before_mean)
            if last_point.before_mean
            else 0.0
        )
        records = [p.as_dict() for p in points]
    else:
        start = 0
        regime = values
        days_since = float(last)
        shift = 0.0
        records = []

    regime_mean = float(regime.mean())
    level_ratio = regime_mean / history_mean if history_mean else 1.0
    volatility = (
        float(regime.std(ddof=0) / regime_mean) if regime_mean and len(regime) > 1 else 0.0
    )
    return RegimeFeatures(
        days_since_changepoint=days_since,
        regime_length=float(len(regime)),
        level_ratio=level_ratio,
        recent_shift=shift,
        regime_volatility=volatility,
        changepoints=records,
    )


def regime_report(frame: pd.DataFrame, *, as_of: object | None = None) -> dict:
    """Regime state per entity, for the UI's structural-change panel.

    Each entity is treated independently; a level shift in GOLD says nothing
    about FASHION, and pooling them would hide a small entity's shift inside
    the total's noise.
    """
    if frame is None or frame.empty:
        return {"entities": [], "as_of": None, "note": ""}
    required = {"date", "entity_id", "successful_orders"}
    if not required.issubset(frame.columns):
        raise RegimeError(f"regime_report needs columns {sorted(required)}")
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"])
    work = work[work["successful_orders"].notna()]

    entities = []
    for entity_id, chunk in work.groupby("entity_id", sort=True):
        series = (
            chunk.set_index("date")["successful_orders"]
            .astype("float64")
            .sort_index()
        )
        try:
            features = regime_features(series, as_of=as_of)
        except RegimeError:
            continue
        entities.append(
            {"entity_id": str(entity_id), **features.as_dict()}
        )
    cutoff = (
        pd.Timestamp(as_of).date()
        if as_of is not None
        else (work["date"].max().date() if not work.empty else None)
    )
    return {
        "entities": entities,
        "as_of": cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff,
        "note": (
            "نقاط تغییر فقط از داده تا مبدأ پیش‌بینی محاسبه می‌شوند؛ "
            "این‌ها ویژگی‌های رژیم فعلی‌اند، نه پیش‌بینی آینده."
        ),
    }
