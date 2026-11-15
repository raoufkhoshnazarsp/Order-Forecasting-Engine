"""Daily data mart: one row per date x leaf entity, plus derived TOTAL.

Key rules from the architecture doc:
  * Grain is ``date x leaf entity``; TOTAL is derived from leaves only.
  * Missing source data must not silently become a zero (7.3).
  * Every active leaf gets a complete daily calendar.
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

import pandas as pd

from src.data_mart.entity_master import EntityMaster

FACT_COLUMNS = [
    "date",
    "entity_id",
    "entity_type",
    "successful_orders",
    "active_users",
    "new_users",
    "returning_users",
    "organic_orders",
    "non_organic_orders",
    "active_merchants",
    "new_merchants",
    "campaign_spend",
    "campaign_impressions",
    "campaign_clicks",
    "data_snapshot_id",
    "extraction_timestamp",
]

ORDER_METRICS = ("successful_orders", "active_users", "new_users", "returning_users",
                 "organic_orders", "non_organic_orders", "active_merchants", "new_merchants")


class QualityFlag(str, Enum):
    """Why a leaf-day row holds the value it holds (doc 7.3)."""

    OBSERVED = "OBSERVED"                    # genuine value, including a true zero
    ENTITY_NOT_ACTIVE = "ENTITY_NOT_ACTIVE"  # entity had not started yet
    ENTITY_DEACTIVATED = "ENTITY_DEACTIVATED"
    MISSING_SOURCE = "MISSING_SOURCE"        # source partition absent -> unknown, not zero
    MAPPING_UNAVAILABLE = "MAPPING_UNAVAILABLE"


def date_range(start: date, end: date) -> list[date]:
    """Half-open [start, end) date range."""
    if start >= end:
        raise ValueError("start must be before end")
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days)]


def _entity_state(entity, day: date) -> QualityFlag:
    if entity.valid_from and day < date.fromisoformat(entity.valid_from):
        return QualityFlag.ENTITY_NOT_ACTIVE
    if entity.valid_to and day >= date.fromisoformat(entity.valid_to):
        return QualityFlag.ENTITY_DEACTIVATED
    return QualityFlag.OBSERVED


def build_full_calendar(
    entity_master: EntityMaster,
    start: date,
    end: date,
    *,
    observed_dates: set[date] | frozenset[date] | None = None,
) -> pd.DataFrame:
    """Cartesian product of dates x active leaves with quality flags.

    ``observed_dates`` are dates for which the source extraction actually
    returned data. A date absent from that set is ``MISSING_SOURCE`` and its
    metric would be unknown — never an implicit zero.
    """
    days = date_range(start, end)
    rows: list[dict] = []
    for entity in entity_master.leaves(active_only=False):
        for day in days:
            state = _entity_state(entity, day)
            if state is QualityFlag.OBSERVED and observed_dates is not None:
                if day not in observed_dates:
                    state = QualityFlag.MISSING_SOURCE
            rows.append(
                {
                    "date": day,
                    "entity_id": entity.entity_id,
                    "entity_name": entity.entity_name,
                    "entity_type": entity.entity_type,
                    "is_active": entity.is_active,
                    "quality_flag": state.value,
                }
            )
    return pd.DataFrame(rows)


def build_daily_mart(
    leaf_facts: pd.DataFrame,
    entity_master: EntityMaster,
    *,
    start: date,
    end: date,
    snapshot_id: str,
    extraction_timestamp: pd.Timestamp | None = None,
    observed_dates: set[date] | frozenset[date] | None = None,
) -> pd.DataFrame:
    """Join extracted leaf facts onto the complete calendar and derive TOTAL."""
    calendar = build_full_calendar(
        entity_master, start, end, observed_dates=observed_dates
    )
    facts = leaf_facts.copy()
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    facts = facts[facts["entity_type"].isin(["VERTICAL", "VENTURE"])]

    unknown_ids = set(facts["entity_id"]) - set(calendar["entity_id"])
    if unknown_ids:
        raise ValueError(
            "Extracted rows reference entities missing from the master: "
            f"{sorted(unknown_ids)}"
        )

    metric_cols = [c for c in ORDER_METRICS if c in facts.columns]
    facts = facts.assign(_fact_present=True)
    merged = calendar.merge(
        facts[["date", "entity_id", *metric_cols, "_fact_present"]],
        on=["date", "entity_id"],
        how="left",
    )

    present = merged["_fact_present"].fillna(False).astype(bool)
    # A row with no matching fact: only a genuine zero if that exact
    # (date, entity) was observed, the entity was active and in scope.
    # Otherwise the value is unknown and must stay missing, never zero.
    genuine_zero = (
        ~present
        & merged["quality_flag"].eq(QualityFlag.OBSERVED.value)
        & merged["is_active"].astype(bool)
    )
    for col in metric_cols:
        filled = merged[col].fillna(0)
        merged[col] = filled.where(present | genuine_zero, other=pd.NA)
        if filled.dtype.kind in "if":
            merged[col] = merged[col].astype("Int64")
    merged = merged.drop(columns=["_fact_present"])

    timestamp = extraction_timestamp or pd.Timestamp.now(tz="UTC").tz_localize(None)
    merged["data_snapshot_id"] = snapshot_id
    merged["extraction_timestamp"] = timestamp

    totals = derive_total(merged, entity_master.total_id())
    result = pd.concat([merged, totals], ignore_index=True)
    result["entity_type"] = result["entity_type"].astype("string")
    return result.sort_values(["date", "entity_type", "entity_id"]).reset_index(drop=True)


def derive_total(leaf_rows: pd.DataFrame, total_id: str) -> pd.DataFrame:
    """Total Business = sum of leaf entities (never an independent query)."""
    metric_cols = [c for c in ORDER_METRICS if c in leaf_rows.columns]
    observed = leaf_rows[leaf_rows["quality_flag"] == QualityFlag.OBSERVED.value]
    grouped = observed.groupby("date", as_index=False)[metric_cols].sum(min_count=0)
    grouped["entity_id"] = total_id
    grouped["entity_name"] = "Total Business"
    grouped["entity_type"] = "TOTAL"
    grouped["is_active"] = True
    grouped["quality_flag"] = QualityFlag.OBSERVED.value
    grouped["data_snapshot_id"] = leaf_rows["data_snapshot_id"].iloc[0]
    grouped["extraction_timestamp"] = leaf_rows["extraction_timestamp"].iloc[0]
    return grouped


def validate_total_reconciliation(mart: pd.DataFrame) -> bool:
    """Assert Total == sum(Vertical) + sum(Venture) for every date."""
    if mart.empty:
        return True
    metric = "successful_orders"
    if metric not in mart.columns:
        metric = next((c for c in ORDER_METRICS if c in mart.columns), None)
    if metric is None:
        raise ValueError("Mart holds no metric columns to reconcile")

    leaf = mart[
        mart["entity_type"].isin(["VERTICAL", "VENTURE"])
        & (mart["quality_flag"] == QualityFlag.OBSERVED.value)
    ]
    total = mart[mart["entity_type"] == "TOTAL"].set_index("date")[metric]
    leaf_sum = leaf.groupby("date")[metric].sum()
    joined = pd.concat({"total": total, "leaf": leaf_sum}, axis=1).dropna()
    mismatch = joined[joined["total"] != joined["leaf"]]
    if not mismatch.empty:
        raise ValueError(
            f"Total != leaf sum on {len(mismatch)} dates: "
            f"{mismatch.head(5).to_dict('index')}"
        )
    return True
