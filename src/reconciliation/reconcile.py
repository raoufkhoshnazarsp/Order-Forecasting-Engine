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


def _residual_covariance(
    residuals: pd.DataFrame, columns: list[str], method: str
) -> np.ndarray:
    """Covariance of base-forecast residuals, optionally shrunk.

    ``ols`` uses the identity (no residual information), ``wls`` keeps only the
    diagonal, and ``shrunk`` applies the Schäfer–Strimmer shrinkage towards a
    diagonal target. Shrinkage matters here because the hierarchy is small and
    the residual covariance is estimated from few origins: an unshrunk sample
    covariance is often singular or wildly over-fitted, which makes the MinT
    projection worse than bottom-up.
    """
    n = len(columns)
    if method not in {"ols", "wls", "shrunk"}:
        raise ReconciliationError(f"Unknown MinTrace covariance method: {method}")
    if method == "ols" or residuals is None or residuals.empty:
        return np.eye(n)
    R = residuals.reindex(columns=columns).dropna()
    if R.empty or len(R) < 2:
        return np.eye(n)
    values = R.to_numpy(dtype="float64")
    cov = np.atleast_2d(np.cov(values.T))
    if method == "wls":
        return np.diag(np.diag(cov))

    # Schäfer–Strimmer: shrink the sample covariance towards its diagonal.
    # The intensity is estimated from the variance of the off-diagonal
    # correlations, clipped to [0, 1] so it can never invert the estimate.
    sample = np.atleast_2d(np.cov(values.T, ddof=1))
    var = np.diag(sample)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = sample / np.sqrt(np.outer(var, var))
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    off = corr[~np.eye(n, dtype=bool)]
    if off.size == 0:
        return np.diag(var)
    intensity = float(np.clip(np.var(off) / max(np.mean(off**2), 1e-12), 0.0, 1.0))
    shrunk = (1.0 - intensity) * sample
    shrunk[np.diag_indices(n)] = var
    return shrunk


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
    used (reasonably robust for small hierarchies). ``method`` selects the
    covariance estimate: ``ols`` (identity), ``wls`` (diagonal) or ``shrunk``
    (Schäfer–Strimmer shrinkage, the safest choice on short histories).
    """
    wide = _wide(base, value_col).reindex(columns=matrix.index)
    if wide.isna().all(axis=1).any():
        # Missing base forecasts: fall back to bottom-up rather than guessing.
        return bottom_up(base, matrix, value_col)
    S = matrix.to_numpy(dtype="float64")
    y = wide.to_numpy(dtype="float64")

    W = _residual_covariance(residuals, list(matrix.index), method)
    # P = S (S' W^-1 S)^-1 S' W^-1
    try:
        winv = np.linalg.pinv(W)
        middle = np.linalg.pinv(S.T @ winv @ S)
        P = S @ middle @ S.T @ winv
    except np.linalg.LinAlgError as exc:  # pragma: no cover
        raise ReconciliationError("MinTrace projection failed") from exc

    reconciled = y @ P.T
    return _to_long(pd.DataFrame(reconciled, index=wide.index, columns=matrix.index), value_col)


def residuals_from_backtest(
    backtest_frame: pd.DataFrame, model_name: str | None = None
) -> pd.DataFrame:
    """Per-entity residuals (actual − yhat) shaped for MinTrace.

    One row per (origin, target_date); columns are entity ids. Rows with a
    missing actual are dropped rather than filled, so a gap never becomes a
    zero residual that biases the covariance.
    """
    if backtest_frame is None or backtest_frame.empty:
        return pd.DataFrame()
    frame = backtest_frame
    if model_name is not None:
        frame = frame[frame["model_name"] == model_name]
    if frame.empty:
        return pd.DataFrame()
    frame = frame.dropna(subset=["actual", "yhat"])
    frame = frame.assign(residual=frame["actual"] - frame["yhat"])
    wide = frame.pivot_table(
        index=["forecast_origin", "target_date"],
        columns="entity_id",
        values="residual",
        aggfunc="mean",
    )
    return wide.sort_index(axis=1)


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
    residuals: pd.DataFrame | None = None,
    as_of: object | None = None,
) -> pd.DataFrame:
    matrix = build_summing_matrix(entity_master, as_of=as_of)
    method = method.lower()
    if method == "bottom_up":
        return bottom_up(base, matrix, value_col)
    if method in {"mintrace", "mint_ols"}:
        return mintrace(base, matrix, residuals, value_col=value_col, method="ols")
    if method in {"mint_wls", "wls"}:
        return mintrace(base, matrix, residuals, value_col=value_col, method="wls")
    if method in {"mint_shrunk", "shrunk"}:
        return mintrace(base, matrix, residuals, value_col=value_col, method="shrunk")
    if method == "top_down":
        if historical is None:
            raise ReconciliationError("top_down requires historical actuals")
        return top_down(base, matrix, historical, value_col=value_col)
    raise ReconciliationError(f"Unknown reconciliation method: {method}")


RECONCILIATION_METHODS = ("bottom_up", "mint_ols", "mint_wls", "mint_shrunk", "top_down")


def compare_methods(
    base: pd.DataFrame,
    entity_master: EntityMaster,
    actuals: pd.DataFrame,
    *,
    historical: pd.DataFrame | None = None,
    residuals: pd.DataFrame | None = None,
    value_col: str = "yhat",
    actual_col: str = "actual",
    methods: tuple[str, ...] = RECONCILIATION_METHODS,
) -> pd.DataFrame:
    """WAPE of each reconciliation method, split by hierarchy level.

    ``base`` holds base forecasts (one row per origin, target_date, entity),
    ``actuals`` the matching observed values with the same keys. The point of
    the comparison is that a method which looks good on the total can be worse
    on the leaves, so the error is reported per level rather than pooled.
    """
    from src.evaluation.metrics import wape

    if base is None or base.empty or actuals is None or actuals.empty:
        return pd.DataFrame(columns=["method", "level", "wape", "points"])

    keys = ["forecast_origin", "target_date", "entity_id"]
    truth = actuals[keys + [actual_col]].copy()
    truth["entity_id"] = truth["entity_id"].astype(object).map(str)
    leaf_ids = set(entity_master.leaf_ids(active_only=False))
    total_id = entity_master.total_id()

    # Top-down allocates by historical share. If the history includes the test
    # window, the shares are computed with data the forecast could not have
    # seen, which flatters the method. Cut the history at the first origin.
    if historical is not None and not historical.empty and "date" in historical.columns:
        first_origin = pd.Timestamp(base["forecast_origin"].min())
        historical = historical[pd.to_datetime(historical["date"]) < first_origin]

    rows: list[dict] = []
    for method in methods:
        try:
            reconciled = reconcile(
                base,
                entity_master,
                method=method,
                historical=historical,
                value_col=value_col,
                residuals=residuals,
            )
        except ReconciliationError:
            continue
        merged = reconciled.merge(truth, on=keys, how="inner")
        if merged.empty:
            continue
        merged["entity_id"] = merged["entity_id"].astype(object).map(str)
        levels = {
            "TOTAL": merged[merged["entity_id"] == total_id],
            "LEAF": merged[merged["entity_id"].isin(leaf_ids)],
        }
        for level, chunk in levels.items():
            if chunk.empty:
                continue
            rows.append(
                {
                    "method": method,
                    "level": level,
                    "wape": float(wape(chunk[actual_col], chunk[value_col])),
                    "points": int(len(chunk)),
                }
            )
    return pd.DataFrame(rows)


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
