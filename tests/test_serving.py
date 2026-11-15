"""Tests: forecast serving output contract and guardrails."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data_mart.entity_master import EntityMaster
from src.models.baselines import naive, seasonal_naive
from src.serving.service import (
    OUTPUT_CONTRACT,
    RunContext,
    ServingError,
    run_forecast,
    to_records,
    validate_forecast,
)


@pytest.fixture
def master(tmp_path) -> EntityMaster:
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


@pytest.fixture
def mart() -> pd.DataFrame:
    dates = pd.date_range("2026-09-01", periods=60, freq="D").date
    rows = []
    for entity, base in (("FASHION", 100), ("GOLD", 40), ("V001", 25)):
        for i, day in enumerate(dates):
            rows.append(
                {
                    "date": day,
                    "entity_id": entity,
                    "entity_type": "VERTICAL" if entity != "V001" else "VENTURE",
                    "successful_orders": base + 5 * np.sin(i * 2 * np.pi / 7),
                    "quality_flag": "OBSERVED",
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def context() -> RunContext:
    return RunContext(
        forecast_run_id="run_test_001",
        forecast_origin=date(2026, 10, 30),
        model_name="seasonal_naive",
        model_version="baseline_seasonal_naive_v1",
        feature_version="features_v1",
        data_snapshot_id="snap_test",
    )


def test_forecast_matches_output_contract(master, mart, context):
    out = run_forecast(mart, master, {"seasonal_naive": seasonal_naive}, context=context, horizon=30)
    assert list(out.columns) == OUTPUT_CONTRACT
    assert out["entity_id"].nunique() == 4  # 3 leaves + TOTAL
    assert out["target_date"].min() == date(2026, 10, 31)
    assert out["target_date"].max() == date(2026, 11, 29)
    assert out["horizon"].max() == 30


def test_forecast_is_non_negative_and_intervals_nested(master, mart, context):
    out = run_forecast(mart, master, {"naive": naive}, context=context, horizon=10)
    assert (out["yhat"] >= 0).all()
    assert (out["lower_80"] <= out["yhat"]).all()
    assert (out["yhat"] <= out["upper_80"]).all()
    assert (out["lower_90"] <= out["lower_80"]).all()
    assert (out["upper_80"] <= out["upper_90"]).all()


def test_reconciled_total_equals_leaf_sum(master, mart, context):
    out = run_forecast(mart, master, {"naive": naive}, context=context, horizon=10)
    totals = out[out["entity_id"] == "TOTAL"].set_index("target_date")["reconciled_yhat"]
    leaves = (
        out[out["entity_id"].isin(["FASHION", "GOLD", "V001"])]
        .groupby("target_date")["reconciled_yhat"].sum()
    )
    pd.testing.assert_series_equal(totals.sort_index(), leaves.sort_index(), check_names=False)
    assert (totals == totals.round()).all()


def test_validate_rejects_negative_forecast(master, mart, context):
    out = run_forecast(mart, master, {"naive": naive}, context=context, horizon=5)
    broken = out.copy()
    broken.loc[broken.index[0], "yhat"] = -1
    with pytest.raises(ServingError):
        validate_forecast(broken, master, 5)


def test_validate_rejects_unordered_intervals(master, mart, context):
    out = run_forecast(mart, master, {"naive": naive}, context=context, horizon=5)
    broken = out.copy()
    broken.loc[broken.index[0], "lower_80"] = broken.loc[broken.index[0], "yhat"] + 100
    with pytest.raises(ServingError):
        validate_forecast(broken, master, 5)


def test_validate_rejects_incomplete_horizon(master, mart, context):
    out = run_forecast(mart, master, {"naive": naive}, context=context, horizon=5)
    truncated = out[out["horizon"] <= 3]
    with pytest.raises(ServingError):
        validate_forecast(truncated, master, 5)


def test_empty_mart_is_rejected(master, context):
    empty = pd.DataFrame(columns=["date", "entity_id", "entity_type", "successful_orders", "quality_flag"])
    with pytest.raises(ServingError):
        run_forecast(empty, master, {"naive": naive}, context=context, horizon=5)


def test_to_records_is_json_safe(master, mart, context):
    out = run_forecast(mart, master, {"naive": naive}, context=context, horizon=3)
    records = to_records(out.head(3))
    assert isinstance(records[0]["forecast_origin"], str)
    assert isinstance(records[0]["target_date"], str)
    assert isinstance(records[0]["yhat"], float)
