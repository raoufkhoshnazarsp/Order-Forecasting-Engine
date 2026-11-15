"""Calendar features. Internal date key stays Gregorian (doc 8.1)."""

from __future__ import annotations

import pandas as pd

try:  # tested Persian calendar library
    import jdatetime
except ImportError:  # pragma: no cover
    jdatetime = None

WEEKEND_DAYS_IR = (4,)  # Friday (Monday=0) in Iran


def _to_series(dates) -> pd.Series:
    return pd.to_datetime(pd.Series(list(dates))).dt.normalize()


def persian_parts(dates) -> pd.DataFrame:
    if jdatetime is None:  # pragma: no cover
        raise RuntimeError("jdatetime is required for Persian calendar features")
    s = _to_series(dates)
    converted = [jdatetime.date.fromgregorian(date=d.date()) for d in s]
    return pd.DataFrame(
        {
            "persian_year": [d.year for d in converted],
            "persian_month": [d.month for d in converted],
            "persian_day": [d.day for d in converted],
            "persian_month_name": [d.j_months_fa if hasattr(d, "j_months_fa") else d.month for d in converted],
        },
        index=s.index,
    )


def calendar_features(dates, *, holidays: set | None = None) -> pd.DataFrame:
    """Build calendar features for an arbitrary (including future) date list.

    All features here are FUTURE_KNOWN: they can be computed for target dates
    without any observed data.
    """
    s = _to_series(dates)
    persian = persian_parts(s)
    month_start = s.dt.to_period("M").dt.to_timestamp()
    month_end = s.dt.to_period("M").dt.end_time.dt.normalize()
    holiday_set = {pd.Timestamp(h).normalize() for h in (holidays or set())}

    day_month = s.dt.day
    days_in_month = s.dt.days_in_month

    frame = pd.DataFrame(
        {
            "date": s.dt.date,
            "weekday": s.dt.day_of_week.astype("int16"),
            "is_weekend": s.dt.day_of_week.isin(WEEKEND_DAYS_IR).astype("int8"),
            "gregorian_year": s.dt.year.astype("int16"),
            "gregorian_month": s.dt.month.astype("int8"),
            "persian_year": persian["persian_year"].astype("int16"),
            "persian_month": persian["persian_month"].astype("int8"),
            "persian_day": persian["persian_day"].astype("int8"),
            "holiday_flag": s.isin(holiday_set).astype("int8"),
            "days_to_month_end": (days_in_month - day_month).astype("int8"),
            "days_from_month_start": (day_month - 1).astype("int8"),
            "month_start_flag": (day_month == 1).astype("int8"),
            "month_end_flag": (day_month == days_in_month).astype("int8"),
        }
    )
    # Distance to the nearest holiday, computed on the calendar only.
    if holiday_set:
        ordinals = [pd.Timestamp(d).toordinal() for d in s]
        holiday_ordinals = sorted({pd.Timestamp(h).normalize().toordinal() for h in holiday_set})
        frame["days_to_holiday"] = [
            min([(h - d) for h in holiday_ordinals if h >= d], default=pd.NA)
            for d in ordinals
        ]
    else:
        frame["days_to_holiday"] = pd.NA
    return frame


def future_calendar(start, periods: int) -> pd.DataFrame:
    """Calendar for a future horizon (used for D+1..D+30 features)."""
    dates = pd.date_range(start=start, periods=periods, freq="D")
    return calendar_features(dates)
