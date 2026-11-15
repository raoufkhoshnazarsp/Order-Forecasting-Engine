"""Persian calendar dimension loaded from the supplied DimDate table.

The organisation's ``dim_date`` export is the single source of truth for
Gregorian -> Jalali conversion. It is shipped as a compressed CSV so the
engine needs no extra dependency and no network access.

Important: the export contains two weekend flags. ``is_weekend`` marks
Saturday/Sunday (a generic Western convention) while ``persian_is_weekend``
marks Friday, which is the actual Iranian weekend. Only the Persian flag is
used for modelling; the generic one is ignored on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

from src import config

CALENDAR_PATH = config.PROJECT_ROOT / "data" / "calendar" / "dim_date.csv.gz"

# Columns kept from the source export.
CALENDAR_COLUMNS = [
    "full_date",
    "persian_full_date",
    "persian_year",
    "persian_month",
    "persian_day",
    "persian_day_name",
    "persian_month_name",
    "persian_week_of_year",
    "persian_is_weekend",
    "persian_is_business_day",
    "persian_is_last_day_of_month",
    "persian_season_name",
    "day_of_week",
    "month",
    "year",
]


class CalendarError(ValueError):
    """Raised when the calendar table is missing or inconsistent."""


@dataclass(frozen=True)
class CalendarCoverage:
    start: date
    end: date
    rows: int

    def covers(self, start: date, end: date) -> bool:
        return self.start <= start and end <= self.end


@lru_cache(maxsize=1)
def load_calendar() -> pd.DataFrame:
    """Load and validate the calendar dimension (cached per process)."""
    if not CALENDAR_PATH.exists():
        raise CalendarError(
            f"Calendar table not found at {CALENDAR_PATH.relative_to(config.PROJECT_ROOT)}"
        )
    frame = pd.read_csv(CALENDAR_PATH, compression="gzip", encoding="utf-8")
    missing = [c for c in CALENDAR_COLUMNS if c not in frame.columns]
    if missing:
        raise CalendarError(f"Calendar table missing columns: {missing}")

    frame = frame.loc[:, CALENDAR_COLUMNS].copy()
    frame["full_date"] = pd.to_datetime(frame["full_date"]).dt.date
    frame = frame.sort_values("full_date").reset_index(drop=True)

    if frame["full_date"].duplicated().any():
        raise CalendarError("Calendar table contains duplicate dates")
    if not frame["full_date"].is_monotonic_increasing:
        raise CalendarError("Calendar table is not sorted by date")
    return frame


def coverage() -> CalendarCoverage:
    frame = load_calendar()
    return CalendarCoverage(
        start=frame["full_date"].iloc[0],
        end=frame["full_date"].iloc[-1],
        rows=len(frame),
    )


def assert_covers(start: date, end: date) -> None:
    """Fail loudly when a forecast horizon leaves the calendar's range."""
    span = coverage()
    if not span.covers(start, end):
        raise CalendarError(
            f"Calendar covers {span.start}..{span.end} but {start}..{end} was requested"
        )


def calendar_slice(start: date, end: date) -> pd.DataFrame:
    """Inclusive slice of the calendar between two dates."""
    frame = load_calendar()
    mask = (frame["full_date"] >= start) & (frame["full_date"] <= end)
    return frame.loc[mask].reset_index(drop=True)


def to_persian(value: date) -> dict:
    """Convert one Gregorian date to its Jalali attributes."""
    frame = load_calendar()
    match = frame[frame["full_date"] == value]
    if match.empty:
        raise CalendarError(f"Date {value} is outside the calendar range")
    row = match.iloc[0]
    return {
        "persian_date": row["persian_full_date"],
        "persian_year": int(row["persian_year"]),
        "persian_month": int(row["persian_month"]),
        "persian_day": int(row["persian_day"]),
        "persian_month_name": row["persian_month_name"],
        "persian_day_name": row["persian_day_name"],
        "persian_is_weekend": int(row["persian_is_weekend"]),
    }


def calendar_features(dates) -> pd.DataFrame:
    """Calendar features for any date list, including future target dates.

    Every column here is FUTURE_KNOWN: it can be produced for a forecast
    horizon without observing any order data.
    """
    frame = load_calendar()
    wanted = pd.DataFrame({"full_date": pd.to_datetime(pd.Series(list(dates))).dt.date})
    merged = wanted.merge(frame, on="full_date", how="left")

    missing = merged["persian_year"].isna()
    if missing.any():
        examples = merged.loc[missing, "full_date"].head(3).tolist()
        raise CalendarError(f"Dates outside the calendar range: {examples}")

    out = pd.DataFrame(
        {
            "date": merged["full_date"],
            "weekday": merged["day_of_week"].astype("int16"),
            "is_weekend": merged["persian_is_weekend"].astype("int8"),
            "is_business_day": merged["persian_is_business_day"].astype("int8"),
            "gregorian_year": merged["year"].astype("int16"),
            "gregorian_month": merged["month"].astype("int8"),
            "persian_year": merged["persian_year"].astype("int16"),
            "persian_month": merged["persian_month"].astype("int8"),
            "persian_day": merged["persian_day"].astype("int8"),
            "persian_week_of_year": merged["persian_week_of_year"].astype("int8"),
            "persian_month_name": merged["persian_month_name"],
            "persian_day_name": merged["persian_day_name"],
            "persian_season": merged["persian_season_name"],
            "is_month_end": merged["persian_is_last_day_of_month"].astype("int8"),
        }
    )
    # Days remaining until the last day of the Persian month (0 on the last day).
    month_key = (
        merged["persian_year"].astype(str) + "-" + merged["persian_month"].astype(str)
    )
    last_day = merged.groupby(month_key)["persian_day"].transform("max")
    out["days_to_persian_month_end"] = (last_day - merged["persian_day"]).astype("int8")
    return out


def future_calendar(origin: date, horizon: int) -> pd.DataFrame:
    """Calendar rows for D+1 .. D+horizon after a forecast origin."""
    dates = pd.date_range(start=pd.Timestamp(origin) + pd.Timedelta(days=1),
                          periods=horizon, freq="D").date
    return calendar_features(dates)
