"""Sensitivity: "what if this one assumption changes?"

The planning scenarios in ``configs/scenarios.yaml`` are a fixed catalogue
applied to the whole book. A commercial analyst usually has a different
question: *if FASHION orders run 15% below the base for the whole horizon,
what happens to the total and to GMV?*

So a sensitivity is the same non-destructive transformation as a scenario —
``base * factor`` on one entity, parents rebuilt as exact sums — but the factor
and the target come from the user. It never mutates the base forecast and it
carries the assumption back in the payload so the number can be explained:

    {"entity_id": "FASHION", "delta_pct": -15, "factor": 0.85, "total": 12345}

``delta_pct`` is a relative change, not a percentage point. The result is a
planning statement, not a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.scenarios.scenario import Adjustment, Scenario, ScenarioError, apply_scenario

TOTAL_ID = "TOTAL"
MAX_DELTA = 60.0


class SensitivityError(ValueError):
    """The requested assumption cannot be applied safely."""


@dataclass(frozen=True)
class SensitivityResult:
    entity_id: str
    delta_pct: float
    factor: float
    base_total: float
    scenario_total: float
    base_gmv: float | None
    scenario_gmv: float | None
    by_origin: list[dict]
    per_entity: list[dict]

    def as_dict(self) -> dict:
        delta = self.scenario_total - self.base_total
        gmv_delta = (
            self.scenario_gmv - self.base_gmv
            if self.scenario_gmv is not None and self.base_gmv is not None
            else None
        )
        return {
            "entity_id": self.entity_id,
            "delta_pct": self.delta_pct,
            "factor": self.factor,
            "base_total": self.base_total,
            "scenario_total": self.scenario_total,
            "delta": delta,
            "delta_share": delta / self.base_total if self.base_total else None,
            "base_gmv": self.base_gmv,
            "scenario_gmv": self.scenario_gmv,
            "gmv_delta": gmv_delta,
            "by_origin": self.by_origin,
            "per_entity": self.per_entity,
        }


def _entity_totals(frame: pd.DataFrame) -> dict[str, float]:
    if frame is None or frame.empty:
        return {}
    grouped = frame.groupby("entity_id")["reconciled_yhat"].sum()
    return {str(k): float(v) for k, v in grouped.items()}


def apply_sensitivity(
    forecast: pd.DataFrame,
    *,
    entity_id: str,
    delta_pct: float,
    gmv_forecast: pd.DataFrame | None = None,
) -> SensitivityResult:
    """Scale one leaf by ``1 + delta_pct/100`` and rebuild the hierarchy."""
    if forecast is None or forecast.empty:
        raise SensitivityError("پیش‌بینی پایه‌ای برای سناریوی حساسیت وجود ندارد.")
    try:
        delta = float(delta_pct)
    except (TypeError, ValueError) as exc:
        raise SensitivityError("میزان تغییر باید یک عدد باشد.") from exc
    if delta <= -100 or abs(delta) > MAX_DELTA:
        raise SensitivityError(
            f"میزان تغییر باید بین −{int(MAX_DELTA)}٪ و +{int(MAX_DELTA)}٪ باشد."
        )

    present = set(forecast["entity_id"].astype(str))
    target = str(entity_id or "").strip().upper()
    if target not in present or target == TOTAL_ID:
        raise SensitivityError(
            "موجودیت انتخابی در این پیش‌بینی وجود ندارد."
        )

    factor = 1.0 + delta / 100.0
    spec = Scenario(
        name="sensitivity",
        label=f"{target} {delta:+.0f}٪",
        description="تغییر نسبی یک موجودیت روی پیش‌بینی پایه.",
        adjustments=[Adjustment(entity_id=target, factor=factor, note="user assumption")],
    )
    try:
        adjusted = apply_scenario(forecast, spec)
    except ScenarioError as exc:
        raise SensitivityError(str(exc)) from exc

    base_totals = _entity_totals(forecast)
    new_totals = _entity_totals(adjusted)
    base_total = base_totals.get(TOTAL_ID, 0.0)
    scenario_total = new_totals.get(TOTAL_ID, 0.0)

    base_gmv, scenario_gmv = _gmv_totals(forecast, adjusted, gmv_forecast, target, factor)

    base_daily = _daily_total(forecast)
    new_daily = _daily_total(adjusted)
    by_origin = [
        {
            "origin": str(origin),
            "base": base_daily.get(origin, 0.0),
            "scenario": new_daily.get(origin, 0.0),
        }
        for origin in sorted(set(base_daily) | set(new_daily))
    ]

    leaves = [
        e for e in sorted(set(base_totals) | set(new_totals)) if e != TOTAL_ID
    ]
    per_entity = []
    for entity in leaves:
        before = base_totals.get(entity, 0.0)
        after = new_totals.get(entity, 0.0)
        per_entity.append(
            {
                "entity_id": entity,
                "base": before,
                "scenario": after,
                "delta": after - before,
            }
        )

    return SensitivityResult(
        entity_id=target,
        delta_pct=delta,
        factor=factor,
        base_total=base_total,
        scenario_total=scenario_total,
        base_gmv=base_gmv,
        scenario_gmv=scenario_gmv,
        by_origin=by_origin,
        per_entity=per_entity,
    )


def _daily_total(frame: pd.DataFrame) -> dict[str, float]:
    total = frame[frame["entity_id"].astype(str).eq(TOTAL_ID)]
    if total.empty:
        return {}
    return {
        str(k): float(v) for k, v in total.groupby("forecast_origin")["reconciled_yhat"].sum().items()
    }


def _gmv_totals(
    base: pd.DataFrame,
    adjusted: pd.DataFrame,
    gmv_forecast: pd.DataFrame | None,
    target: str,
    factor: float,
) -> tuple[float | None, float | None]:
    """GMV at the TOTAL row. A scenario scales orders, not the AOV, so the
    GMV effect equals the order effect on a flat price book — which is exactly
    what the GMV frame produces when its orders are scaled by the same factor."""
    frame = gmv_forecast if gmv_forecast is not None else base
    column = "gmv_yhat"
    if frame is None or frame.empty or column not in frame.columns:
        return None, None
    total = frame[frame["entity_id"].astype(str).eq(TOTAL_ID)]
    if total.empty:
        return None, None
    base_gmv = float(total[column].sum())
    scaled = frame[frame["entity_id"].astype(str).eq(target)]
    if scaled.empty:
        return base_gmv, base_gmv
    effect = float(scaled[column].sum()) * (factor - 1.0)
    return base_gmv, base_gmv + effect
