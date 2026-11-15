"""Tests: Excel ingestion, layout detection and the Excel -> forecast pipeline."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.ingestion.excel import (
    ExcelInputError,
    build_header_lookup,
    detect_layout,
    load_excel_config,
    load_workbook,
    map_columns,
    normalize_text,
    parse_dates,
    write_template,
)
from src.pipeline import PipelineError, entity_master_for_workbook, run_pipeline


def write_workbook(path, sheets: dict[str, pd.DataFrame]):
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return path


def daily_frame(days: int = 90, entities=(("FASHION", 120), ("GOLD", 40), ("V001", 25))):
    rows = []
    start = date(2026, 1, 1)
    for i in range(days):
        for entity, base in entities:
            rows.append(
                {
                    "date": (start + timedelta(days=i)).isoformat(),
                    "entity_id": entity,
                    "entity_type": "VENTURE" if entity.startswith("V") else "VERTICAL",
                    "successful_orders": base + (i % 7) * 3,
                }
            )
    return pd.DataFrame(rows)


def test_normalize_text_handles_persian_variants():
    assert normalize_text("تاریخ_سفارش") == "تاریخ سفارش"
    assert normalize_text("Order  ID") == "order id"
    assert normalize_text("ي") == "ی"
    assert normalize_text("ك") == "ک"


def test_header_lookup_accepts_persian_and_english():
    lookup = build_header_lookup(load_excel_config())
    mapped, ignored = map_columns(["تاریخ", "تعداد سفارش", "ستون عجیب"], lookup)
    assert mapped == {"date": "تاریخ", "successful_orders": "تعداد سفارش"}
    assert ignored == ["ستون عجیب"]


def test_layout_detection_prefers_required_columns():
    config = load_excel_config()
    lookup = build_header_lookup(config)
    mapped, _ = map_columns(["date", "entity_id", "successful_orders"], lookup)
    assert detect_layout(mapped, config) == "daily_aggregated"
    mapped, _ = map_columns(["date", "order_id", "venture_id"], lookup)
    assert detect_layout(mapped, config) == "raw_orders"
    mapped, _ = map_columns(["date", "something"], lookup)
    assert detect_layout(mapped, config) is None


def test_parse_dates_handles_persian_digits():
    parsed = parse_dates(pd.Series(["۱۴۰۵-۰۱-۰۱", "2026-03-21", "not a date"]))
    assert parsed.iloc[0] == date(2026, 3, 21)
    assert parsed.iloc[1] == date(2026, 3, 21)
    assert pd.isna(parsed.iloc[2])


def test_load_aggregated_workbook(tmp_path):
    path = write_workbook(tmp_path / "agg.xlsx", {"data": daily_frame(10)})
    loaded = load_workbook(path)
    assert len(loaded.facts) == 30
    assert set(loaded.facts["entity_id"]) == {"FASHION", "GOLD", "V001"}
    assert loaded.reports[0].layout == "daily_aggregated"


def test_load_raw_orders_workbook(tmp_path):
    raw = pd.DataFrame(
        [
            {"date": "2026-01-01", "order_id": "A1", "venture_id": "4711",
             "status": "SETTLE", "vertical": "FASHION", "category": "apparel"},
            {"date": "2026-01-01", "order_id": "A2", "venture_id": "4711",
             "status": "SETTLE", "vertical": "FASHION", "category": "apparel"},
            {"date": "2026-01-01", "order_id": "A3", "venture_id": "9001",
             "status": "SETTLE", "vertical": "Snapp-Venture", "category": "ventures"},
            {"date": "2026-01-01", "order_id": "A4", "venture_id": "4711",
             "status": "CANCELLED", "vertical": "FASHION", "category": "apparel"},
        ]
    )
    path = write_workbook(tmp_path / "raw.xlsx", {"orders": raw})
    loaded = load_workbook(path)
    facts = loaded.facts.set_index("entity_id")["successful_orders"]
    assert facts["FASHION"] == 2
    assert facts["9001"] == 1
    assert "A4" not in set(loaded.facts.get("order_id", []))
    assert any("non-successful" in w for w in loaded.warnings)


def test_raw_orders_deduplicates_order_ids(tmp_path):
    raw = pd.DataFrame(
        [
            {"date": "2026-01-01", "order_id": "A1", "venture_id": "1", "vertical": "FASHION"},
            {"date": "2026-01-01", "order_id": "A1", "venture_id": "1", "vertical": "FASHION"},
        ]
    )
    path = write_workbook(tmp_path / "dup.xlsx", {"orders": raw})
    loaded = load_workbook(path)
    assert loaded.facts["successful_orders"].sum() == 1


def test_unmapped_rows_are_kept_and_flagged(tmp_path):
    raw = pd.DataFrame(
        [
            {"date": "2026-01-01", "order_id": "A1", "venture_id": "1", "vertical": "FASHION"},
            {"date": "2026-01-01", "order_id": "A2", "venture_id": "2", "vertical": ""},
        ]
    )
    path = write_workbook(tmp_path / "unmapped.xlsx", {"orders": raw})
    loaded = load_workbook(path)
    assert "UNMAPPED" in set(loaded.facts["entity_type"])


def test_unsupported_workbook_raises(tmp_path):
    path = write_workbook(tmp_path / "bad.xlsx", {"x": pd.DataFrame({"a": [1], "b": [2]})})
    with pytest.raises(ExcelInputError):
        load_workbook(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ExcelInputError):
        load_workbook(tmp_path / "nope.xlsx")


def test_template_round_trips(tmp_path):
    path = write_template(tmp_path / "template.xlsx")
    loaded = load_workbook(path)
    assert not loaded.facts.empty
    assert {"VERTICAL", "VENTURE"} <= set(loaded.facts["entity_type"])


def test_entity_master_extends_with_workbook_ventures(tmp_path):
    path = write_workbook(tmp_path / "agg.xlsx", {"data": daily_frame(5)})
    loaded = load_workbook(path)
    master = entity_master_for_workbook(loaded)
    assert "V001" in master.leaf_ids()
    assert master.by_id("V001").entity_type == "VENTURE"


def test_entity_master_contains_only_entities_in_workbook(tmp_path):
    """Configured verticals absent from the file must not get invented zero rows."""
    path = write_workbook(tmp_path / "agg.xlsx", {"data": daily_frame(5)})
    loaded = load_workbook(path)
    master = entity_master_for_workbook(loaded)
    assert set(master.leaf_ids()) == {"FASHION", "GOLD", "V001"}
    assert "DIGITAL" not in master.leaf_ids()


def test_vertical_ids_are_mapped_to_stable_ids(tmp_path):
    frame = daily_frame(5, entities=(("H&B", 50), ("Home", 20)))
    path = write_workbook(tmp_path / "agg.xlsx", {"data": frame})
    result = run_pipeline(path, horizon=7, run_backtest=False)
    assert "H_B" in result.entities
    assert "HOME" in result.entities
    assert "H&B" not in result.entities


def test_pipeline_produces_coherent_forecast(tmp_path):
    path = write_workbook(tmp_path / "agg.xlsx", {"data": daily_frame(120)})
    result = run_pipeline(path, horizon=14, backtest_step=30, min_train_days=60)
    assert result.forecast is not None
    assert result.horizon == 14
    assert set(result.total_forecast) == set(range(1, 15))
    assert result.total_last_actual is not None
    assert result.model in {"naive", "seasonal_naive", "moving_average", "drift", "ets"}
    assert result.backtest_ranking  # ranking produced from real history

    forecast = result.forecast
    totals = forecast[forecast["entity_id"] == "TOTAL"].set_index("horizon")["reconciled_yhat"]
    leaves = (
        forecast[forecast["entity_id"].isin(["FASHION", "GOLD", "V001"])]
        .groupby("horizon")["reconciled_yhat"].sum()
    )
    pd.testing.assert_series_equal(totals.sort_index(), leaves.sort_index(), check_names=False)


def test_pipeline_rejects_workbook_without_entities(tmp_path):
    raw = pd.DataFrame(
        [{"date": "2026-01-01", "order_id": "A1", "venture_id": "1", "vertical": ""}]
    )
    path = write_workbook(tmp_path / "noent.xlsx", {"orders": raw})
    with pytest.raises(PipelineError):
        run_pipeline(path, horizon=7, run_backtest=False)


def test_pipeline_short_history_falls_back(tmp_path):
    path = write_workbook(tmp_path / "short.xlsx", {"data": daily_frame(10)})
    result = run_pipeline(path, horizon=7, min_train_days=60)
    assert result.model == "seasonal_naive"
    assert any("seasonal naive" in w for w in result.warnings)
