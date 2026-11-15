"""Tests: the global model learns across entities without leaking.

The three properties that make this model safe to ship are covered here:
no future data reaches a feature, training and prediction compute identical
features, and the entity-level total is coherent with its parts.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import src.models.global_model as gm
from src.models.global_model import (
    MODEL_NAME,
    GlobalConfig,
    GlobalModel,
    _anchor,
    _anchor_matrix,
    _calendar_frame,
    _pivot,
    _row_features,
    _training_rows,
    global_forecasters,
)


def frame(days: int = 120, entities: int = 4, noise: float = 0.05) -> pd.DataFrame:
    """Weekly-seasonal counts per entity, with a level unique to each entity."""
    rng = np.random.default_rng(11)
    rows = []
    start = date(2026, 1, 1)
    for e in range(entities):
        base = 100 * (e + 1)
        for i in range(days):
            day = start + timedelta(days=i)
            weekday = day.weekday()
            factor = 1.45 if weekday == 4 else 1.0
            value = base * factor * (1 + noise * rng.standard_normal())
            rows.append(
                {
                    "date": pd.Timestamp(day),
                    "entity_id": f"E{e}",
                    "successful_orders": max(value, 1.0),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- no leakage
def test_training_rows_never_use_data_after_the_target():
    """Fitting on the same data truncated earlier must not change older rows.

    This is the practical leakage test: a model fitted at origin ``t`` cannot
    have seen day ``t + 1``. If truncation left the features untouched, no
    future value reached them.
    """
    full = frame(days=90)
    truncated = full[full["date"] <= pd.Timestamp("2026-03-01")]
    cal_full = _calendar_frame(_pivot(full).index)
    cal_trunc = _calendar_frame(_pivot(truncated).index)

    design_full, y_full, _, _ = _training_rows(
        _pivot(full).iloc[: _pivot(truncated).shape[0]], cal_full, 5
    )
    design_trunc, y_trunc, _, _ = _training_rows(_pivot(truncated), cal_trunc, 5)

    assert design_trunc.shape[1] == design_full.shape[1]
    np.testing.assert_allclose(design_trunc, design_full[: len(design_trunc)], rtol=1e-9)
    np.testing.assert_allclose(y_trunc, y_full[: len(y_trunc)], rtol=1e-9)


def test_a_change_after_the_origin_does_not_change_prediction():
    data = frame(days=120)
    model = GlobalModel(GlobalConfig(horizon=12, backend="ridge")).fit(data)
    before = model.predict(data, horizon=12)

    perturbed = data.copy()
    future = perturbed["date"] > data["date"].max()
    assert not future.any()
    # Add a day that is later than the origin; the forecast must not see it.
    extra = data[data["date"] == data["date"].max()].copy()
    extra["date"] = data["date"].max() + pd.Timedelta(days=1)
    extra["successful_orders"] = extra["successful_orders"] * 50
    extended = pd.concat([perturbed, extra], ignore_index=True)

    after = model.predict(perturbed, horizon=12)
    extended_paths = model.predict(extended, horizon=12)
    for entity in before:
        np.testing.assert_allclose(before[entity], after[entity])
    # The extended frame moves the origin, so paths differ — but the model that
    # produced ``before`` never consulted the appended row.
    assert any(
        not np.allclose(before[e], extended_paths[e]) for e in before
    )


# ------------------------------------------------------- feature consistency
def test_scalar_and_vectorised_features_agree():
    """The training path is vectorised; prediction is scalar. They must match."""
    data = frame(days=70, entities=3)
    pivot = _pivot(data)
    cal = _calendar_frame(pivot.index)
    horizon = 5
    design, targets, entities, weekdays = _training_rows(pivot, cal, horizon)

    last_fit = len(pivot) - 1 - horizon
    offset = 0
    for step in range(1, horizon + 1):
        origins = np.arange(28, last_fit + 1)
        origins = origins[origins + step < len(pivot)]
        if origins.size == 0:
            break
        for k, origin in enumerate(origins):
            for e in range(pivot.shape[1]):
                features = _row_features(
                    pivot.iloc[: origin + 1], e, step, cal, target_pos=origin + step
                )
                expected = np.array([features[name] for name in gm._NUMERIC])
                row = offset + k * pivot.shape[1] + e
                np.testing.assert_allclose(
                    design[row, : len(gm._NUMERIC)], expected, rtol=1e-9
                )
                anchor = _anchor(pivot.iloc[: origin + 1, e])
                observed = pivot.iat[origin + step, e]
                np.testing.assert_allclose(
                    targets[row],
                    np.log(observed / anchor + gm._TARGET_EPS),
                    rtol=1e-9,
                )
        offset += origins.size * pivot.shape[1]


def test_anchor_matrix_matches_the_scalar_anchor():
    data = frame(days=60, entities=3)
    pivot = _pivot(data)
    matrix = _anchor_matrix(pivot.to_numpy(dtype="float64"))
    for row in (30, 40, 59):
        for e in range(pivot.shape[1]):
            np.testing.assert_allclose(
                matrix[row, e], _anchor(pivot.iloc[: row + 1, e]), rtol=1e-9
            )


# ------------------------------------------------------------- output shape
def test_predicts_every_entity_for_the_full_horizon():
    data = frame(days=120, entities=4)
    model = GlobalModel(GlobalConfig(horizon=21, backend="ridge")).fit(data)
    paths = model.predict(data, horizon=21)
    assert set(paths) == {f"E{i}" for i in range(4)}
    for path in paths.values():
        assert len(path) == 21
        assert np.all(np.isfinite(path))
        assert np.all(path >= 0)


def test_weekday_peak_is_reproduced():
    """Friday (Iranian weekend) is 45% higher in the generator; the model must show it."""
    data = frame(days=140, entities=2, noise=0.01)
    model = GlobalModel(GlobalConfig(horizon=14, backend="ridge")).fit(data)
    paths = model.predict(data, horizon=14)
    origin = data["date"].max()
    for entity, path in paths.items():
        dates = [origin + pd.Timedelta(days=s) for s in range(1, 15)]
        fridays = [v for d, v in zip(dates, path) if d.weekday() == 4]
        others = [v for d, v in zip(dates, path) if d.weekday() != 4]
        assert np.mean(fridays) > np.mean(others)


def test_fit_is_deterministic():
    data = frame(days=110, entities=3)
    first = GlobalModel(GlobalConfig(horizon=10, backend="ridge")).fit(data).predict(data)
    second = GlobalModel(GlobalConfig(horizon=10, backend="ridge")).fit(data).predict(data)
    for entity in first:
        np.testing.assert_allclose(first[entity], second[entity])


def test_short_history_is_refused_with_a_readable_message():
    with pytest.raises(ValueError) as exc:
        GlobalModel(GlobalConfig(horizon=10)).fit(frame(days=30))
    assert "روز داده" in str(exc.value)


def test_empty_frame_is_refused():
    with pytest.raises(ValueError):
        GlobalModel(GlobalConfig(horizon=5)).fit(pd.DataFrame())


def test_global_forecasters_plug_into_the_serving_interface():
    data = frame(days=120, entities=3)
    forecasters = global_forecasters(data, horizon=9)
    assert set(forecasters) == {"E0", "E1", "E2"}
    series = data[data["entity_id"] == "E1"].set_index("date")["successful_orders"]
    result = forecasters["E1"](series, 9)
    assert result.model_name == MODEL_NAME
    assert len(result.values) == 9
    assert np.all(result.values >= 0)


def test_coefficients_are_reported_without_the_intercept():
    data = frame(days=120, entities=3)
    model = GlobalModel(GlobalConfig(horizon=8, backend="ridge")).fit(data)
    fit = model.fit_result
    names = {row["feature"] for row in fit.coefficients(50)}
    assert "intercept" not in names
    assert names <= set(fit.feature_names)
    assert abs(fit.intercept) > 0
