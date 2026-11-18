"""Monthly comparison dashboard: the past, and three scenarios for next month.

Two questions, one page:

1. **What happened?** Monthly orders and GMV per vertical over the last N
   complete months, straight from the source data.
2. **What is coming?** The next calendar month under three scenarios.

Scenarios are never hand-picked numbers. They come from the model's own
rolling-origin error:

    pessimistic = point forecast x (1 - z * sigma_rel)
    realistic   = point forecast
    optimistic  = point forecast x (1 + z * sigma_rel)

where ``sigma_rel`` is the relative standard deviation of the backtest
residuals for that vertical. ``VERTICALS`` is always the sum of the seven
verticals in the same scenario, never its own forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from src.models.baselines import BASELINES
from src.serving.delivery import (
    PAY_LABEL,
    TOTAL_LABEL,
    VENTURE_LABEL,
    VERTICAL_ORDER,
    _display_vertical,
    _forecast_aov,
    _forecast_orders,
    select_model,
)

Z_90 = 1.6449
MAX_SIGMA_REL = 0.60
MIN_WINDOWS = 3
SCENARIOS = ("pessimistic", "realistic", "optimistic")


class ComparisonError(ValueError):
    """Raised when the comparison cannot be built from the source."""


@dataclass(frozen=True)
class ComparisonResult:
    months: list[dict]
    scenarios: list[dict]
    next_month: str
    past_months: list[str]
    model: str
    validity: dict
    warnings: tuple[str, ...] = ()
    scenario_months: list[dict] = field(default_factory=list)
    current_month: str = ""
    payment_method: str = ""

    def as_dict(self) -> dict:
        return {
            "months": self.months,
            "scenarios": self.scenarios,
            "scenario_months": self.scenario_months,
            "next_month": self.next_month,
            "current_month": self.current_month,
            "past_months": self.past_months,
            "model": self.model,
            "payment_method": self.payment_method,
            "validity": self.validity,
            "warnings": list(self.warnings),
        }


def _month_key(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series).dt.to_period("M")


def _complete_months(facts: pd.DataFrame, count: int) -> list[pd.Period]:
    """The last ``count`` months that are fully covered by the data."""
    dates = pd.to_datetime(facts["date"])
    last = dates.max()
    last_month = last.to_period("M")
    # A month is complete only if the data reaches its final day.
    if last.day < last_month.days_in_month:
        last_month -= 1
    months = [last_month - i for i in range(count - 1, -1, -1)]
    return [m for m in months if m >= dates.min().to_period("M")]


def _monthly_actuals(facts: pd.DataFrame, months: list[pd.Period]) -> list[dict]:
    frame = facts.copy()
    frame["month"] = _month_key(frame["date"])
    frame["vertical"] = frame["vertical"].map(_display_vertical)
    frame = frame[frame["month"].isin(months)]

    grouped = (
        frame.groupby(["month", "vertical"], as_index=False)
        .agg(orders=("orders", "sum"), gmv=("gmv", "sum"))
    )

    rows: list[dict] = []
    for month in months:
        chunk = grouped[grouped["month"] == month]
        by_vertical = {r["vertical"]: r for _, r in chunk.iterrows()}
        core = chunk[chunk["vertical"].isin(VERTICAL_ORDER)]
        total_orders = float(core["orders"].sum())
        total_gmv = float(core["gmv"].sum())

        for vertical in VERTICAL_ORDER:
            row = by_vertical.get(vertical)
            orders = float(row["orders"]) if row is not None else 0.0
            gmv = float(row["gmv"]) if row is not None else 0.0
            rows.append(
                {
                    "month": str(month),
                    "vertical": vertical,
                    "orders": orders,
                    "gmv": gmv,
                    "aov": gmv / orders if orders else 0.0,
                    "share": orders / total_orders if total_orders else 0.0,
                }
            )
        for label in (VENTURE_LABEL, PAY_LABEL):
            row = by_vertical.get(label)
            orders = float(row["orders"]) if row is not None else 0.0
            gmv = float(row["gmv"]) if row is not None else 0.0
            rows.append(
                {
                    "month": str(month),
                    "vertical": label,
                    "orders": orders,
                    "gmv": gmv,
                    "aov": gmv / orders if orders else 0.0,
                    "share": None,
                }
            )
        rows.append(
            {
                "month": str(month),
                "vertical": TOTAL_LABEL,
                "orders": total_orders,
                "gmv": total_gmv,
                "aov": total_gmv / total_orders if total_orders else 0.0,
                "share": 1.0 if total_orders else 0.0,
            }
        )
    return rows


def _relative_sigma(facts: pd.DataFrame, forecaster, horizon: int) -> dict[str, float]:
    """Relative residual spread per vertical from rolling-origin backtests."""
    frame = facts.copy()
    frame["vertical"] = frame["vertical"].map(_display_vertical)
    frame["date"] = pd.to_datetime(frame["date"])
    daily = (
        frame.groupby(["date", "vertical"], as_index=False)
        .agg(orders=("orders", "sum"))
        .sort_values("date")
    )

    sigmas: dict[str, float] = {}
    for vertical, chunk in daily.groupby("vertical"):
        series = chunk.set_index("date")["orders"].astype("float64").sort_index()
        series = series[~series.index.duplicated(keep="last")]
        # Reindex onto a complete daily calendar. A vertical with no orders on
        # a day has no row at all, and slicing a sparse index gives fewer rows
        # than the horizon -- which silently pairs residuals with the wrong
        # forecast days.
        series = series.reindex(
            pd.date_range(series.index.min(), series.index.max(), freq="D")
        )
        if len(series) < 60:
            continue
        residuals: list[float] = []
        origin = series.index[0] + pd.Timedelta(days=45)
        while origin + pd.Timedelta(days=horizon) <= series.index[-1]:
            train = series.loc[:origin].dropna()
            if len(train) >= 45:
                pred = _forecast_orders(train, forecaster, horizon)
                actual = series.reindex(
                    pd.date_range(
                        origin + pd.Timedelta(days=1), periods=horizon, freq="D"
                    )
                )
                for value, forecast in zip(actual.to_numpy(dtype="float64"), pred):
                    if not np.isnan(value) and value > 0:
                        residuals.append((value - forecast) / value)
            origin += pd.Timedelta(days=horizon)
        if len(residuals) >= 5:
            sigmas[vertical] = float(np.std(residuals, ddof=1))
    return sigmas


def _horizon_months(origin: date, horizon: int) -> list[dict]:
    """The calendar months the horizon touches, in order.

    The business question is "the rest of this month plus next month", so the
    horizon is reported per calendar month rather than lumped into the month
    it ends in. A horizon that stays inside one month yields one entry.
    """
    start = pd.Timestamp(origin) + pd.Timedelta(days=1)
    dates = pd.date_range(start=start, periods=horizon, freq="D")
    out: list[dict] = []
    for period, chunk in pd.Series(dates).groupby(dates.to_period("M")):
        out.append(
            {
                "month": str(period),
                "days": int(len(chunk)),
                "start": chunk.iloc[0].date().isoformat(),
                "end": chunk.iloc[-1].date().isoformat(),
                "scope": "current" if len(out) == 0 else "next",
            }
        )
    return out


def _next_month_scenarios(
    facts: pd.DataFrame,
    *,
    origin: date,
    horizon: int,
    forecaster,
    sigmas: dict[str, float],
) -> tuple[list[dict], list[dict]]:
    frame = facts.copy()
    frame["vertical"] = frame["vertical"].map(_display_vertical)
    pairs = (
        frame[["vertical", "category"]]
        .drop_duplicates()
        .sort_values(["vertical", "category"])
    )

    target_dates = pd.date_range(
        start=pd.Timestamp(origin) + pd.Timedelta(days=1), periods=horizon, freq="D"
    )
    month_days = [str(day.to_period("M")) for day in target_dates]
    months = _horizon_months(origin, horizon)
    month_keys = [m["month"] for m in months]

    # per_vertical[vertical][month] -> orders / gmv for the horizon window.
    per_vertical: dict[str, dict[str, dict[str, float]]] = {}
    for vertical, category in pairs.itertuples(index=False):
        rows = frame[
            frame["vertical"].eq(vertical) & frame["category"].eq(category)
        ]
        if rows.empty:
            continue
        series = (
            rows.assign(date=pd.to_datetime(rows["date"]))
            .groupby("date")
            .agg(orders=("orders", "sum"), gmv=("gmv", "sum"))
        )
        full = pd.date_range(series.index.min(), series.index.max(), freq="D")
        series = series.reindex(full)
        orders = series["orders"].astype("float64")
        gmv = series["gmv"].astype("float64")

        order_path = _forecast_orders(orders, forecaster, horizon)
        aov_path = _forecast_aov(orders, gmv, horizon)
        gmv_path = order_path * aov_path
        buckets = per_vertical.setdefault(vertical, {})
        for index, key in enumerate(month_keys):
            mask = [m == key for m in month_days]
            bucket = buckets.setdefault(key, {"orders": 0.0, "gmv": 0.0})
            bucket["orders"] += float(order_path[mask].sum())
            bucket["gmv"] += float(gmv_path[mask].sum())

    rows: list[dict] = []
    core_totals = {
        (key, scenario): {"orders": 0.0, "gmv": 0.0}
        for key in month_keys
        for scenario in SCENARIOS
    }

    def push(vertical: str, key: str, sigma: float, core: bool = False) -> None:
        bucket = per_vertical.get(vertical, {}).get(key)
        if bucket is None:
            return
        for scenario, factor in (
            ("pessimistic", 1 - Z_90 * sigma),
            ("realistic", 1.0),
            ("optimistic", 1 + Z_90 * sigma),
        ):
            orders = max(bucket["orders"] * factor, 0.0)
            gmv = max(bucket["gmv"] * factor, 0.0)
            if core:
                core_totals[(key, scenario)]["orders"] += orders
                core_totals[(key, scenario)]["gmv"] += gmv
            rows.append(
                {
                    "month": key,
                    "vertical": vertical,
                    "scenario": scenario,
                    "orders": orders,
                    "gmv": gmv,
                    "aov": gmv / orders if orders else 0.0,
                    "sigma_rel": sigma,
                }
            )

    for vertical in VERTICAL_ORDER:
        if vertical not in per_vertical:
            continue
        sigma = min(sigmas.get(vertical, 0.0), MAX_SIGMA_REL)
        for key in month_keys:
            push(vertical, key, sigma, core=True)

    for label in (VENTURE_LABEL, PAY_LABEL):
        if label not in per_vertical:
            continue
        sigma = min(sigmas.get(label, 0.0), MAX_SIGMA_REL)
        for key in month_keys:
            push(label, key, sigma)

    for key in month_keys:
        for scenario in SCENARIOS:
            totals = core_totals[(key, scenario)]
            rows.append(
                {
                    "month": key,
                    "vertical": TOTAL_LABEL,
                    "scenario": scenario,
                    "orders": totals["orders"],
                    "gmv": totals["gmv"],
                    "aov": (
                        totals["gmv"] / totals["orders"]
                        if totals["orders"]
                        else 0.0
                    ),
                    "sigma_rel": None,
                }
            )
    return rows, months


def build_comparison(
    facts: pd.DataFrame,
    *,
    origin: date,
    horizon: int,
    months: int = 6,
    model: str = "auto",
    payment_method: str = "",
) -> ComparisonResult:
    """Build the past table and the three next-month scenarios."""
    if facts.empty:
        raise ComparisonError("داده‌ای برای مقایسه وجود ندارد.")
    months = max(1, min(int(months), 12))

    warnings: list[str] = []
    ranking: list[dict] = []
    if model == "auto":
        model, ranking = select_model(facts, horizon=min(horizon, 30))
        if not ranking:
            warnings.append("تاریخچه برای مقایسهٔ مدل‌ها کافی نبود؛ مدل ساده استفاده شد.")
    if model not in BASELINES:
        raise ComparisonError(f"مدل ناشناخته: {model}")
    forecaster = BASELINES[model]

    past = _complete_months(facts, months)
    if not past:
        raise ComparisonError("هیچ ماه کاملی در داده وجود ندارد.")
    if len(past) < months:
        warnings.append(
            f"فقط {len(past)} ماه کامل در داده موجود است (درخواست: {months})."
        )

    month_rows = _monthly_actuals(facts, past)
    sigmas = _relative_sigma(facts, forecaster, min(horizon, 30))
    scenario_rows, scenario_months = _next_month_scenarios(
        facts, origin=origin, horizon=horizon, forecaster=forecaster, sigmas=sigmas
    )

    windows = 0
    if ranking:
        windows = 1
    validity = {
        "model": model,
        "ranking": ranking,
        "windows": windows,
        "sigma_source": "backtest" if sigmas else "none",
        "low_confidence": (not sigmas) or windows < MIN_WINDOWS,
        "note": (
            "سناریوها از انحراف معیار خطای بک‌تست ساخته شده‌اند."
            if sigmas
            else "خطای بک‌تست کافی نبود؛ سناریوها با پیش‌بینی نقطه‌ای یکسان‌اند."
        ),
    }

    # The scenario months are the calendar months the horizon touches: the
    # remainder of the origin's month and the following month in full.
    next_month = scenario_months[-1]["month"] if scenario_months else str(
        (pd.Timestamp(origin) + pd.Timedelta(days=horizon)).to_period("M")
    )
    return ComparisonResult(
        months=month_rows,
        scenarios=scenario_rows,
        scenario_months=scenario_months,
        current_month=str(pd.Timestamp(origin).to_period("M")),
        next_month=next_month,
        past_months=[str(m) for m in past],
        model=model,
        validity=validity,
        warnings=tuple(warnings),
        payment_method=payment_method,
    )
