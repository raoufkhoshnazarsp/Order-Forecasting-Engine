"""Tests: the forecast file in the client's own frame, and its report filters.

The client's sample file is the contract:

    date | level | vertical | category | order | GMV

one row per day per vertical, ``level`` = ``vertical``, ``category`` = ``null``,
the seven core verticals plus ``SNAPP-VENTURE``, and no aggregate rows. The
report the UI filters must be the same numbers, with a payment-method column,
and its filter lists must come from the data itself.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import date, timedelta

import openpyxl
import pandas as pd
import pytest

from src.serving import api
from src.serving.delivery import FRAME_COLUMNS, to_frame_layout


class _Upload:
    def __init__(self, name: str, payload: bytes):
        self.filename = name
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def workbook_bytes(days: int = 200, end: date = date(2026, 11, 16)) -> bytes:
    rows = []
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
            ("Snapp-Venture", "ventures"),
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


def _create() -> dict:
    response = asyncio.run(
        api.create_run(_Upload("data.xlsx", workbook_bytes()), horizon="auto")
    )
    return json.loads(response.body)


def _frame(run_id: str) -> pd.DataFrame:
    payload = api.download_frame(run_id).body
    return pd.read_excel(io.BytesIO(payload))


def test_the_frame_file_has_exactly_the_client_columns():
    body = _create()
    frame = _frame(body["run"]["run_id"])
    assert list(frame.columns) == FRAME_COLUMNS
    assert FRAME_COLUMNS == ["date", "level", "vertical", "category", "order", "GMV"]


def test_one_row_per_day_per_vertical_and_no_aggregate_rows():
    body = _create()
    run_id = body["run"]["run_id"]
    frame = _frame(run_id)
    assert frame["level"].unique().tolist() == ["vertical"]
    # The client's sample writes the literal string "null", which pandas reads
    # back as NaN; check the cell itself.
    sheet = openpyxl.load_workbook(io.BytesIO(api.download_frame(run_id).body))["Sheet1"]
    categories = {sheet.cell(row=r, column=4).value for r in range(2, sheet.max_row + 1)}
    assert categories == {"null"}
    assert "VERTICALS" not in set(frame["vertical"])
    assert "SNAPP-PAY" not in set(frame["vertical"])
    assert "SNAPP-VENTURE" in set(frame["vertical"])
    assert not frame.duplicated(subset=["date", "vertical"]).any()
    days = frame["date"].nunique()
    assert len(frame) == days * frame["vertical"].nunique()
    assert set(frame["vertical"]) == {
        "DIGITAL", "FASHION", "FMCG", "GOLD", "H&B", "HOME", "SERVICES",
        "SNAPP-VENTURE",
    }


def test_the_frame_starts_the_day_after_the_last_data_day():
    body = _create()
    frame = _frame(body["run"]["run_id"])
    assert pd.to_datetime(frame["date"]).min().date() == date(2026, 11, 17)


def test_frame_totals_match_the_detailed_delivery_workbook():
    """The frame is the same forecast as the VERTICALS total, not a second one.

    SNAPP-VENTURE is a sibling of VERTICALS, so it is excluded from the
    comparison on both sides.
    """
    body = _create()
    run_id = body["run"]["run_id"]
    detailed = pd.read_excel(io.BytesIO(api.download_package(run_id).body))
    frame = to_frame_layout(detailed)
    core = frame[frame["vertical"] != "SNAPP-VENTURE"]
    totals = detailed[detailed["vertical"] == "VERTICALS"]
    assert core["order"].sum() == pytest.approx(totals["order"].sum(), rel=1e-6)
    assert core["GMV"].sum() == pytest.approx(totals["GMV"].sum(), rel=1e-6)


def test_report_filters_are_built_from_the_forecast_itself():
    body = _create()
    view = json.loads(api.report_rows(body["run"]["run_id"]).body)
    assert view["ready"] is True
    assert set(view["payment_methods"]) == {"INSTALLMENT", "PAY_NOW"}
    assert "VERTICALS" not in view["verticals"]
    assert view["months"] == sorted(view["months"])
    assert view["days"] == len({row["date"] for row in view["rows"]})


def test_report_rows_add_up_to_the_frame():
    body = _create()
    run_id = body["run"]["run_id"]
    view = json.loads(api.report_rows(run_id).body)
    rows = pd.DataFrame(view["rows"])
    frame = _frame(run_id)
    assert rows["order"].sum() == pytest.approx(frame["order"].sum(), rel=1e-6)


def test_a_payment_method_slice_is_a_subset_of_the_whole_report():
    body = _create()
    view = json.loads(api.report_rows(body["run"]["run_id"]).body)
    rows = pd.DataFrame(view["rows"])
    one = rows[rows["payment_method"] == "INSTALLMENT"]["order"].sum()
    assert 0 < one < rows["order"].sum()
