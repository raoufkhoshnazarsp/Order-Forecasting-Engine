"""Tests: drivers for every model, not only the ML one.

The contract:

* the components decompose the selected model's own forecast, so they sum to it;
* the shares are signed-safe proportions and never exceed 1 together;
* a series too short to decompose yields nothing rather than a wrong number;
* the entity composition adds up over the entities the forecast covers.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.models.baselines import BASELINES
from src.serving.drivers import MIN_DAYS, component_drivers


def mart(days: int = 120) -> pd.DataFrame:
    rows = []
    start = date(2026, 1, 1)
    for i in range(days):
        day = start + timedelta(days=i)
        rows.append(
            {
                "date": pd.Timestamp(day),
                "entity_id": "TOTAL",
                "entity_type": "TOTAL",
                "successful_orders": 1000 + 10 * i + 80 * (day.weekday() >= 5),
            }
        )
        rows.append(
            {
                "date": pd.Timestamp(day),
                "entity_id": "FASHION",
                "entity_type": "VERTICAL",
                "successful_orders": 600 + 6 * i,
            }
        )
        rows.append(
            {
                "date": pd.Timestamp(day),
                "entity_id": "GOLD",
                "entity_type": "VERTICAL",
                "successful_orders": 400 + 4 * i,
            }
        )
    return pd.DataFrame(rows)


def forecast_frame(mart_frame: pd.DataFrame, horizon: int = 20, model: str = "naive") -> pd.DataFrame:
    """Run each entity through one baseline, the way the pipeline does."""
    out = []
    for entity, chunk in mart_frame.groupby("entity_id"):
        series = chunk.set_index("date")["successful_orders"].astype("float64").sort_index()
        values = np.asarray(BASELINES[model](series, horizon).values, dtype="float64")
        for step, value in enumerate(values, start=1):
            out.append(
                {
                    "entity_id": entity,
                    "horizon": step,
                    "reconciled_yhat": float(value),
                }
            )
    return pd.DataFrame(out)


def test_components_sum_to_the_forecast_for_every_model():
    frame = mart()
    for model in ("naive", "drift", "moving_average", "calendar_profile"):
        report = component_drivers(
            frame, forecast_frame(frame, 20, model), horizon=20, model=model
        )
        assert report, f"{model} produced no driver report"
        drivers = report["drivers"]
        total_contribution = sum(d["contribution"] for d in drivers)
        assert np.isclose(total_contribution, report["total"], rtol=1e-6)
        assert report["total"] > 0
        assert report["method"] == "component_decomposition"
        assert report["is_causal"] is False
        assert 0 < sum(d["share"] for d in drivers) <= 1 + 1e-9


def test_report_names_the_model_that_was_decomposed():
    frame = mart()
    report = component_drivers(
        frame, forecast_frame(frame, 14, "drift"), horizon=14, model="drift"
    )
    assert report["model"] == "drift"
    assert report["horizon"] == 14
    assert {d["feature"] for d in report["drivers"]} <= {
        "level",
        "trend",
        "weekday",
        "model",
    }


def test_short_history_returns_nothing_instead_of_a_guess():
    frame = mart(days=MIN_DAYS - 1)
    assert component_drivers(frame, forecast_frame(frame, 10), horizon=10, model="naive") == {}


def test_missing_total_row_returns_nothing():
    frame = mart().query("entity_id != 'TOTAL'")
    assert component_drivers(frame, forecast_frame(frame, 10), horizon=10, model="naive") == {}


def test_entity_shares_cover_the_forecast_and_exclude_the_total():
    frame = mart()
    report = component_drivers(
        frame, forecast_frame(frame, 20, "naive"), horizon=20, model="naive"
    )
    entities = report["entities"]
    assert entities, "the composition must name the entities"
    assert all(e["entity_id"] != "TOTAL" for e in entities)
    assert np.isclose(sum(e["share"] for e in entities), 1.0, atol=1e-6)
    assert entities == sorted(entities, key=lambda e: -e["orders"])
