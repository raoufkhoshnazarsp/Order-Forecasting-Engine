"""Forecast horizon and input-window policy.

The business question is always "what will the rest of next calendar month
look like?", so the horizon is derived from the data, not typed by hand:

* the last day in the file is the origin;
* the target is the last day of the **next** calendar month;
* the horizon is the number of days between them.

That is at most 61 days (origin on the 1st of a 31-day month followed by a
31-day month), which is why
the recommended history is 13 complete months: enough for several 61-day
rolling-origin windows.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

MIN_HISTORY_DAYS = 120
RECOMMENDED_HISTORY_DAYS = 400
MAX_HORIZON_DAYS = 61
SHORT_HISTORY_HORIZON_DAYS = 31


def month_end(day: date) -> date:
    """Last calendar day of the month containing ``day``."""
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def next_month_end(day: date) -> date:
    """Last calendar day of the month after the one containing ``day``."""
    first = date(day.year, day.month, 1)
    following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return month_end(following)


def horizon_to_next_month_end(origin: date) -> int:
    """Days from ``origin`` to the end of the following calendar month."""
    return (next_month_end(origin) - origin).days


@dataclass(frozen=True)
class HorizonPlan:
    origin: date
    target: date
    horizon: int
    requested: int
    capped: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "origin": self.origin.isoformat(),
            "target": self.target.isoformat(),
            "horizon": self.horizon,
            "requested": self.requested,
            "capped": self.capped,
            "reason": self.reason,
        }


def plan_horizon(
    origin: date,
    *,
    requested: int | None = None,
    history_days: int | None = None,
) -> HorizonPlan:
    """Resolve the horizon for a run.

    ``requested=None`` means "to the end of next calendar month". An explicit
    request is honoured but never allowed to exceed the 61-day ceiling, and a
    short history caps it further so the model is not asked to extrapolate
    further than the data can support.
    """
    natural = horizon_to_next_month_end(origin)
    target = next_month_end(origin)
    reason = "افق تا پایان ماه میلادی بعد محاسبه شد."

    if requested is None:
        horizon = natural
    else:
        horizon = max(1, int(requested))
        if horizon > natural:
            horizon = natural
            target = origin + timedelta(days=horizon)
            reason = (
                f"افق درخواستی {requested} روز بود؛ به پایان ماه میلادی بعد "
                f"({horizon} روز) محدود شد."
            )
        else:
            target = origin + timedelta(days=horizon)
            reason = f"افق درخواستی {requested} روز اعمال شد."

    capped = False
    if horizon > MAX_HORIZON_DAYS:
        horizon = MAX_HORIZON_DAYS
        target = origin + timedelta(days=horizon)
        capped = True
        reason = f"افق به سقف {MAX_HORIZON_DAYS} روز محدود شد."

    if history_days is not None and history_days < MIN_HISTORY_DAYS:
        if horizon > SHORT_HISTORY_HORIZON_DAYS:
            horizon = SHORT_HISTORY_HORIZON_DAYS
            target = origin + timedelta(days=horizon)
            capped = True
            reason = (
                f"تاریخچه فقط {history_days} روز است؛ افق به "
                f"{SHORT_HISTORY_HORIZON_DAYS} روز محدود شد."
            )

    return HorizonPlan(
        origin=origin,
        target=target,
        horizon=horizon,
        requested=requested if requested is not None else natural,
        capped=capped,
        reason=reason,
    )


def history_days(facts: pd.DataFrame) -> int:
    """Number of distinct days present in the source."""
    if facts.empty or "date" not in facts:
        return 0
    return int(pd.to_datetime(facts["date"]).dt.normalize().nunique())


def window_advice(days: int) -> dict:
    """How much history the file carries, in business terms."""
    if days < MIN_HISTORY_DAYS:
        level = "short"
        message = (
            f"{days} روز داده: کمتر از حداقل {MIN_HISTORY_DAYS} روز. "
            "افق به پایان ماه جاری محدود می‌شود."
        )
    elif days < RECOMMENDED_HISTORY_DAYS:
        level = "ok"
        message = (
            f"{days} روز داده: برای افق تا پایان ماه بعد کافی است، اما "
            f"{RECOMMENDED_HISTORY_DAYS} روز (۱۳ ماه کامل) برای اعتبارسنجی سالانه توصیه می‌شود."
        )
    else:
        level = "good"
        message = f"{days} روز داده: برای اعتبارسنجی سالانه و افق ۶۲ روزه کافی است."
    return {"level": level, "days": days, "message": message}
