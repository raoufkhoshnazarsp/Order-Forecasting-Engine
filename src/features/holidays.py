"""Official Iranian holiday features.

Holidays are FUTURE_KNOWN: the calendar of a future date is known today, so
these features are safe to use on a target date. Two sources are combined:

* fixed Persian month/day holidays, derived from ``dim_date`` for any year;
* lunar holidays, listed explicitly per Persian year in ``configs/holidays.yaml``
  because they shift every year.

A year with no lunar entries is reported, never guessed.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

import pandas as pd

from src import config
from src.features.persian_calendar import load_calendar

HOLIDAY_WINDOW = 3  # days either side counted as "near a holiday"


def _fixed_dates() -> set[date]:
    """Every fixed Persian holiday, as Gregorian dates, from dim_date."""
    spec = config.load_yaml("holidays.yaml").get("fixed_persian") or []
    if not spec:
        return set()
    table = load_calendar()
    wanted = {(int(item["month"]), int(item["day"])) for item in spec}
    mask = [
        (int(m), int(d)) in wanted
        for m, d in zip(table["persian_month"], table["persian_day"])
    ]
    return {d for d, keep in zip(table["full_date"], mask) if keep}


def _lunar_dates() -> set[date]:
    spec = config.load_yaml("holidays.yaml").get("lunar") or {}
    out: set[date] = set()
    for entries in spec.values():
        for item in entries or []:
            out.add(pd.Timestamp(item["date"]).date())
    return out


def holiday_dates() -> set[date]:
    """All known official holidays, fixed and lunar."""
    return _fixed_dates() | _lunar_dates()


def missing_lunar_years(dates: Iterable) -> list[int]:
    """Persian years in ``dates`` that have no lunar holiday data."""
    spec = config.load_yaml("holidays.yaml").get("lunar") or {}
    covered = {int(year) for year in spec}
    table = load_calendar()
    wanted = pd.to_datetime(pd.Series(list(dates))).dt.date
    years = set(
        table.loc[table["full_date"].isin(set(wanted)), "persian_year"].astype(int)
    )
    return sorted(years - covered)


def holiday_features(dates: Iterable) -> pd.DataFrame:
    """Holiday markers for any date list, including future target dates.

    Columns
    -------
    is_holiday
        1 when the date is an official holiday.
    days_to_next_holiday
        Days until the next holiday (0 on a holiday itself).
    days_since_holiday
        Days since the previous holiday (0 on a holiday itself).
    holiday_window
        1 when within ``HOLIDAY_WINDOW`` days of a holiday, either side.
    """
    index = pd.to_datetime(pd.Series(list(dates))).dt.normalize()
    holidays = sorted(holiday_dates())
    ordinals = [h.toordinal() for h in holidays]
    rows = []
    for stamp in index:
        ordinal = stamp.date().toordinal()
        forward = [h - ordinal for h in ordinals if h >= ordinal]
        backward = [ordinal - h for h in ordinals if h <= ordinal]
        to_next = min(forward) if forward else pd.NA
        since = min(backward) if backward else pd.NA
        near = (
            (to_next is not pd.NA and to_next <= HOLIDAY_WINDOW)
            or (since is not pd.NA and since <= HOLIDAY_WINDOW)
        )
        rows.append(
            {
                "is_holiday": int(ordinal in set(ordinals)),
                "days_to_next_holiday": to_next,
                "days_since_holiday": since,
                "holiday_window": int(bool(near)),
            }
        )
    return pd.DataFrame(rows, index=index)
