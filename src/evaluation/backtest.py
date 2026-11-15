"""Rolling-origin / expanding-window backtesting (doc 13.1).

Random train/test splits are prohibited; this module has no such code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Sequence

import numpy as np
import pandas as pd

from src.evaluation.metrics import bias, wape
from src.models.baselines import ForecastResult

Forecaster = Callable[[pd.Series, int], ForecastResult]


@dataclass(frozen=True)
class BacktestConfig:
    horizon: int = 30
    step: int = 1
    min_train_days: int = 90
    seasonality: int = 7


def make_origins(
    dates: Sequence[date], config: BacktestConfig
) -> list[pd.Timestamp]:
    """Forecast origins where a full horizon of actuals exists."""
    ordered = sorted(pd.Timestamp(d) for d in dates)
    if not ordered:
        return []
    last = ordered[-1]
    origins: list[pd.Timestamp] = []
    origin = ordered[0] + pd.Timedelta(days=config.min_train_days)
    while origin + pd.Timedelta(days=config.horizon) <= last:
        origins.append(origin)
        origin = origin + pd.Timedelta(days=config.step)
    return origins


def _series_for_entity(frame: pd.DataFrame, entity_id: str) -> pd.Series:
    entity = frame[frame["entity_id"] == entity_id].copy()
    entity["date"] = pd.to_datetime(entity["date"])
    return entity.sort_values("date").set_index("date")["successful_orders"].astype("float64")


def backtest_entity(
    series: pd.Series,
    forecaster: Forecaster,
    config: BacktestConfig,
) -> pd.DataFrame:
    """One entity: expand the window, forecast h steps, join actuals."""
    series = series[~series.index.duplicated(keep="last")].sort_index()
    origins = make_origins(list(series.index), config)
    rows: list[dict] = []
    for origin in origins:
        train = series.loc[:origin]
        if train.notna().sum() < config.min_train_days:
            continue
        result = forecaster(train.dropna(), config.horizon)
        for step, value in enumerate(result.values, start=1):
            target = origin + pd.Timedelta(days=step)
            if target not in series.index:
                continue
            actual = series.loc[target]
            if pd.isna(actual):
                continue
            rows.append(
                {
                    "forecast_origin": origin.date(),
                    "target_date": target.date(),
                    "horizon": step,
                    "yhat": float(value),
                    "actual": float(actual),
                    "model_name": result.model_name,
                }
            )
    return pd.DataFrame(rows)


def backtest(
    frame: pd.DataFrame,
    forecasters: dict[str, Forecaster],
    config: BacktestConfig | None = None,
    *,
    entity_ids: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Run every forecaster over every entity on rolling origins."""
    cfg = config or BacktestConfig()
    ids = list(entity_ids) if entity_ids else sorted(frame["entity_id"].unique())
    outputs: list[pd.DataFrame] = []
    for entity_id in ids:
        series = _series_for_entity(frame, entity_id)
        for name, forecaster in forecasters.items():
            result = backtest_entity(series, forecaster, cfg)
            if result.empty:
                continue
            result["entity_id"] = entity_id
            result["model_name"] = name
            outputs.append(result)
    if not outputs:
        return pd.DataFrame(
            columns=[
                "forecast_origin",
                "target_date",
                "horizon",
                "yhat",
                "actual",
                "model_name",
                "entity_id",
            ]
        )
    return pd.concat(outputs, ignore_index=True)


def summarize(backtest_frame: pd.DataFrame) -> pd.DataFrame:
    """WAPE/Bias by model, entity and horizon bucket (D+1, D+7, D+30)."""
    if backtest_frame.empty:
        return pd.DataFrame(columns=["model_name", "entity_id", "horizon_bucket", "wape", "bias"])
    frame = backtest_frame.copy()
    frame["horizon_bucket"] = frame["horizon"].map(_horizon_bucket)
    rows = []
    for (model, entity, bucket), chunk in frame.groupby(
        ["model_name", "entity_id", "horizon_bucket"]
    ):
        rows.append(
            {
                "model_name": model,
                "entity_id": entity,
                "horizon_bucket": bucket,
                "wape": wape(chunk["actual"], chunk["yhat"]),
                "bias": bias(chunk["actual"], chunk["yhat"]),
                "n_points": len(chunk),
            }
        )
    return pd.DataFrame(rows)


def backtest_global(
    frame: pd.DataFrame,
    config: BacktestConfig | None = None,
    *,
    global_config=None,
) -> pd.DataFrame:
    """Rolling-origin backtest of the global model.

    The global model trains *across* entities, so it cannot use
    ``backtest_entity``: one fit per origin produces the path for every entity
    at once. The model is refitted from scratch on data up to the origin, so
    the same no-leakage guarantee holds as in the per-entity backtest.
    """
    from src.models.global_model import GlobalConfig, GlobalModel

    cfg = config or BacktestConfig()
    nested = frame[frame["successful_orders"].notna()].copy()
    nested["date"] = pd.to_datetime(nested["date"])
    if nested.empty:
        return pd.DataFrame(
            columns=[
                "forecast_origin",
                "target_date",
                "horizon",
                "yhat",
                "actual",
                "model_name",
                "entity_id",
            ]
        )

    gcfg = global_config or GlobalConfig(
        horizon=cfg.horizon,
        min_train_days=min(cfg.min_train_days, 42),
    )
    actuals = nested.set_index(["date", "entity_id"])["successful_orders"]
    origins = make_origins(list(nested["date"].unique()), cfg)

    rows: list[dict] = []
    for origin in origins:
        train = nested[nested["date"] <= origin]
        if train["date"].nunique() < cfg.min_train_days:
            continue
        try:
            model = GlobalModel(gcfg).fit(train, horizon=cfg.horizon)
            paths = model.predict(train, horizon=cfg.horizon)
        except ValueError:
            continue
        origin_date = origin.date()
        for entity_id, path in paths.items():
            for step, value in enumerate(path, start=1):
                target = origin + pd.Timedelta(days=step)
                key = (target, entity_id)
                if key not in actuals.index:
                    continue
                rows.append(
                    {
                        "forecast_origin": origin_date,
                        "target_date": target.date(),
                        "horizon": step,
                        "yhat": float(value),
                        "actual": float(actuals.loc[key]),
                        "model_name": "global_ml",
                        "entity_id": entity_id,
                    }
                )
    if not rows:
        return pd.DataFrame(
            columns=[
                "forecast_origin",
                "target_date",
                "horizon",
                "yhat",
                "actual",
                "model_name",
                "entity_id",
            ]
        )
    return pd.DataFrame(rows)


def _horizon_bucket(horizon: int) -> str:
    if horizon <= 1:
        return "D+1"
    if horizon <= 7:
        return "D+1..D+7"
    return "D+8..D+30"


def horizon_accuracy(backtest_frame: pd.DataFrame, origin=None) -> pd.DataFrame:
    """Single point accuracy at exactly D+1, D+7 and D+30 (doc 10.4)."""
    if backtest_frame.empty:
        return pd.DataFrame()
    frame = backtest_frame
    if origin is not None:
        frame = frame[frame["forecast_origin"] == origin]
    rows = []
    for model, chunk in frame.groupby("model_name"):
        for entity, entity_chunk in chunk.groupby("entity_id"):
            record = {"model_name": model, "entity_id": entity}
            for h in (1, 7, 30):
                point = entity_chunk[entity_chunk["horizon"] == h]
                record[f"wape_d{h}"] = (
                    wape(point["actual"], point["yhat"]) if not point.empty else float("nan")
                )
                record[f"bias_d{h}"] = (
                    bias(point["actual"], point["yhat"]) if not point.empty else float("nan")
                )
            rows.append(record)
    return pd.DataFrame(rows)
