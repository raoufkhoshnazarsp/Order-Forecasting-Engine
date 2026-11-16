"""Model drivers: which features moved a forecast, and how much.

Two things this module deliberately does **not** claim:

* It is not causal. A feature with a large weight is one the model *used*, not
  one that *caused* the orders. The UI must say "تخصیص مدل" (model attribution),
  never "علت" (cause).
* It is not a SHAP implementation. SHAP needs a background distribution and a
  tree/linear explainer; the engine ships without scikit-learn or shap. What is
  computed here is the exact linear contribution ``weight * (x - mean) / scale``
  for the ridge backend, which is the same quantity SHAP's linear explainer
  returns for a linear model. For LightGBM the contribution is approximated by
  the feature's global importance, and the result is labelled as such.

The output is per feature: a signed contribution and a plain-language direction.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# A contribution smaller than this share of the total absolute contribution is
# noise, not a driver worth showing.
MIN_SHARE = 0.02

MAX_DRIVERS = 8


@dataclass(frozen=True)
class Driver:
    feature: str
    contribution: float
    share: float
    direction: str  # "increase" | "decrease"

    def as_dict(self) -> dict:
        return {
            "feature": self.feature,
            "contribution": self.contribution,
            "share": self.share,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class DriverReport:
    method: str
    is_causal: bool
    drivers: list[Driver]
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "is_causal": self.is_causal,
            "drivers": [d.as_dict() for d in self.drivers],
            "note": self.note,
        }


def linear_contributions(
    weights: np.ndarray,
    feature_names: list[str],
    row: np.ndarray,
    mean: np.ndarray,
    scale: np.ndarray,
) -> np.ndarray:
    """Exact per-feature contribution of a standardised linear model.

    ``weights`` includes the intercept at index 0; ``row`` is the raw feature
    vector in the same order as ``feature_names``.
    """
    if len(feature_names) != len(row):
        raise ValueError("feature_names and row must have the same length")
    if len(weights) != len(feature_names) + 1:
        raise ValueError("weights must carry one intercept plus one weight per feature")
    scaled = (np.asarray(row, dtype="float64") - mean) / scale
    return np.asarray(weights[1:], dtype="float64") * scaled


def _direction(value: float) -> str:
    return "increase" if value >= 0 else "decrease"


def build_driver_report(
    contributions: np.ndarray,
    feature_names: list[str],
    *,
    method: str,
    is_causal: bool = False,
    note: str = "",
) -> DriverReport:
    """Turn raw contributions into a ranked, human-readable list."""
    values = np.asarray(contributions, dtype="float64")
    if len(values) != len(feature_names):
        raise ValueError("contributions and feature_names must have the same length")
    if not len(values):
        return DriverReport(method=method, is_causal=is_causal, drivers=[], note=note)

    total = float(np.abs(values).sum())
    if total <= 0:
        return DriverReport(method=method, is_causal=is_causal, drivers=[], note=note)

    order = np.argsort(-np.abs(values))
    drivers: list[Driver] = []
    for index in order:
        share = float(abs(values[index]) / total)
        if share < MIN_SHARE:
            break
        drivers.append(
            Driver(
                feature=feature_names[index],
                contribution=float(values[index]),
                share=share,
                direction=_direction(float(values[index])),
            )
        )
        if len(drivers) >= MAX_DRIVERS:
            break
    return DriverReport(
        method=method, is_causal=is_causal, drivers=drivers, note=note
    )


def global_importance(
    weights: np.ndarray, feature_names: list[str]
) -> list[tuple[str, float]]:
    """Absolute weight per feature, for backends without per-row attribution.

    This is a *global* statement ("this feature matters to the model overall"),
    not a statement about one forecast. The caller must label it that way.
    """
    values = np.abs(np.asarray(weights, dtype="float64")[1:])
    if len(values) != len(feature_names):
        raise ValueError("weights must carry one intercept plus one weight per feature")
    order = np.argsort(-values)
    return [(feature_names[i], float(values[i])) for i in order]
