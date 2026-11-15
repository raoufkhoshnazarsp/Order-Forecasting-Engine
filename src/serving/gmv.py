"""AOV and GMV forecasting.

GMV is never modelled as an independent series. It is the product of two
forecasts, each modelled on its own scale:

    GMV(day) = orders(day) x AOV(day)

Orders are additive across entities, so they are reconciled bottom-up exactly
like the order forecast. AOV is a ratio and is **not** additive: a total AOV is
the weighted average ``sum(GMV) / sum(orders)``, never the sum of leaf AOVs.
Leaf AOV is therefore forecast directly, and the total AOV is derived from the
coherent GMV and order totals.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data_mart.entity_master import EntityMaster
from src.serving.service import RunContext, ServingError, residual_sigma

AOV_COLUMNS = [
    "target_date",
    "entity_id",
    "horizon",
    "aov_yhat",
    "aov_lower_80",
    "aov_upper_80",
    "aov_lower_90",
    "aov_upper_90",
]

GMV_COLUMNS = [
    "target_date",
    "entity_id",
    "entity_type",
    "horizon",
    "orders_yhat",
    "aov_yhat",
    "gmv_yhat",
    "gmv_lower_80",
    "gmv_upper_80",
    "gmv_lower_90",
    "gmv_upper_90",
]


def has_gmv(mart: pd.DataFrame) -> bool:
    """True when the workbook carried a GMV column with usable values."""
    return "gmv" in mart.columns and mart["gmv"].notna().any()


def aov_series(mart: pd.DataFrame, entity_id: str) -> pd.Series:
    """Daily AOV for one entity: GMV / orders, only where orders > 0."""
    rows = mart[
        mart["entity_id"].eq(entity_id)
        & mart["quality_flag"].eq("OBSERVED")
        & mart["successful_orders"].notna()
        & mart["gmv"].notna()
    ]
    rows = rows[rows["successful_orders"] > 0]
    if rows.empty:
        return pd.Series(dtype="float64")
    frame = rows.assign(date=pd.to_datetime(rows["date"])).set_index("date")
    series = (frame["gmv"].astype("float64") / frame["successful_orders"].astype("float64"))
    return series.sort_index()


def forecast_aov(
    mart: pd.DataFrame,
    entity_master: EntityMaster,
    forecaster,
    *,
    context: RunContext,
    horizon: int,
    z_80: float = 1.2816,
    z_90: float = 1.6449,
) -> pd.DataFrame:
    """Forecast leaf AOV directly. No additive reconciliation is applied."""
    rows: list[pd.DataFrame] = []
    origin_ts = pd.Timestamp(context.forecast_origin)
    steps = np.arange(1, horizon + 1)
    target_dates = [(origin_ts + pd.Timedelta(days=int(s))).date() for s in steps]

    for entity in entity_master.leaves(active_only=False):
        series = aov_series(mart, entity.entity_id)
        if series.empty:
            continue
        sigma = residual_sigma(series, horizon)
        result = forecaster(series.dropna(), horizon)
        values = np.clip(np.asarray(result.values, dtype="float64"), 0, None)
        rows.append(
            pd.DataFrame(
                {
                    "target_date": target_dates,
                    "entity_id": entity.entity_id,
                    "horizon": steps,
                    "aov_yhat": values,
                    "aov_lower_80": np.clip(values - z_80 * sigma, 0, None),
                    "aov_upper_80": values + z_80 * sigma,
                    "aov_lower_90": np.clip(values - z_90 * sigma, 0, None),
                    "aov_upper_90": values + z_90 * sigma,
                }
            )
        )

    if not rows:
        raise ServingError("No entity has a usable AOV history (GMV or orders missing)")
    out = pd.concat(rows, ignore_index=True)
    out["entity_id"] = out["entity_id"].astype(object).map(str)
    return out[AOV_COLUMNS]


def combine_gmv(
    orders_forecast: pd.DataFrame,
    aov_forecast: pd.DataFrame,
    entity_master: EntityMaster,
) -> pd.DataFrame:
    """GMV = orders x AOV per leaf, then summed to TOTAL.

    Leaf GMV is the product of the two point forecasts; its interval is the
    product of the matching bounds (both factors are non-negative, so the
    product is monotone and the bounds stay ordered). TOTAL GMV is the exact
    sum of leaf GMV, and TOTAL AOV is the weighted average GMV / orders.
    """
    leaf_ids = set(entity_master.leaf_ids(active_only=False))
    total_id = entity_master.total_id()

    orders = orders_forecast[orders_forecast["entity_id"].isin(leaf_ids)][
        ["target_date", "entity_id", "reconciled_yhat", "lower_80", "upper_80",
         "lower_90", "upper_90"]
    ].rename(
        columns={
            "reconciled_yhat": "orders_yhat",
            "lower_80": "orders_lower_80",
            "upper_80": "orders_upper_80",
            "lower_90": "orders_lower_90",
            "upper_90": "orders_upper_90",
        }
    )
    merged = orders.merge(aov_forecast, on=["target_date", "entity_id"], how="inner")
    if merged.empty:
        raise ServingError("Orders and AOV forecasts share no entity/date to combine")

    merged["gmv_yhat"] = merged["orders_yhat"] * merged["aov_yhat"]
    for level in ("80", "90"):
        merged[f"gmv_lower_{level}"] = (
            merged[f"orders_lower_{level}"] * merged[f"aov_lower_{level}"]
        )
        merged[f"gmv_upper_{level}"] = (
            merged[f"orders_upper_{level}"] * merged[f"aov_upper_{level}"]
        )

    merged["entity_type"] = merged["entity_id"].map(
        {e.entity_id: e.entity_type for e in entity_master.entities}
    )
    merged["horizon"] = pd.to_datetime(merged["target_date"]).sub(
        pd.Timestamp(merged["target_date"].min())
    ).dt.days + 1

    leaf = merged[GMV_COLUMNS].copy()

    sums = leaf.groupby("target_date", as_index=False).agg(
        orders_yhat=("orders_yhat", "sum"),
        gmv_yhat=("gmv_yhat", "sum"),
        gmv_lower_80=("gmv_lower_80", "sum"),
        gmv_upper_80=("gmv_upper_80", "sum"),
        gmv_lower_90=("gmv_lower_90", "sum"),
        gmv_upper_90=("gmv_upper_90", "sum"),
    )
    sums["entity_id"] = total_id
    sums["entity_type"] = "TOTAL"
    sums["aov_yhat"] = np.where(
        sums["orders_yhat"] > 0, sums["gmv_yhat"] / sums["orders_yhat"], np.nan
    )
    sums["horizon"] = pd.to_datetime(sums["target_date"]).sub(
        pd.Timestamp(sums["target_date"].min())
    ).dt.days + 1

    out = pd.concat([leaf, sums[GMV_COLUMNS]], ignore_index=True)
    out["target_date"] = pd.to_datetime(out["target_date"]).dt.date
    return out.sort_values(["target_date", "entity_type", "entity_id"]).reset_index(drop=True)


def validate_gmv(frame: pd.DataFrame, entity_master: EntityMaster) -> bool:
    """GMV must be non-negative, coherent, and equal orders x AOV on leaves."""
    if (frame["gmv_yhat"] < 0).any():
        raise ServingError("GMV forecast contains negative values")
    leaf = frame[frame["entity_id"].isin(entity_master.leaf_ids(active_only=False))]
    product = leaf["orders_yhat"] * leaf["aov_yhat"]
    if not np.allclose(product, leaf["gmv_yhat"], rtol=1e-9, atol=1e-6):
        raise ServingError("Leaf GMV is not equal to orders x AOV")
    total = frame[frame["entity_id"] == entity_master.total_id()].set_index("target_date")
    leaf_sum = leaf.groupby("target_date")["gmv_yhat"].sum()
    joined = pd.concat({"t": total["gmv_yhat"], "l": leaf_sum}, axis=1).dropna()
    if not np.allclose(joined["t"], joined["l"], rtol=1e-9, atol=1e-6):
        raise ServingError("Total GMV is not the sum of leaf GMV")
    return True
