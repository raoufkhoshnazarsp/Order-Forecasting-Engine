"""FastAPI application for the BNPL forecasting engine.

The UI is server-rendered HTML with a small amount of vanilla JS: no build
step, no CDN dependency, fully right-to-left.
"""

from __future__ import annotations

import io
import json
import mimetypes
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src import config
from src.ingestion.excel import (
    ExcelInputError,
    load_category_facts,
    load_workbook,
    payment_method_shares,
    template_bytes,
)
from src.pipeline import PipelineError, run_pipeline
from src.serving.delivery import (
    INPUT_COLUMNS,
    OUTPUT_COLUMNS,
    DeliveryError,
    build_delivery_frame,
    to_input_layout,
    validate_delivery,
)
from src.serving.comparison import ComparisonError, build_comparison
from src.serving.drivers import component_drivers
from src.serving.preview import build_preview
from src.serving.quality import QualityGateError, check_workbook, require_quality
from src.serving.sensitivity import SensitivityError, apply_sensitivity
from src.monitoring.health import health_report, job_health, render_prometheus
from src.monitoring.registry import current_version, history as registry_history
from src.serving import runs as session
from src.serving.state import last_run, record_run

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
ARTIFACTS = config.PROJECT_ROOT / "artifacts" / "web_runs"

app = FastAPI(title="استودیوی پیش‌بینی BNPL", docs_url="/api/docs", redoc_url=None)
# Slim Python images carry no mapping for web fonts, so /static/*.woff would go
# out as application/octet-stream; browsers still load it, but a correct type
# keeps strict proxies and caching layers happy.
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/woff2", ".woff2")
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
    context.setdefault("cur_help", "")
    # {{cur_*}} carries the class, {{*_current}} carries the accessible marker;
    # they are separate so a page can highlight one link and mark another.
    context.setdefault("home_current", "")
    context.setdefault("comparison_current", "")
    for key, value in context.items():
        page = page.replace("{{" + key + "}}", str(value))
    return HTMLResponse(page)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _render(
        "index.html",
        cur_home=CURRENT,
        package_columns=" | ".join(OUTPUT_COLUMNS),
        input_columns=" | ".join(INPUT_COLUMNS),
        side_foot="پردازش کاملاً محلی روی فایل اکسل<br />بدون اتصال به دیتابیس",
    )


@app.get("/comparison", response_class=HTMLResponse)
def comparison_page() -> HTMLResponse:
    return _render(
        "comparison.html",
        comparison_current=CURRENT,
        side_foot="گذشته از دادهٔ واقعی<br />آینده از خطای بک‌تست",
    )


@app.get("/help", response_class=HTMLResponse)
def help_page() -> HTMLResponse:
    """Static report and the API reference used to sit in the main menu."""
    return _render("help.html", cur_help=CURRENT)


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
    payment_method: str = Form(""),
) -> JSONResponse:
    """Pre-flight gate: report every data check before anything is computed."""
    saved = await _save_upload(file)
    report = check_workbook(
        saved, horizon=_requested_horizon(horizon), payment_method=payment_method
    )
    payload = report.as_dict()
    payload["preview"] = _preview_of(saved, report)
    return JSONResponse(payload)


def _preview_of(saved: Path, report) -> dict:
    """Describe what was read; a preview failure must not hide the gate."""
    try:
        loaded = load_workbook(saved)
        sheets = [r.as_dict() for r in loaded.reports]
        return build_preview(loaded.facts, sheets)
    except Exception:  # noqa: BLE001 - preview is context, never a blocker
        return build_preview(None, [])


@app.post("/api/forecast")
async def forecast(
    file: UploadFile = File(...),
    horizon: str = Form("auto"),
    model: str = Form("auto"),
    quality_token: str = Form(""),
    payment_method: str = Form(""),
) -> JSONResponse:
    saved = await _save_upload(file)
    requested = _requested_horizon(horizon)

    try:
        report = require_quality(
            saved, quality_token, horizon=requested, payment_method=payment_method
        )
    except QualityGateError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc

    plan = report.plan or {}
    horizon = int(plan.get("horizon") or 30)

    try:
        result = run_pipeline(
            saved,
            horizon=horizon,
            model=model,
            payment_method=report.payment_method or None,
        )
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
    summary["backtest_view"] = _backtest_summary(summary)
    summary["backtest_view"]["chart"] = _backtest_chart(result)
    summary["drivers"] = component_drivers(
        result.mart, result.forecast, horizon=horizon, model=result.model
    )
    summary["payment_method"] = report.payment_method
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


# ---------------------------------------------------------------------------
# Run sessions: one upload, read once, reused by every step of the flow.
# ---------------------------------------------------------------------------


def _run_or_404(run_id: str) -> dict:
    try:
        return session.load(run_id)
    except session.RunError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _quality_payload(report) -> dict:
    return report.as_dict()


def _backtest_summary(summary: dict) -> dict:
    """The step-4 view: why this model, what it scored, and how it was tested."""
    report = summary.get("backtest_report") or {}
    ranking = summary.get("backtest_ranking") or []
    models = report.get("models") or ranking
    winner = models[0] if models else None
    chosen = summary.get("model")
    chosen_row = next(
        (row for row in models if row.get("model") == chosen), winner
    ) if models else None
    return {
        "model": chosen,
        "chosen": chosen_row,
        "models": models,
        "windows": report.get("windows"),
        "points": report.get("points"),
        "test_start": report.get("test_start"),
        "test_end": report.get("test_end"),
        "horizon": report.get("horizon"),
        "note": report.get("note"),
        "reconciliation": report.get("reconciliation") or [],
        "ranking": ranking,
    }


def _backtest_chart(result) -> dict | None:
    """Actual versus the model's own walk-forward prediction, over the test span.

    The backtest already holds, for every origin and horizon, the prediction and
    what actually happened. Plotting it is the only honest answer to "was the
    model right about the past?" — much better than a number alone. Where two
    origins predict the same day, the prediction made from the closest origin
    (smallest horizon) is kept, so the line is comparable to the actuals.
    """
    rows = getattr(result, "backtest_rows", None)
    if rows is None or rows.empty:
        return None
    frame = rows.copy()
    frame["target_date"] = pd.to_datetime(frame["target_date"])
    frame = frame.sort_values(["target_date", "horizon"])
    frame = frame.drop_duplicates(subset=["target_date", "entity_id"], keep="first")
    grouped = frame.groupby("target_date", as_index=False).agg(
        actual=("actual", "sum"),
        yhat=("yhat", "sum"),
    )
    grouped = grouped.sort_values("target_date")
    # A year of daily points would be unreadable in one chart; keep the latest
    # stretch that still shows several origins.
    grouped = grouped.tail(120)
    dates = [pd_date(d.date()) for d in grouped["target_date"]]
    return {
        "labels": dates,
        "actual": [round(float(v), 1) for v in grouped["actual"]],
        "forecast": [round(float(v), 1) for v in grouped["yhat"]],
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
    }


@app.post("/api/runs")
async def create_run(
    file: UploadFile = File(...),
    horizon: str = Form("auto"),
    payment_method: str = Form(""),
) -> JSONResponse:
    """Step 1+2: save the workbook, read it once, run the gate, preview it."""
    saved = await _save_upload(file)
    report = check_workbook(
        saved, horizon=_requested_horizon(horizon), payment_method=payment_method
    )
    payload = _quality_payload(report)
    preview = _preview_of(saved, report)
    plan = report.plan or {}

    try:
        record = session.create(
            saved,
            file_name=_display_name(saved),
            origin=report.origin.isoformat() if report.origin else None,
            target=plan.get("target"),
            horizon=plan.get("horizon"),
            payment_method=report.payment_method,
            quality=payload,
            preview=preview,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=507, detail=f"ذخیرهٔ نشست اجرا ممکن نشد: {exc}"
        ) from exc

    steps = dict(record["steps"])
    steps["data"] = True
    steps["quality"] = not report.blocked
    session.update(record["run_id"], steps=steps)

    payload["run"] = {
        "run_id": record["run_id"],
        "file": record["file"],
        "created_at": record["created_at"],
        "payment_method": record.get("payment_method") or "",
        "steps": steps,
    }
    payload["preview"] = preview
    return JSONResponse(payload)


@app.post("/api/runs/{run_id}/forecast")
def run_forecast(
    run_id: str,
    quality_token: str = Form(""),
    model: str = Form("auto"),
    horizon: str = Form("auto"),
) -> JSONResponse:
    """Step 3+4+5: forecast from the stored workbook after the token matches."""
    record = _run_or_404(run_id)
    try:
        saved = session.workbook_path(record)
    except session.RunError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc

    requested = _requested_horizon(horizon)
    method = record.get("payment_method") or ""
    try:
        report = require_quality(
            saved, quality_token, horizon=requested, payment_method=method
        )
    except QualityGateError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc

    plan = report.plan or {}
    effective = int(plan.get("horizon") or 30)
    try:
        result = run_pipeline(
            saved, horizon=effective, model=model, payment_method=method or None
        )
    except (ExcelInputError, PipelineError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"پردازش فایل با خطا مواجه شد: {exc}"
        ) from exc

    summary = result.summary()
    summary["horizon_plan"] = plan
    summary["window"] = report.window
    summary["chart"] = _chart_payload(result)
    summary["entities_table"] = _entity_table(result)
    summary["backtest_view"] = _backtest_summary(summary)
    summary["backtest_view"]["chart"] = _backtest_chart(result)
    summary["drivers"] = component_drivers(
        result.mart, result.forecast, horizon=effective, model=result.model
    )
    summary["payment_method"] = report.payment_method
    summary["run_id"] = run_id

    session.save_view(run_id, "forecast", summary)
    # The sensitivity step works on the full forecast grain, so it is stored
    # as rows next to the compact UI payload instead of being rebuilt later.
    session.save_view(run_id, "forecast_rows", _forecast_rows(result))
    record = session.update(
        run_id,
        origin=report.origin.isoformat() if report.origin else record.get("origin"),
        target=plan.get("target"),
        horizon=effective,
        model=summary.get("model"),
    )
    steps = dict(record.get("steps") or {})
    steps.update({"forecast": True, "backtest": bool(summary.get("backtest_report"))})
    session.update(run_id, steps=steps)
    summary["last_run"] = record_run(
        "forecast",
        {
            "file": record.get("file"),
            "origin": record.get("origin"),
            "target": record.get("target"),
            "horizon": effective,
            "model": summary.get("model"),
            "rows": len(summary.get("entities_table") or []),
        },
    )
    return JSONResponse(summary)


@app.post("/api/runs/{run_id}/comparison")
def run_comparison(
    run_id: str,
    months: int = Form(6),
    quality_token: str = Form(""),
) -> JSONResponse:
    """Step 7: the monthly dashboard, from the same workbook as the forecast."""
    record = _run_or_404(run_id)
    try:
        saved = session.workbook_path(record)
    except session.RunError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc

    try:
        report = require_quality(
            saved, quality_token, payment_method=record.get("payment_method") or ""
        )
    except QualityGateError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc

    plan = report.plan or {}
    horizon_days = int(plan.get("horizon") or 30)
    model = record.get("model") or "auto"
    try:
        result = build_comparison(
            report.facts,
            origin=report.origin,
            horizon=horizon_days,
            months=max(1, min(int(months or 6), 12)),
            model=model,
            payment_method=report.payment_method,
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
    payload["run_id"] = run_id
    session.save_view(run_id, "comparison", payload)
    session.mark(run_id, "monthly")
    last = record_run(
        "comparison",
        {
            "file": record.get("file"),
            "origin": record.get("origin"),
            "target": plan.get("target"),
            "horizon": horizon_days,
            "model": result.model,
            "next_month": result.next_month,
        },
    )
    payload["last_run"] = last
    return JSONResponse(payload)


@app.post("/api/runs/{run_id}/sensitivity")
def run_sensitivity(
    run_id: str,
    entity_id: str = Form(...),
    delta_pct: float = Form(0.0),
) -> JSONResponse:
    """Step 7: one adjustable assumption on top of the saved base forecast."""
    _run_or_404(run_id)
    rows = session.load_view(run_id, "forecast_rows")
    if not rows:
        raise HTTPException(
            status_code=409, detail="ابتدا پیش‌بینی را اجرا کنید تا سناریوی حساسیت ساخته شود."
        )

    source = pd.DataFrame(rows.get("orders") or [])
    gmv_frame = pd.DataFrame(rows.get("gmv") or [])
    try:
        result = apply_sensitivity(
            source,
            entity_id=entity_id,
            delta_pct=delta_pct,
            gmv_forecast=gmv_frame if not gmv_frame.empty else None,
        )
    except SensitivityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = result.as_dict()
    session.save_view(run_id, "sensitivity", payload)
    session.mark(run_id, "sensitivity")
    return JSONResponse(payload)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    """Restore a session after a refresh: every step that has been computed."""
    record = _run_or_404(run_id)
    return JSONResponse(session.session_payload(record))


@app.get("/api/runs")
def get_runs(limit: int = 12) -> JSONResponse:
    return JSONResponse({"runs": session.list_runs(limit=max(1, min(int(limit), 50)))})


def _forecast_rows(result) -> dict:
    """The full forecast grain, so later steps never need the workbook again."""
    orders = []
    for row in result.forecast.itertuples(index=False):
        orders.append(
            {
                "forecast_origin": str(row.forecast_origin),
                "target_date": pd_date(row.target_date),
                "entity_id": str(row.entity_id),
                "entity_type": str(row.entity_type),
                "horizon": int(row.horizon),
                "yhat": float(row.yhat),
                "reconciled_yhat": float(row.reconciled_yhat),
                "lower_80": float(row.lower_80),
                "upper_80": float(row.upper_80),
                "lower_90": float(row.lower_90),
                "upper_90": float(row.upper_90),
                "model_version": str(row.model_version),
            }
        )
    gmv = []
    if result.gmv_forecast is not None and not result.gmv_forecast.empty:
        for row in result.gmv_forecast.itertuples(index=False):
            gmv.append(
                {
                    "target_date": pd_date(row.target_date),
                    "entity_id": str(row.entity_id),
                    "entity_type": str(row.entity_type),
                    "horizon": int(row.horizon),
                    "gmv_yhat": float(row.gmv_yhat),
                }
            )
    return {"orders": orders, "gmv": gmv}


def _delivery_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        out = frame.copy()
        out["date"] = pd.to_datetime(out["date"])
        out.to_excel(writer, sheet_name="Sheet1", index=False)
        sheet = writer.sheets["Sheet1"]
        for cell in sheet["A"][1:]:
            cell.number_format = "yyyy-mm-dd"
        for column, width in zip("ABCDEFG", (12, 10, 16, 30, 18, 16, 20)):
            sheet.column_dimensions[column].width = width
    return buffer.getvalue()


def _ensure_package(run_id: str) -> tuple[Path, dict]:
    """Build the delivery workbook once; later calls read the cached file."""
    record = _run_or_404(run_id)
    try:
        path = session.package_path(run_id)
    except session.RunError:
        path = None

    if path is None:
        try:
            saved = session.workbook_path(record)
        except session.RunError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        horizon = int(record.get("horizon") or 30)
        model = record.get("model") or "auto"
        method = record.get("payment_method") or None
        try:
            facts = load_category_facts(saved, payment_method=method)
            built = build_delivery_frame(
                facts,
                origin=max(facts["date"]).date(),
                horizon=horizon,
                model=model,
                method_shares=payment_method_shares(
                    saved, payment_method=method
                ),
            )
            validate_delivery(built.frame)
        except (ExcelInputError, DeliveryError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"ساخت بستهٔ تحویل با خطا مواجه شد: {exc}"
            ) from exc
        path = session.save_package(run_id, _delivery_bytes(built.frame))
        session.update(
            run_id,
            package={
                "rows": int(len(built.frame)),
                "categories": int(built.categories),
                "days": int(built.horizon),
                "origin": built.origin.isoformat(),
                "target": record.get("target"),
                "sample": built.frame.head(5).astype(str).to_dict("records"),
            },
        )
        session.mark(run_id, "package")
        record = session.load(run_id)

    return path, record


@app.get("/api/runs/{run_id}/package")
def download_package(run_id: str) -> Response:
    """The delivery workbook of a run, built on first request and then cached."""
    path, record = _ensure_package(run_id)
    info = record.get("package") or {}
    filename = f"forecast-{record.get('horizon') or 30}d-{record.get('origin') or 'run'}.xlsx"
    return Response(
        path.read_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Forecast-Rows": str(info.get("rows") or ""),
        },
    )


@app.get("/api/runs/{run_id}/export")
def download_input_format(run_id: str) -> Response:
    """The forecast in the same columns as the uploaded data.

    The delivery file is what the business consumes, but an analyst often
    wants the forecast back in the layout they handed in, so the file can be
    re-uploaded or joined without any reshaping.
    """
    path, record = _ensure_package(run_id)
    try:
        delivered = pd.read_excel(path)
        shaped = to_input_layout(delivered)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"ساخت خروجی هم‌قالب ورودی با خطا مواجه شد: {exc}"
        ) from exc

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        out = shaped.copy()
        out["date"] = pd.to_datetime(out["date"])
        out.to_excel(writer, sheet_name="Sheet1", index=False)
        sheet = writer.sheets["Sheet1"]
        for cell in sheet["A"][1:]:
            cell.number_format = "yyyy-mm-dd"
        for column, width in zip("ABCDEF", (12, 16, 30, 22, 14, 20)):
            sheet.column_dimensions[column].width = width

    filename = f"forecast-{record.get('horizon') or 30}d-input-format.xlsx"
    return Response(
        buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Forecast-Rows": str(len(shaped)),
        },
    )


@app.get("/api/runs/{run_id}/export/preview")
def export_preview(run_id: str) -> JSONResponse:
    """Headers and a few rows of the input-format export, without downloading."""
    path, record = _ensure_package(run_id)
    shaped = to_input_layout(pd.read_excel(path))
    sample = shaped.head(5).astype(str).to_dict("records")
    return JSONResponse(
        {
            "ready": True,
            "columns": list(shaped.columns),
            "rows": int(len(shaped)),
            "days": int(shaped["date"].nunique()),
            "verticals": sorted({str(v) for v in shaped["vertical"].unique()}),
            "sample": sample,
        }
    )


@app.get("/api/runs/{run_id}/package/preview")
def package_preview(run_id: str) -> JSONResponse:
    """What the delivery file will contain, before the user downloads it."""
    record = _run_or_404(run_id)
    info = record.get("package")
    if not info:
        return JSONResponse({"ready": False})
    return JSONResponse({"ready": True, **info})


def _entity_series(frame: pd.DataFrame, column: str) -> dict[str, pd.Series]:
    if frame is None or frame.empty or column not in frame.columns:
        return {}
    out: dict[str, pd.Series] = {}
    for entity_id, chunk in frame.groupby("entity_id"):
        series = chunk.sort_values("horizon").set_index("horizon")[column]
        out[str(entity_id)] = series.astype("float64")
    return out


def _chart_payload(result) -> dict:
    """Actual history and forecast for every entity, not just the total.

    The UI lets the analyst switch entity without another round trip, so the
    payload carries the same three metrics per entity. The total is always
    ``entities["TOTAL"]`` and is also mirrored at the top level for older
    callers.
    """
    assert result.mart is not None and result.forecast is not None
    mart = result.mart
    horizon = int(result.horizon)
    has_gmv = (
        result.gmv_forecast is not None
        and not result.gmv_forecast.empty
        and "gmv" in mart.columns
    )

    history = mart[mart["successful_orders"].notna()].sort_values("date")
    history = history.groupby("entity_id", group_keys=False).tail(90)
    hist_dates: dict[str, list] = {}
    hist_orders: dict[str, list] = {}
    hist_gmv: dict[str, list] = {}
    hist_aov: dict[str, list] = {}
    for entity_id, chunk in history.groupby("entity_id"):
        chunk = chunk.sort_values("date")
        key = str(entity_id)
        hist_dates[key] = [pd_date(d) for d in chunk["date"]]
        hist_orders[key] = [float(v) for v in chunk["successful_orders"]]
        if has_gmv:
            gmv = chunk["gmv"].astype("float64")
            orders = chunk["successful_orders"].astype("float64")
            hist_gmv[key] = [None if pd.isna(v) else float(v) for v in gmv]
            hist_aov[key] = [
                float(g) / float(o) if o and not pd.isna(g) else None
                for g, o in zip(gmv, orders)
            ]

    fc_dates = {
        str(k): [pd_date(d) for d in c.sort_values("horizon")["target_date"]]
        for k, c in result.forecast.groupby("entity_id")
    }
    fc_orders = _entity_series(result.forecast, "reconciled_yhat")
    fc_low = _entity_series(result.forecast, "lower_90")
    fc_high = _entity_series(result.forecast, "upper_90")
    fc_gmv = _entity_series(result.gmv_forecast, "gmv_yhat")
    fc_gmv_low = _entity_series(result.gmv_forecast, "gmv_lower_90")
    fc_gmv_high = _entity_series(result.gmv_forecast, "gmv_upper_90")
    fc_aov = _entity_series(result.gmv_forecast, "aov_yhat")

    def values(series: dict[str, pd.Series], key: str) -> list:
        frame = series.get(key)
        return [] if frame is None else frame.round(1).tolist()

    entities: dict[str, dict] = {}
    for entity_id in fc_orders:
        entry = {
            "orders": {
                "history": {
                    "labels": hist_dates.get(entity_id, []),
                    "values": hist_orders.get(entity_id, []),
                },
                "forecast": {
                    "labels": fc_dates.get(entity_id, []),
                    "values": values(fc_orders, entity_id),
                    "lower": values(fc_low, entity_id),
                    "upper": values(fc_high, entity_id),
                },
            }
        }
        if entity_id in fc_gmv:
            entry["gmv"] = {
                "history": {
                    "labels": hist_dates.get(entity_id, []),
                    "values": hist_gmv.get(entity_id, []),
                },
                "forecast": {
                    "labels": fc_dates.get(entity_id, []),
                    "values": values(fc_gmv, entity_id),
                    "lower": values(fc_gmv_low, entity_id),
                    "upper": values(fc_gmv_high, entity_id),
                },
            }
            entry["aov"] = {
                "history": {
                    "labels": hist_dates.get(entity_id, []),
                    "values": hist_aov.get(entity_id, []),
                },
                "forecast": {
                    "labels": fc_dates.get(entity_id, []),
                    "values": values(fc_aov, entity_id),
                },
            }
        entities[entity_id] = entry

    payload = {"horizon": horizon, "entities": entities}
    total = entities.get("TOTAL")
    if total:
        # Backwards-compatible top level: the total series only.
        payload.update(total)
    return payload


def _entity_table(result) -> list[dict]:
    """One row per entity, with columns derived from the actual horizon.

    The old table hard-coded D+1 / D+7 / last-day / 30-day total, which
    silently lied for 7, 14 or 60-day runs. Columns now follow the horizon.
    """
    assert result.forecast is not None
    horizon = int(result.horizon)
    day_points = sorted({1, min(7, horizon), horizon})

    gmv_by_entity: dict[str, dict] = {}
    if result.gmv_forecast is not None:
        for entity_id, chunk in result.gmv_forecast.groupby("entity_id"):
            chunk = chunk.sort_values("horizon")
            by_h = chunk.set_index("horizon")
            gmv_by_entity[str(entity_id)] = {
                "gmv_points": {
                    f"d{day}": _round(by_h["gmv_yhat"].get(day)) for day in day_points
                },
                "gmv_total": _round(by_h["gmv_yhat"].sum()),
                "aov_d1": _round(by_h["aov_yhat"].get(1)),
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
                "points": {f"d{day}": _round(by_h.get(day)) for day in day_points},
                "total": _round(by_h.sum()),
                "gmv_points": extra.get("gmv_points", {}),
                "gmv_total": extra.get("gmv_total"),
                "aov_d1": extra.get("aov_d1"),
                # Legacy field names kept so older clients keep working.
                "d1": _round(by_h.get(1)),
                "d7": _round(by_h.get(7)),
                "d30": _round(by_h.get(horizon)),
                "total_30": _round(by_h.sum()),
                "gmv_d1": (extra.get("gmv_points") or {}).get("d1"),
                "gmv_total_30": extra.get("gmv_total"),
            }
        )
    return sorted(
        rows,
        key=lambda r: (r["entity_type"] != "TOTAL", r["entity_type"], r["entity_id"]),
    )


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
    payment_method: str = Form(""),
) -> StreamingResponse:
    """Return the delivery workbook: date | level | vertical | category | payment_method | order | GMV.

    This is the business-facing format, one row per date x vertical x category,
    covering ``horizon`` days after the last day present in the upload.
    """
    saved = await _save_upload(file)
    requested = _requested_horizon(horizon)

    try:
        report = require_quality(
            saved, quality_token, horizon=requested, payment_method=payment_method
        )
    except QualityGateError as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc

    horizon = int((report.plan or {}).get("horizon") or 30)

    try:
        facts = load_category_facts(saved, payment_method=report.payment_method or None)
        result = build_delivery_frame(
            facts,
            origin=max(facts["date"]),
            horizon=horizon,
            model=model,
            method_shares=payment_method_shares(
                saved, payment_method=report.payment_method or None
            ),
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
        for column, width in zip("ABCDEFG", (12, 10, 16, 30, 18, 16, 20)):
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
    payment_method: str = Form(""),
) -> JSONResponse:
    """Past months plus three scenarios for the rest of this month and the next."""
    saved = await _save_upload(file)
    requested = _requested_horizon(horizon)

    try:
        report = require_quality(
            saved, quality_token, horizon=requested, payment_method=payment_method
        )
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
            payment_method=report.payment_method,
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
