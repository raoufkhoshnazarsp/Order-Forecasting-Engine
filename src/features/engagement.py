"""User, merchant and campaign features.

The engine's workbook contract makes these columns optional. When a column is
missing the engine must **not** invent a zero: a fake zero teaches a model that
customers and campaigns vanished. Instead the feature is simply absent, and the
caller can report exactly which families the input could not support.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

USER_COLUMNS = ("active_users", "new_users", "returning_users")
MERCHANT_COLUMNS = ("active_merchants", "new_merchants")
CAMPAIGN_COLUMNS = ("campaign_spend", "campaign_impressions", "campaign_clicks")

ALL_INPUT_COLUMNS = USER_COLUMNS + MERCHANT_COLUMNS + CAMPAIGN_COLUMNS


@dataclass(frozen=True)
class EngagementAvailability:
    """What the input actually supports, so nothing is silently faked."""

    available: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def families(self) -> list[str]:
        present = set(self.available)
        out = []
        if present & set(USER_COLUMNS):
            out.append("users")
        if present & set(MERCHANT_COLUMNS):
            out.append("merchants")
        if present & set(CAMPAIGN_COLUMNS):
            out.append("campaign")
        return out

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "missing": self.missing,
            "families": self.families,
        }


def availability(frame: pd.DataFrame) -> EngagementAvailability:
    available = [c for c in ALL_INPUT_COLUMNS if c in frame.columns]
    missing = [c for c in ALL_INPUT_COLUMNS if c not in frame.columns]
    return EngagementAvailability(available=available, missing=missing)


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Divide only where the denominator is positive; never divide by zero."""
    num = pd.to_numeric(numerator, errors="coerce").astype("float64")
    den = pd.to_numeric(denominator, errors="coerce").astype("float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(den > 0, num / den, np.nan)
    return pd.Series(ratio, index=numerator.index, dtype="float64")


def engagement_features(
    frame: pd.DataFrame, *, target: str = "successful_orders"
) -> pd.DataFrame:
    """Add every ratio the input supports; add nothing it does not.

    Ratios rather than raw counts are the useful features: a raw user count
    carries the size of the business, which the model already knows from the
    order level.
    """
    out = frame.copy()
    if target not in out.columns:
        raise ValueError(f"Frame has no {target!r} column")

    if "active_users" in out.columns:
        out["orders_per_active_user"] = _safe_ratio(out[target], out["active_users"])
    if "active_merchants" in out.columns:
        out["merchant_productivity"] = _safe_ratio(
            out[target], out["active_merchants"]
        )
    if "new_users" in out.columns and "active_users" in out.columns:
        out["new_user_share"] = _safe_ratio(out["new_users"], out["active_users"])
    if "returning_users" in out.columns and "active_users" in out.columns:
        out["returning_user_share"] = _safe_ratio(
            out["returning_users"], out["active_users"]
        )
    if "campaign_spend" in out.columns and "campaign_clicks" in out.columns:
        out["campaign_cost_per_click"] = _safe_ratio(
            out["campaign_spend"], out["campaign_clicks"]
        )
    if "campaign_impressions" in out.columns and "campaign_clicks" in out.columns:
        out["campaign_click_rate"] = _safe_ratio(
            out["campaign_clicks"], out["campaign_impressions"]
        )
    if "campaign_spend" in out.columns:
        out["campaign_active"] = (
            pd.to_numeric(out["campaign_spend"], errors="coerce").fillna(0) > 0
        ).astype("int8")
    return out


def campaign_plan_features(dates, plan: pd.DataFrame | None) -> pd.DataFrame:
    """Future campaign markers from a plan the business supplies.

    These are FUTURE_ESTIMATED: they are an assumption about the future, not an
    observation, so they are only produced when a plan is given.
    """
    index = pd.to_datetime(pd.Series(list(dates))).dt.normalize()
    if plan is None or plan.empty:
        return pd.DataFrame(index=index)
    required = {"start", "end"}
    if not required.issubset(plan.columns):
        raise ValueError("Campaign plan needs 'start' and 'end' columns")
    budget = (
        pd.to_numeric(plan["budget"], errors="coerce").astype("float64")
        if "budget" in plan.columns
        else pd.Series(1.0, index=plan.index)
    )
    planned = np.zeros(len(index), dtype="float64")
    days_to = np.full(len(index), np.nan)
    starts = []
    for row, amount in zip(plan.itertuples(index=False), budget):
        start = pd.Timestamp(row.start).normalize()
        end = pd.Timestamp(row.end).normalize()
        starts.append(start)
        inside = (index >= start) & (index <= end)
        planned[inside] += float(amount)
    for i, stamp in enumerate(index):
        forward = [s.toordinal() - stamp.date().toordinal() for s in starts
                   if s >= stamp]
        if forward:
            days_to[i] = min(forward)
    return pd.DataFrame(
        {
            "planned_budget": planned,
            "campaign_active": (planned > 0).astype("int8"),
            "days_to_campaign": days_to,
        },
        index=index,
    )
