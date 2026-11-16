"""Conformal calibration of forecast intervals.

The engine used to build intervals from ``z * sigma`` where sigma came from a
naive-difference heuristic. That assumes a symmetric, normal error and ignores
how the error actually grows with the horizon. Conformal prediction replaces
the assumption with a measurement: take the model's own backtest residuals and
use their empirical quantile as the interval half-width.

The score is **relative** (``|actual - yhat| / yhat``) rather than absolute,
because order volume differs by orders of magnitude between a vertical and the
business total. A single absolute sigma would be far too wide for small
entities and far too narrow for the total.

Calibration is per horizon bucket, so a D+1 interval is not forced to be as
wide as a D+30 one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Buckets match the ones the backtest report already uses, so the coverage the
# user sees and the calibration that produced the band describe the same rows.
BUCKETS = ((1, 7), (8, 14), (15, 30))

# Below this many calibration points a bucket is not trusted; the widest
# bucket that does have enough points is used instead.
MIN_CALIBRATION_POINTS = 30

# A relative half-width above this is not a forecast, it is a shrug. Capping
# keeps a pathological bucket from producing an unusable band.
MAX_RELATIVE_HALF_WIDTH = 1.0


def bucket_for(horizon: int) -> tuple[int, int]:
    """The calibration bucket a horizon step belongs to."""
    for low, high in BUCKETS:
        if low <= horizon <= high:
            return (low, high)
    return BUCKETS[-1]


@dataclass(frozen=True)
class ConformalCalibration:
    """Empirical relative half-widths, per bucket and confidence level."""

    levels: dict[float, dict[tuple[int, int], float]] = field(default_factory=dict)
    points: dict[tuple[int, int], int] = field(default_factory=dict)
    fallback_bucket: tuple[int, int] | None = None

    def half_width(self, horizon: int, level: float) -> float:
        """Relative half-width for a horizon step at a confidence level."""
        table = self.levels.get(level)
        if not table:
            return 0.0
        bucket = bucket_for(horizon)
        if bucket in table and self.points.get(bucket, 0) >= MIN_CALIBRATION_POINTS:
            return table[bucket]
        if self.fallback_bucket is not None and self.fallback_bucket in table:
            return table[self.fallback_bucket]
        # No trustworthy bucket: use the widest measured one rather than zero.
        return max(table.values()) if table else 0.0

    def as_dict(self) -> dict:
        return {
            "levels": {
                str(level): {f"D+{lo}..D+{hi}": value for (lo, hi), value in table.items()}
                for level, table in self.levels.items()
            },
            "points": {f"D+{lo}..D+{hi}": n for (lo, hi), n in self.points.items()},
            "fallback": (
                f"D+{self.fallback_bucket[0]}..D+{self.fallback_bucket[1]}"
                if self.fallback_bucket
                else None
            ),
        }


def _relative_scores(frame: pd.DataFrame) -> pd.DataFrame:
    """|actual - yhat| / yhat, dropping rows where the ratio is undefined."""
    out = frame.copy()
    yhat = pd.to_numeric(out["yhat"], errors="coerce").astype("float64")
    actual = pd.to_numeric(out["actual"], errors="coerce").astype("float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        score = np.where(yhat > 0, np.abs(actual - yhat) / yhat, np.nan)
    out["score"] = score
    out = out[np.isfinite(out["score"])]
    out["bucket"] = out["horizon"].map(bucket_for)
    return out


def calibrate(
    backtest_frame: pd.DataFrame,
    *,
    levels: tuple[float, ...] = (0.80, 0.90),
) -> ConformalCalibration:
    """Fit empirical half-widths from a rolling-origin backtest frame.

    The frame must carry ``horizon``, ``yhat`` and ``actual``. An empty or
    unusable frame yields an empty calibration, which the caller must treat as
    "no measured band" rather than "zero width".
    """
    if backtest_frame is None or backtest_frame.empty:
        return ConformalCalibration()
    required = {"horizon", "yhat", "actual"}
    if not required.issubset(backtest_frame.columns):
        raise ValueError(
            "Conformal calibration needs horizon, yhat and actual columns"
        )

    scored = _relative_scores(backtest_frame)
    if scored.empty:
        return ConformalCalibration()

    tables: dict[float, dict[tuple[int, int], float]] = {}
    for level in levels:
        table: dict[tuple[int, int], float] = {}
        for bucket, chunk in scored.groupby("bucket"):
            if len(chunk) < 2:
                continue
            quantile = float(np.quantile(chunk["score"], level))
            table[bucket] = min(quantile, MAX_RELATIVE_HALF_WIDTH)
        tables[level] = table

    points = {
        bucket: int(len(chunk)) for bucket, chunk in scored.groupby("bucket")
    }
    usable = [
        bucket
        for bucket, n in points.items()
        if n >= MIN_CALIBRATION_POINTS
        and any(bucket in table for table in tables.values())
    ]
    fallback = max(usable, key=lambda b: points[b]) if usable else None
    return ConformalCalibration(levels=tables, points=points, fallback_bucket=fallback)


def apply_interval(
    yhat: np.ndarray,
    calibration: ConformalCalibration,
    *,
    level: float,
    horizon: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Lower/upper bounds around ``yhat`` at a calibrated level."""
    values = np.asarray(yhat, dtype="float64")
    width = calibration.half_width(horizon, level)
    lower = np.clip(values * (1.0 - width), 0.0, None)
    upper = values * (1.0 + width)
    return lower, upper


def coverage_by(
    backtest_frame: pd.DataFrame,
    calibration: ConformalCalibration,
    *,
    level: float = 0.90,
    by: str = "horizon",
) -> pd.DataFrame:
    """Empirical coverage of the calibrated band, split by a column.

    This is the honest check: the band is only "90%" if roughly 90% of the
    backtest actuals fall inside it. Splitting by horizon and by entity shows
    where the calibration is wrong instead of hiding it in one average.
    """
    if backtest_frame is None or backtest_frame.empty:
        return pd.DataFrame(columns=[by, "points", "coverage", "target"])
    frame = backtest_frame.copy()
    frame["bucket"] = frame["horizon"].map(bucket_for)
    rows = []
    for key, chunk in frame.groupby(by):
        inside = 0
        total = 0
        for bucket, part in chunk.groupby("bucket"):
            width = calibration.half_width(int(part["horizon"].iloc[0]), level)
            yhat = part["yhat"].to_numpy(dtype="float64")
            actual = part["actual"].to_numpy(dtype="float64")
            lower = np.clip(yhat * (1.0 - width), 0.0, None)
            upper = yhat * (1.0 + width)
            inside += int(((actual >= lower) & (actual <= upper)).sum())
            total += len(part)
        rows.append(
            {
                by: key,
                "points": total,
                "coverage": (inside / total) if total else float("nan"),
                "target": level,
            }
        )
    return pd.DataFrame(rows).sort_values(by).reset_index(drop=True)
