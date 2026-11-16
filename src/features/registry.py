"""Feature availability registry.

Every feature the engine can build declares how far into the future it
remains valid. That declaration is what makes a leakage bug a *test failure*
instead of a silent modelling mistake: a feature marked ``PAST_ONLY`` may
never be handed a row whose date is after the forecast origin.

Levels
------
``FUTURE_KNOWN``
    Computable for any future date with no observed data (calendar, holidays).
``FUTURE_ESTIMATED``
    Requires an assumption about the future but is still produced at forecast
    time (a campaign plan, a budget). It is allowed, but flagged.
``PAST_ONLY``
    Observed only. Legitimate as a *lag* (the value at t-k), never as the
    value on the target date itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

FUTURE_KNOWN = "FUTURE_KNOWN"
FUTURE_ESTIMATED = "FUTURE_ESTIMATED"
PAST_ONLY = "PAST_ONLY"

LEVELS = (FUTURE_KNOWN, FUTURE_ESTIMATED, PAST_ONLY)

# Levels that may be used directly on a target date. PAST_ONLY features are
# only usable through a lag, which the builder handles.
TARGET_COMPATIBLE = (FUTURE_KNOWN, FUTURE_ESTIMATED)


class FeatureRegistryError(ValueError):
    """Raised when a feature is registered or used unsafely."""


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    family: str
    level: str
    description: str = ""

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise FeatureRegistryError(
                f"Unknown availability level {self.level!r} for {self.name!r}; "
                f"expected one of {LEVELS}"
            )

    @property
    def target_compatible(self) -> bool:
        return self.level in TARGET_COMPATIBLE


class FeatureRegistry:
    """An ordered collection of feature specs, keyed by name."""

    def __init__(self) -> None:
        self._specs: dict[str, FeatureSpec] = {}

    def register(
        self,
        name: str,
        *,
        family: str,
        level: str,
        description: str = "",
    ) -> FeatureSpec:
        if name in self._specs:
            raise FeatureRegistryError(f"Feature {name!r} is already registered")
        spec = FeatureSpec(
            name=name, family=family, level=level, description=description
        )
        self._specs[name] = spec
        return spec

    def register_many(
        self, names: Iterable[str], *, family: str, level: str, description: str = ""
    ) -> None:
        for name in names:
            self.register(name, family=family, level=level, description=description)

    def get(self, name: str) -> FeatureSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise FeatureRegistryError(f"Feature {name!r} is not registered") from None

    def is_registered(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return list(self._specs)

    def by_family(self, family: str) -> list[FeatureSpec]:
        return [s for s in self._specs.values() if s.family == family]

    def by_level(self, level: str) -> list[FeatureSpec]:
        return [s for s in self._specs.values() if s.level == level]

    def assert_target_usable(self, names: Iterable[str]) -> None:
        """Reject any feature that cannot be produced for a target date."""
        unsafe = [
            name
            for name in names
            if not self.is_registered(name) or not self.get(name).target_compatible
        ]
        if unsafe:
            raise FeatureRegistryError(
                "These features cannot be produced for a target date: "
                + ", ".join(sorted(unsafe))
            )

    def as_records(self) -> list[dict[str, str | bool]]:
        return [
            {
                "name": s.name,
                "family": s.family,
                "level": s.level,
                "target_compatible": s.target_compatible,
                "description": s.description,
            }
            for s in self._specs.values()
        ]


def build_default_registry() -> FeatureRegistry:
    """The registry the engine ships with.

    Names must match the columns the feature builders actually emit; the test
    suite cross-checks this so a rename cannot silently escape registration.
    """
    registry = FeatureRegistry()

    registry.register_many(
        ("weekday", "is_weekend", "is_business_day", "persian_day",
         "persian_month", "persian_year", "persian_week_of_year",
         "month_sin", "month_cos", "month_end_distance", "season"),
        family="calendar",
        level=FUTURE_KNOWN,
        description="Persian calendar position, read from dim_date",
    )
    registry.register_many(
        ("holiday_flag", "is_holiday", "days_to_holiday", "days_to_next_holiday",
         "days_since_holiday", "holiday_window"),
        family="holiday",
        level=FUTURE_KNOWN,
        description="Official Iranian holiday markers",
    )
    registry.register_many(
        ("lag_1", "lag_7", "lag_14", "lag_28", "lag_364",
         "rolling_mean_7", "rolling_mean_28", "rolling_std_7",
         "rolling_min_7", "rolling_max_7"),
        family="orders_history",
        level=PAST_ONLY,
        description="Observed order history, usable only through a lag",
    )
    registry.register_many(
        ("active_users", "new_users", "returning_users"),
        family="users",
        level=PAST_ONLY,
        description="Observed user counts; ratios are valid, the raw value is not",
    )
    registry.register(
        "orders_per_active_user",
        family="users",
        level=PAST_ONLY,
        description="Orders divided by active users on the same observed day",
    )
    registry.register_many(
        ("active_merchants", "new_merchants"),
        family="merchants",
        level=PAST_ONLY,
        description="Observed merchant counts",
    )
    registry.register(
        "merchant_productivity",
        family="merchants",
        level=PAST_ONLY,
        description="Orders per active merchant on the same observed day",
    )
    registry.register_many(
        ("campaign_spend", "campaign_impressions", "campaign_clicks"),
        family="campaign",
        level=PAST_ONLY,
        description="Observed campaign delivery",
    )
    registry.register(
        "campaign_active",
        family="campaign",
        level=PAST_ONLY,
        description="Whether a campaign ran that day, observed after the fact",
    )
    registry.register(
        "planned_budget",
        family="campaign",
        level=FUTURE_ESTIMATED,
        description="Planned campaign budget for a future date",
    )
    registry.register(
        "days_to_campaign",
        family="campaign",
        level=FUTURE_KNOWN,
        description="Distance to a scheduled campaign start, from the campaign plan",
    )
    return registry


DEFAULT_REGISTRY = build_default_registry()


def registry_frame(registry: FeatureRegistry | None = None):
    """Registry as a pandas frame, for the UI's feature table."""
    import pandas as pd

    return pd.DataFrame((registry or DEFAULT_REGISTRY).as_records())
