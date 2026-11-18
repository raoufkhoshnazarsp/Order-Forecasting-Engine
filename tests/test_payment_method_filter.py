"""Tests: the payment-method filter, from the workbook to the forecast.

The business asks for "forecast only INSTALLMENT orders". That has to filter at
ingestion, not after the fact: the daily total, the baselines and the backtest
must all see the same filtered series, otherwise the numbers disagree with each
other.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.ingestion.excel import (
    ExcelInputError,
    available_payment_methods,
    filter_by_method,
    load_category_facts,
    load_workbook,
    normalize_method,
)
from src.pipeline import run_pipeline


def metrics_frame(days: int = 120) -> pd.DataFrame:
    """A daily_metrics sheet with two payment methods per day."""
    rows = []
    start = date(2026, 1, 1)
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        for method, base in (("INSTALLMENT", 100), ("PAY_NOW", 40)):
            for vertical, category in (("FASHION", "RW"), ("GOLD", "Gold")):
                rows.append(
                    {
                        "date": day,
                        "vertical": vertical,
                        "category": category,
                        "payment_method_type": method,
                        "orders": base + (i % 7),
                        "gmv": (base + (i % 7)) * 1000,
                    }
                )
    return pd.DataFrame(rows)


def write(path, frame, sheet="daily_metrics"):
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=sheet, index=False)
    return path


def test_normalize_method_treats_placeholders_as_no_filter():
    assert normalize_method("") == ""
    assert normalize_method(None) == ""
    assert normalize_method(" all ") == ""
    assert normalize_method("همه") == ""
    assert normalize_method(" installment ") == "INSTALLMENT"


def test_filter_by_method_keeps_only_the_requested_rows():
    frame = metrics_frame(5)
    kept = filter_by_method(frame, "installment")
    assert set(kept["payment_method_type"]) == {"INSTALLMENT"}
    assert len(kept) == len(frame) // 2
    assert len(filter_by_method(frame, "")) == len(frame)


def test_filter_raises_when_the_column_is_missing():
    with pytest.raises(ExcelInputError):
        filter_by_method(pd.DataFrame({"orders": [1, 2]}), "INSTALLMENT")


def test_available_methods_come_from_the_workbook(tmp_path):
    path = write(tmp_path / "metrics.xlsx", metrics_frame(14))
    assert available_payment_methods(path) == ["INSTALLMENT", "PAY_NOW"]


def test_load_workbook_filters_the_series_not_only_the_labels(tmp_path):
    frame = metrics_frame(60)
    path = write(tmp_path / "metrics.xlsx", frame)
    everything = load_workbook(path)
    only_installment = load_workbook(path, payment_method="INSTALLMENT")
    total_all = everything.facts["successful_orders"].sum()
    total_one = only_installment.facts["successful_orders"].sum()
    expected = frame[frame["payment_method_type"] == "INSTALLMENT"]["orders"].sum()
    assert total_one == pytest.approx(expected)
    assert total_one < total_all


def test_category_facts_drop_other_methods(tmp_path):
    path = write(tmp_path / "metrics.xlsx", metrics_frame(30))
    facts = load_category_facts(path, payment_method="PAY_NOW")
    assert len(facts) > 0
    assert facts["orders"].sum() == pytest.approx(
        metrics_frame(30).query("payment_method_type == 'PAY_NOW'")["orders"].sum()
    )


def test_unknown_method_is_an_explicit_error(tmp_path):
    path = write(tmp_path / "metrics.xlsx", metrics_frame(30))
    with pytest.raises(ExcelInputError):
        load_category_facts(path, payment_method="CRYPTO")


def test_pipeline_on_one_method_stays_internally_consistent(tmp_path):
    path = write(tmp_path / "metrics.xlsx", metrics_frame(200))
    result = run_pipeline(path, horizon=14, payment_method="INSTALLMENT")
    total = result.forecast[result.forecast["entity_id"] == "TOTAL"]
    assert not total.empty
    # One method alone carries fewer orders than both, and the forecast is positive.
    both = run_pipeline(path, horizon=14)
    assert total["reconciled_yhat"].sum() < (
        both.forecast[both.forecast["entity_id"] == "TOTAL"]["reconciled_yhat"].sum()
    )
    assert total["reconciled_yhat"].min() >= 0
