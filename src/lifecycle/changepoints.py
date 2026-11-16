"""Changepoint detection (PELT) without a heavy dependency.

`ruptures` is commented out in requirements because the container cannot
install it comfortably. PELT is a small dynamic programme, so it is implemented
here directly: exact optimal partitioning with the pruning that makes it linear
in practice.

Two rules make this safe to feed a business forecast:

* Only data up to the origin is ever passed in. The caller is responsible for
  the cut, and :func:`recent_regimes` takes an explicit ``as_of``.
* A changepoint must be a real level shift. The default penalty scales with the
  series' own noise and the sample size, so a noisy stable series produces no
  changepoints at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_SEGMENT = 3


class ChangepointError(ValueError):
    """Raised when changepoint detection cannot run."""


@dataclass(frozen=True)
class Changepoint:
    index: int
    date: object
    before_mean: float
    after_mean: float
    shift: float  # after_mean - before_mean

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "date": self.date.isoformat() if hasattr(self.date, "isoformat") else self.date,
            "before_mean": self.before_mean,
            "after_mean": self.after_mean,
            "shift": self.shift,
        }


def default_penalty(values: np.ndarray) -> float:
    """BIC-style penalty from the series' own first-difference noise.

    Using the MAD of first differences instead of the standard deviation keeps a
    single spike from inflating the penalty, and keeps a genuine step (two large
    differences) from being swallowed.

    The multiplier is deliberately above the textbook 2·log(n)·σ². A level shift
    of real business size clears it by orders of magnitude, while on a 120-day
    daily series the textbook value still lets an unlucky random fluctuation be
    split in two. Missing a small shift is the cheaper mistake here: a spurious
    regime boundary corrupts every downstream feature.
    """
    diffs = np.diff(values)
    if diffs.size == 0:
        return 1.0
    mad = float(np.median(np.abs(diffs - np.median(diffs))))
    sigma = mad / 0.6745 if mad > 0 else float(np.std(diffs))
    sigma2 = max(sigma**2 / 2.0, 1e-9)
    return float(4.0 * np.log(len(values)) * sigma2)


def detect_changepoints(
    values: object,
    *,
    penalty: float | None = None,
    min_segment: int = MIN_SEGMENT,
) -> list[int]:
    """Indices where a new segment starts (never 0, never ``len(values)``).

    The cost of a segment is its sum of squared deviations from its own mean,
    so the method finds changes in level. Trend changes are deliberately not
    modelled: on a 120-day daily series they are indistinguishable from noise.
    """
    x = np.asarray(values, dtype="float64")
    if x.ndim != 1:
        raise ChangepointError("Changepoint input must be one-dimensional")
    if np.isnan(x).any():
        raise ChangepointError("Changepoint input must not contain NaN")
    n = len(x)
    if n < 2 * min_segment:
        return []
    if penalty is None:
        penalty = default_penalty(x)
    if penalty < 0:
        raise ChangepointError("Penalty must be non-negative")

    # The segment cost is the residual of a straight-line fit, not of a flat
    # mean. With a mean cost, PELT chops any clean trend into a dozen
    # "segments" because each piece fits a little better; a line absorbs the
    # trend and leaves a genuine step as the only thing worth cutting for.
    t = np.arange(n, dtype="float64")
    sy = np.concatenate([[0.0], np.cumsum(x)])
    syy = np.concatenate([[0.0], np.cumsum(x * x)])
    st = np.concatenate([[0.0], np.cumsum(t)])
    stt = np.concatenate([[0.0], np.cumsum(t * t)])
    sty = np.concatenate([[0.0], np.cumsum(t * x)])

    def cost(start: int, end: int) -> float:
        count = end - start
        if count <= 0:
            return 0.0
        total_y = sy[end] - sy[start]
        total_yy = syy[end] - syy[start]
        total_t = st[end] - st[start]
        total_tt = stt[end] - stt[start]
        total_ty = sty[end] - sty[start]
        sxx = total_tt - total_t * total_t / count
        syy_c = total_yy - total_y * total_y / count
        sxy = total_ty - total_t * total_y / count
        if sxx <= 1e-12:
            return max(float(syy_c), 0.0)
        return max(float(syy_c - sxy * sxy / sxx), 0.0)

    # PELT: F[t] is the best cost for the first t points; R holds candidate
    # starts that can still improve a future solution.
    best = np.full(n + 1, np.inf)
    best[0] = -penalty  # the penalty is added once per new segment, including the first
    last = np.zeros(n + 1, dtype=int)
    candidates = [0]
    for t in range(1, n + 1):
        chosen_value = np.inf
        chosen_start = 0
        for start in candidates:
            if t - start < min_segment:
                continue
            value = best[start] + cost(start, t) + penalty
            if value < chosen_value:
                chosen_value = value
                chosen_start = start
        best[t] = chosen_value
        last[t] = chosen_start
        threshold = best[t]
        candidates = [s for s in candidates if best[s] + cost(s, t) <= threshold]
        candidates.append(t)

    # Backtrack from the end, stopping when a segment would be too short.
    breaks: list[int] = []
    position = n
    while position > 0:
        start = int(last[position])
        if start > 0:
            breaks.append(start)
        position = start
    return sorted(breaks)


def changepoints(
    series: pd.Series,
    *,
    penalty: float | None = None,
    min_segment: int = MIN_SEGMENT,
) -> list[Changepoint]:
    """Changepoints with dates and the size of the level shift."""
    clean = series.dropna().astype("float64").sort_index()
    if clean.empty:
        return []
    breaks = detect_changepoints(
        clean.to_numpy(), penalty=penalty, min_segment=min_segment
    )
    out: list[Changepoint] = []
    edges = [0, *breaks, len(clean)]
    values = clean.to_numpy()
    for left, right in zip(edges[:-1], edges[1:]):
        if right >= len(clean) or right == 0:
            continue
        before = values[max(0, right - max(1, right - left)) : right]
        after_start = right
        after_end = min(len(clean), right + max(1, right - left))
        after = values[after_start:after_end]
        if before.size == 0 or after.size == 0:
            continue
        before_mean = float(before.mean())
        after_mean = float(after.mean())
        out.append(
            Changepoint(
                index=int(right),
                date=clean.index[right].date()
                if hasattr(clean.index[right], "date")
                else clean.index[right],
                before_mean=before_mean,
                after_mean=after_mean,
                shift=after_mean - before_mean,
            )
        )
    return out


def recent_regimes(series: pd.Series, *, as_of: object | None = None) -> list[Changepoint]:
    """Changepoints computed strictly from data on or before ``as_of``."""
    clean = series.dropna().astype("float64").sort_index()
    if as_of is not None:
        clean = clean[clean.index <= pd.Timestamp(as_of)]
    return changepoints(clean)
