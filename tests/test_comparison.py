"""Tests: the monthly comparison dashboard and its three scenarios."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.serving.comparison import (
    SCENARIOS,
    ComparisonError,
    build_comparison,
)

CORE = ("DIGITAL", "FASHION", "FMCG", "GOLD", "H&B", "HOME", "SERVICES")

VERTICALS = {
    "DIGITAL": ["Laptop", "Mobile"],
    "FASHION": ["RW"],
    "FMCG": ["Pet Care"],
    "GOLD": ["Gold"],
    "H&B": ["Pharmacy"],
    "Home": ["AT"],
    "Services": ["Service"],
    "Snapp-Venture": ["ventures"],
}


def facts(days: int = 240, start: date = date(2026, 1, 1), noise: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        for vertical, categories in VERTICALS.items():
            for j, category in enumerate(categories):
                base = 100 + 10 * j
                wobble = 1 + (noise * rng.standard_normal() if noise else 0)
                orders = max(base * wobble, 1)
                rows.append(
                    {
                        "date": pd.Timestamp(day),
                        "vertical": vertical,
                        "category": category,
                        "orders": orders,
                        "gmv": orders * 500_000,
                    }
                )
    return pd.DataFrame(rows)


def test_past_months_are_complete_and_ordered():
    data = facts(240)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=6, model="naive"
    )
    assert len(result.past_months) == 6
    assert result.past_months == sorted(result.past_months)
    # The last complete month must be the one before the origin month.
    assert result.past_months[-1] == "2026-07"


def test_verticals_total_equals_the_sum_of_the_seven():
    data = facts(240)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=4, model="naive"
    )
    for month in result.past_months:
        chunk = [r for r in result.months if r["month"] == month]
        total = next(r for r in chunk if r["vertical"] == "VERTICALS")
        core = [r for r in chunk if r["vertical"] in CORE]
        assert np.isclose(total["orders"], sum(r["orders"] for r in core))
        assert np.isclose(total["gmv"], sum(r["gmv"] for r in core))


def test_venture_is_not_counted_inside_verticals():
    data = facts(240)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=3, model="naive"
    )
    month = result.past_months[-1]
    chunk = [r for r in result.months if r["month"] == month]
    total = next(r for r in chunk if r["vertical"] == "VERTICALS")
    venture = next(r for r in chunk if r["vertical"] == "SNAPP-VENTURE")
    assert venture["orders"] > 0
    assert total["orders"] < total["orders"] + venture["orders"]


def test_scenarios_are_ordered_pessimistic_realistic_optimistic():
    data = facts(240, noise=0.15)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=3, model="naive"
    )
    by_vertical: dict[str, dict[str, dict]] = {}
    for row in result.scenarios:
        by_vertical.setdefault(row["vertical"], {})[row["scenario"]] = row

    for vertical, scenarios in by_vertical.items():
        assert set(scenarios) == set(SCENARIOS), vertical
        assert scenarios["pessimistic"]["orders"] <= scenarios["realistic"]["orders"]
        assert scenarios["realistic"]["orders"] <= scenarios["optimistic"]["orders"]


def test_scenario_total_is_the_sum_of_the_seven():
    data = facts(240, noise=0.15)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=3, model="naive"
    )
    for scenario in SCENARIOS:
        chunk = [r for r in result.scenarios if r["scenario"] == scenario]
        total = next(r for r in chunk if r["vertical"] == "VERTICALS")
        core = [r for r in chunk if r["vertical"] in CORE]
        assert np.isclose(total["orders"], sum(r["orders"] for r in core))
        assert np.isclose(total["gmv"], sum(r["gmv"] for r in core))


def test_gmv_equals_orders_times_aov_everywhere():
    data = facts(240, noise=0.1)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=3, model="naive"
    )
    for row in result.months + result.scenarios:
        if row["orders"] > 0:
            assert np.isclose(row["gmv"] / row["orders"], row["aov"], rtol=1e-9)


def test_next_month_is_the_month_after_the_origin():
    data = facts(240)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=3, model="naive"
    )
    assert result.next_month == "2026-09"
    assert {r["month"] for r in result.scenarios} == {"2026-09"}


def test_monthly_actuals_match_daily_sums():
    data = facts(240)
    result = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=2, model="naive"
    )
    month = result.past_months[-1]
    expected = data[
        pd.to_datetime(data["date"]).dt.to_period("M").astype(str) == month
    ]
    expected_total = expected[expected["vertical"] != "Snapp-Venture"]["orders"].sum()
    row = next(
        r for r in result.months if r["month"] == month and r["vertical"] == "VERTICALS"
    )
    assert np.isclose(row["orders"], expected_total)


def test_short_history_warns_but_still_builds():
    data = facts(100)
    result = build_comparison(
        data, origin=date(2026, 4, 10), horizon=20, months=6, model="naive"
    )
    assert result.warnings
    assert result.validity["low_confidence"] is True


def test_empty_facts_raise():
    with pytest.raises(ComparisonError):
        build_comparison(pd.DataFrame(), origin=date(2026, 1, 1), horizon=30)


def test_unknown_model_raises():
    with pytest.raises(ComparisonError):
        build_comparison(
            facts(120), origin=date(2026, 4, 1), horizon=30, model="not-a-model"
        )


def test_result_is_json_safe():
    import json

    data = facts(240)
    payload = build_comparison(
        data, origin=date(2026, 8, 28), horizon=33, months=3, model="naive"
    ).as_dict()
    json.dumps(payload)
    assert set(payload) == {
        "months",
        "scenarios",
        "next_month",
        "past_months",
        "model",
        "validity",
        "warnings",
    }
