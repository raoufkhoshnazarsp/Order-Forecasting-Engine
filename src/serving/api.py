"""FastAPI application for the BNPL forecasting engine.

The UI is server-rendered HTML with a small amount of vanilla JS: no build
step, no CDN dependency, fully right-to-left.
"""

from __future__ import annotations

import io
import json
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src import config
from src.ingestion.excel import ExcelInputError, load_category_facts, template_bytes
from src.pipeline import PipelineError, run_pipeline
from src.serving.delivery import (
    DeliveryError,
    build_delivery_frame,
    validate_delivery,
)
from src.serving.comparison import ComparisonError, build_comparison
from src.serving.quality import QualityGateError, check_workbook, require_quality
from src.monitoring.health import health_report, job_health, render_prometheus
from src.monitoring.registry import current_version, history as registry_history
from src.serving.state import last_run, record_run

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
ARTIFACTS = config.PROJECT_ROOT / "artifacts" / "web_runs"

app = FastAPI(title="BNPL Forecast Studio", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


INCLUDE_RE = re.compile(r"\{\{>\s*([A-Za-z0-9_.-]+)\s*\}\}")
CURRENT = ' aria-current="page"'


def _load(name: str, depth: int = 0) -> str:
    """Read a template and expand ``{{> partial}}`` includes."""
    if depth > 5:
        raise RuntimeError(f"template include too deep at {name}")
    page = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return INCLUDE_RE.sub(lambda m: _load(m.group(1), depth + 1), page)


def _render(name: str, **context) -> HTMLResponse:
    page = _load(name)
    context.setdefault("year", datetime.now().year)
    context.setdefault("cur_home", "")
    context.setdefault("cur_comparison", "")
    for key, value in context.items():
        page = page.replace("{{" + key + "}}", str(value))
    return HTMLResponse(page)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _render(
        "index.html",
        cur_home=CURRENT,
        side_foot="پردازش کاملاً محلی روی فایل اکسل<br />بدون اتصال به دیتابیس",
    )


@app.get("/comparison", response_class=HTMLResponse)
def comparison_page() -> HTMLResponse:
    return _render(
        "comparison.html",
        cur_comparison=CURRENT,
        side_foot="گذشته از دادهٔ واقعی<br />آینده از خطای بک‌تست",
    )


@app.get("/api/status")
def status() -> dict:
    """What the other page last computed, so the pages agree with each other."""
    return {"last_run": last_run()}


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only: the process is up. Full state lives at /api/health."""
    return {"status": "ok"}


@app.get("/api/health")
def health(reference: str | None = None) -> dict:
    """Data freshness, model drift and job state in one verdict.

    Deliberately does not re-run the pipeline: the dashboard is polled, and a
    two-minute forecast per poll is not a health check. It reads the last run's
    artefacts instead, so it answers "is what we last published still sound?".
    """
    try:
        as_of = date.fromisoformat(reference) if reference else None
    except ValueError:
        as_of = None
    last = last_run() or {}
    mart = None
    backtest = None
    cached = _last_artifacts()
    if cached:
        mart, backtest = cached
    report = health_report(mart, backtest_frame=backtest, reference=as_of)
    payload = report.as_dict()
    payload["last_run"] = last
    payload["registry"] = {
        "current": current_version(),
        "versions": registry_history()[:10],
    }
    payload["job"] = job_health()
    return payload


@app.get("/api/health/metrics")
def health_metrics(reference: str | None = None) -> Response:
    """Prometheus text format, for scraping by an external monitor."""
    try:
        as_of = date.fromisoformat(reference) if reference else None
    except ValueError:
        as_of = None
    cached = _last_artifacts()
    mart, backtest = cached if cached else (None, None)
    report = health_report(mart, backtest_frame=backtest, reference=as_of)
    return Response(render_prometheus(report), media_type="text/plain; version=0.0.4")


def _last_artifacts():
    """The mart and selected backtest rows of the newest scheduled run, if any."""
    base = Path("artifacts") / "scheduled"
    if not base.is_dir():
        return None
    runs = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not runs:
        return None
    latest = runs[-1]
    try:
        mart = pd.read_parquet(latest / "mart.parquet")
    except Exception:  # noqa: BLE001 - a broken artefact is "no data", not a 500
        mart = None
    try:
        backtest = pd.read_parquet(latest / "backtest.parquet")
    except Exception:  # noqa: BLE001
        backtest = None
    if mart is None and backtest is None:
        return None
    return mart, backtest


@app.get("/api/template")
def download_template() -> Response:
    headers = {"Content-Disposition": 'attachment; filename="forecast-template.xlsx"'}
    return Response(
        template_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


async def _save_upload(file: UploadFile) -> Path:
    """Validate the upload envelope and persist it for the run."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="فقط فایل اکسل (.xlsx) پذیرفته می‌شود.")
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="حجم فایل بیش از حد مجاز است.")
    if not payload:
        raise HTTPException(status_code=400, detail="فایل خالی است.")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = ARTIFACTS / f"{stamp}_{Path(file.filename or 'upload.xlsx').name}"
    saved.write_bytes(payload)
    return saved


STAMP_RE = re.compile(r"^\d{8}_\d{6}_")


def _display_name(saved: Path) -> str:
    """Undo the timestamp prefix so the UI shows the name the user uploaded."""
    return STAMP_RE.sub("", saved.name)


def _requested_horizon(raw: str | int | None) -> int | None:
    """Empty or "auto" means "to the end of next calendar month"."""
    if isinstance(raw, int):
        return max(1, raw)
    text = (raw or "").strip().lower()
    if text in {"", "auto", "month"}:
        return None
    try:
        return max(1, int(float(text)))
    except ValueError:
        return None


@app.post("/api/quality")
async def quality(
    file: UploadFile = File(...),
    horizon: str = Form("auto"),
) -> JSONResponse:
    """Pre-flight gate: report every data check before anything is computed."""
    saved = await _save_upload(file)
    report = check_workbook(saved, horizon=_requested_horizon(horizon))
    return JSONResponse(report.as_dict())


@app.post("/api/forecast")
async def forecast(
    file: UploadFile = File(...),
    horizon: str = Form("auto"),
    model: str = Form("auto"),
    quality_token: str = Form(""),
) -> JSONResponse:
    saved = await _save_upload(file)
    requested = _requested_horizon(horizon)

    try:
        report = require_quality(saved, quality_token, horizon=requested)
    except QualityGateError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc

    plan = report.plan or {}
    horizon = int(plan.get("horizon") or 30)

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
    summary["horizon_plan"] = plan
    summary["window"] = report.window
    summary["chart"] = _chart_payload(result)
    summary["entities_table"] = _entity_table(result)
    summary["last_run"] = record_run(
        "forecast",
        {
            "file": _display_name(saved),
            "origin": report.origin.isoformat() if report.origin else None,
            "target": plan.get("target"),
            "horizon": horizon,
            "model": summary.get("model"),
            "rows": len(summary.get("entities_table") or []),
        },
    )
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


@app.post("/api/delivery")
async def delivery(
    file: UploadFile = File(...),
    horizon: str = Form("auto"),
    model: str = Form("auto"),
    quality_token: str = Form(""),
) -> StreamingResponse:
    """Return the delivery workbook: date | level | vertical | category | order | GMV.

    This is the business-facing format, one row per date x vertical x category,
    covering ``horizon`` days after the last day present in the upload.
    """
    saved = await _save_upload(file)
    requested = _requested_horizon(horizon)

    try:
        report = require_quality(saved, quality_token, horizon=requested)
    except QualityGateError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc

    horizon = int((report.plan or {}).get("horizon") or 30)

    try:
        facts = load_category_facts(saved)
        result = build_delivery_frame(
            facts, origin=max(facts["date"]), horizon=horizon, model=model
        )
        validate_delivery(result.frame)
    except ExcelInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DeliveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"ساخت فایل خروجی با خطا مواجه شد: {exc}"
        ) from exc

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        out = result.frame.copy()
        out["date"] = pd.to_datetime(out["date"])
        out.to_excel(writer, sheet_name="Sheet1", index=False)
        sheet = writer.sheets["Sheet1"]
        for cell in sheet["A"][1:]:
            cell.number_format = "yyyy-mm-dd"
        for column, width in zip("ABCDEF", (12, 10, 16, 30, 16, 20)):
            sheet.column_dimensions[column].width = width
    buffer.seek(0)

    filename = f"forecast-{horizon}d-{result.origin.isoformat()}.xlsx"
    record_run(
        "delivery",
        {
            "file": _display_name(saved),
            "origin": result.origin.isoformat(),
            "target": (report.plan or {}).get("target"),
            "horizon": horizon,
            "model": result.model,
            "rows": len(result.frame),
        },
    )
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Forecast-Model": result.model,
        "X-Forecast-Rows": str(len(result.frame)),
        "X-Forecast-Target": (report.plan or {}).get("target", ""),
    }
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.post("/api/comparison")
async def comparison(
    file: UploadFile = File(...),
    horizon: str = Form("auto"),
    model: str = Form("auto"),
    months: int = Form(6),
    quality_token: str = Form(""),
) -> JSONResponse:
    """Past months plus three scenarios for the next calendar month."""
    saved = await _save_upload(file)
    requested = _requested_horizon(horizon)

    try:
        report = require_quality(saved, quality_token, horizon=requested)
    except QualityGateError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc

    plan = report.plan or {}
    horizon_days = int(plan.get("horizon") or 30)
    origin = report.origin

    try:
        result = build_comparison(
            report.facts,
            origin=origin,
            horizon=horizon_days,
            months=max(1, min(int(months or 6), 12)),
            model=model,
        )
    except ComparisonError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"ساخت داشبورد مقایسه‌ای با خطا مواجه شد: {exc}"
        ) from exc

    payload = result.as_dict()
    payload["horizon_plan"] = plan
    payload["window"] = report.window
    payload["last_run"] = record_run(
        "comparison",
        {
            "file": _display_name(saved),
            "origin": origin.isoformat() if origin else None,
            "target": plan.get("target"),
            "horizon": horizon_days,
            "model": result.model,
            "next_month": result.next_month,
        },
    )
    return JSONResponse(payload)


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
