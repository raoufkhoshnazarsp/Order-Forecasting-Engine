"""Tests: feature availability registry, holidays and engagement features.

The phase-4 exit criterion is that every feature can be reproduced for a
historical origin without leakage. These tests enforce the declaration that
makes that checkable rather than aspirational.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.features.engagement import (
    ALL_INPUT_COLUMNS,
    availability,
    campaign_plan_features,
    engagement_features,
)
from src.features.holidays import (
    holiday_dates,
    holiday_features,
    missing_lunar_years,
)
from src.features.registry import (
    DEFAULT_REGISTRY,
    FUTURE_ESTIMATED,
    FUTURE_KNOWN,
    PAST_ONLY,
    FeatureRegistry,
    FeatureRegistryError,
    build_default_registry,
    registry_frame,
)


# ------------------------------------------------------------------ registry
def test_registry_rejects_unknown_level():
    registry = FeatureRegistry()
    with pytest.raises(FeatureRegistryError):
        registry.register("x", family="f", level="MAYBE")


def test_registry_rejects_duplicate_names():
    registry = FeatureRegistry()
    registry.register("x", family="f", level=FUTURE_KNOWN)
    with pytest.raises(FeatureRegistryError):
        registry.register("x", family="f", level=PAST_ONLY)


def test_target_usable_guard_rejects_past_only_features():
    with pytest.raises(FeatureRegistryError) as exc:
        DEFAULT_REGISTRY.assert_target_usable(["weekday", "active_users"])
    assert "active_users" in str(exc.value)


def test_target_usable_guard_accepts_calendar_features():
    DEFAULT_REGISTRY.assert_target_usable(
        ["weekday", "holiday_flag", "days_to_campaign", "planned_budget"]
    )


def test_unknown_feature_is_not_silently_accepted():
    with pytest.raises(FeatureRegistryError):
        DEFAULT_REGISTRY.get("no_such_feature")


def test_registry_covers_every_family_from_the_roadmap():
    families = {record["family"] for record in DEFAULT_REGISTRY.as_records()}
    assert {"calendar", "holiday", "orders_history", "users", "merchants",
            "campaign"} <= families


def test_registry_frame_has_one_row_per_feature():
    frame = registry_frame()
    assert len(frame) == len(DEFAULT_REGISTRY.names())
    assert {"name", "family", "level", "target_compatible"} <= set(frame.columns)
    assert frame["target_compatible"].dtype == bool


def test_build_default_registry_is_independent_per_call():
    first = build_default_registry()
    second = build_default_registry()
    first.register("only_in_first", family="f", level=FUTURE_KNOWN)
    assert not second.is_registered("only_in_first")


# ------------------------------------------------------------------ holidays
def test_nowruz_is_a_holiday_in_any_year():
    """1 Farvardin is fixed, so it must hold for several Persian years."""
    dates = [pd.Timestamp("2026-03-21"), pd.Timestamp("2027-03-21"),
             pd.Timestamp("2025-03-21")]
    frame = holiday_features(dates)
    assert frame["is_holiday"].tolist() == [1, 1, 1]


def test_holiday_is_its_own_zero_distance():
    frame = holiday_features([pd.Timestamp("2026-03-21")])
    assert frame["days_to_next_holiday"].iloc[0] == 0
    assert frame["days_since_holiday"].iloc[0] == 0
    assert frame["holiday_window"].iloc[0] == 1


def test_days_to_next_holiday_counts_forward():
    """Two days before Nowruz, the distance must be two."""
    frame = holiday_features([pd.Timestamp("2026-03-19")])
    assert frame["days_to_next_holiday"].iloc[0] == 2
    assert frame["is_holiday"].iloc[0] == 0


def test_holiday_window_covers_a_few_days_either_side():
    """Three days out is "near"; a week away is not."""
    frame = holiday_features(
        [pd.Timestamp("2026-03-21") + pd.Timedelta(days=offset)
         for offset in range(-6, 7)]
    )
    near = frame["holiday_window"].tolist()
    assert near[0] == 0          # 6 days before: outside the window
    assert near[-1] == 1         # 3 days after Nowruz: still inside
    assert frame.loc[pd.Timestamp("2026-03-18"), "holiday_window"] == 1
    assert frame.loc[pd.Timestamp("2026-03-17"), "holiday_window"] == 0


def test_holidays_are_future_known_for_dates_without_data():
    """No observed row is needed: a far-future date still gets features."""
    frame = holiday_features([pd.Timestamp("2030-05-01")])
    assert frame.shape == (1, 4)
    assert frame["is_holiday"].iloc[0] in (0, 1)


def test_missing_lunar_years_is_reported_not_guessed():
    missing = missing_lunar_years([date(2035, 1, 1)])
    assert 1413 in missing


def test_holiday_dates_include_both_sources():
    dates = holiday_dates()
    assert date(2026, 3, 21) in dates      # fixed: Nowruz
    assert date(2026, 6, 25) in dates      # lunar: Ashura


# ---------------------------------------------------------------- engagement
def base_frame(days: int = 30) -> pd.DataFrame:
    start = date(2026, 1, 1)
    return pd.DataFrame(
        {
            "date": [start + timedelta(days=i) for i in range(days)],
            "entity_id": "FASHION",
            "successful_orders": 100,
        }
    )


def test_missing_columns_are_not_invented_as_zero():
    frame = engagement_features(base_frame())
    for column in ("orders_per_active_user", "merchant_productivity",
                   "campaign_active", "campaign_click_rate"):
        assert column not in frame.columns


def test_availability_reports_what_the_input_supports():
    frame = base_frame()
    frame["active_users"] = 50
    frame["campaign_spend"] = 0
    report = availability(frame)
    assert "active_users" in report.available
    assert "active_merchants" in report.missing
    assert report.families == ["users", "campaign"]
    assert set(report.available) | set(report.missing) == set(ALL_INPUT_COLUMNS)


def test_ratios_are_computed_only_where_inputs_exist():
    frame = base_frame()
    frame["active_users"] = 50
    frame["active_merchants"] = 25
    out = engagement_features(frame)
    assert (out["orders_per_active_user"] == 2.0).all()
    assert (out["merchant_productivity"] == 4.0).all()


def test_zero_denominators_produce_nan_not_infinity():
    frame = base_frame()
    frame["active_users"] = [0, 10] * 15
    out = engagement_features(frame)
    assert np.isnan(out["orders_per_active_user"].iloc[0])
    assert np.isfinite(out["orders_per_active_user"].iloc[1])
    assert not np.isinf(out["orders_per_active_user"]).any()


def test_engagement_features_do_not_look_forward():
    """Changing a later row must not change an earlier row's features."""
    frame = base_frame()
    frame["active_users"] = 50
    frame["campaign_spend"] = 10
    frame["campaign_clicks"] = 2
    out = engagement_features(frame)

    changed = frame.copy()
    changed.loc[20:, "active_users"] = 1
    changed.loc[20:, "campaign_clicks"] = 99
    out_changed = engagement_features(changed)

    pd.testing.assert_frame_equal(out.iloc[:20], out_changed.iloc[:20])


def test_campaign_active_flags_spend_days_only():
    frame = base_frame()
    frame["campaign_spend"] = [0] * 10 + [500] * 20
    out = engagement_features(frame)
    assert out["campaign_active"].iloc[0] == 0
    assert out["campaign_active"].iloc[15] == 1


def test_campaign_plan_marks_the_window():
    plan = pd.DataFrame(
        {"start": ["2026-01-10"], "end": ["2026-01-12"], "budget": [1000.0]}
    )
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(20)]
    frame = campaign_plan_features(dates, plan)
    assert frame["campaign_active"].iloc[9] == 1
    assert frame["campaign_active"].iloc[0] == 0
    assert frame["planned_budget"].iloc[10] == 1000.0
    assert frame["days_to_campaign"].iloc[0] == 9


def test_campaign_plan_without_columns_is_empty_not_wrong():
    frame = campaign_plan_features([date(2026, 1, 1)], None)
    assert frame.empty

    with pytest.raises(ValueError):
        campaign_plan_features(
            [date(2026, 1, 1)], pd.DataFrame({"budget": [10.0]})
        )


def test_lag_and_rolling_features_never_see_the_current_day():
    """The existing time-series builder must stay strictly backward-looking."""
    from src.features.lags import add_time_series_features

    frame = base_frame(60)
    frame["successful_orders"] = np.arange(60)
    out = add_time_series_features(frame)
    assert np.isnan(out["lag_1"].iloc[0])
    assert out["lag_1"].iloc[5] == 4
    assert out["rolling_mean_7"].iloc[7] == pytest.approx(np.mean(range(7)))
