"""Tests: a run is a session — one upload, read once, reused by every step.

The properties that matter:

* creating a run stores the workbook and returns the gate plus a preview,
* a forecast is refused without the token of *this* run's file,
* a forecast is stored and comes back on ``GET /api/runs/{id}``,
* sensitivity never mutates the base forecast,
* the delivery package is built once and served from the session,
* a session id that is not a session is rejected, never used as a path.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from src.serving import api, runs


class _Upload:
    """Minimal stand-in for fastapi.UploadFile."""

    def __init__(self, name: str, payload: bytes):
        self.filename = name
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def workbook_bytes(days: int = 150) -> bytes:
    rows = []
    start = date(2026, 1, 1)
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
            orders = 100 + (i % 7) * 3
            rows.append(
                {
                    "vertical": vertical,
                    "Date": day.isoformat(),
                    "category": category,
                    "payment_method_type": "INSTALLMENT",
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


def _create(days: int = 150) -> dict:
    response = asyncio.run(
        api.create_run(_Upload("data.xlsx", workbook_bytes(days)), horizon="30")
    )
    return json.loads(response.body)


def _run_forecast(run_id: str, token: str) -> dict:
    return json.loads(
        api.run_forecast(run_id, quality_token=token, model="auto", horizon="30").body
    )


def test_create_run_stores_file_quality_and_preview():
    body = _create()
    assert body["run"]["run_id"]
    assert body["token"]
    assert body["run"]["steps"]["data"] is True
    assert body["run"]["steps"]["quality"] is True
    preview = body["preview"]
    assert preview["rows"] > 0
    assert preview["days"] == 150
    assert "FASHION" in preview["verticals"]
    assert preview["sample"]
    # The stored file exists under the run directory.
    stored = runs.run_dir(body["run"]["run_id"])
    assert any(p.suffix == ".xlsx" for p in stored.iterdir())


def test_forecast_needs_the_token_of_this_run():
    from fastapi import HTTPException

    body = _create()
    with pytest.raises(HTTPException) as exc:
        api.run_forecast(body["run"]["run_id"], quality_token="", model="auto", horizon="30")
    assert exc.value.status_code == 428


def test_forecast_is_saved_and_restored():
    body = _create(days=200)
    run_id = body["run"]["run_id"]
    forecast = _run_forecast(run_id, body["token"])
    assert forecast["horizon"] == 30
    assert forecast["entities_table"]
    assert forecast["backtest_view"]["model"]
    # Entity-aware chart, not just the total.
    assert "TOTAL" in forecast["chart"]["entities"]
    assert "FASHION" in forecast["chart"]["entities"]

    restored = json.loads(api.get_run(run_id).body)
    assert restored["run"]["steps"]["forecast"] is True
    assert restored["forecast"]["model"] == forecast["model"]


def test_entity_table_follows_the_horizon():
    body = _create(days=200)
    response = json.loads(
        api.run_forecast(body["run"]["run_id"], quality_token=body["token"], model="auto", horizon="14").body
    )
    points = response["entities_table"][0]["points"]
    assert set(points) == {"d1", "d7", "d14"}


def test_sensitivity_scales_one_entity_without_touching_the_base():
    body = _create(days=200)
    run_id = body["run"]["run_id"]
    forecast = _run_forecast(run_id, body["token"])
    result = json.loads(api.run_sensitivity(run_id, entity_id="FASHION", delta_pct=-20.0).body)

    base = forecast["total_forecast"]
    base_total = sum(base.values())
    assert result["base_total"] == pytest.approx(base_total, rel=1e-6)
    assert result["scenario_total"] < result["base_total"]
    assert result["delta_share"] < 0
    fashion = next(e for e in result["per_entity"] if e["entity_id"] == "FASHION")
    assert fashion["scenario"] == pytest.approx(fashion["base"] * 0.8, rel=1e-6)
    # The saved base forecast is untouched.
    again = json.loads(api.get_run(run_id).body)
    assert again["forecast"]["total_forecast"] == forecast["total_forecast"]


def test_sensitivity_rejects_unknown_entity_and_bad_delta():
    from fastapi import HTTPException

    body = _create(days=200)
    run_id = body["run"]["run_id"]
    _run_forecast(run_id, body["token"])
    with pytest.raises(HTTPException) as exc:
        api.run_sensitivity(run_id, entity_id="NOPE", delta_pct=-10.0)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        api.run_sensitivity(run_id, entity_id="FASHION", delta_pct=-100.0)
    assert exc.value.status_code == 422


def test_sensitivity_requires_a_forecast_first():
    from fastapi import HTTPException

    body = _create()
    with pytest.raises(HTTPException) as exc:
        api.run_sensitivity(body["run"]["run_id"], entity_id="FASHION", delta_pct=-10.0)
    assert exc.value.status_code == 409


def test_package_is_built_once_and_served_from_the_session():
    body = _create(days=200)
    run_id = body["run"]["run_id"]
    _run_forecast(run_id, body["token"])
    assert json.loads(api.package_preview(run_id).body) == {"ready": False}

    first = api.download_package(run_id)
    assert first.body[:2] == b"PK"
    preview = json.loads(api.package_preview(run_id).body)
    assert preview["ready"] is True
    assert preview["rows"] > 0
    assert (runs.run_dir(run_id) / "package.xlsx").exists()

    second = api.download_package(run_id)
    assert second.body == first.body


def test_unknown_and_malformed_run_ids_are_404():
    from fastapi import HTTPException

    for bad in ("../../etc/passwd", "not-a-run", ""):
        with pytest.raises(HTTPException) as exc:
            api.get_run(bad)
        assert exc.value.status_code == 404


def test_run_list_is_newest_first():
    first = _create()
    second = _create()
    listed = json.loads(api.get_runs().body)["runs"]
    assert [r["run_id"] for r in listed][:2] == [
        second["run"]["run_id"],
        first["run"]["run_id"],
    ]
