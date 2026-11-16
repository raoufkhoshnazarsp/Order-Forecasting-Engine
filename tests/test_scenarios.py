"""Tests: scenarios as a reproducible, non-destructive transformation.

The properties that matter:

* the base forecast is byte-for-byte unchanged after a scenario,
* the scenario is coherent at every level,
* the same spec yields the same ``scenario_id`` and the same numbers,
* an invalid rule fails loudly instead of silently doing nothing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.scenarios.scenario import (
    Adjustment,
    Scenario,
    ScenarioError,
    apply_scenario,
    comparison,
    load_scenarios,
)


def base_forecast(days: int = 5) -> pd.DataFrame:
    rows = []
    for day in range(1, days + 1):
        leaves = {"FASHION": 100.0 + day, "GOLD": 50.0 + day, "V001": 10.0 + day}
        for entity_id, value in leaves.items():
            rows.append(
                {
                    "forecast_origin": "2026-01-01",
                    "target_date": f"2026-01-{day + 1:02d}",
                    "entity_id": entity_id,
                    "entity_type": "VENTURE" if entity_id.startswith("V") else "VERTICAL",
                    "horizon": day,
                    "yhat": value,
                    "reconciled_yhat": value,
                    "lower_80": value * 0.8,
                    "upper_80": value * 1.2,
                    "lower_90": value * 0.7,
                    "upper_90": value * 1.3,
                    "model_version": "baseline_v1",
                }
            )
        total = sum(leaves.values())
        rows.append(
            {
                "forecast_origin": "2026-01-01",
                "target_date": f"2026-01-{day + 1:02d}",
                "entity_id": "TOTAL",
                "entity_type": "TOTAL",
                "horizon": day,
                "yhat": total,
                "reconciled_yhat": total,
                "lower_80": total * 0.8,
                "upper_80": total * 1.2,
                "lower_90": total * 0.7,
                "upper_90": total * 1.3,
                "model_version": "baseline_v1",
            }
        )
    return pd.DataFrame(rows)


def scenario(name="s", adjustments=None) -> Scenario:
    return Scenario(
        name=name,
        label=name,
        adjustments=adjustments or [Adjustment(entity_id="ALL", factor=1.2)],
    )


def leaf_total(frame: pd.DataFrame, entity_id: str) -> float:
    return float(frame[frame["entity_id"].eq(entity_id)]["reconciled_yhat"].sum())


# ------------------------------------------------------------ purity
def test_base_forecast_is_not_modified():
    base = base_forecast()
    snapshot = base.copy(deep=True)
    apply_scenario(base, scenario(), None)
    pd.testing.assert_frame_equal(base, snapshot)


def test_scenario_output_is_a_new_frame():
    base = base_forecast()
    out = apply_scenario(base, scenario(), None)
    assert out is not base


# ---------------------------------------------------------- coherence
def test_parent_is_the_exact_sum_of_the_leaves():
    out = apply_scenario(base_forecast(), scenario(), None)
    for day, chunk in out.groupby("target_date"):
        total = chunk[chunk["entity_id"].eq("TOTAL")]["reconciled_yhat"].iloc[0]
        leaves = chunk[~chunk["entity_id"].eq("TOTAL")]["reconciled_yhat"].sum()
        assert total == pytest.approx(leaves)


def test_factor_scales_every_leaf_and_the_total():
    base = base_forecast()
    out = apply_scenario(base, scenario(adjustments=[Adjustment("ALL", factor=1.25)]), None)
    assert leaf_total(out, "FASHION") == pytest.approx(leaf_total(base, "FASHION") * 1.25)
    assert leaf_total(out, "TOTAL") == pytest.approx(leaf_total(base, "TOTAL") * 1.25)


def test_a_single_entity_rule_leaves_the_others_alone():
    base = base_forecast()
    out = apply_scenario(base, scenario(adjustments=[Adjustment("FASHION", factor=2.0)]), None)
    assert leaf_total(out, "FASHION") == pytest.approx(leaf_total(base, "FASHION") * 2)
    assert leaf_total(out, "GOLD") == pytest.approx(leaf_total(base, "GOLD"))


def test_total_level_addition_moves_the_total_exactly():
    base = base_forecast()
    out = apply_scenario(base, scenario(adjustments=[Adjustment("TOTAL", add_daily=7)]), None)
    days = base["target_date"].nunique()
    assert leaf_total(out, "TOTAL") == pytest.approx(leaf_total(base, "TOTAL") + 7 * days)


def test_intervals_stay_ordered_around_the_point_forecast():
    out = apply_scenario(base_forecast(), scenario(), None)
    assert (out["lower_80"] <= out["reconciled_yhat"]).all()
    assert (out["reconciled_yhat"] <= out["upper_80"]).all()
    assert (out["lower_90"] <= out["lower_80"]).all()
    assert (out["upper_80"] <= out["upper_90"]).all()


def test_horizon_is_recomputed():
    out = apply_scenario(base_forecast(days=3), scenario(), None)
    assert sorted(out["horizon"].unique()) == [1, 2, 3]


def test_scenario_id_is_stamped_on_every_row():
    sc = scenario("budget")
    out = apply_scenario(base_forecast(), sc, None)
    assert set(out["scenario_id"]) == {sc.scenario_id}
    assert out["model_version"].str.endswith("__budget").all()


# ------------------------------------------------------- reproducibility
def test_same_spec_gives_the_same_id_and_numbers():
    first = apply_scenario(base_forecast(), scenario(), None)
    second = apply_scenario(base_forecast(), scenario(), None)
    assert first["scenario_id"].iloc[0] == second["scenario_id"].iloc[0]
    pd.testing.assert_series_equal(
        first["reconciled_yhat"], second["reconciled_yhat"], check_names=False
    )


def test_rule_order_does_not_change_the_id():
    a = Scenario(
        name="mix",
        label="mix",
        adjustments=[Adjustment("FASHION", 1.2), Adjustment("GOLD", 0.9)],
    )
    b = Scenario(
        name="mix",
        label="mix",
        adjustments=[Adjustment("GOLD", 0.9), Adjustment("FASHION", 1.2)],
    )
    assert a.scenario_id == b.scenario_id


def test_a_different_factor_changes_the_id():
    a = scenario(adjustments=[Adjustment("ALL", 1.2)])
    b = scenario(adjustments=[Adjustment("ALL", 1.3)])
    assert a.scenario_id != b.scenario_id


# -------------------------------------------------------------- errors
def test_zero_factor_is_rejected():
    with pytest.raises(ScenarioError):
        Adjustment("ALL", factor=0.0)


def test_negative_factor_is_rejected():
    with pytest.raises(ScenarioError):
        Adjustment("ALL", factor=-0.2)


def test_unknown_entity_is_rejected():
    with pytest.raises(ScenarioError):
        apply_scenario(
            base_forecast(), scenario(adjustments=[Adjustment("NOPE", 1.1)]), None
        )


def test_factor_on_total_is_rejected():
    base = base_forecast()
    with pytest.raises(ScenarioError):
        apply_scenario(base, scenario(adjustments=[Adjustment("TOTAL", factor=1.1)]), None)


def test_missing_base_forecast_columns_are_reported():
    base = base_forecast().drop(columns=["lower_90"])
    with pytest.raises(ScenarioError):
        apply_scenario(base, scenario(), None)


def test_empty_forecast_is_rejected():
    with pytest.raises(ScenarioError):
        apply_scenario(pd.DataFrame(), scenario(), None)


# --------------------------------------------------------------- config
def test_shipped_catalogue_loads_and_is_readable():
    scenarios = load_scenarios()
    assert {"budget_up_20", "merchant_up_10", "new_venture", "downside_10"} <= set(scenarios)
    for sc in scenarios.values():
        assert sc.label and sc.scenario_id.startswith("scn_")


def test_shipped_catalogue_applies_cleanly():
    base = base_forecast()
    for sc in load_scenarios().values():
        out = apply_scenario(base, sc, None)
        assert len(out) == len(base)


# ----------------------------------------------------------- comparison
def test_comparison_reports_the_delta_per_scenario():
    base = base_forecast()
    up = apply_scenario(base, scenario(adjustments=[Adjustment("ALL", 1.2)]), None)
    down = apply_scenario(base, scenario(adjustments=[Adjustment("ALL", 0.8)]), None)
    table = comparison(base, [up, down])
    assert list(table["scenario"])[0] == "base"
    row_up = table[table["scenario"] == up["scenario_id"].iloc[0]].iloc[0]
    assert row_up["delta"] == pytest.approx(leaf_total(base, "TOTAL") * 0.2)
    assert row_up["delta_share"] == pytest.approx(0.2)


def test_comparison_of_base_only_has_no_delta():
    table = comparison(base_forecast(), [])
    assert len(table) == 1
    assert "delta" not in table.columns
