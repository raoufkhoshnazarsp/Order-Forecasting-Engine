"""Tests: the quality gate is enforced by the HTTP API, not just the UI."""

from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import pytest

from src.serving import api


class _Upload:
    """Minimal stand-in for fastapi.UploadFile."""

    def __init__(self, name: str, payload: bytes):
        self.filename = name
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def workbook_bytes(days: int = 150, negative: bool = False) -> bytes:
    rows = []
    for i in range(days):
        day = date(2026, 1, 1) + timedelta(days=i)
        for vertical, category in (
            ("DIGITAL", "Laptop"),
            ("FASHION", "RW"),
            ("FMCG", "Pet Care"),
            ("GOLD", "Gold"),
            ("H&B", "Pharmacy"),
            ("Home", "AT"),
            ("Services", "Service"),
        ):
            rows.append(
                {
                    "vertical": vertical,
                    "Date": day.isoformat(),
                    "category": category,
                    "payment_method_type": "INSTALLMENT",
                    "Orders": -5 if negative and i == 0 else 100,
                    "GMV": 50_000_000,
                }
            )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Sheet1", index=False)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolated_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ARTIFACTS", tmp_path / "runs")


def test_quality_endpoint_reports_every_check():
    import asyncio

    payload = workbook_bytes()
    response = asyncio.run(api.quality(_Upload("data.xlsx", payload), horizon=30))
    body = response.body.decode()
    assert '"status"' in body
    assert '"checks"' in body
    assert '"token"' in body


def test_forecast_is_refused_without_an_approved_token():
    import asyncio

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            api.forecast(_Upload("data.xlsx", workbook_bytes()), 30, "auto", "")
        )
    assert exc.value.status_code == 428


def test_forecast_is_refused_when_the_gate_fails():
    import asyncio

    from fastapi import HTTPException

    payload = workbook_bytes(negative=True)
    report = asyncio.run(api.quality(_Upload("bad.xlsx", payload), horizon=30))
    token = __import__("json").loads(report.body)["token"]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.forecast(_Upload("bad.xlsx", payload), 30, "auto", token))
    assert exc.value.status_code == 428
    assert "کیفیت داده" in exc.value.detail


def test_forecast_runs_once_the_token_matches():
    import asyncio
    import json

    payload = workbook_bytes()
    report = asyncio.run(api.quality(_Upload("data.xlsx", payload), horizon=30))
    token = json.loads(report.body)["token"]

    response = asyncio.run(api.forecast(_Upload("data.xlsx", payload), 30, "auto", token))
    body = json.loads(response.body)
    assert body["horizon"] == 30
    assert body["entities_table"]
