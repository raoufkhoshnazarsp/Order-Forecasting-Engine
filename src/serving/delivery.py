"""The delivery workbook: one row per date x vertical x category.

This is the format the business consumes, and it is deliberately different
from the engine's internal contract:

    date | level | vertical | category | payment_method | order | GMV

Rules taken from the reference file:

* ``level`` is always ``vertical``.
* Category rows exist for every vertical that has categories in the source.
* ``VERTICALS`` is the sum of those category rows (a total, not a leaf).
* ``SNAPP-VENTURE`` is a sibling of ``VERTICALS``, not a child of it, and
  carries the single category ``ventures``.
* ``SNAPP-PAY`` has no category and is a sibling too.
* Rows are ordered by vertical, then category, then the two totals last.

Forecasts are produced per category, because that is the grain the file
needs. A vertical's total is the exact sum of its categories, so the file is
internally coherent by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.models.baselines import BASELINES
from src.serving.service import RunContext, residual_sigma

OUTPUT_COLUMNS = ["date", "level", "vertical", "category", "payment_method", "order", "GMV"]

# The input layout this engine reads back. An export in this format can be
# uploaded again unchanged, which is what "download in the same format as my
# data" means to an analyst.
INPUT_COLUMNS = ["date", "vertical", "category", "payment_method_type", "orders", "gmv"]

# Display names used by the delivery file, keyed by the source spelling.
VERTICAL_DISPLAY = {
    "DIGITAL": "DIGITAL",
    "FASHION": "FASHION",
    "FMCG": "FMCG",
    "GOLD": "GOLD",
    "H&B": "H&B",
    "HOME": "HOME",
    "SERVICES": "SERVICES",
    "SNAPP-VENTURE": "SNAPP-VENTURE",
    "SNAPP-PAY": "SNAPP-PAY",
}

TOTAL_LABEL = "VERTICALS"
VENTURE_LABEL = "SNAPP-VENTURE"
PAY_LABEL = "SNAPP-PAY"
VENTURE_CATEGORY = "ventures"

# Order of the vertical blocks in the file.
VERTICAL_ORDER = [
    "DIGITAL",
    "FASHION",
    "FMCG",
    "GOLD",
    "H&B",
    "HOME",
    "SERVICES",
]


class DeliveryError(ValueError):
    """Raised when the delivery workbook cannot be produced safely."""


@dataclass(frozen=True)
class DeliveryResult:
    frame: pd.DataFrame
    origin: date
    horizon: int
    categories: int
    verticals: int
    model: str = ""
    ranking: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()

    def summary(self) -> dict:
        return {
            "origin": self.origin.isoformat(),
            "horizon": self.horizon,
            "rows": len(self.frame),
            "categories": self.categories,
            "verticals": self.verticals,
            "model": self.model,
            "ranking": list(self.ranking),
            "warnings": list(self.warnings),
        }


def _display_vertical(name: str) -> str:
    key = str(name).strip().upper().replace("_", "-")
    return VERTICAL_DISPLAY.get(key, key)


NO_METHOD = "UNKNOWN"


def _method_split(
    shares: pd.DataFrame | None, vertical: str, category: str
) -> list[tuple[str, float]]:
    """The payment methods of one category and their historical share."""
    if shares is None or shares.empty:
        return [(NO_METHOD, 1.0)]
    rows = shares[
        shares["vertical"].astype(str).eq(str(vertical))
        & shares["category"].astype(str).eq(str(category))
    ].sort_values("payment_method")
    pairs = [
        (str(row.payment_method), float(row.share))
        for row in rows.itertuples(index=False)
        if float(row.share) > 0
    ]
    total = sum(share for _name, share in pairs)
    if not pairs or total <= 0:
        return [(NO_METHOD, 1.0)]
    return [(name, share / total) for name, share in pairs]


def _split_across_methods(
    orders: float, gmv: float, methods: list[tuple[str, float]]
) -> list[tuple[str, float, float]]:
    """Split one category-day total, with the last method absorbing rounding.

    The split happens before rounding, and the remainder is assigned to the
    last method after rounding, so the methods always add back up to the
    category total exactly — the same guarantee VERTICALS has over verticals.
    """
    total_orders = round(orders, 6)
    total_gmv = round(gmv, 0)
    if len(methods) == 1:
        return [(methods[0][0], total_orders, total_gmv)]

    out: list[tuple[str, float, float]] = []
    running_orders = 0.0
    running_gmv = 0.0
    for name, share in methods[:-1]:
        part_orders = round(orders * share, 6)
        part_gmv = round(gmv * share, 0)
        out.append((name, part_orders, part_gmv))
        running_orders += part_orders
        running_gmv += part_gmv
    last_name = methods[-1][0]
    out.append(
        (last_name, round(total_orders - running_orders, 6), total_gmv - running_gmv)
    )
    return out


def _category_series(
    facts: pd.DataFrame, vertical: str, category: str
) -> tuple[pd.Series, pd.Series]:
    """Daily orders and GMV for one vertical x category, on a full date index."""
    rows = facts[
        facts["vertical"].eq(vertical) & facts["category"].eq(category)
    ]
    if rows.empty:
        return pd.Series(dtype="float64"), pd.Series(dtype="float64")
    frame = rows.assign(date=pd.to_datetime(rows["date"])).groupby("date").agg(
        orders=("orders", "sum"), gmv=("gmv", "sum")
    )
    full = pd.date_range(frame.index.min(), frame.index.max(), freq="D")
    frame = frame.reindex(full)
    return frame["orders"].astype("float64"), frame["gmv"].astype("float64")


def _forecast_orders(series: pd.Series, forecaster, horizon: int) -> np.ndarray:
    clean = series.dropna()
    if clean.empty:
        return np.zeros(horizon)
    values = np.asarray(forecaster(clean, horizon).values, dtype="float64")
    if len(values) < horizon:
        # Never let a short forecast desync the arrays that are compared with it.
        values = np.concatenate([values, np.full(horizon - len(values), np.nan)])
    return np.clip(values[:horizon], 0, None)


def _forecast_aov(orders: pd.Series, gmv: pd.Series, horizon: int) -> np.ndarray:
    """AOV from a 14-day moving average of GMV/orders, then held flat."""
    mask = orders.notna() & gmv.notna() & (orders > 0)
    if not mask.any():
        return np.zeros(horizon)
    aov = (gmv[mask] / orders[mask]).astype("float64")
    window = aov.iloc[-14:]
    return np.full(horizon, float(window.mean()))


def select_model(
    facts: pd.DataFrame, *, horizon: int = 30, step: int = 30, min_train_days: int = 45
) -> tuple[str, list[dict]]:
    """Pick the baseline with the lowest backtest WAPE across all categories.

    Only rolling-origin backtesting is used; there is no random split.
    """
    rows: list[dict] = []
    for (vertical, category), chunk in facts.groupby(["vertical", "category"]):
        series = _category_series(facts, vertical, category)[0].dropna()
        if len(series) < min_train_days + 7:
            continue
        origin = series.index[0] + pd.Timedelta(days=min_train_days)
        last = series.index[-1]
        while origin + pd.Timedelta(days=horizon) <= last:
            train = series.loc[:origin]
            # Reindex onto the exact horizon instead of slicing. A day with no
            # sales leaves a NaN hole in the series, and slicing a non-contiguous
            # index silently returns fewer than ``horizon`` rows; the boolean
            # mask then no longer matches the forecast array.
            window = pd.date_range(
                origin + pd.Timedelta(days=1), periods=horizon, freq="D"
            )
            actual = series.reindex(window)
            if actual.notna().sum() >= horizon // 2:
                for name, forecaster in BASELINES.items():
                    pred = _forecast_orders(train, forecaster, horizon)
                    aligned = actual.to_numpy(dtype="float64")
                    mask = ~np.isnan(aligned)
                    if mask.sum() == 0:
                        continue
                    rows.append(
                        {
                            "model": name,
                            "vertical": vertical,
                            "category": category,
                            "abs_err": float(np.abs(aligned[mask] - pred[mask]).sum()),
                            "abs_actual": float(np.abs(aligned[mask]).sum()),
                        }
                    )
            origin += pd.Timedelta(days=step)

    if not rows:
        return "seasonal_naive", []

    scored = pd.DataFrame(rows)
    summary = scored.groupby("model").agg(
        err=("abs_err", "sum"), actual=("abs_actual", "sum")
    )
    summary["wape"] = summary["err"] / summary["actual"].replace(0, np.nan)
    summary = summary.dropna(subset=["wape"]).sort_values("wape")
    if summary.empty:
        return "seasonal_naive", []

    ranking = [
        {"model": name, "wape": float(row["wape"])}
        for name, row in summary.iterrows()
    ]
    return ranking[0]["model"], ranking


def build_delivery_frame(
    facts: pd.DataFrame,
    *,
    origin: date,
    horizon: int = 30,
    model: str = "auto",
    backtest: pd.DataFrame | None = None,
    include_empty_siblings: bool = True,
    method_shares: pd.DataFrame | None = None,
) -> DeliveryResult:
    """Forecast every category and lay the result out in the delivery format.

    ``facts`` must carry ``date, vertical, category, orders, gmv`` at the
    source grain (one row per date x vertical x category).

    ``method_shares`` (``vertical, category, payment_method, share``) splits
    each category row across payment methods. The split is applied *after*
    the category total is fixed and the last method of each category absorbs
    the rounding remainder, so the methods still add up to the category row
    exactly. Without it every row carries ``UNKNOWN``.
    """
    if facts.empty:
        raise DeliveryError("No source rows to forecast from")

    warnings: list[str] = []
    ranking: list[dict] = []
    if model == "auto":
        model, ranking = select_model(facts, horizon=min(horizon, 30))
        if not ranking:
            warnings.append(
                "Not enough history for a model comparison; seasonal naive was used."
            )
    if model not in BASELINES:
        raise DeliveryError(f"Unknown model: {model}")

    forecaster = BASELINES[model]

    pairs = (
        facts[["vertical", "category"]]
        .drop_duplicates()
        .sort_values(["vertical", "category"])
    )

    target_dates = pd.date_range(
        start=pd.Timestamp(origin) + pd.Timedelta(days=1), periods=horizon, freq="D"
    ).date

    records: list[dict] = []
    for vertical, category in pairs.itertuples(index=False):
        orders, gmv = _category_series(facts, vertical, category)
        if orders.empty:
            continue
        order_path = _forecast_orders(orders, forecaster, horizon)
        aov_path = _forecast_aov(orders, gmv, horizon)
        gmv_path = order_path * aov_path

        display = _display_vertical(vertical)
        methods = _method_split(method_shares, vertical, category)
        for day, o, g in zip(target_dates, order_path, gmv_path):
            for method, order_part, gmv_part in _split_across_methods(
                float(o), float(g), methods
            ):
                records.append(
                    {
                        "date": day,
                        "level": "vertical",
                        "vertical": display,
                        "category": category,
                        "payment_method": method,
                        "order": order_part,
                        "GMV": gmv_part,
                    }
                )

    if not records:
        raise DeliveryError("No category produced a forecast")

    frame = pd.DataFrame.from_records(records)

    # VERTICALS = exact sum of the seven vertical blocks' category rows.
    core = frame[frame["vertical"].isin(VERTICAL_ORDER)]
    totals = (
        core.groupby("date", as_index=False)
        .agg(order=("order", "sum"), GMV=("GMV", "sum"))
        .assign(level="vertical", vertical=TOTAL_LABEL, category=None, payment_method=None)
    )

    extras = []
    venture = frame[frame["vertical"] == VENTURE_LABEL]
    if not venture.empty:
        extras.append(
            venture.groupby("date", as_index=False)
            .agg(order=("order", "sum"), GMV=("GMV", "sum"))
            .assign(
                level="vertical",
                vertical=VENTURE_LABEL,
                category=VENTURE_CATEGORY,
                payment_method=None,
            )
        )
    else:
        warnings.append(
            "No Snapp-Venture rows in the source, so SNAPP-VENTURE is absent from the file."
        )

    # Venture and Pay are reported once, as their own block, never as leaves.
    frame = frame[frame["vertical"].isin(VERTICAL_ORDER)]

    pay = frame[frame["vertical"] == PAY_LABEL]
    if not pay.empty:
        extras.append(
            pay.groupby("date", as_index=False)
            .agg(order=("order", "sum"), GMV=("GMV", "sum"))
            .assign(level="vertical", vertical=PAY_LABEL, category=None, payment_method=None)
        )
    elif include_empty_siblings:
        # The delivery format always carries a SNAPP-PAY row. The source has
        # no Snapp-Pay business, so the honest value is zero, not a forecast.
        extras.append(
            pd.DataFrame(
                {
                    "date": list(target_dates),
                    "level": "vertical",
                    "vertical": PAY_LABEL,
                    "category": None,
                    "payment_method": None,
                    "order": 0.0,
                    "GMV": 0.0,
                }
            )
        )
        warnings.append(
            "The source has no Snapp-Pay rows, so SNAPP-PAY is written as zero "
            "to keep the delivery format complete."
        )
    else:
        warnings.append(
            "No Snapp-Pay rows in the source, so SNAPP-PAY is absent from the file."
        )

    frame = pd.concat([frame, totals, *extras], ignore_index=True)

    # Deterministic order: vertical block, then category, totals last.
    rank = {name: i for i, name in enumerate(VERTICAL_ORDER)}
    frame["_vrank"] = frame["vertical"].map(rank).fillna(len(rank))
    frame["_trank"] = frame["vertical"].map(
        {TOTAL_LABEL: 1, VENTURE_LABEL: 2, PAY_LABEL: 3}
    ).fillna(0)
    frame = (
        frame.sort_values(
            ["date", "_vrank", "_trank", "category", "payment_method"],
            na_position="last",
        )
        .drop(columns=["_vrank", "_trank"])
        .reset_index(drop=True)
    )

    frame["order"] = frame["order"].round(6)
    frame["GMV"] = frame["GMV"].round(0)
    frame = frame[OUTPUT_COLUMNS]

    return DeliveryResult(
        frame=frame,
        origin=origin,
        horizon=horizon,
        categories=int(pairs.shape[0]),
        verticals=int(frame["vertical"].nunique()),
        model=model,
        ranking=tuple(ranking),
        warnings=tuple(warnings),
    )


def validate_delivery(frame: pd.DataFrame) -> bool:
    """The file must be internally coherent before it is published."""
    if list(frame.columns) != OUTPUT_COLUMNS:
        raise DeliveryError(f"Unexpected columns: {list(frame.columns)}")
    if (frame["order"] < 0).any() or (frame["GMV"] < 0).any():
        raise DeliveryError("Delivery file contains negative values")
    if frame["date"].isna().any():
        raise DeliveryError("Delivery file contains a missing date")

    for day, chunk in frame.groupby("date"):
        total = chunk[chunk["vertical"] == TOTAL_LABEL]
        if total.empty:
            raise DeliveryError(f"{day}: VERTICALS row is missing")
        core = chunk[chunk["vertical"].isin(VERTICAL_ORDER)]
        if not np.isclose(total["order"].iloc[0], core["order"].sum(), rtol=1e-9, atol=1e-6):
            raise DeliveryError(f"{day}: VERTICALS order is not the sum of its categories")
        if not np.isclose(total["GMV"].iloc[0], core["GMV"].sum(), rtol=1e-9, atol=1e-3):
            raise DeliveryError(f"{day}: VERTICALS GMV is not the sum of its categories")
    return True


def to_input_layout(frame: pd.DataFrame) -> pd.DataFrame:
    """Reshape a delivery frame into the layout the engine reads back in.

    The delivery file mixes leaf rows with three aggregate rows. The input
    format has no aggregates: ``VERTICALS`` is not a business line and the
    ``SNAPP-PAY`` placeholder is not data, so both are dropped. ``SNAPP-VENTURE``
    is a real line in the input, written as ``Snapp-Venture`` with its
    ``ventures`` category. The result can be uploaded again unchanged.
    """
    leaves = frame[~frame["vertical"].isin([TOTAL_LABEL, PAY_LABEL])].copy()
    vertical = leaves["vertical"].replace(
        {**{name: name for name in VERTICAL_ORDER}, VENTURE_LABEL: "Snapp-Venture"}
    )
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(leaves["date"]).dt.date,
            "vertical": vertical,
            "category": leaves["category"].fillna(""),
            "payment_method_type": leaves["payment_method"].fillna(NO_METHOD),
            "orders": leaves["order"].astype("float64"),
            "gmv": leaves["GMV"].astype("float64"),
        }
    )
    # Sum payment methods first: rounding the split leaves decimals that add
    # up to the category total only after re-summing.
    out = (
        out.groupby(
            ["date", "vertical", "category", "payment_method_type"], as_index=False
        )
        .agg(orders=("orders", "sum"), gmv=("gmv", "sum"))
        .loc[:, INPUT_COLUMNS]
        .sort_values(["date", "vertical", "category", "payment_method_type"])
        .reset_index(drop=True)
    )
    return out


def write_input_layout(frame: pd.DataFrame, path: str | Path) -> Path:
    """Write the input-format export with the same headers the engine reads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="Sheet1", index=False)
        sheet = writer.sheets["Sheet1"]
        for cell in sheet["A"][1:]:
            cell.number_format = "yyyy-mm-dd"
        for column, width in zip("ABCDEF", (12, 16, 30, 22, 14, 20)):
            sheet.column_dimensions[column].width = width
    return path


def write_delivery(frame: pd.DataFrame, path: str | Path) -> Path:
    """Write the workbook with the exact header and date formatting."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="Sheet1", index=False)
        sheet = writer.sheets["Sheet1"]
        for cell in sheet["A"][1:]:
            cell.number_format = "yyyy-mm-dd"
        sheet.column_dimensions["A"].width = 12
        sheet.column_dimensions["B"].width = 10
        sheet.column_dimensions["C"].width = 16
        sheet.column_dimensions["D"].width = 30
        sheet.column_dimensions["E"].width = 18
        sheet.column_dimensions["F"].width = 16
        sheet.column_dimensions["G"].width = 20
    return path
