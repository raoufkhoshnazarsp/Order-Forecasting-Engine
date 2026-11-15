"""Tests: data mart grain, missing-vs-zero, and total reconciliation."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.data_mart.entity_master import EntityMaster, EntityMasterError
from src.data_mart.orders import (
    QualityFlag,
    build_daily_mart,
    date_range,
    derive_total,
    validate_total_reconciliation,
)


@pytest.fixture
def master() -> EntityMaster:
    return EntityMaster.load("configs/entity_master.csv")


@pytest.fixture
def master_with_venture(tmp_path) -> EntityMaster:
    csv = tmp_path / "entity_master.csv"
    csv.write_text(
        "entity_id,entity_name,entity_type,parent_entity_id,is_active,valid_from,valid_to\n"
        "TOTAL,Total Business,TOTAL,,true,2023-01-01,\n"
        "FASHION,Fashion,VERTICAL,TOTAL,true,2023-01-01,\n"
        "GOLD,Gold,VERTICAL,TOTAL,true,2023-01-01,\n"
        "V001,Venture One,VENTURE,TOTAL,true,2023-01-01,\n",
        encoding="utf-8",
    )
    return EntityMaster.load(csv)


def leaf_facts(rows):
    return pd.DataFrame(rows, columns=["date", "entity_type", "entity_id", "successful_orders"])


def test_date_range_is_half_open():
    assert date_range(date(2026, 1, 1), date(2026, 1, 4)) == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    ]
    with pytest.raises(ValueError):
        date_range(date(2026, 1, 4), date(2026, 1, 4))


def test_entity_master_has_one_total_and_unique_leaves(master):
    assert len(master.totals()) == 1
    assert master.total_id() == "TOTAL"
    ids = [e.entity_id for e in master.leaves()]
    assert len(ids) == len(set(ids))


def test_entity_master_rejects_duplicate_ids():
    frame = pd.DataFrame(
        {
            "entity_id": ["TOTAL", "A", "A"],
            "entity_name": ["Total", "A", "A2"],
            "entity_type": ["TOTAL", "VERTICAL", "VERTICAL"],
            "parent_entity_id": ["", "TOTAL", "TOTAL"],
            "is_active": ["true", "true", "true"],
            "valid_from": ["2023-01-01", "", ""],
            "valid_to": ["", "", ""],
        }
    )
    with pytest.raises(EntityMasterError):
        EntityMaster(frame)


def test_entity_master_rejects_unknown_parent():
    frame = pd.DataFrame(
        {
            "entity_id": ["TOTAL", "A"],
            "entity_name": ["Total", "A"],
            "entity_type": ["TOTAL", "VERTICAL"],
            "parent_entity_id": ["", "NOPE"],
            "is_active": ["true", "true"],
            "valid_from": ["2023-01-01", ""],
            "valid_to": ["", ""],
        }
    )
    with pytest.raises(EntityMasterError):
        EntityMaster(frame)


def test_mart_grain_is_one_row_per_date_leaf(master):
    facts = leaf_facts(
        [
            (date(2026, 1, 1), "VERTICAL", "FASHION", 10),
            (date(2026, 1, 2), "VERTICAL", "FASHION", 12),
        ]
    )
    mart = build_daily_mart(
        facts,
        master,
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
        snapshot_id="snap_1",
        observed_dates={date(2026, 1, 1), date(2026, 1, 2)},
    )
    leaves = mart[mart["entity_type"].isin(["VERTICAL", "VENTURE"])]
    assert len(leaves) == len(master.leaves()) * 2
    assert not leaves.duplicated(["date", "entity_id"]).any()


def test_missing_source_is_not_zero(master):
    facts = leaf_facts([(date(2026, 1, 1), "VERTICAL", "FASHION", 10)])
    mart = build_daily_mart(
        facts,
        master,
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
        snapshot_id="snap_1",
        observed_dates={date(2026, 1, 1)},  # day 2 has no source partition
    )
    day2 = mart[(mart["date"] == date(2026, 1, 2)) & (mart["entity_id"] == "FASHION")]
    assert day2["quality_flag"].iloc[0] == QualityFlag.MISSING_SOURCE.value
    assert pd.isna(day2["successful_orders"].iloc[0])


def test_genuine_zero_is_zero(master):
    """Observed date + active entity + no orders == genuine zero, not NaN."""
    facts = leaf_facts([(date(2026, 1, 1), "VERTICAL", "FASHION", 10)])
    mart = build_daily_mart(
        facts,
        master,
        start=date(2026, 1, 1),
        end=date(2026, 1, 2),
        snapshot_id="snap_1",
        observed_dates={date(2026, 1, 1)},
    )
    gold = mart[(mart["entity_id"] == "GOLD") & (mart["date"] == date(2026, 1, 1))]
    assert gold["successful_orders"].iloc[0] == 0
    assert gold["quality_flag"].iloc[0] == QualityFlag.OBSERVED.value


def test_total_equals_leaf_sum(master_with_venture):
    master = master_with_venture
    facts = leaf_facts(
        [
            (date(2026, 1, 1), "VERTICAL", "FASHION", 10),
            (date(2026, 1, 1), "VERTICAL", "GOLD", 5),
            (date(2026, 1, 1), "VENTURE", "V001", 7),
            (date(2026, 1, 2), "VERTICAL", "FASHION", 3),
            (date(2026, 1, 2), "VENTURE", "V001", 1),
        ]
    )
    mart = build_daily_mart(
        facts,
        master,
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
        snapshot_id="snap_1",
        observed_dates={date(2026, 1, 1), date(2026, 1, 2)},
    )
    totals = mart[mart["entity_type"] == "TOTAL"].set_index("date")["successful_orders"]
    assert totals[date(2026, 1, 1)] == 22
    assert totals[date(2026, 1, 2)] == 4
    assert validate_total_reconciliation(mart)


def test_total_never_double_counts_venture(master_with_venture):
    """A venture row must not also be counted under a vertical."""
    master = master_with_venture
    facts = leaf_facts(
        [
            (date(2026, 1, 1), "VERTICAL", "FASHION", 10),
            (date(2026, 1, 1), "VENTURE", "V001", 100),
        ]
    )
    mart = build_daily_mart(
        facts,
        master,
        start=date(2026, 1, 1),
        end=date(2026, 1, 2),
        snapshot_id="snap_1",
        observed_dates={date(2026, 1, 1)},
    )
    total = mart[(mart["entity_type"] == "TOTAL")]["successful_orders"].iloc[0]
    assert total == 110


def test_unknown_entity_id_is_an_error(master):
    facts = leaf_facts([(date(2026, 1, 1), "VERTICAL", "GHOST", 1)])
    with pytest.raises(ValueError):
        build_daily_mart(
            facts,
            master,
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            snapshot_id="snap_1",
            observed_dates={date(2026, 1, 1)},
        )


def test_derive_total_ignores_unknown_values(master):
    frame = pd.DataFrame(
        {
            "date": [date(2026, 1, 1)] * 2,
            "entity_id": ["A", "B"],
            "successful_orders": pd.array([5, pd.NA], dtype="Int64"),
            "quality_flag": [QualityFlag.OBSERVED.value, QualityFlag.MISSING_SOURCE.value],
            "data_snapshot_id": ["s", "s"],
            "extraction_timestamp": [pd.Timestamp("2026-01-02")] * 2,
        }
    )
    total = derive_total(frame, "TOTAL")
    assert total["successful_orders"].iloc[0] == 5
