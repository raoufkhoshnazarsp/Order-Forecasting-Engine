"""Tests: extract normalization and the vertical -> stable-id mapping."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src import config
from src.ingestion.extract import (
    build_snapshot_metadata,
    measure_unmapped,
    normalize_orders_frame,
    split_leaves,
)


def raw_frame(rows):
    return pd.DataFrame(
        rows, columns=["date", "entity_type", "entity_id", "successful_orders"]
    )


def test_vertical_mapping_uses_stable_ids():
    vertical_map = config.load_yaml("hierarchy.yaml")["vertical_entity_map"]
    raw = raw_frame(
        [
            (date(2026, 1, 1), "VERTICAL", "H&B", 5),
            (date(2026, 1, 1), "VERTICAL", "Home", 3),
            (date(2026, 1, 1), "VENTURE", "4711", 2),
        ]
    )
    out = normalize_orders_frame(raw, vertical_map)
    ids = set(out["entity_id"])
    assert ids == {"H_B", "HOME", "4711"}


def test_unknown_vertical_is_not_renamed_or_dropped():
    raw = raw_frame([(date(2026, 1, 1), "VERTICAL", "NEW_VERTICAL", 4)])
    out = normalize_orders_frame(raw, {"DIGITAL": "DIGITAL"})
    assert out["entity_id"].tolist() == ["NEW_VERTICAL"]


def test_unmapped_is_reported_and_excluded_from_leaves():
    raw = raw_frame(
        [
            (date(2026, 1, 1), "VERTICAL", "FASHION", 10),
            (date(2026, 1, 1), "UNMAPPED", "", 7),
        ]
    )
    out = normalize_orders_frame(raw)
    leaves, unmapped = split_leaves(out)
    assert leaves["successful_orders"].sum() == 10
    stats = measure_unmapped(out)
    assert stats["unmapped_orders"] == 7
    assert stats["unmapped_rows"] == 1


def test_negative_orders_are_rejected():
    raw = raw_frame([(date(2026, 1, 1), "VERTICAL", "FASHION", -1)])
    with pytest.raises(ValueError):
        normalize_orders_frame(raw)


def test_missing_contract_column_is_rejected():
    raw = pd.DataFrame({"date": [date(2026, 1, 1)], "entity_id": ["A"]})
    with pytest.raises(ValueError):
        normalize_orders_frame(raw)


def test_snapshot_metadata_is_reproducible():
    meta = build_snapshot_metadata(
        snapshot_id="snap_1",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 2, 1),
        source_max_date=date(2026, 1, 31),
        query_version="v1",
        record_count=10,
        validation_status="PASSED",
    )
    assert meta["snapshot_id"] == "snap_1"
    assert meta["query_version"] == "v1"
    assert meta["record_count"] == 10
    assert meta["source_max_date"] == "2026-01-31"
