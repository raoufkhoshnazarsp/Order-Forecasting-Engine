"""Tests: choosing a payment method at the forecast step, not only at upload.

The workbook is approved once (the quality token hashes the whole file), so
any slice of it can be forecast later. Re-slicing must:
  * forecast only the chosen method,
  * update the run record, so every later download follows the new slice,
  * throw away the delivery file cached for the previous slice,
  * refuse a method the workbook does not contain,
  * and keep an empty selection meaning "every method".
"""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from src.serving import api


class _Upload:
    def __init__(self, name: str, payload: bytes):
        self.filename = name
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def workbook_bytes(days: int = 120, end: date = date(2026, 11, 16)) -> bytes:
    rows = []
    start = end - timedelta(days=days - 1)
    for i in range(days):
        day = start + timedelta(days=i)
        for vertical, category in (
            ("DIGITAL", "Laptop"),
            ("FASHION", "RW"),
            ("Home", "AT"),
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


def _call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return json.loads(result.body)


def _start(method: str = "") -> tuple[str, str]:
    body = _call(
        api.create_run, _Upload("data.xlsx", workbook_bytes()), horizon="auto",
        payment_method=method,
    )
    return body["run"]["run_id"], body["token"]


def _forecast(run_id: str, token: str, method: str | None = None) -> dict:
    kwargs = {"quality_token": token, "model": "auto", "horizon": "auto"}
    if method is not None:
        kwargs["payment_method"] = method
    return _call(api.run_forecast, run_id, **kwargs)


def test_the_upload_response_lists_every_method_of_the_file():
    body = _call(
        api.create_run, _Upload("data.xlsx", workbook_bytes()), horizon="auto"
    )
    assert body["payment_methods"] == ["INSTALLMENT", "PAY_NOW"]
    assert body["payment_method"] == ""


def test_the_restored_session_still_lists_the_methods():
    run_id, _token = _start()
    restored = _call(api.get_run, run_id)
    assert restored["run"]["payment_methods"] == ["INSTALLMENT", "PAY_NOW"]


def test_forecast_can_pick_a_method_after_upload():
    run_id, token = _start()
    summary = _forecast(run_id, token, "PAY_NOW")
    assert summary["payment_method"] == "PAY_NOW"
    report = _call(api.report_rows, run_id)
    assert report["payment_methods"] == ["PAY_NOW"]


def test_switching_the_slice_moves_every_later_download_with_it():
    run_id, token = _start()
    _forecast(run_id, token, "INSTALLMENT")
    first = pd.read_excel(io.BytesIO(api.download_package(run_id).body))
    assert set(first["payment_method"].dropna()) == {"INSTALLMENT"}

    # The second forecast re-slices the same run; the cached file must go.
    _forecast(run_id, token, "PAY_NOW")
    second = pd.read_excel(io.BytesIO(api.download_package(run_id).body))
    assert set(second["payment_method"].dropna()) == {"PAY_NOW"}
    assert _call(api.report_rows, run_id)["payment_methods"] == ["PAY_NOW"]
    assert _call(api.get_run, run_id)["run"]["payment_method"] == "PAY_NOW"


def test_an_explicit_method_beats_the_upload_choice():
    run_id, token = _start("INSTALLMENT")
    assert _forecast(run_id, token, "PAY_NOW")["payment_method"] == "PAY_NOW"


def test_an_empty_selection_keeps_the_upload_choice():
    run_id, token = _start("INSTALLMENT")
    assert _forecast(run_id, token, "")["payment_method"] == "INSTALLMENT"


def test_a_method_the_file_does_not_carry_is_refused():
    run_id, token = _start()
    with pytest.raises(Exception) as excinfo:
        _forecast(run_id, token, "WALLET")
    detail = getattr(excinfo.value, "detail", str(excinfo.value))
    assert "WALLET" in str(detail)


def test_the_frame_file_follows_the_chosen_slice():
    run_id, token = _start()
    _forecast(run_id, token, "PAY_NOW")
    frame = pd.read_excel(io.BytesIO(api.download_frame(run_id).body))
    whole = _call(api.report_rows, run_id)
    assert frame["order"].sum() == pytest.approx(
        sum(row["order"] for row in whole["rows"]), rel=1e-6
    )
