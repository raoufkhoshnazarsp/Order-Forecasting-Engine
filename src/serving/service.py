"""Forecast serving: run baselines per leaf, reconcile, emit the output contract.

Implements the output contract of doc 21 in a DataFrame with the documented
column names.
"""

from __future__ import annotations

import collections.abc
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping

import numpy as np
import pandas as pd

from src.data_mart.entity_master import EntityMaster
from src.evaluation.conformal import ConformalCalibration
from src.models.baselines import ForecastResult
from src.reconciliation.reconcile import coherent_round, reconcile

OUTPUT_CONTRACT = [
    "forecast_run_id",
    "forecast_origin",
    "target_date",
    "entity_id",
    "entity_type",
    "horizon",
    "model_name",
    "model_version",
    "feature_version",
    "data_snapshot_id",
    "yhat",
    "lower_80",
    "upper_80",
    "lower_90",
    "upper_90",
    "reconciled_yhat",
    "scenario_id",
]


class ServingError(ValueError):
    """Raised when a forecast run cannot be produced safely."""


@dataclass(frozen=True)
class RunContext:
    forecast_run_id: str
    forecast_origin: date
    model_name: str
    model_version: str
    feature_version: str
    data_snapshot_id: str
    scenario_id: str | None = None


def residual_sigma(history: pd.Series, horizon: int) -> np.ndarray:
    """Simple multiplicative error growth by horizon, from history residuals."""
    y = history.dropna().astype("float64")
    if len(y) < 14:
        base = float(y.std()) if len(y) > 1 else 0.0
        return np.full(horizon, base)
    naive_resid = (y.diff()).abs().dropna()
    sigma1 = float(naive_resid.mean())
    return sigma1 * np.sqrt(np.arange(1, horizon + 1))


def _calibrated_bounds(
    values: np.ndarray, calibration: "ConformalCalibration", level: float
) -> tuple[np.ndarray, np.ndarray]:
    """Per-step calibrated bounds: the width depends on the horizon bucket."""
    lower = np.empty_like(values)
    upper = np.empty_like(values)
    for i, value in enumerate(values):
        width = calibration.half_width(i + 1, level)
        lower[i] = max(value * (1.0 - width), 0.0)
        upper[i] = value * (1.0 + width)
    return lower, upper


def forecast_entity(
    history: pd.Series,
    forecaster,
    horizon: int,
) -> ForecastResult:
    clean = history.dropna()
    if clean.empty:
        raise ServingError("Entity has no observed history to forecast from")
    return forecaster(clean, horizon)


def run_forecast(
    mart: pd.DataFrame,
    entity_master: EntityMaster,
    forecasters: Mapping[str, object],
    *,
    context: RunContext,
    horizon: int = 30,
    reconciliation_method: str = "bottom_up",
    z_80: float = 1.2816,
    z_90: float = 1.6449,
    entity_forecasters: Mapping[str, Mapping[str, object]] | None = None,
    calibration: "ConformalCalibration | None" = None,
) -> pd.DataFrame:
    """Produce a reconciled, coherent daily forecast path for D+1..D+horizon.

    ``entity_forecasters`` exists for models that are fitted jointly across
    entities (the global model): each entity has its own slice of one shared
    fit, so it cannot be expressed as a single callable. When it is given, the
    entity's own forecasters take precedence over ``forecasters``.

    ``calibration`` replaces the heuristic ``z * sigma`` band with empirical
    half-widths measured on the backtest. When it is absent the old heuristic
    is used, so a run without a backtest still produces a band.
    """
    observed = mart[mart["quality_flag"].eq("OBSERVED") & mart["successful_orders"].notna()]
    observed = observed[observed["entity_type"].isin(["VERTICAL", "VENTURE"])]
    if observed.empty:
        raise ServingError("Data mart holds no observed leaf history")

    rows: list[pd.DataFrame] = []
    for entity in entity_master.leaves(active_only=False):
        series = (
            observed[observed["entity_id"] == entity.entity_id]
            .assign(date=lambda d: pd.to_datetime(d["date"]))
            .set_index("date")["successful_orders"]
            .astype("float64")
            .sort_index()
        )
        if series.empty:
            continue
        sigma = residual_sigma(series, horizon)
        chosen = forecasters
        bare = False
        if entity_forecasters and entity.entity_id in entity_forecasters:
            candidate = entity_forecasters[entity.entity_id]
            # A jointly fitted model hands over one callable per entity; a
            # baseline hands over a ``{name: callable}`` mapping.
            bare = not isinstance(candidate, collections.abc.Mapping)
            chosen = {context.model_name: candidate} if bare else candidate
        for name, forecaster in chosen.items():
            result = forecast_entity(series, forecaster, horizon)
            if bare:
                # The model names itself; the stored context name is a fallback.
                name = getattr(result, "model_name", name) or name
            values = np.clip(np.asarray(result.values, dtype="float64"), 0, None)
            origin_ts = pd.Timestamp(context.forecast_origin)
            steps = np.arange(1, horizon + 1)
            if calibration is not None:
                lower_80, upper_80 = _calibrated_bounds(values, calibration, 0.80)
                lower_90, upper_90 = _calibrated_bounds(values, calibration, 0.90)
            else:
                lower_80 = np.clip(values - z_80 * sigma, 0, None)
                upper_80 = values + z_80 * sigma
                lower_90 = np.clip(values - z_90 * sigma, 0, None)
                upper_90 = values + z_90 * sigma
            rows.append(
                pd.DataFrame(
                    {
                        "forecast_origin": context.forecast_origin,
                        "target_date": [
                            (origin_ts + pd.Timedelta(days=int(s))).date() for s in steps
                        ],
                        "entity_id": entity.entity_id,
                        "entity_type": entity.entity_type,
                        "horizon": steps,
                        "model_name": name,
                        "yhat": values,
                        "lower_80": lower_80,
                        "upper_80": upper_80,
                        "lower_90": lower_90,
                        "upper_90": upper_90,
                    }
                )
            )

    if not rows:
        raise ServingError("No entity produced a forecast")

    base = pd.concat(rows, ignore_index=True)
    base["entity_id"] = base["entity_id"].astype(object).map(str)
    base["forecast_run_id"] = context.forecast_run_id
    base["model_version"] = context.model_version
    base["feature_version"] = context.feature_version
    base["data_snapshot_id"] = context.data_snapshot_id
    base["scenario_id"] = context.scenario_id
    base["horizon"] = pd.to_datetime(base["target_date"]).sub(
        pd.Timestamp(context.forecast_origin)
    ).dt.days

    entity_types = {e.entity_id: e.entity_type for e in entity_master.entities}

    reconciled_frames = []
    for _model_name, chunk in base.groupby("model_name"):
        # reconcile() returns every hierarchy node (leaves + TOTAL), so the
        # parent rows are born coherent rather than merged in afterwards.
        rec = reconcile(chunk, entity_master, method=reconciliation_method,
                        historical=None, value_col="yhat")
        rec["entity_type"] = rec["entity_id"].map(entity_types)
        rec["forecast_origin"] = pd.to_datetime(rec["forecast_origin"]).dt.date
        rec["target_date"] = pd.to_datetime(rec["target_date"]).dt.date
        rec["model_name"] = _model_name
        rec["yhat"] = rec["yhat"].clip(lower=0)
        keep = chunk.drop_duplicates(["forecast_origin", "target_date", "entity_id"])[
            ["forecast_origin", "target_date", "entity_id", "lower_80", "upper_80",
             "lower_90", "upper_90", "model_version", "feature_version",
             "data_snapshot_id", "forecast_run_id", "scenario_id"]
        ]
        rec = rec.merge(
            keep,
            on=["forecast_origin", "target_date", "entity_id"],
            how="left",
        )
        rounded = coherent_round(rec, entity_master, value_col="yhat")
        reconciled_frames.append(_fill_parent_intervals(rounded, entity_master, horizon))

    out = pd.concat(reconciled_frames, ignore_index=True)
    out["target_date"] = pd.to_datetime(out["target_date"]).dt.date
    out["forecast_origin"] = pd.to_datetime(out["forecast_origin"]).dt.date
    validate_forecast(out, entity_master, horizon)
    return out[OUTPUT_CONTRACT]


def _fill_parent_intervals(
    frame: pd.DataFrame, entity_master: EntityMaster, horizon: int
) -> pd.DataFrame:
    """Give aggregated rows intervals built from their leaves.

    Leaf forecasts carry model-based intervals; parent rows are sums, so their
    interval is the sum of the children's bounds. This keeps totals coherent
    and the intervals correctly nested.
    """
    interval_cols = ["lower_80", "upper_80", "lower_90", "upper_90"]
    leaf_ids = entity_master.leaf_ids(active_only=False)
    out = frame.copy()
    leaves = out[out["entity_id"].isin(leaf_ids)]
    sums = leaves.groupby("target_date")[interval_cols].sum(min_count=1)
    for col in interval_cols:
        mask = out[col].isna()
        out.loc[mask, col] = out.loc[mask, "target_date"].map(sums[col])
    # Point forecast bounds for parent rows must still bracket the rounded yhat.
    out["lower_80"] = np.minimum(out["lower_80"], out["reconciled_yhat"])
    out["upper_80"] = np.maximum(out["upper_80"], out["reconciled_yhat"])
    out["lower_90"] = np.minimum(out["lower_90"], out["lower_80"])
    out["upper_90"] = np.maximum(out["upper_90"], out["upper_80"])
    out["lower_90"] = np.minimum(out["lower_90"], out["reconciled_yhat"])
    out["upper_90"] = np.maximum(out["upper_90"], out["reconciled_yhat"])
    out["horizon"] = pd.to_datetime(out["target_date"]).sub(
        pd.to_datetime(out["forecast_origin"])
    ).dt.days
    return out


def validate_forecast(frame: pd.DataFrame, entity_master: EntityMaster, horizon: int) -> bool:
    """Model-level guardrails from doc 24 must pass before publication."""
    if (frame["yhat"] < 0).any():
        raise ServingError("Forecast contains negative values")
    if (frame["lower_80"] > frame["yhat"]).any() or (frame["yhat"] > frame["upper_80"]).any():
        raise ServingError("80% interval is not ordered around the point forecast")
    if (frame["lower_90"] > frame["lower_80"]).any() or (frame["upper_80"] > frame["upper_90"]).any():
        raise ServingError("Intervals are not nested (90% must contain 80%)")
    per_model_entity = frame.groupby(["model_name", "entity_id"])["horizon"].max()
    if (per_model_entity < horizon).any():
        raise ServingError("Forecast horizon is incomplete for some entity")
    expected_first = (pd.Timestamp(frame["forecast_origin"].min()) + pd.Timedelta(days=1)).date()
    if frame["target_date"].min() != expected_first:
        raise ServingError("Forecast does not start at D+1")

    leaf = frame[frame["entity_id"].isin(entity_master.leaf_ids(active_only=False))]
    total_id = entity_master.total_id()
    for model_name, chunk in frame.groupby("model_name"):
        chunk = chunk[chunk["entity_id"].isin(entity_master.leaf_ids(active_only=False)) | chunk["entity_id"].eq(total_id)]
        totals = chunk[chunk["entity_id"] == total_id].set_index("target_date")["reconciled_yhat"]
        leaf_sums = (
            chunk[chunk["entity_id"].isin(entity_master.leaf_ids(active_only=False))]
            .groupby("target_date")["reconciled_yhat"].sum()
        )
        joined = pd.concat({"t": totals, "l": leaf_sums}, axis=1).dropna()
        if not np.allclose(joined["t"], joined["l"]):
            raise ServingError(f"Reconciled total != leaf sum for model {model_name}")
    return True


def to_records(frame: pd.DataFrame) -> list[dict]:
    """JSON-serializable output for the serving API."""
    records = frame.to_dict("records")
    for record in records:
        for key in ("forecast_origin", "target_date"):
            if isinstance(record[key], (date, datetime)):
                record[key] = record[key].isoformat()
        for key in ("yhat", "lower_80", "upper_80", "lower_90", "upper_90", "reconciled_yhat"):
            value = record.get(key)
            record[key] = None if value is None or pd.isna(value) else float(value)
    return records
