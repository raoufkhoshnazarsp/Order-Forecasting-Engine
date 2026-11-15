"""Hierarchical reconciliation: bottom-up, MinTrace and top-down (doc 11).

All methods return coherent forecasts where
    total == sum(verticals) + sum(ventures)
exactly, before and after count rounding.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.reconciliation.summing_matrix import build_summing_matrix
from src.data_mart.entity_master import EntityMaster


class ReconciliationError(ValueError):
    """Raised when forecasts cannot be made coherent."""


def _wide(base: pd.DataFrame, value_col: str) -> pd.DataFrame:
    wide = base.pivot_table(
        index=["forecast_origin", "target_date"],
        columns="entity_id",
        values=value_col,
        aggfunc="sum",
    )
    return wide.sort_index(axis=1)


def bottom_up(base: pd.DataFrame, matrix: pd.DataFrame, value_col: str = "yhat") -> pd.DataFrame:
    """Coherent by construction: total and parents are sums of leaves."""
    wide = _wide(base, value_col).reindex(columns=matrix.columns)
    coherent = wide @ matrix.T
    return _to_long(coherent, value_col)


def mintrace(
    base: pd.DataFrame,
    matrix: pd.DataFrame,
    residuals: pd.DataFrame | None = None,
    value_col: str = "yhat",
    method: str = "ols",
) -> pd.DataFrame:
    """MinT projection of base forecasts onto the coherent subspace.

    ``residuals`` holds in-sample residuals with the same columns as the
    matrix rows (one row per observation). Without residuals, OLS MinT is
    used (reasonably robust for small hierarchies).
    """
    wide = _wide(base, value_col).reindex(columns=matrix.index)
    if wide.isna().all(axis=1).any():
        # Missing base forecasts: fall back to bottom-up rather than guessing.
        return bottom_up(base, matrix, value_col)
    S = matrix.to_numpy(dtype="float64")
    y = wide.to_numpy(dtype="float64")
    n_leaf = S.shape[1]

    if residuals is None or residuals.empty:
        W = np.eye(S.shape[0])
    else:
        R = residuals.reindex(columns=matrix.index).dropna()
        if R.empty:
            W = np.eye(S.shape[0])
        else:
            cov = np.cov(R.to_numpy(dtype="float64").T)
            W = np.atleast_2d(cov)
            if method == "wls":
                W = np.diag(np.diag(W))
    # P = S (S' W^-1 S)^-1 S' W^-1
    try:
        winv = np.linalg.pinv(W)
        middle = np.linalg.pinv(S.T @ winv @ S)
        P = S @ middle @ S.T @ winv
    except np.linalg.LinAlgError as exc:  # pragma: no cover
        raise ReconciliationError("MinTrace projection failed") from exc

    reconciled = y @ P.T
    return _to_long(pd.DataFrame(reconciled, index=wide.index, columns=matrix.index), value_col)


def top_down(
    base: pd.DataFrame,
    matrix: pd.DataFrame,
    historical: pd.DataFrame,
    value_col: str = "yhat",
    actual_col: str = "successful_orders",
) -> pd.DataFrame:
    """Allocate the total forecast by each leaf's historical share."""
    wide = _wide(base, value_col)
    total_id = matrix.index[0]
    if total_id not in wide.columns:
        raise ReconciliationError(f"Base forecasts missing the TOTAL row {total_id!r}")

    hist = historical[historical["entity_id"].isin(matrix.columns)]
    shares = (
        hist.groupby("entity_id")[actual_col].sum().reindex(matrix.columns).fillna(0.0)
    )
    if shares.sum() <= 0:
        raise ReconciliationError("Historical shares are all zero; cannot top-down")
    shares = shares / shares.sum()

    totals = wide[total_id]
    leaf = pd.DataFrame(
        np.outer(totals.to_numpy(), shares.to_numpy()),
        index=wide.index,
        columns=matrix.columns,
    )
    coherent = leaf @ matrix.T
    return _to_long(coherent, value_col)


def _to_long(wide: pd.DataFrame, value_col: str) -> pd.DataFrame:
    long = wide.stack().reset_index()
    long.columns = ["forecast_origin", "target_date", "entity_id", value_col]
    # Normalize the key dtype: pandas 3 may yield ``str`` vs ``string`` and
    # silently fail to match on merge.
    long["entity_id"] = long["entity_id"].astype(object).map(str)
    return long


def reconcile(
    base: pd.DataFrame,
    entity_master: EntityMaster,
    *,
    method: str = "bottom_up",
    historical: pd.DataFrame | None = None,
    value_col: str = "yhat",
) -> pd.DataFrame:
    matrix = build_summing_matrix(entity_master)
    method = method.lower()
    if method == "bottom_up":
        return bottom_up(base, matrix, value_col)
    if method == "mintrace":
        return mintrace(base, matrix, value_col=value_col)
    if method == "top_down":
        if historical is None:
            raise ReconciliationError("top_down requires historical actuals")
        return top_down(base, matrix, historical, value_col=value_col)
    raise ReconciliationError(f"Unknown reconciliation method: {method}")


def coherent_round(
    frame: pd.DataFrame,
    entity_master: EntityMaster,
    value_col: str = "yhat",
    out_col: str = "reconciled_yhat",
) -> pd.DataFrame:
    """Round leaves to integers, then rebuild parents as exact leaf sums.

    Children always sum exactly to the parent (doc 11.4); no independent
    rounding of parent and child.
    """
    matrix = build_summing_matrix(entity_master)
    out = frame.copy()
    out["entity_id"] = out["entity_id"].astype(object).map(str)
    if value_col not in out.columns:
        raise ReconciliationError(f"Column {value_col!r} not present")

    out["_rounded_leaf"] = np.nan
    leaf_mask = out["entity_id"].isin(matrix.columns)
    out.loc[leaf_mask, "_rounded_leaf"] = (
        out.loc[leaf_mask, value_col].clip(lower=0).round().astype("float64")
    )
    leaf_values = out.loc[leaf_mask, ["forecast_origin", "target_date", "entity_id", "_rounded_leaf"]]
    wide = leaf_values.pivot_table(
        index=["forecast_origin", "target_date"],
        columns="entity_id",
        values="_rounded_leaf",
        aggfunc="sum",
    ).reindex(columns=matrix.columns)

    parents = (wide @ matrix.T).stack().rename("_parent_value").reset_index()
    parents.columns = ["forecast_origin", "target_date", "entity_id", "_parent_value"]
    out = out.merge(parents, on=["forecast_origin", "target_date", "entity_id"], how="left")
    out[out_col] = out["_parent_value"].fillna(out["_rounded_leaf"]).astype("float64")
    return out.drop(columns=["_rounded_leaf", "_parent_value"])


def assert_coherent(frame: pd.DataFrame, entity_master: EntityMaster, value_col: str) -> bool:
    """Verify total == sum(leaves) for every (origin, target_date)."""
    if frame.empty:
        return True
    leaf = frame[frame["entity_id"].isin(entity_master.leaf_ids(active_only=False))]
    total_id = entity_master.total_id()
    total = frame[frame["entity_id"] == total_id].set_index(["forecast_origin", "target_date"])[value_col]
    leaf_sum = leaf.groupby(["forecast_origin", "target_date"])[value_col].sum()
    joined = pd.concat({"total": total, "leaf": leaf_sum}, axis=1).dropna()
    bad = joined[~np.isclose(joined["total"], joined["leaf"])]
    if not bad.empty:
        raise ReconciliationError(
            f"Reconciliation violated on {len(bad)} (origin, date) pairs; "
            f"first: {bad.head(3).to_dict('index')}"
        )
    return True
