"""Scenario engine: apply a business change without touching the base forecast.

The contract that makes a scenario useful is reproducibility. A scenario is a
pure function of (base forecast, scenario spec), it never mutates the input, and
its ``scenario_id`` is a hash of the canonical spec — so the same scenario on the
same base run yields the same id and the same numbers, and a scenario can be
re-derived later without storing it.

A scenario is *not* a forecast. It answers "what happens to the already
forecast path if budgets rise 20%", not "what will actually happen".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src import config
from src.data_mart.entity_master import EntityMaster

SCENARIO_COLUMNS = ("forecast_origin", "target_date", "entity_id", "entity_type", "horizon")
VALUE_COLUMNS = ("yhat", "reconciled_yhat", "lower_80", "upper_80", "lower_90", "upper_90")


class ScenarioError(ValueError):
    """Raised when a scenario cannot be applied safely."""


@dataclass(frozen=True)
class Adjustment:
    entity_id: str
    factor: float = 1.0
    add_daily: float = 0.0
    note: str = ""

    def __post_init__(self) -> None:
        if self.factor < 0:
            raise ScenarioError(f"Negative factor for {self.entity_id}")
        if self.factor == 0:
            raise ScenarioError(f"A zero factor would erase {self.entity_id}")

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "factor": self.factor,
            "add_daily": self.add_daily,
            "note": self.note,
        }


@dataclass(frozen=True)
class Scenario:
    name: str
    label: str
    description: str = ""
    adjustments: list[Adjustment] = field(default_factory=list)

    def spec(self) -> dict:
        """Canonical, order-independent representation used for the id hash."""
        return {
            "name": self.name,
            "adjustments": sorted(
                (a.as_dict() for a in self.adjustments), key=lambda d: d["entity_id"]
            ),
        }

    @property
    def scenario_id(self) -> str:
        payload = json.dumps(self.spec(), sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"scn_{self.name}_{digest}"


def load_scenarios(path: str | Path | None = None) -> dict[str, Scenario]:
    """Read the scenario catalogue from ``configs/scenarios.yaml``."""
    if path is None:
        document = config.load_yaml("scenarios.yaml")
    else:
        import yaml

        document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    entries = document.get("scenarios") or {}
    if not isinstance(entries, dict) or not entries:
        raise ScenarioError("scenarios.yaml must define at least one scenario")

    out: dict[str, Scenario] = {}
    for name, spec in entries.items():
        adjustments = [
            Adjustment(
                entity_id=str(item["entity_id"]),
                factor=float(item.get("factor", 1.0)),
                add_daily=float(item.get("add_daily", 0.0)),
                note=str(item.get("note", "")),
            )
            for item in spec.get("adjustments", [])
        ]
        out[name] = Scenario(
            name=name,
            label=str(spec.get("label", name)),
            description=str(spec.get("description", "")),
            adjustments=adjustments,
        )
    return out


def _split_rules(
    base: pd.DataFrame, scenario: Scenario, leaf_ids: set[str], total_id: str
) -> tuple[tuple[float, float] | None, dict[str, tuple[float, float]], float]:
    """Split the rules into wildcard, leaf-level and total-level adjustments.

    A leaf rule scales that leaf and must match a row in the base forecast.
    ``ALL`` scales every leaf *present in the forecast* — the master may list an
    entity the workbook never had, and that is not an error. A rule on the total
    (used for a new venture's capacity) cannot be applied leaf by leaf, so its
    daily orders are returned separately and distributed later in proportion to
    each leaf's forecast — that keeps the total exact and the mix realistic.
    """
    wildcard: tuple[float, float] | None = None
    leaf_rules: dict[str, tuple[float, float]] = {}
    total_add = 0.0
    present = set(base["entity_id"].astype(object).map(str))
    for adjustment in scenario.adjustments:
        if adjustment.entity_id == "ALL":
            wildcard = (adjustment.factor, adjustment.add_daily)
        elif adjustment.entity_id in leaf_ids:
            if adjustment.entity_id not in present:
                raise ScenarioError(
                    f"Scenario {scenario.name!r} targets "
                    f"{adjustment.entity_id!r}, which has no base forecast"
                )
            leaf_rules[adjustment.entity_id] = (
                adjustment.factor,
                adjustment.add_daily,
            )
        elif adjustment.entity_id == total_id:
            total_add += adjustment.add_daily
            if adjustment.factor != 1.0:
                raise ScenarioError(
                    "A factor on TOTAL would double-count the leaves; "
                    "apply it with entity_id ALL instead"
                )
        else:
            raise ScenarioError(
                f"Scenario {scenario.name!r} names unknown entity {adjustment.entity_id!r}"
            )
    return wildcard, leaf_rules, total_add


def _leaf_and_total_ids(
    frame: pd.DataFrame, entity_master: EntityMaster | None
) -> tuple[set[str], str]:
    """Which rows are leaves and which is the total.

    The forecast itself carries ``entity_type`` and is the authority: the
    entity master on disk can be a different vintage than the workbook (a
    venture that only exists in the data, or a vertical renamed since). Falling
    back to the master keeps the function usable on frames without the column.
    """
    if "entity_type" in frame.columns:
        leaves = set(
            frame.loc[
                frame["entity_type"].isin(["VERTICAL", "VENTURE"]), "entity_id"
            ].astype(object).map(str)
        )
        totals = set(
            frame.loc[frame["entity_type"].eq("TOTAL"), "entity_id"]
            .astype(object)
            .map(str)
        )
        total_id = sorted(totals)[0] if totals else "TOTAL"
        if leaves:
            return leaves, total_id
    if entity_master is None:
        raise ScenarioError("Cannot tell leaves from the forecast; pass an entity master")
    return (
        set(entity_master.leaf_ids(active_only=False)),
        entity_master.total_id(),
    )


def apply_scenario(
    forecast: pd.DataFrame,
    scenario: Scenario,
    entity_master: EntityMaster | None = None,
) -> pd.DataFrame:
    """Return a scenario forecast; the input frame is never modified.

    Leaves are scaled by their rule, unknown entities are rejected rather than
    ignored, and every parent is rebuilt as an exact sum of its leaves so the
    result stays coherent. Intervals are scaled by the same factor so their
    ordering and nesting survive — a scenario changes the level, not the
    measured width.
    """
    if forecast is None or forecast.empty:
        raise ScenarioError("A scenario needs a base forecast")
    missing = [c for c in (*SCENARIO_COLUMNS, *VALUE_COLUMNS) if c not in forecast.columns]
    if missing:
        raise ScenarioError(f"Base forecast is missing columns: {missing}")

    base = forecast.copy(deep=True)
    base["entity_id"] = base["entity_id"].astype(object).map(str)
    leaf_ids, total_id = _leaf_and_total_ids(base, entity_master)
    wildcard, leaf_rules, total_add = _split_rules(base, scenario, leaf_ids, total_id)

    adjusted = base.copy()
    rules = dict(leaf_rules)
    if wildcard is not None:
        factor, add = wildcard
        for leaf in sorted(leaf_ids & set(base["entity_id"])):
            rules.setdefault(leaf, (factor, add))
    for entity_id, (factor, add) in rules.items():
        mask = adjusted["entity_id"].eq(entity_id)
        for column in VALUE_COLUMNS:
            values = adjusted.loc[mask, column].astype("float64")
            adjusted.loc[mask, column] = (values * factor + add).clip(lower=0.0)

    # Distribute a total-level addition across leaves in proportion to their
    # forecast level, so the total moves by exactly that many orders per day.
    if total_add:
        keys = ["forecast_origin", "target_date"]
        leaf_mask = adjusted["entity_id"].isin(leaf_ids)
        weights = adjusted.loc[leaf_mask, keys + ["entity_id", "reconciled_yhat"]].copy()
        day_sum = weights.groupby(keys)["reconciled_yhat"].transform("sum")
        weights["_weight"] = np.where(day_sum > 0, weights["reconciled_yhat"] / day_sum, 0.0)
        weights["_add"] = weights["_weight"] * total_add
        mapping = weights.set_index(keys + ["entity_id"])["_add"]
        idx = adjusted.loc[leaf_mask, keys + ["entity_id"]].set_index(
            keys + ["entity_id"]
        ).index
        adds = mapping.reindex(idx).fillna(0.0).to_numpy()
        for column in VALUE_COLUMNS:
            adjusted.loc[leaf_mask, column] = (
                adjusted.loc[leaf_mask, column].to_numpy(dtype="float64") + adds
            )

    # Coherence: rebuild every parent from its adjusted leaves. Bottom-up is
    # used on purpose — a scenario must not be reconciled by a different
    # method than the base, or the comparison would mix two changes at once.
    keys = ["forecast_origin", "target_date"]
    leaves = adjusted[adjusted["entity_id"].isin(leaf_ids)]
    sums = (
        leaves.groupby(keys)[list(VALUE_COLUMNS)]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={column: f"_{column}" for column in VALUE_COLUMNS})
    )
    parent_mask = ~adjusted["entity_id"].isin(leaf_ids)
    rebuilt = adjusted.loc[parent_mask].drop(columns=list(VALUE_COLUMNS)).merge(
        sums, on=keys, how="left"
    )
    for column in VALUE_COLUMNS:
        rebuilt[column] = rebuilt[f"_{column}"]
    rebuilt = rebuilt.drop(columns=[f"_{column}" for column in VALUE_COLUMNS])
    adjusted = pd.concat(
        [adjusted.loc[~parent_mask], rebuilt], ignore_index=True, sort=False
    )

    adjusted = adjusted[adjusted["entity_id"].isin(leaf_ids | {total_id})]
    # Keep the interval bounds ordered around the new point forecast.
    adjusted["lower_80"] = adjusted[["lower_80", "reconciled_yhat"]].min(axis=1)
    adjusted["upper_80"] = adjusted[["upper_80", "reconciled_yhat"]].max(axis=1)
    adjusted["lower_90"] = adjusted[["lower_90", "lower_80"]].min(axis=1)
    adjusted["upper_90"] = adjusted[["upper_90", "upper_80"]].max(axis=1)
    adjusted["horizon"] = (
        pd.to_datetime(adjusted["target_date"])
        .sub(pd.to_datetime(adjusted["forecast_origin"]))
        .dt.days
    )
    adjusted["scenario_id"] = scenario.scenario_id
    adjusted["scenario_label"] = scenario.label
    adjusted["model_version"] = adjusted["model_version"].astype(object) + "__" + scenario.name
    adjusted = adjusted.sort_values(["entity_id", "target_date"]).reset_index(drop=True)
    _assert_coherent(adjusted, total_id)
    return adjusted


def _assert_coherent(frame: pd.DataFrame, total_id: str) -> None:
    keys = ["forecast_origin", "target_date"]
    totals = (
        frame[frame["entity_id"].eq(total_id)]
        .set_index(keys)["reconciled_yhat"]
    )
    leaf_sum = (
        frame[~frame["entity_id"].eq(total_id)]
        .groupby(keys)["reconciled_yhat"]
        .sum()
    )
    joined = pd.concat({"t": totals, "l": leaf_sum}, axis=1).dropna()
    if joined.empty:
        raise ScenarioError("Scenario output has no rows to verify")
    if not np.allclose(joined["t"], joined["l"]):
        raise ScenarioError("Scenario broke hierarchy coherence")


def comparison(
    base: pd.DataFrame,
    scenarios: list[pd.DataFrame],
    entity_master: EntityMaster | None = None,
) -> pd.DataFrame:
    """Base vs each scenario per entity, as deltas the user can read.

    Aggregates over the whole horizon rather than per day, because a scenario
    is a monthly planning question ("does a 20% budget lift close the gap?")
    and a 30-column daily table is unreadable.
    """
    _, total_id = _leaf_and_total_ids(base, entity_master)
    rows: list[dict] = []

    def _sum(frame: pd.DataFrame, entity_id: str) -> float:
        chunk = frame[frame["entity_id"].eq(entity_id)]
        return float(chunk["reconciled_yhat"].sum()) if not chunk.empty else 0.0

    base_totals = {
        entity_id: _sum(base, entity_id)
        for entity_id in sorted(base["entity_id"].unique())
    }
    rows.append(
        {
            "scenario": "base",
            "label": "پیش‌بینی پایه",
            **{
                entity_id: value for entity_id, value in base_totals.items()
            },
            "total": base_totals.get(total_id, 0.0),
        }
    )
    for frame in scenarios:
        if frame is None or frame.empty:
            continue
        scenario_id = str(frame["scenario_id"].iloc[0])
        label = (
            str(frame["scenario_label"].iloc[0])
            if "scenario_label" in frame.columns
            else scenario_id
        )
        totals = {
            entity_id: _sum(frame, entity_id)
            for entity_id in sorted(frame["entity_id"].unique())
        }
        rows.append(
            {
                "scenario": scenario_id,
                "label": label,
                **totals,
                "total": totals.get(total_id, 0.0),
                "delta": totals.get(total_id, 0.0) - base_totals.get(total_id, 0.0),
                "delta_share": (
                    (totals.get(total_id, 0.0) - base_totals.get(total_id, 0.0))
                    / base_totals[total_id]
                    if base_totals.get(total_id)
                    else 0.0
                ),
            }
        )
    return pd.DataFrame(rows)
