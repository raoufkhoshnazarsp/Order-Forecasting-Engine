"""Tests: SQL contract — templates parse/placeholders/separation of concerns."""

from __future__ import annotations

import re
from datetime import date

import pytest

from src import config
from src.ingestion.extract import DAILY_LEAF_ORDERS, build_query
from src.validation.target import TargetDefinition

TEMPLATE_FILES = [
    "extraction/v1/daily_leaf_orders.sql",
    "extraction/v1/daily_leaf_users.sql",
    "extraction/v1/calendar_dates.sql",
]

DISCOVERY_FILES = [
    "discovery/01_describe_tables.sql",
    "discovery/02_date_fields.sql",
    "discovery/03_status_and_deletion.sql",
    "discovery/03b_deletion_behavior.sql",
    "discovery/04_scope_fields.sql",
    "discovery/05_commercial_mapping.sql",
    "discovery/06_venture_coverage.sql",
]


@pytest.mark.parametrize("path", TEMPLATE_FILES + DISCOVERY_FILES)
def test_sql_files_exist_and_have_queries(path):
    sql = config.load_sql(path)
    assert sql.strip()
    assert re.search(r"\bSELECT\b|\bDESCRIBE\b|\bSHOW\b", sql, re.IGNORECASE)


@pytest.mark.parametrize("path", TEMPLATE_FILES)
def test_templates_use_half_open_interval(path):
    sql = config.load_sql(path)
    assert ">= toDate('{{start_date}}')" in sql
    assert "<  toDate('{{end_date}}')" in sql
    assert "BETWEEN" not in sql.upper()


def test_no_select_star_in_extraction():
    for path in ("extraction/v1/daily_leaf_orders.sql", "extraction/v1/daily_leaf_users.sql"):
        sql = config.load_sql(path)
        assert not re.search(r"\bSELECT\s+\*", sql, re.IGNORECASE), path


def test_leaf_query_never_produces_total():
    sql = config.load_sql("extraction/v1/daily_leaf_orders.sql")
    assert "'TOTAL'" not in sql
    assert "entity_type IN ('VERTICAL', 'VENTURE')" not in sql.split("FROM classified_orders")[0]


def test_commercial_exclusions_are_commented_out_only():
    """The Commercial-only scope filters must not be active filters."""
    target = TargetDefinition.from_config(config.load_yaml("target.yaml"))
    sql = build_query(
        DAILY_LEAF_ORDERS,
        target,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 2, 1),
        verticals=config.load_yaml("target.yaml")["scope"]["verticals"],
        venture_vertical_value="Snapp-Venture",
        venture_category_value="ventures",
    )
    assert "Snapp-Venture" in sql  # classification rule only
    assert "in_store" not in sql
    assert "social" not in sql


def test_build_query_rejects_reversed_interval():
    target = TargetDefinition.from_config(config.load_yaml("target.yaml"))
    with pytest.raises(ValueError):
        build_query(
            DAILY_LEAF_ORDERS,
            target,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 1, 1),
            verticals=["FASHION"],
            venture_vertical_value="Snapp-Venture",
            venture_category_value="ventures",
        )


def test_build_query_applies_approved_status():
    target = TargetDefinition.from_config(config.load_yaml("target.yaml"))
    sql = build_query(
        DAILY_LEAF_ORDERS,
        target,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 2, 1),
        verticals=config.load_yaml("target.yaml")["scope"]["verticals"],
        venture_vertical_value="Snapp-Venture",
        venture_category_value="ventures",
    )
    assert "O.status = 'SETTLE'" in sql
    assert "{{" not in sql
