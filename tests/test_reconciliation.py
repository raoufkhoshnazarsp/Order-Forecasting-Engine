"""Tests: hierarchy construction, reconciliation and rounding coherence."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data_mart.entity_master import EntityMaster
from src.reconciliation.reconcile import (
    ReconciliationError,
    assert_coherent,
    bottom_up,
    coherent_round,
    compare_methods,
    mintrace,
    reconcile,
    residuals_from_backtest,
    top_down,
)
from src.reconciliation.summing_matrix import SummingMatrixError, build_summing_matrix


@pytest.fixture
def master() -> EntityMaster:
    return EntityMaster.load("configs/entity_master.csv")


@pytest.fixture
def master_with_ventures(tmp_path):
    csv = tmp_path / "entity_master.csv"
    csv.write_text(
        "entity_id,entity_name,entity_type,parent_entity_id,is_active,valid_from,valid_to\n"
        "TOTAL,Total Business,TOTAL,,true,2023-01-01,\n"
        "FASHION,Fashion,VERTICAL,TOTAL,true,2023-01-01,\n"
        "GOLD,Gold,VERTICAL,TOTAL,true,2023-01-01,\n"
        "V001,Venture One,VENTURE,TOTAL,true,2023-01-01,\n"
        "V002,Venture Two,VENTURE,TOTAL,true,2023-06-01,\n",
        encoding="utf-8",
    )
    return EntityMaster.load(csv)


def test_matrix_shape_matches_entity_master(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    assert list(matrix.columns) == ["FASHION", "GOLD", "V001", "V002"]
    assert list(matrix.index) == ["TOTAL", "FASHION", "GOLD", "V001", "V002"]
    assert matrix.loc["TOTAL"].tolist() == [1, 1, 1, 1]
    assert matrix.loc["FASHION"].tolist() == [1, 0, 0, 0]
    assert matrix.loc["V001"].tolist() == [0, 0, 1, 0]


def test_matrix_rejects_duplicate_leaves(master_with_ventures):
    with pytest.raises(SummingMatrixError):
        build_summing_matrix(master_with_ventures, ["FASHION", "FASHION"])


def base_forecasts():
    return pd.DataFrame(
        {
            "forecast_origin": [date(2026, 1, 1)] * 4,
            "target_date": [date(2026, 1, 2)] * 4,
            "entity_id": ["FASHION", "GOLD", "V001", "V002"],
            "yhat": [10.0, 20.0, 5.0, 7.0],
        }
    )


def test_bottom_up_is_coherent(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    out = bottom_up(base_forecasts(), matrix)
    assert assert_coherent(out, master_with_ventures, "yhat")
    total = out[out["entity_id"] == "TOTAL"]["yhat"].iloc[0]
    assert total == 42.0


def test_mintrace_is_coherent(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    base = base_forecasts()
    base.loc[base["entity_id"] == "TOTAL", "yhat"] = 50.0  # deliberately incoherent
    base = pd.concat([base, pd.DataFrame({
        "forecast_origin": [date(2026, 1, 1)],
        "target_date": [date(2026, 1, 2)],
        "entity_id": ["TOTAL"],
        "yhat": [50.0],
    })], ignore_index=True)
    out = mintrace(base, matrix)
    assert assert_coherent(out, master_with_ventures, "yhat")


def test_top_down_preserves_total(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    base = base_forecasts()
    base = pd.concat([base, pd.DataFrame({
        "forecast_origin": [date(2026, 1, 1)],
        "target_date": [date(2026, 1, 2)],
        "entity_id": ["TOTAL"],
        "yhat": [100.0],
    })], ignore_index=True)
    history = pd.DataFrame(
        {
            "entity_id": ["FASHION", "GOLD", "V001", "V002"],
            "successful_orders": [10, 30, 50, 10],
        }
    )
    out = top_down(base, matrix, history)
    assert assert_coherent(out, master_with_ventures, "yhat")
    assert out[out["entity_id"] == "TOTAL"]["yhat"].iloc[0] == pytest.approx(100.0)


def test_coherent_round_sums_exactly(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    base = base_forecasts()
    base["yhat"] = [10.4, 20.6, 5.5, 7.5]
    out = bottom_up(base, matrix)
    rounded = coherent_round(out, master_with_ventures)
    assert assert_coherent(rounded, master_with_ventures, "reconciled_yhat")
    leaves = rounded[rounded["entity_id"].isin(matrix.columns)]["reconciled_yhat"]
    assert (leaves == leaves.round()).all()
    total = rounded[rounded["entity_id"] == "TOTAL"]["reconciled_yhat"].iloc[0]
    assert total == leaves.sum()


def test_reconcile_dispatch_and_unknown_method(master_with_ventures):
    out = reconcile(base_forecasts(), master_with_ventures, method="bottom_up")
    assert "yhat" in out.columns
    with pytest.raises(ReconciliationError):
        reconcile(base_forecasts(), master_with_ventures, method="magic")
    with pytest.raises(ReconciliationError):
        reconcile(base_forecasts(), master_with_ventures, method="top_down")


def test_assert_coherent_detects_violation(master_with_ventures):
    bad = base_forecasts()
    bad = pd.concat([bad, pd.DataFrame({
        "forecast_origin": [date(2026, 1, 1)],
        "target_date": [date(2026, 1, 2)],
        "entity_id": ["TOTAL"],
        "yhat": [999.0],
    })], ignore_index=True)
    with pytest.raises(ReconciliationError):
        assert_coherent(bad, master_with_ventures, "yhat")


# ------------------------------------------------- historical hierarchy
def test_matrix_respects_the_validity_window(master_with_ventures):
    """A venture that launched in June must not exist in a January matrix."""
    early = build_summing_matrix(master_with_ventures, as_of=date(2023, 1, 15))
    assert "V002" not in early.columns
    assert "V001" in early.columns
    late = build_summing_matrix(master_with_ventures, as_of=date(2023, 7, 1))
    assert "V002" in late.columns


def test_matrix_without_as_of_keeps_every_leaf(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    assert "V002" in matrix.columns


def test_retired_leaf_drops_out_after_its_valid_to(tmp_path):
    csv = tmp_path / "entity_master.csv"
    csv.write_text(
        "entity_id,entity_name,entity_type,parent_entity_id,is_active,valid_from,valid_to\n"
        "TOTAL,Total Business,TOTAL,,true,2023-01-01,\n"
        "FASHION,Fashion,VERTICAL,TOTAL,true,2023-01-01,\n"
        "V001,Venture One,VENTURE,TOTAL,false,2023-01-01,2025-12-31\n",
        encoding="utf-8",
    )
    master = EntityMaster.load(csv)
    assert "V001" in build_summing_matrix(master, as_of=date(2025, 6, 1)).columns
    assert "V001" not in build_summing_matrix(master, as_of=date(2026, 6, 1)).columns


# --------------------------------------------------- residual covariance
def test_residuals_from_backtest_are_actual_minus_forecast():
    frame = pd.DataFrame(
        {
            "forecast_origin": [date(2026, 1, 1)] * 2,
            "target_date": [date(2026, 1, 2)] * 2,
            "entity_id": ["FASHION", "GOLD"],
            "yhat": [10.0, 20.0],
            "actual": [12.0, 18.0],
            "model_name": ["m", "m"],
        }
    )
    residuals = residuals_from_backtest(frame, "m")
    assert residuals.loc[(date(2026, 1, 1), date(2026, 1, 2)), "FASHION"] == 2.0
    assert residuals.loc[(date(2026, 1, 1), date(2026, 1, 2)), "GOLD"] == -2.0


def test_residuals_from_backtest_filters_by_model():
    frame = pd.DataFrame(
        {
            "forecast_origin": [date(2026, 1, 1)] * 2,
            "target_date": [date(2026, 1, 2)] * 2,
            "entity_id": ["FASHION", "FASHION"],
            "yhat": [10.0, 10.0],
            "actual": [12.0, 12.0],
            "model_name": ["a", "b"],
        }
    )
    assert residuals_from_backtest(frame, "missing").empty


def test_shrunk_mintrace_stays_coherent(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    base = base_forecasts()
    base = pd.concat([base, pd.DataFrame({
        "forecast_origin": [date(2026, 1, 1)],
        "target_date": [date(2026, 1, 2)],
        "entity_id": ["TOTAL"],
        "yhat": [50.0],
    })], ignore_index=True)
    residuals = pd.DataFrame(
        {
            "FASHION": [1.0, -2.0, 0.5, 1.5, -1.0],
            "GOLD": [2.0, 1.0, -1.0, 0.0, 1.0],
            "V001": [0.5, 0.2, -0.3, 0.1, 0.0],
            "V002": [0.1, -0.1, 0.2, 0.0, 0.1],
        }
    )
    out = mintrace(base, matrix, residuals, method="shrunk")
    assert assert_coherent(out, master_with_ventures, "yhat")


def test_unknown_covariance_method_is_rejected(master_with_ventures):
    matrix = build_summing_matrix(master_with_ventures)
    with pytest.raises(ReconciliationError):
        mintrace(base_forecasts(), matrix, pd.DataFrame(), method="magic")


def test_reconcile_dispatches_every_mintrace_variant(master_with_ventures):
    for method in ("mint_ols", "mint_wls", "mint_shrunk"):
        out = reconcile(base_forecasts(), master_with_ventures, method=method)
        assert assert_coherent(out, master_with_ventures, "yhat")


# ------------------------------------------------------ method comparison
def test_compare_methods_reports_error_per_level(master_with_ventures):
    base = base_forecasts()
    base = pd.concat([base, pd.DataFrame({
        "forecast_origin": [date(2026, 1, 1)],
        "target_date": [date(2026, 1, 2)],
        "entity_id": ["TOTAL"],
        "yhat": [50.0],
    })], ignore_index=True)
    actuals = base.copy()
    actuals["actual"] = [10.0, 20.0, 5.0, 7.0, 42.0]
    history = pd.DataFrame(
        {
            "entity_id": ["FASHION", "GOLD", "V001", "V002"],
            "successful_orders": [10, 30, 50, 10],
        }
    )
    table = compare_methods(base, master_with_ventures, actuals, historical=history)
    assert set(table["level"]) == {"TOTAL", "LEAF"}
    assert "bottom_up" in set(table["method"])
    assert (table["points"] > 0).all()
    assert table["wape"].notna().all()


def test_compare_methods_on_empty_input_returns_empty():
    master = EntityMaster.load("configs/entity_master.csv")
    table = compare_methods(pd.DataFrame(), master, pd.DataFrame())
    assert table.empty
