"""Tests: model drivers (attribution, not causality).

The contract these tests pin down:

* a contribution is signed and its share sums to at most 1,
* tiny contributions are dropped instead of padding the list,
* the report always says ``is_causal=False``,
* the global model explains a real forecast without leaking or crashing.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.evaluation.drivers import (
    MAX_DRIVERS,
    MIN_SHARE,
    build_driver_report,
    global_importance,
    linear_contributions,
)
from src.models.global_model import GlobalConfig, GlobalModel

from tests.test_global_model import frame


# ------------------------------------------------------------ contributions
def test_linear_contribution_is_weight_times_standardised_value():
    weights = np.array([5.0, 2.0, -3.0])
    names = ["a", "b"]
    row = np.array([1.0, 4.0])
    mean = np.array([0.0, 2.0])
    scale = np.array([1.0, 2.0])
    contributions = linear_contributions(weights, names, row, mean, scale)
    assert contributions.tolist() == [2.0, -3.0]


def test_mismatched_shapes_are_rejected():
    with pytest.raises(ValueError):
        linear_contributions(np.array([1.0, 2.0]), ["a", "b"], np.array([1.0]), np.array([0.0, 0.0]), np.array([1.0, 1.0]))
    with pytest.raises(ValueError):
        linear_contributions(np.array([1.0, 2.0, 3.0]), ["a"], np.array([1.0]), np.array([0.0]), np.array([1.0]))


# ------------------------------------------------------------------ report
def test_report_is_ranked_by_absolute_contribution():
    report = build_driver_report(
        np.array([0.1, -0.6, 0.3]), ["a", "b", "c"], method="linear_contribution"
    )
    assert [d.feature for d in report.drivers] == ["b", "c", "a"]
    assert report.drivers[0].direction == "decrease"
    assert report.drivers[1].direction == "increase"


def test_report_is_never_labelled_causal():
    report = build_driver_report(np.array([1.0]), ["a"], method="linear_contribution")
    assert report.is_causal is False
    assert report.as_dict()["is_causal"] is False


def test_noise_below_the_share_threshold_is_dropped():
    values = np.array([1.0, 0.001, 0.001])
    report = build_driver_report(values, ["big", "tiny1", "tiny2"], method="linear_contribution")
    assert [d.feature for d in report.drivers] == ["big"]
    assert report.drivers[0].share == pytest.approx(1.0, abs=0.01)
    assert MIN_SHARE > 0.001


def test_driver_list_is_capped():
    values = np.linspace(1.0, 2.0, 20)
    names = [f"f{i}" for i in range(20)]
    report = build_driver_report(values, names, method="linear_contribution")
    assert len(report.drivers) == MAX_DRIVERS


def test_all_zero_contributions_yield_an_empty_report():
    report = build_driver_report(np.zeros(3), ["a", "b", "c"], method="linear_contribution")
    assert report.drivers == []


def test_global_importance_ignores_the_intercept():
    ranked = global_importance(np.array([99.0, 1.0, -5.0]), ["a", "b"])
    assert ranked == [("b", 5.0), ("a", 1.0)]


# ------------------------------------------------------------- global model
def test_global_model_explains_a_forecast():
    data = frame(days=120, entities=3)
    model = GlobalModel(GlobalConfig(horizon=8, backend="ridge")).fit(data)
    report = model.explain(data, entity_id="E1", step=1)
    assert report["entity_id"] == "E1"
    assert report["horizon"] == 1
    assert report["is_causal"] is False
    assert report["method"] == "linear_contribution"
    assert report["drivers"]
    assert all(d["feature"] in model.fit_result.feature_names for d in report["drivers"])
    assert sum(d["share"] for d in report["drivers"]) <= 1.0 + 1e-9


def test_explaining_an_unknown_entity_is_refused():
    data = frame(days=120, entities=2)
    model = GlobalModel(GlobalConfig(horizon=5, backend="ridge")).fit(data)
    with pytest.raises(ValueError):
        model.explain(data, entity_id="NOPE")


def test_explanation_does_not_change_the_forecast():
    data = frame(days=120, entities=3)
    model = GlobalModel(GlobalConfig(horizon=6, backend="ridge")).fit(data)
    before = model.predict(data, horizon=6)["E0"].copy()
    model.explain(data, entity_id="E0", step=1)
    after = model.predict(data, horizon=6)["E0"]
    assert np.allclose(before, after)
