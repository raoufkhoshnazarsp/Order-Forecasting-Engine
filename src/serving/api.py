"""FastAPI application for the BNPL forecasting engine.

The UI is server-rendered HTML with a small amount of vanilla JS: no build
step, no CDN dependency, fully right-to-left.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src import config
from src.ingestion.excel import ExcelInputError, template_bytes
from src.pipeline import PipelineError, run_pipeline

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
ARTIFACTS = config.PROJECT_ROOT / "artifacts" / "web_runs"

app = FastAPI(title="BNPL Forecast Studio", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _render(name: str, **context) -> HTMLResponse:
    page = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for key, value in context.items():
        page = page.replace("{{" + key + "}}", str(value))
    return HTMLResponse(page)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _render("index.html", year=datetime.now().year)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/template")
def download_template() -> Response:
    headers = {"Content-Disposition": 'attachment; filename="forecast-template.xlsx"'}
    return Response(
        template_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.post("/api/forecast")
async def forecast(
    file: UploadFile = File(...),
    horizon: int = Form(30),
    model: str = Form("auto"),
) -> JSONResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="فقط فایل اکسل (.xlsx) پذیرفته می‌شود.")
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="حجم فایل بیش از حد مجاز است.")
    if not payload:
        raise HTTPException(status_code=400, detail="فایل خالی است.")

    horizon = max(1, min(int(horizon or 30), 90))
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = ARTIFACTS / f"{stamp}_{Path(file.filename or 'upload.xlsx').name}"
    saved.write_bytes(payload)

    try:
        result = run_pipeline(saved, horizon=horizon, model=model)
    except ExcelInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - return a readable message, log the rest
        raise HTTPException(
            status_code=500,
            detail=f"پردازش فایل با خطا مواجه شد: {exc}",
        ) from exc

    summary = result.summary()
    summary["chart"] = _chart_payload(result)
    summary["entities_table"] = _entity_table(result)
    return JSONResponse(summary)


def _chart_payload(result) -> dict:
    assert result.mart is not None and result.forecast is not None
    total_id = "TOTAL"
    mart = result.mart
    history = mart[
        (mart["entity_id"] == total_id) & mart["successful_orders"].notna()
    ].sort_values("date").tail(90)
    forecast = result.forecast[
        (result.forecast["entity_id"] == total_id)
        & (result.forecast["reconciled_yhat"].notna())
    ].sort_values("horizon")
    payload = {
        "history": {
            "labels": [pd_date(d) for d in history["date"]],
            "values": [float(v) for v in history["successful_orders"]],
        },
        "forecast": {
            "labels": [pd_date(d) for d in forecast["target_date"]],
            "values": [round(float(v), 1) for v in forecast["reconciled_yhat"]],
            "lower": [round(float(v), 1) for v in forecast["lower_90"]],
            "upper": [round(float(v), 1) for v in forecast["upper_90"]],
        },
    }

    if result.gmv_forecast is not None and "gmv" in mart.columns:
        gmv_history = history[history["gmv"].notna()]
        gmv_fc = result.gmv_forecast[
            result.gmv_forecast["entity_id"] == total_id
        ].sort_values("horizon")
        payload["gmv"] = {
            "history": {
                "labels": [pd_date(d) for d in gmv_history["date"]],
                "values": [float(v) for v in gmv_history["gmv"]],
            },
            "forecast": {
                "labels": [pd_date(d) for d in gmv_fc["target_date"]],
                "values": [round(float(v), 1) for v in gmv_fc["gmv_yhat"]],
                "lower": [round(float(v), 1) for v in gmv_fc["gmv_lower_90"]],
                "upper": [round(float(v), 1) for v in gmv_fc["gmv_upper_90"]],
            },
        }
        aov_history = gmv_history[gmv_history["successful_orders"] > 0]
        payload["aov"] = {
            "history": {
                "labels": [pd_date(d) for d in aov_history["date"]],
                "values": [
                    float(g) / float(o)
                    for g, o in zip(aov_history["gmv"], aov_history["successful_orders"])
                ],
            },
            "forecast": {
                "labels": [pd_date(d) for d in gmv_fc["target_date"]],
                "values": [round(float(v), 1) for v in gmv_fc["aov_yhat"]],
            },
        }
    return payload


def _entity_table(result) -> list[dict]:
    assert result.forecast is not None
    gmv_by_entity: dict[str, dict] = {}
    if result.gmv_forecast is not None:
        for entity_id, chunk in result.gmv_forecast.groupby("entity_id"):
            chunk = chunk.sort_values("horizon")
            gmv_by_entity[str(entity_id)] = {
                "gmv_d1": _round(chunk["gmv_yhat"].iloc[0]),
                "gmv_total_30": _round(chunk["gmv_yhat"].sum()),
                "aov_d1": _round(chunk["aov_yhat"].iloc[0]),
            }
    rows = []
    for entity_id, chunk in result.forecast.groupby("entity_id"):
        chunk = chunk.sort_values("horizon")
        by_h = chunk.set_index("horizon")["reconciled_yhat"]
        extra = gmv_by_entity.get(str(entity_id), {})
        rows.append(
            {
                "entity_id": entity_id,
                "entity_type": str(chunk["entity_type"].iloc[0]),
                "d1": _round(by_h.get(1)),
                "d7": _round(by_h.get(7)),
                "d30": _round(by_h.get(len(by_h))),
                "total_30": _round(by_h.sum()),
                "gmv_d1": extra.get("gmv_d1"),
                "gmv_total_30": extra.get("gmv_total_30"),
                "aov_d1": extra.get("aov_d1"),
            }
        )
    return sorted(rows, key=lambda r: (r["entity_type"] != "TOTAL",
                                       r["entity_type"], r["entity_id"]))


def _round(value):
    return None if value is None else round(float(value))


def pd_date(value) -> str:
    return str(value)


@app.get("/api/sample")
def sample_workbook() -> StreamingResponse:
    """Download a realistic 200-day synthetic workbook for a first run."""
    import pandas as pd

    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=200, freq="D")
    rows = []
    for i, day in enumerate(dates):
        for entity, entity_type, base in (
            ("FASHION", "VERTICAL", 420),
            ("GOLD", "VERTICAL", 130),
            ("H&B", "VERTICAL", 90),
            ("V001", "VENTURE", 60),
        ):
            weekly = 1 + 0.18 * (i % 7) / 7
            rows.append(
                {
                    "date": day.date().isoformat(),
                    "entity_id": entity,
                    "entity_type": entity_type,
                    "successful_orders": int(round(base * weekly)),
                }
            )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="daily_aggregated", index=False)
    buffer.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="sample-data.xlsx"'}
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
