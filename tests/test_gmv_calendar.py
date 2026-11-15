"""Tests: the commercial data-model layout, AOV/GMV math and the calendar table."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.features.persian_calendar import (
    CalendarError,
    calendar_features,
    coverage,
    to_persian,
)
from src.ingestion.excel import load_workbook
from src.pipeline import run_pipeline
from src.serving.gmv import aov_series, combine_gmv, forecast_aov, validate_gmv
from src.serving.service import RunContext


def write_workbook(path, sheets: dict[str, pd.DataFrame]):
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return path


def metrics_frame(days: int = 90, start: date = date(2026, 1, 1)) -> pd.DataFrame:
    """A workbook shaped like the commercial data-model export."""
    rows = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        for vertical, orders, aov in (
            ("FASHION", 100, 500_000),
            ("GOLD", 40, 2_000_000),
            ("Snapp-Venture", 300, 300_000),
        ):
            for method in ("INSTALLMENT", "PAY_NOW"):
                rows.append(
                    {
                        "vertical": vertical,
                        "Date": day,
                        "category": "ventures" if vertical == "Snapp-Venture" else "general",
                        "Year": start.year,
                        "Month": start.month,
                        "payment_method_type": method,
                        "Orders": orders // 2,
                        "GMV": (orders // 2) * aov,
                        "Active Merchants": 5,
                    }
                )
    return pd.DataFrame(rows)


def test_daily_metrics_layout_is_detected_and_aggregated(tmp_path):
    path = write_workbook(tmp_path / "model.xlsx", {"Sheet1": metrics_frame(30)})
    loaded = load_workbook(path)
    assert loaded.reports[0].layout == "daily_metrics"
    assert "gmv" in loaded.facts.columns

    # Two payment methods per vertical collapse into one row per date x entity.
    per_day = loaded.facts.groupby("date").size()
    assert (per_day == 3).all()

    venture = loaded.facts[loaded.facts["entity_id"] == "Snapp-Venture"]
    assert not venture.empty
    assert (venture["entity_type"] == "VENTURE").all()

    # GMV is summed, never averaged.
    first = loaded.facts[loaded.facts["date"] == date(2026, 1, 1)]
    fashion = first[first["entity_id"] == "FASHION"].iloc[0]
    assert fashion["successful_orders"] == 100
    assert fashion["gmv"] == 100 * 500_000


def test_aov_is_gmv_over_orders(tmp_path):
    path = write_workbook(tmp_path / "model.xlsx", {"Sheet1": metrics_frame(30)})
    result = run_pipeline(path, horizon=7, model="naive", run_backtest=False)
    assert result.gmv_forecast is not None
    assert result.aov_forecast is not None

    total = result.gmv_forecast[result.gmv_forecast["entity_id"] == "TOTAL"]
    assert (total["gmv_yhat"] > 0).all()
    # Total AOV is the weighted average, not the sum of leaf AOVs.
    expected = total["gmv_yhat"] / total["orders_yhat"]
    assert np.allclose(total["aov_yhat"], expected)


def test_gmv_equals_orders_times_aov_and_sums_to_total(tmp_path):
    path = write_workbook(tmp_path / "model.xlsx", {"Sheet1": metrics_frame(60)})
    result = run_pipeline(path, horizon=10, model="naive", run_backtest=False)
    frame = result.gmv_forecast
    leaves = frame[frame["entity_type"].isin(["VERTICAL", "VENTURE"])]
    assert np.allclose(leaves["orders_yhat"] * leaves["aov_yhat"], leaves["gmv_yhat"])

    total = frame[frame["entity_id"] == "TOTAL"].set_index("target_date")["gmv_yhat"]
    leaf_sum = leaves.groupby("target_date")["gmv_yhat"].sum()
    assert np.allclose(total.sort_index(), leaf_sum.sort_index())


def test_validate_gmv_rejects_incoherent_total(tmp_path):
    path = write_workbook(tmp_path / "model.xlsx", {"Sheet1": metrics_frame(30)})
    result = run_pipeline(path, horizon=5, model="naive", run_backtest=False)
    frame = result.gmv_forecast.copy()
    frame.loc[frame["entity_id"] == "TOTAL", "gmv_yhat"] *= 2
    from src.data_mart.entity_master import EntityMaster

    master = EntityMaster.load()
    with pytest.raises(Exception):
        validate_gmv(frame, master)


def test_partial_last_day_is_excluded(tmp_path):
    frame = metrics_frame(60)
    last_day = frame["Date"].max()
    # Halve the final day, as a mid-day export would.
    mask = frame["Date"] == last_day
    frame.loc[mask, "Orders"] = (frame.loc[mask, "Orders"] * 0.3).round()
    frame.loc[mask, "GMV"] = (frame.loc[mask, "GMV"] * 0.3).round()
    path = write_workbook(tmp_path / "partial.xlsx", {"Sheet1": frame})

    result = run_pipeline(path, horizon=7, model="naive", run_backtest=False)
    assert result.dropped_partial_days == [last_day]
    # The training window now ends on the last complete day.
    assert result.date_end.isoformat() < last_day
    assert any("Partial day" in w for w in result.warnings)


def test_calendar_table_covers_and_converts():
    span = coverage()
    assert span.start <= date(2026, 7, 1) and span.end >= date(2026, 12, 31)

    converted = to_persian(date(2026, 10, 28))
    assert converted["persian_date"] == "1405-08-06"
    assert converted["persian_month_name"] == "آبان"


def test_calendar_marks_friday_as_the_iranian_weekend():
    features = calendar_features([date(2026, 7, 3), date(2026, 7, 4)])
    friday = features.iloc[0]
    saturday = features.iloc[1]
    assert friday["is_weekend"] == 1
    assert saturday["is_weekend"] == 0


def test_calendar_rejects_dates_outside_its_range():
    with pytest.raises(CalendarError):
        calendar_features([date(1980, 1, 1)])


def test_calendar_profile_model_uses_the_calendar_table():
    from src.models.baselines import calendar_profile

    dates = pd.date_range("2026-07-01", periods=70, freq="D")
    values = []
    for day in dates:
        # Friday (weekday 4) is a deep dip, everything else is flat.
        values.append(100.0 if day.day_of_week != 4 else 40.0)
    series = pd.Series(values, index=dates)

    result = calendar_profile(series, 14)
    future = pd.date_range(dates[-1] + pd.Timedelta(days=1), periods=14, freq="D")
    fridays = [i for i, d in enumerate(future) if d.day_of_week == 4]
    others = [i for i, d in enumerate(future) if d.day_of_week != 4]
    assert result.values[fridays].mean() < result.values[others].mean() * 0.7


def test_aov_series_ignores_zero_order_days():
    mart = pd.DataFrame(
        {
            "date": [date(2026, 1, 1), date(2026, 1, 2)],
            "entity_id": ["A", "A"],
            "quality_flag": ["OBSERVED", "OBSERVED"],
            "successful_orders": [10, 0],
            "gmv": [1000, 0],
        }
    )
    series = aov_series(mart, "A")
    assert len(series) == 1
    assert series.iloc[0] == 100.0


def test_auto_selection_ranks_on_pooled_wape_not_a_mean_of_ratios(tmp_path):
    """A tiny entity with a bad ratio must not outvote the business total.

    The ranking used to average per-entity WAPEs, so a small vertical could
    decide the winner. Pooled WAPE weights every order equally, which is the
    number the user actually forecasts.
    """
    rows = []
    start = date(2026, 1, 1)
    for i in range(150):
        day = (start + timedelta(days=i)).isoformat()
        weekday = (start + timedelta(days=i)).weekday()
        big = 1000 * (1.4 if weekday == 4 else 1.0)
        small = 5 * (1.0 + 0.5 * np.sin(i))
        rows.append(
            {
                "date": day,
                "vertical": "FASHION",
                "successful_orders": int(big),
                "GMV": int(big) * 500_000,
            }
        )
        rows.append(
            {
                "date": day,
                "vertical": "GOLD",
                "successful_orders": int(small),
                "GMV": int(small) * 2_000_000,
            }
        )
    path = write_workbook(tmp_path / "model.xlsx", {"Sheet1": pd.DataFrame(rows)})

    result = run_pipeline(
        path, horizon=14, model="auto", backtest_step=14, min_train_days=60
    )
    assert result.backtest_ranking
    wapes = [row["wape"] for row in result.backtest_ranking]
    assert wapes == sorted(wapes)
    assert result.model == result.backtest_ranking[0]["model"]
