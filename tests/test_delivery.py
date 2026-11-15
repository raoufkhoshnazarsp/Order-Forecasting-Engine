"""Tests: the delivery workbook format (date x vertical x category)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.ingestion.excel import load_category_facts
from src.serving.delivery import (
    OUTPUT_COLUMNS,
    DeliveryError,
    build_delivery_frame,
    select_model,
    validate_delivery,
    write_delivery,
)

VERTICALS = {
    "DIGITAL": ["Laptop", "Mobile"],
    "FASHION": ["RW", "Accessories"],
    "FMCG": ["Pet Care"],
    "GOLD": ["Gold"],
    "H&B": ["Pharmacy"],
    "Home": ["AT", "HW"],
    "Services": ["Service"],
    "Snapp-Venture": ["ventures"],
}


def source_facts(days: int = 90, start: date = date(2026, 1, 1)) -> pd.DataFrame:
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        for vertical, categories in VERTICALS.items():
            for j, category in enumerate(categories):
                base = 100 + 10 * j
                rows.append(
                    {
                        "date": day,
                        "vertical": vertical,
                        "category": category,
                        "orders": float(base),
                        "gmv": float(base * 500_000),
                    }
                )
    return pd.DataFrame(rows)


def test_delivery_has_the_exact_columns_and_grain():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=30, model="naive")
    frame = result.frame

    assert list(frame.columns) == OUTPUT_COLUMNS
    assert (frame["level"] == "vertical").all()
    assert frame["date"].nunique() == 30
    assert frame["date"].min() == date(2026, 3, 1)
    assert frame["date"].max() == date(2026, 3, 30)


def test_verticals_row_is_the_exact_sum_of_its_categories():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=10, model="naive")
    validate_delivery(result.frame)

    for day, chunk in result.frame.groupby("date"):
        total = chunk[chunk["vertical"] == "VERTICALS"].iloc[0]
        core = chunk[
            chunk["vertical"].isin(
                ["DIGITAL", "FASHION", "FMCG", "GOLD", "H&B", "HOME", "SERVICES"]
            )
        ]
        assert np.isclose(total["order"], core["order"].sum())
        assert np.isclose(total["GMV"], core["GMV"].sum())


def test_venture_is_a_sibling_not_a_child_of_verticals():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=5, model="naive")
    frame = result.frame

    venture = frame[frame["vertical"] == "SNAPP-VENTURE"]
    assert not venture.empty
    assert set(venture["category"].dropna()) == {"ventures"}

    # The venture block must not be counted inside VERTICALS.
    day = frame["date"].min()
    chunk = frame[frame["date"] == day]
    total = chunk[chunk["vertical"] == "VERTICALS"]["order"].iloc[0]
    venture_orders = chunk[chunk["vertical"] == "SNAPP-VENTURE"]["order"].iloc[0]
    assert total < total + venture_orders
    assert not np.isclose(total, total + venture_orders)


def test_venture_appears_exactly_once_per_day():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=5, model="naive")
    # Venture and Pay are single blocks; the seven verticals carry one row
    # per category, so only those may repeat within a day.
    for vertical in ("SNAPP-VENTURE", "SNAPP-PAY", "VERTICALS"):
        counts = result.frame[result.frame["vertical"] == vertical].groupby("date").size()
        assert (counts == 1).all(), vertical


def test_missing_snapp_pay_is_written_as_zero_with_a_warning():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=5, model="naive")
    pay = result.frame[result.frame["vertical"] == "SNAPP-PAY"]
    assert len(pay) == 5
    assert (pay["order"] == 0).all()
    assert any("Snapp-Pay" in w for w in result.warnings)


def test_gmv_equals_orders_times_aov_per_category():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=5, model="naive")
    leaves = result.frame[result.frame["category"].notna()]
    # Source AOV is a flat 500,000, so the forecast must reproduce it.
    implied = leaves["GMV"] / leaves["order"].replace(0, np.nan)
    assert np.allclose(implied.dropna(), 500_000, rtol=1e-6)


def test_validate_rejects_a_broken_total():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=5, model="naive")
    frame = result.frame.copy()
    frame.loc[frame["vertical"] == "VERTICALS", "order"] *= 2
    with pytest.raises(DeliveryError):
        validate_delivery(frame)


def test_validate_rejects_negative_values():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=5, model="naive")
    frame = result.frame.copy()
    frame.loc[frame.index[0], "order"] = -1
    with pytest.raises(DeliveryError):
        validate_delivery(frame)


def test_select_model_returns_a_ranking():
    facts = source_facts(120)
    model, ranking = select_model(facts, horizon=14, step=30, min_train_days=45)
    assert model in {r["model"] for r in ranking}
    assert ranking == sorted(ranking, key=lambda r: r["wape"])


def test_write_delivery_round_trips(tmp_path):
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=7, model="naive")
    path = write_delivery(result.frame, tmp_path / "out.xlsx")
    back = pd.read_excel(path)
    assert list(back.columns) == OUTPUT_COLUMNS
    assert len(back) == len(result.frame)
    assert pd.api.types.is_datetime64_any_dtype(back["date"])


def test_load_category_facts_keeps_the_category_grain(tmp_path):
    rows = []
    for i in range(10):
        day = date(2026, 1, 1) + timedelta(days=i)
        for category in ("Laptop", "Mobile"):
            for method in ("INSTALLMENT", "PAY_NOW"):
                rows.append(
                    {
                        "vertical": "DIGITAL",
                        "Date": day.isoformat(),
                        "category": category,
                        "payment_method_type": method,
                        "Orders": 10,
                        "GMV": 5_000_000,
                    }
                )
    frame = pd.DataFrame(rows)
    path = tmp_path / "model.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Sheet1", index=False)

    facts = load_category_facts(path)
    assert set(facts.columns) == {"date", "vertical", "category", "orders", "gmv"}
    # Two payment methods collapse; the two categories stay separate.
    assert len(facts) == 20
    assert facts["orders"].eq(20).all()
    assert facts["gmv"].eq(10_000_000).all()
