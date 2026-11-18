"""Tests: what the flow's steps actually receive from the API.

Four things the user reported as broken, pinned at the response level:

* "پیش‌بینی تاریخی در برابر واقعی" needs a real actual-vs-predicted series,
* step 5 needs drivers for whatever model won, not only the ML model,
* the monthly dashboard must cover the rest of this month *and* next month,
* the payment-method filter must narrow the whole run, not just a label.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from src.serving import api


class _Upload:
    def __init__(self, name: str, payload: bytes):
        self.filename = name
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def workbook_bytes(days: int = 200, end: date | None = None) -> bytes:
    rows = []
    end = end or date(2026, 6, 30)
    start = end - timedelta(days=days - 1)
    for i in range(days):
        day = start + timedelta(days=i)
        for vertical, category in (
            ("DIGITAL", "Laptop"),
            ("FASHION", "RW"),
            ("FMCG", "Pet Care"),
            ("GOLD", "Gold"),
            ("H&B", "Pharmacy"),
            ("Home", "AT"),
            ("Services", "Service"),
        ):
            for method, factor in (("INSTALLMENT", 1.0), ("PAY_NOW", 0.4)):
                orders = int((100 + (i % 7) * 3) * factor)
                rows.append(
                    {
                        "vertical": vertical,
                        "Date": day.isoformat(),
                        "category": category,
                        "payment_method_type": method,
                        "Orders": orders,
                        "GMV": orders * 500_000,
                    }
                )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Sheet1", index=False)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ARTIFACTS", tmp_path / "runs")
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path / "state"))


def _create(days: int = 200, end: date | None = None, payment_method: str = "") -> dict:
    response = asyncio.run(
        api.create_run(
            _Upload("data.xlsx", workbook_bytes(days, end)),
            horizon="auto",
            payment_method=payment_method,
        )
    )
    return json.loads(response.body)


def _forecast(body: dict) -> dict:
    return json.loads(
        api.run_forecast(
            body["run"]["run_id"],
            quality_token=body["token"],
            model="auto",
            horizon="auto",
        ).body
    )


def test_backtest_view_carries_a_drawable_actual_vs_predicted_series():
    body = _create()
    forecast = _forecast(body)
    chart = forecast["backtest_view"]["chart"]
    assert chart, "the backtest chart must be present for a model with a backtest"
    assert len(chart["actual"]) == len(chart["forecast"]) > 0
    assert chart["start"] and chart["end"]
    assert all(isinstance(v, (int, float)) for v in chart["actual"][:5])


def test_drivers_are_produced_for_the_model_that_actually_ran():
    body = _create()
    forecast = _forecast(body)
    drivers = forecast["drivers"]
    assert drivers["model"] == forecast["model"]
    assert drivers["drivers"], "every model must explain its forecast somehow"
    assert drivers["is_causal"] is False
    assert abs(sum(d["contribution"] for d in drivers["drivers"]) - drivers["total"]) < 1
    assert drivers["entities"]


def test_monthly_dashboard_covers_the_rest_of_this_month_and_the_next(monkeypatch):
    """Data ending 2026-11-16 must forecast 17–30 Nov *and* all of December."""
    body = _create(end=date(2026, 11, 16), days=200)
    run_id = body["run"]["run_id"]
    comparison = json.loads(
        api.run_comparison(run_id, quality_token=body["token"], months=6).body
    )
    months = {m["month"]: m for m in comparison["scenario_months"]}
    assert comparison["current_month"] == "2026-11"
    assert comparison["next_month"] == "2026-12"
    assert months["2026-11"]["days"] == 14
    assert months["2026-12"]["days"] == 31
    assert months["2026-11"]["scope"] == "current"
    assert months["2026-12"]["scope"] == "next"
    assert {r["month"] for r in comparison["scenarios"]} == {"2026-11", "2026-12"}
    # No day of December is lost: the horizon ends on the 31st.
    assert months["2026-12"]["end"] == "2026-12-31"


def test_payment_method_filter_changes_the_numbers_and_is_reported():
    everything = _create()
    installment = _create(payment_method="INSTALLMENT")
    all_forecast = _forecast(everything)
    one_forecast = _forecast(installment)

    assert installment["payment_method"] == "INSTALLMENT"
    assert "INSTALLMENT" in installment["payment_methods"]
    assert set(installment["payment_methods"]) == {"INSTALLMENT", "PAY_NOW"}
    assert everything["payment_method"] == ""

    total_all = sum(all_forecast["total_forecast"].values())
    total_one = sum(one_forecast["total_forecast"].values())
    assert total_one < total_all
    assert one_forecast["payment_method"] == "INSTALLMENT"


def test_comparison_follows_the_run_payment_method():
    body = _create(payment_method="PAY_NOW")
    comparison = json.loads(
        api.run_comparison(
            body["run"]["run_id"], quality_token=body["token"], months=6
        ).body
    )
    assert comparison["payment_method"] == "PAY_NOW"
    # One method is a subset of both, so its monthly total must be smaller
    # than the unfiltered total for the same past month.
    full = _create()
    full_comparison = json.loads(
        api.run_comparison(
            full["run"]["run_id"], quality_token=full["token"], months=6
        ).body
    )
    month = comparison["past_months"][-1]
    one = next(
        r
        for r in comparison["months"]
        if r["month"] == month and r["vertical"] == "VERTICALS"
    )
    both = next(
        r
        for r in full_comparison["months"]
        if r["month"] == month and r["vertical"] == "VERTICALS"
    )
    assert one["orders"] < both["orders"]
