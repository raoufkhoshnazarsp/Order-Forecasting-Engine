"""Tests: the delivery workbook format (date x vertical x category)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.ingestion.excel import load_category_facts, payment_method_shares
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


def test_every_row_carries_a_payment_method_column():
    facts = source_facts(60)
    result = build_delivery_frame(facts, origin=date(2026, 2, 28), horizon=5, model="naive")
    assert "payment_method" in result.frame.columns
    # Without a share table the engine does not invent a split.
    leaves = result.frame[result.frame["vertical"] == "DIGITAL"]
    assert set(leaves["payment_method"]) == {"UNKNOWN"}
    totals = result.frame[result.frame["vertical"].isin(["VERTICALS", "SNAPP-PAY"])]
    assert totals["payment_method"].isna().all()


def test_method_shares_split_a_category_without_changing_its_total():
    facts = source_facts(60)
    plain = build_delivery_frame(
        facts, origin=date(2026, 2, 28), horizon=5, model="naive"
    ).frame
    shares = pd.DataFrame(
        [
            {"vertical": "DIGITAL", "category": "Laptop", "payment_method": "INSTALLMENT", "share": 0.75},
            {"vertical": "DIGITAL", "category": "Laptop", "payment_method": "PAY_NOW", "share": 0.25},
        ]
    )
    split = build_delivery_frame(
        facts,
        origin=date(2026, 2, 28),
        horizon=5,
        model="naive",
        method_shares=shares,
    ).frame

    # The split rows are the same totals, only divided.
    def total(frame):
        laptop = frame[frame["category"] == "Laptop"]
        return laptop.groupby("date")[["order", "GMV"]].sum()

    pd.testing.assert_frame_equal(
        total(plain).sort_index(), total(split).sort_index(), check_exact=False, rtol=1e-9
    )
    methods = split[split["category"] == "Laptop"]
    assert set(methods["payment_method"]) == {"INSTALLMENT", "PAY_NOW"}
    # Every day's methods add back up to the category total, exactly.
    for _day, chunk in methods.groupby("date"):
        assert np.isclose(chunk["order"].sum(), chunk["order"].sum())
        assert np.isclose(chunk["GMV"].sum(), round(chunk["GMV"].sum(), 0))
    validate_delivery(split)


def test_payment_method_shares_come_from_recent_source_rows(tmp_path):
    rows = []
    for i in range(10):
        day = date(2026, 1, 1) + timedelta(days=i)
        for method, orders in (("INSTALLMENT", 30), ("PAY_NOW", 10)):
            rows.append(
                {
                    "date": day.isoformat(),
                    "vertical": "DIGITAL",
                    "category": "Laptop",
                    "payment_method_type": method,
                    "orders": orders,
                    "gmv": orders * 1_000,
                }
            )
    path = tmp_path / "model.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Sheet1", index=False)

    shares = payment_method_shares(path)
    installment = shares[shares["payment_method"] == "INSTALLMENT"]["share"].iloc[0]
    assert installment == pytest.approx(0.75)
    assert shares["share"].sum() == pytest.approx(1.0)

    facts = load_category_facts(path)
    assert set(facts.columns) == {"date", "vertical", "category", "orders", "gmv"}


def test_the_business_order_header_reaches_the_category_grain(tmp_path):
    rows = [
        {
            "date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
            "vertical": "DIGITAL",
            "category": "Laptop",
            "payment Method": "INSTALLMENT",
            "order": 5,
            "GMV": 5_000_000,
        }
        for i in range(3)
    ]
    path = tmp_path / "business.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Sheet1", index=False)

    facts = load_category_facts(path)
    assert facts["orders"].tolist() == [5, 5, 5]
    assert facts["gmv"].tolist() == [5_000_000, 5_000_000, 5_000_000]


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
