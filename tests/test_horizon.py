"""Tests: horizon policy and input-window advice."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.serving.horizon import (
    MAX_HORIZON_DAYS,
    SHORT_HISTORY_HORIZON_DAYS,
    history_days,
    horizon_to_next_month_end,
    month_end,
    next_month_end,
    plan_horizon,
    window_advice,
)


def test_month_end_handles_leap_and_short_months():
    assert month_end(date(2026, 2, 10)) == date(2026, 2, 28)
    assert month_end(date(2028, 2, 10)) == date(2028, 2, 29)
    assert month_end(date(2026, 4, 1)) == date(2026, 4, 30)


def test_next_month_end_crosses_the_year():
    assert next_month_end(date(2026, 11, 15)) == date(2026, 12, 31)
    assert next_month_end(date(2026, 12, 5)) == date(2027, 1, 31)


def test_horizon_reaches_the_end_of_next_month():
    # Worst case is the 1st of a 31-day month followed by a 31-day month: 61 days.
    assert horizon_to_next_month_end(date(2026, 10, 1)) == 60
    assert horizon_to_next_month_end(date(2026, 12, 1)) == 61
    assert horizon_to_next_month_end(date(2026, 10, 28)) == 33
    assert horizon_to_next_month_end(date(2026, 11, 30)) == 31


def test_default_plan_targets_next_month_end():
    plan = plan_horizon(date(2026, 10, 28))
    assert plan.target == date(2026, 11, 30)
    assert plan.horizon == 33
    assert not plan.capped


def test_explicit_request_is_capped_at_next_month_end():
    plan = plan_horizon(date(2026, 10, 28), requested=90)
    assert plan.horizon == 33
    assert plan.target == date(2026, 11, 30)
    assert "محدود" in plan.reason


def test_explicit_shorter_request_is_honoured():
    plan = plan_horizon(date(2026, 10, 28), requested=7)
    assert plan.horizon == 7
    assert plan.target == date(2026, 11, 4)


def test_short_history_caps_the_horizon():
    plan = plan_horizon(date(2026, 10, 28), history_days=60)
    assert plan.horizon == SHORT_HISTORY_HORIZON_DAYS
    assert plan.capped
    assert "تاریخچه" in plan.reason


def test_horizon_never_exceeds_the_ceiling():
    plan = plan_horizon(date(2026, 10, 1), requested=200)
    assert plan.horizon <= MAX_HORIZON_DAYS


def test_history_days_counts_distinct_days():
    facts = pd.DataFrame(
        {"date": pd.to_datetime(["2026-01-01", "2026-01-01", "2026-01-02"])}
    )
    assert history_days(facts) == 2
    assert history_days(pd.DataFrame()) == 0


@pytest.mark.parametrize(
    "days,level",
    [(60, "short"), (200, "ok"), (400, "good")],
)
def test_window_advice_levels(days, level):
    advice = window_advice(days)
    assert advice["level"] == level
    assert str(days) in advice["message"]
