"""Tests: rolling-origin backtesting and metrics."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.evaluation.backtest import (
    BacktestConfig,
    backtest,
    backtest_entity,
    horizon_accuracy,
    make_origins,
    summarize,
)
from src.evaluation.metrics import bias, mae, mase, rmse, wape
from src.models.baselines import (
    moving_average,
    naive,
    run_baselines,
    seasonal_naive,
    _as_float,
)


def series(n=150):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    values = 100 + 10 * np.sin(np.arange(n) * 2 * np.pi / 7)
    return pd.Series(values, index=idx)


def test_wape_and_bias_are_exact():
    actual = [10, 20, 30]
    forecast = [12, 18, 30]
    assert wape(actual, forecast) == pytest.approx(4 / 60)
    assert bias(actual, forecast) == pytest.approx(0.0)
    assert mae(actual, forecast) == pytest.approx(4 / 3)
    assert rmse(actual, forecast) == pytest.approx(np.sqrt(8 / 3))


def test_metrics_handle_zero_denominator():
    assert np.isnan(wape([0, 0], [1, 1]))
    assert np.isnan(bias([0, 0], [1, 1]))


def test_mase_requires_history():
    assert np.isnan(mase([1, 2], [1, 2], None))
    history = 10 + np.sin(np.arange(40))
    assert mase([1, 2], [1, 2], history) >= 0


def test_origins_are_rolling_and_never_random():
    dates = [d.date() for d in pd.date_range("2025-01-01", periods=200, freq="D")]
    origins = make_origins(dates, BacktestConfig(horizon=30, step=7, min_train_days=90))
    assert origins == sorted(origins)
    assert all(o + pd.Timedelta(days=30) <= pd.Timestamp(dates[-1]) for o in origins)
    gaps = {(b - a).days for a, b in zip(origins, origins[1:])}
    assert gaps == {7}


def test_expanding_window_never_shrinks():
    s = series()
    cfg = BacktestConfig(horizon=14, step=14, min_train_days=60)
    origins = make_origins(list(s.index.date), cfg)
    assert len(origins) >= 2
    for earlier, later in zip(origins, origins[1:]):
        assert later > earlier


def test_backtest_result_has_one_row_per_horizon():
    s = series()
    res = backtest_entity(s, naive, BacktestConfig(horizon=10, step=30, min_train_days=60))
    assert not res.empty
    assert res["horizon"].max() == 10
    assert set(res["horizon"]) == set(range(1, 11))
    assert (res["yhat"] >= 0).all()


def test_naive_and_seasonal_naive_values():
    s = series()
    assert naive(s, 3).values.tolist() == [float(s.iloc[-1])] * 3
    sn = seasonal_naive(s, 8).values
    assert sn[0] == pytest.approx(s.iloc[-7])
    assert sn[7] == pytest.approx(s.iloc[-7])


def test_moving_average_is_mean_of_window():
    s = series()
    assert moving_average(s, 2, window=7).values[0] == pytest.approx(s.iloc[-7:].mean())


def test_all_baselines_non_negative():
    s = series()
    for name, result in run_baselines(s, 30).items():
        assert len(result.values) == 30, name
        assert (result.values >= 0).all(), name


def test_backtest_frame_covers_every_model_and_entity():
    frame = pd.DataFrame(
        {
            "date": list(pd.date_range("2025-01-01", periods=120, freq="D").date) * 2,
            "entity_id": ["A"] * 120 + ["B"] * 120,
            "successful_orders": list(range(120)) * 2,
        }
    )
    out = backtest(frame, {"naive": naive}, BacktestConfig(horizon=7, step=30, min_train_days=60))
    assert set(out["entity_id"]) == {"A", "B"}
    assert set(out["model_name"]) == {"naive"}
    summary = summarize(out)
    assert not summary.empty
    assert {"wape", "bias", "horizon_bucket"} <= set(summary.columns)


def test_horizon_accuracy_reports_d1_d7_d30():
    frame = pd.DataFrame(
        {
            "date": list(pd.date_range("2025-01-01", periods=200, freq="D").date),
            "entity_id": ["A"] * 200,
            "successful_orders": np.arange(200),
        }
    )
    out = backtest(frame, {"naive": naive}, BacktestConfig(horizon=30, step=60, min_train_days=60))
    acc = horizon_accuracy(out)
    assert {"wape_d1", "wape_d7", "wape_d30"} <= set(acc.columns)
