"""Parameterized extraction of daily leaf-level facts from ClickHouse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

import pandas as pd

from src import config
from src.ingestion import render
from src.validation.target import TargetDefinition

LEAF_ENTITY_TYPES = ("VERTICAL", "VENTURE")


@dataclass(frozen=True)
class QuerySpec:
    name: str
    sql_path: str
    version: str


DAILY_LEAF_ORDERS = QuerySpec(
    name="daily_leaf_orders",
    sql_path="extraction/v1/daily_leaf_orders.sql",
    version="2026-11-15.daily_leaf_orders.v1",
)


def build_query(
    spec: QuerySpec,
    target: TargetDefinition,
    *,
    start_date: date,
    end_date: date,
    verticals: Sequence[str],
    venture_vertical_value: str,
    venture_category_value: str,
) -> str:
    """Render a query for the approved-target extraction.

    The ``scope_filters`` placeholder is filled with the *unmapped* marker
    predicate list from the Target Definition only. Scope exclusions from the
    Commercial report are never applied automatically.
    """
    if start_date >= end_date:
        raise ValueError("start_date must be before end_date (half-open interval)")

    template = config.load_sql(spec.sql_path)
    predicates = render.render_predicates(target.scope_predicates())
    return render.render_template(
        template,
        start_date=start_date,
        end_date=end_date,
        verticals=verticals,
        extra={
            "date_field": render.validate_column(target.date_field_for_draft()),
            "status_filter": target.status_predicate(),
            "scope_filters": predicates,
            "venture_vertical_value": venture_vertical_value,
            "venture_category_value": venture_category_value,
        },
    )


def run_daily_leaf_orders(
    client: Any,
    target: TargetDefinition,
    *,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Execute the canonical daily leaf-order extraction."""
    hierarchy = config.load_yaml("hierarchy.yaml")
    sql = build_query(
        DAILY_LEAF_ORDERS,
        target,
        start_date=start_date,
        end_date=end_date,
        verticals=(config.load_yaml("target.yaml"))["scope"]["verticals"],
        venture_vertical_value=hierarchy["resolution"]["venture_vertical_value"],
        venture_category_value=hierarchy["resolution"]["venture_category_value"],
    )
    frame = normalize_orders_frame(
        client.query_df(sql), hierarchy.get("vertical_entity_map")
    )
    return frame


def normalize_orders_frame(
    frame: pd.DataFrame, vertical_entity_map: dict[str, str] | None = None
) -> pd.DataFrame:
    """Coerce extracted rows into the documented contract.

    Vertical rows arrive with the raw source vertical value; they are mapped to
    the stable entity_id from the Entity Master. Unmapped verticals are kept
    and flagged rather than renamed silently.
    """
    expected = ["date", "entity_type", "entity_id", "successful_orders"]
    missing = [c for c in expected if c not in frame.columns]
    if missing:
        raise ValueError(f"Extraction output missing columns: {missing}")
    out = frame.loc[:, expected].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    out["entity_type"] = out["entity_type"].astype(str).str.upper()
    out["entity_id"] = out["entity_id"].fillna("").astype(str)
    out["successful_orders"] = out["successful_orders"].astype("int64")
    if (out["successful_orders"] < 0).any():
        raise ValueError("Extraction returned negative order counts")
    if vertical_entity_map:
        is_vertical = out["entity_type"] == "VERTICAL"
        out.loc[is_vertical, "entity_id"] = (
            out.loc[is_vertical, "entity_id"].map(vertical_entity_map).fillna(
                out.loc[is_vertical, "entity_id"]
            )
        )
    return out.sort_values(["date", "entity_type", "entity_id"]).reset_index(drop=True)


def measure_unmapped(frame: pd.DataFrame) -> dict[str, int]:
    """UNMAPPED orders must be reported, never silently dropped."""
    unmapped = frame[frame["entity_type"] == "UNMAPPED"]
    return {
        "unmapped_rows": int(len(unmapped)),
        "unmapped_orders": int(unmapped["successful_orders"].sum()),
        "unmapped_entities": int(unmapped.loc[unmapped["entity_type"] == "UNMAPPED", "entity_id"].nunique()),
    }


def split_leaves(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separate leaf entities from the UNMAPPED report rows."""
    mask = frame["entity_type"].isin(LEAF_ENTITY_TYPES)
    return frame[mask].copy(), frame[~mask].copy()


def build_snapshot_metadata(
    *,
    snapshot_id: str,
    start_date: date,
    end_date: date,
    source_max_date: date | None,
    query_version: str,
    record_count: int,
    validation_status: str,
    extraction_start: datetime | None = None,
    extraction_end: datetime | None = None,
) -> dict[str, Any]:
    """Reproducibility metadata recorded for every extraction (doc 7.4)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "snapshot_id": snapshot_id,
        "extraction_start": (extraction_start or now).isoformat(),
        "extraction_end": (extraction_end or now).isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "source_max_date": source_max_date.isoformat() if source_max_date else None,
        "query_version": query_version,
        "record_count": int(record_count),
        "validation_status": validation_status,
    }
