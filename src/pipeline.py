"""Pipeline: Excel workbook in, forecast + reports out.

``run_pipeline`` is the single orchestration path shared by the web UI and the
CLI, so both always produce identical results.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src import config
from src.data_mart.entity_master import EntityMaster
from src.data_mart.orders import build_daily_mart, validate_total_reconciliation
from src.evaluation.backtest import (
    BacktestConfig,
    backtest,
    backtest_global,
    horizon_accuracy,
)
from src.evaluation.metrics import bias, wape
from src.evaluation.conformal import calibrate, coverage_by
from src.evaluation.report import build_backtest_report
from src.ingestion.excel import ExcelLoadResult, load_workbook
from src.features.engagement import availability as engagement_availability
from src.models.baselines import BASELINES, moving_average
from src.models.global_model import MODEL_NAME as GLOBAL_MODEL
from src.models.global_model import fit_global, forecasters_from_model
from src.lifecycle.regime import regime_report
from src.reconciliation.reconcile import compare_methods, residuals_from_backtest
from src.scenarios.scenario import apply_scenario, load_scenarios
from src.scenarios.scenario import comparison as scenario_comparison
from src.serving.gmv import combine_gmv, forecast_aov, has_gmv, validate_gmv
from src.serving.service import RunContext, run_forecast


class PipelineError(RuntimeError):
    """Raised when a run cannot produce a trustworthy forecast."""


def _json_safe(value):
    """Recursively replace NaN with None so the payload is strict JSON.

    The base row of the scenario table has no delta (there is nothing to
    compare against), and pandas renders that as NaN rather than None.
    """
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return _json_safe(value.item())
        except (AttributeError, ValueError):
            return value
    return value


@dataclass
class PipelineResult:
    run_id: str
    source_file: str
    date_start: date
    date_end: date
    horizon: int
    model: str
    mart_rows: int
    entities: list[str]
    unmapped_orders: int
    total_last_actual: int | None
    total_forecast: dict[int, float]
    backtest_ranking: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    forecast: pd.DataFrame | None = None
    mart: pd.DataFrame | None = None
    sheet_reports: list[dict[str, Any]] = field(default_factory=list)
    gmv_forecast: pd.DataFrame | None = None
    aov_forecast: pd.DataFrame | None = None
    total_gmv_forecast: dict[int, float] = field(default_factory=dict)
    total_aov_forecast: dict[int, float] = field(default_factory=dict)
    total_last_actual_gmv: float | None = None
    total_last_actual_aov: float | None = None
    dropped_partial_days: list[str] = field(default_factory=list)
    backtest_report: dict[str, Any] = field(default_factory=dict)
    # The selected model's backtest rows, with calibrated bounds attached.
    # Not part of summary(): monitoring reads it, the UI does not need it.
    backtest_rows: pd.DataFrame | None = None
    global_model: dict[str, Any] = field(default_factory=dict)
    feature_availability: dict[str, Any] = field(default_factory=dict)
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    regimes: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_file": self.source_file,
            "date_start": self.date_start.isoformat(),
            "date_end": self.date_end.isoformat(),
            "horizon": self.horizon,
            "model": self.model,
            "mart_rows": self.mart_rows,
            "entities": self.entities,
            "unmapped_orders": self.unmapped_orders,
            "total_last_actual": self.total_last_actual,
            "total_forecast": {f"D+{k}": v for k, v in sorted(self.total_forecast.items())},
            "total_last_actual_gmv": self.total_last_actual_gmv,
            "total_last_actual_aov": self.total_last_actual_aov,
            "total_gmv_forecast": {
                f"D+{k}": v for k, v in sorted(self.total_gmv_forecast.items())
            },
            "total_aov_forecast": {
                f"D+{k}": v for k, v in sorted(self.total_aov_forecast.items())
            },
            "dropped_partial_days": self.dropped_partial_days,
            "backtest_ranking": self.backtest_ranking,
            "backtest_report": self.backtest_report,
            "global_model": self.global_model,
            "feature_availability": self.feature_availability,
            "scenarios": _json_safe(self.scenarios),
            "regimes": _json_safe(self.regimes),
            "warnings": self.warnings,
            "sheets": self.sheet_reports,
        }

    def to_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False, indent=2)


def entity_master_for_workbook(
    loaded: ExcelLoadResult, base: EntityMaster | None = None
) -> EntityMaster:
    """Build an entity master containing exactly the entities in the workbook.

    Only entities that actually appear in the file are kept (plus TOTAL). Any
    other configured vertical would otherwise receive invented zero-order rows
    for every date, which is exactly the silent-zero behaviour the engine must
    avoid. Ventures discovered in the file become VENTURE children of TOTAL.
    """
    master = base or EntityMaster.load()
    vertical_map = {
        str(k): str(v)
        for k, v in (config.load_yaml("hierarchy.yaml").get("vertical_entity_map") or {}).items()
    }

    present: dict[str, str] = {}
    for _, row in loaded.facts.iterrows():
        entity_id = str(row["entity_id"]).strip()
        entity_type = str(row["entity_type"]).strip().upper()
        if not entity_id or entity_type not in {"VERTICAL", "VENTURE"}:
            continue
        entity_id = vertical_map.get(entity_id, entity_id)
        present.setdefault(entity_id, entity_type)

    if not present:
        raise PipelineError("Workbook contains no entity that could be classified.")

    rows = []
    total = master.totals()[0]
    rows.append(
        {
            "entity_id": total.entity_id,
            "entity_name": total.entity_name,
            "entity_type": "TOTAL",
            "parent_entity_id": "",
            "is_active": "true",
            "valid_from": total.valid_from or "",
            "valid_to": total.valid_to or "",
        }
    )
    for entity_id, entity_type in sorted(present.items()):
        existing = master.by_id(entity_id)
        rows.append(
            {
                "entity_id": entity_id,
                "entity_name": existing.entity_name if existing else entity_id,
                "entity_type": entity_type,
                "parent_entity_id": total.entity_id,
                "is_active": "true",
                "valid_from": (existing.valid_from or "") if existing else "",
                "valid_to": (existing.valid_to or "") if existing else "",
            }
        )
    return EntityMaster(pd.DataFrame(rows))


def _drop_partial_last_day(
    observed_rows: pd.DataFrame, total_id: str, warnings: list[str]
) -> tuple[pd.DataFrame, list[str]]:
    """Exclude a trailing day that is clearly incomplete.

    An export taken mid-day contains a partial final day. Training on it would
    teach the model a fake collapse. The last day is dropped only when it is
    below 70% of the median of the same weekday over the previous four weeks,
    so a genuine low day is never removed.
    """
    totals = (
        observed_rows[observed_rows["entity_id"] == total_id]
        .assign(date=lambda d: pd.to_datetime(d["date"]))
        .set_index("date")["successful_orders"]
        .sort_index()
    )
    if len(totals) < 8:
        return observed_rows, []
    last = totals.index.max()
    history = totals[totals.index < last]
    same_weekday = history[history.index.day_of_week == last.day_of_week].iloc[-4:]
    if len(same_weekday) < 2:
        return observed_rows, []
    baseline = float(same_weekday.median())
    if baseline <= 0 or float(totals.loc[last]) >= 0.7 * baseline:
        return observed_rows, []

    dropped = last.date().isoformat()
    warnings.append(
        f"Partial day {dropped} excluded from training: "
        f"{int(totals.loc[last]):,} orders vs {int(baseline):,} typical for that weekday."
    )
    kept = observed_rows[pd.to_datetime(observed_rows["date"]).dt.date != last.date()]
    return kept, [dropped]


def _global_diagnostics(global_bt: pd.DataFrame) -> dict[str, Any]:
    """Explain which backend ran and how much evidence backed it.

    The global model is only trustworthy if the user can see whether it was
    ridge or LightGBM, on how many origins, and how confident the fit was.
    """
    if global_bt is None or global_bt.empty:
        return {}
    origins = int(global_bt["forecast_origin"].nunique())
    return {
        "model": GLOBAL_MODEL,
        "backend": "lightgbm" if _has_lightgbm() else "ridge",
        "origins": origins,
        "points": int(len(global_bt)),
        "low_confidence": origins < 3,
    }


def _has_lightgbm() -> bool:
    try:
        import lightgbm  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def run_pipeline(
    workbook_path: str | Path,
    *,
    horizon: int = 30,
    model: str = "auto",
    backtest_step: int = 30,
    min_train_days: int = 60,
    run_backtest: bool = True,
    vertical_map: dict[str, str] | None = None,
    payment_method: str | None = None,
) -> PipelineResult:
    """Load a workbook, build the mart, backtest, and forecast D+1..D+horizon.

    ``payment_method`` forecasts one payment method only (for example
    INSTALLMENT); every number downstream then describes that slice.
    """
    loaded = load_workbook(workbook_path, payment_method=payment_method)
    facts = loaded.facts.copy()

    if vertical_map is None:
        vertical_map = {
            str(k): str(v)
            for k, v in (config.load_yaml("hierarchy.yaml").get("vertical_entity_map") or {}).items()
        }
    if vertical_map:
        is_vertical = facts["entity_type"] == "VERTICAL"
        facts.loc[is_vertical, "entity_id"] = (
            facts.loc[is_vertical, "entity_id"].map(vertical_map).fillna(
                facts.loc[is_vertical, "entity_id"]
            )
        )

    start = min(facts["date"])
    end = max(facts["date"]) + pd.Timedelta(days=1)
    if isinstance(end, pd.Timestamp):
        end = end.date()

    master = entity_master_for_workbook(loaded)
    unmapped = facts[facts["entity_type"] == "UNMAPPED"]
    leaves = facts[facts["entity_type"].isin(["VERTICAL", "VENTURE"])]
    if leaves.empty:
        raise PipelineError(
            "No classified orders found. Add a 'vertical' column (or entity_id) "
            "to the workbook so orders can be assigned to an entity."
        )

    observed = set(pd.to_datetime(facts["date"]).dt.date.unique())
    snapshot_id = f"xlsx_{datetime.now():%Y%m%d_%H%M%S}"
    mart = build_daily_mart(
        leaves,
        master,
        start=min(observed),
        end=max(observed) + pd.Timedelta(days=1),
        snapshot_id=snapshot_id,
        observed_dates=observed,
    )
    validate_total_reconciliation(mart)

    observed_rows = mart[
        mart["quality_flag"].eq("OBSERVED") & mart["successful_orders"].notna()
    ].copy()
    observed_rows["successful_orders"] = observed_rows["successful_orders"].astype("int64")

    warnings = list(loaded.warnings)
    # Which optional feature families this workbook can actually support. A
    # missing column is reported, never filled with a fake zero.
    feature_availability = engagement_availability(observed_rows).as_dict()
    if feature_availability["missing"]:
        warnings.append(
            "Optional feature columns absent from the workbook: "
            + ", ".join(feature_availability["missing"])
            + ". Those features were not built."
        )
    observed_rows, dropped_partial = _drop_partial_last_day(
        observed_rows, master.total_id(), warnings
    )
    if dropped_partial:
        observed = {d for d in observed if d.isoformat() not in dropped_partial}

    if not unmapped.empty:
        warnings.append(
            f"{int(unmapped['successful_orders'].sum())} orders could not be mapped to an "
            "entity and are excluded from the total."
        )

    ranking: list[dict[str, Any]] = []
    backtest_report: dict[str, Any] = {}
    global_fit: dict[str, Any] = {}
    backtest_rows: pd.DataFrame | None = None
    calibration = None
    selected = model
    bt_config = BacktestConfig(
        horizon=min(horizon, 30), step=backtest_step, min_train_days=min_train_days
    )
    if run_backtest:
        try:
            bt = backtest(observed_rows, BASELINES, bt_config)
        except Exception as exc:  # noqa: BLE001 - backtest is advisory, never blocks
            bt = pd.DataFrame()
            warnings.append(f"Backtest skipped: {exc}")
        # The global model trains across entities, so it has its own loop.
        # A failure here must not cost the user the baseline comparison.
        try:
            global_bt = backtest_global(observed_rows, bt_config)
            if not global_bt.empty:
                bt = (
                    global_bt
                    if bt.empty
                    else pd.concat([bt, global_bt], ignore_index=True)
                )
        except Exception as exc:  # noqa: BLE001
            global_bt = pd.DataFrame()
            warnings.append(f"Global model backtest skipped: {exc}")
        if not bt.empty:
            # Rank on the pooled WAPE, not the mean of per-entity WAPEs: a
            # simple average lets a small entity with a bad ratio outvote the
            # business total, which is the number the user actually forecasts.
            overall = (
                bt.groupby("model_name")
                .apply(
                    lambda c: pd.Series(
                        {
                            "wape": wape(c["actual"], c["yhat"]),
                            "bias": bias(c["actual"], c["yhat"]),
                        }
                    ),
                    include_groups=False,
                )
                .sort_values("wape")
            )
            ranking = [
                {"model": name, "wape": float(row["wape"]), "bias": float(row["bias"])}
                for name, row in overall.iterrows()
                if pd.notna(row["wape"])
            ]
            if model == "auto" and ranking:
                selected = ranking[0]["model"]
            backtest_report = build_backtest_report(
                bt, horizon=min(horizon, 30)
            ).as_dict()
            global_fit = _global_diagnostics(global_bt)
            # Calibrate the band on the selected model's own residuals. This
            # must happen after selection, so the band describes the model the
            # user will actually see.
            selected_rows = bt[bt["model_name"] == selected]
            if not selected_rows.empty:
                calibration = calibrate(selected_rows)
                coverage = coverage_by(selected_rows, calibration, level=0.90)
                backtest_report["coverage"] = coverage.to_dict("records")
                backtest_report["calibration"] = calibration.as_dict()
                # Keep the selected rows with their calibrated band so the
                # monitoring job can measure drift on the model that shipped,
                # not on a re-derived approximation of it.
                backtest_rows = selected_rows.copy()
                lower, upper = [], []
                for index, row in backtest_rows.iterrows():
                    width = calibration.half_width(int(row["horizon"]), 0.90)
                    lower.append(max(float(row["yhat"]) * (1 - width), 0.0))
                    upper.append(float(row["yhat"]) * (1 + width))
                backtest_rows["lower_90"] = lower
                backtest_rows["upper_90"] = upper
                # Which reconciliation method actually reduces error on this
                # data? Measured per level, because a method can win on the
                # total and lose on the leaves.
                try:
                    reconciliation_table = compare_methods(
                        selected_rows,
                        master,
                        selected_rows,
                        historical=observed_rows,
                        residuals=residuals_from_backtest(selected_rows),
                    )
                    if not reconciliation_table.empty:
                        backtest_report["reconciliation"] = reconciliation_table.to_dict("records")
                except Exception as exc:  # noqa: BLE001 - advisory, never blocks
                    warnings.append(f"Reconciliation comparison skipped: {exc}")
    if selected == "auto":
        selected = "seasonal_naive"
        warnings.append(
            "Not enough history for a model comparison; seasonal naive was used."
        )
    if selected != GLOBAL_MODEL and selected not in BASELINES:
        raise PipelineError(f"Unknown model: {selected}")

    context = RunContext(
        forecast_run_id=f"run_{max(observed):%Y%m%d}_{datetime.now():%H%M%S}",
        forecast_origin=max(observed),
        model_name=selected,
        model_version=(
            f"global_{global_fit.get('backend', 'ridge')}_v1"
            if selected == GLOBAL_MODEL
            else f"baseline_{selected}_v1"
        ),
        feature_version="features_v2" if selected == GLOBAL_MODEL else "features_v1",
        data_snapshot_id=snapshot_id,
    )
    if selected == GLOBAL_MODEL:
        model_fit = fit_global(observed_rows, horizon=horizon)
        entity_forecasters = forecasters_from_model(
            model_fit, observed_rows, horizon=horizon
        )
        forecast = run_forecast(
            observed_rows, master, {},
            context=context, horizon=horizon,
            entity_forecasters=entity_forecasters,
            calibration=calibration,
        )
        # Explain the business total at D+1: that is the number the user acts
        # on. Attribution only — the report says so explicitly.
        try:
            global_fit["drivers"] = model_fit.explain(
                observed_rows, entity_id=master.total_id(), step=1
            )
        except Exception as exc:  # noqa: BLE001 - explanation never blocks a forecast
            warnings.append(f"Model drivers unavailable: {exc}")
    else:
        forecast = run_forecast(
            observed_rows, master, {selected: BASELINES[selected]},
            context=context, horizon=horizon,
            calibration=calibration,
        )

    total_rows = forecast[forecast["entity_id"] == master.total_id()].set_index("horizon")
    total_forecast = {
        int(h): float(total_rows.loc[h, "reconciled_yhat"]) for h in total_rows.index
    }
    last_total = observed_rows[
        (observed_rows["entity_id"] == master.total_id())
    ]["successful_orders"]
    last_total_value = int(last_total.iloc[-1]) if not last_total.empty else None

    gmv_forecast = None
    aov_forecast = None
    total_gmv_forecast: dict[int, float] = {}
    total_aov_forecast: dict[int, float] = {}
    last_gmv = None
    last_aov = None

    if has_gmv(observed_rows):
        try:
            # AOV is a ratio, so its day-to-day noise is not signal. A 14-day
            # moving average is the stable choice regardless of which model
            # won the order backtest.
            aov_forecast = forecast_aov(
                observed_rows, master, lambda s, h: moving_average(s, h, window=14),
                context=context, horizon=horizon,
            )
            gmv_forecast = combine_gmv(forecast, aov_forecast, master)
            validate_gmv(gmv_forecast, master)
            total_gmv = gmv_forecast[
                gmv_forecast["entity_id"] == master.total_id()
            ].set_index("horizon")
            total_gmv_forecast = {
                int(h): float(total_gmv.loc[h, "gmv_yhat"]) for h in total_gmv.index
            }
            total_aov_forecast = {
                int(h): float(total_gmv.loc[h, "aov_yhat"]) for h in total_gmv.index
            }
            last_total_rows = observed_rows[
                observed_rows["entity_id"] == master.total_id()
            ].sort_values("date")
            if not last_total_rows.empty:
                last_row = last_total_rows.iloc[-1]
                last_gmv = float(last_row["gmv"])
                last_aov = (
                    float(last_row["gmv"]) / float(last_row["successful_orders"])
                    if float(last_row["successful_orders"]) > 0
                    else None
                )
        except Exception as exc:  # noqa: BLE001 - GMV is additive value, never blocks orders
            warnings.append(f"GMV/AOV forecast skipped: {exc}")
            gmv_forecast = None
            aov_forecast = None
            total_gmv_forecast = {}
            total_aov_forecast = {}
    else:
        warnings.append(
            "Workbook has no GMV column, so AOV and GMV forecasts were not produced."
        )

    # Scenarios are transformations of the forecast that already exists; the
    # base frame is never touched, and a failure here must not cost the user
    # the forecast itself.
    scenario_table: list[dict[str, Any]] = []
    scenario_frames: dict[str, pd.DataFrame] = {}
    try:
        catalogue = load_scenarios()
        scenario_frames = {
            name: apply_scenario(forecast, spec)
            for name, spec in catalogue.items()
        }
        table = scenario_comparison(forecast, list(scenario_frames.values()))
        scenario_table = table.to_dict("records")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Scenarios skipped: {exc}")

    # Structural regime state, computed from observations only.
    regimes: dict[str, Any] = {}
    try:
        regimes = regime_report(observed_rows)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Regime detection skipped: {exc}")

    return PipelineResult(
        run_id=context.forecast_run_id,
        source_file=loaded.source_file,
        date_start=min(observed),
        date_end=max(observed),
        horizon=horizon,
        model=selected,
        mart_rows=len(mart),
        entities=sorted(observed_rows["entity_id"].unique().tolist()),
        unmapped_orders=int(unmapped["successful_orders"].sum()) if not unmapped.empty else 0,
        total_last_actual=last_total_value,
        total_forecast=total_forecast,
        backtest_ranking=ranking,
        backtest_report=backtest_report,
        backtest_rows=backtest_rows,
        global_model=global_fit,
        feature_availability=feature_availability,
        scenarios=scenario_table,
        regimes=regimes,
        warnings=warnings,
        forecast=forecast,
        mart=mart,
        sheet_reports=[r.as_dict() for r in loaded.reports],
        gmv_forecast=gmv_forecast,
        aov_forecast=aov_forecast,
        total_gmv_forecast=total_gmv_forecast,
        total_aov_forecast=total_aov_forecast,
        total_last_actual_gmv=last_gmv,
        total_last_actual_aov=last_aov,
        dropped_partial_days=dropped_partial,
    )
