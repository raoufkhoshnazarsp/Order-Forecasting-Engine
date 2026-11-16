"""Tests: Target Definition guards and SQL rendering safety."""

from __future__ import annotations

from datetime import date

import pytest

from src.ingestion import render
from src.validation.target import (
    TargetDefinition,
    TargetDefinitionError,
    render_vertical_list,
)


def draft_config(**overrides):
    base = {
        "target_definition": {
            "version": "target_v0_draft",
            "definition_status": "DRAFT",
            "order_status": "SETTLE",
            "merchant_type": "ONLINE",
            "exclude_deleted": None,
            "date_field": None,
            "payment_methods": "all",
            "reversal_policy": None,
            "organic_rule": {"field": "voucher_discount_amount"},
        }
    }
    base["target_definition"].update(overrides)
    return base


def test_draft_is_not_production_ready():
    target = TargetDefinition.from_config(draft_config())
    assert not target.is_production_ready()
    assert set(target.unresolved_decisions()) == {
        "exclude_deleted",
        "date_field",
        "reversal_policy",
    }
    with pytest.raises(TargetDefinitionError):
        target.assert_production_ready()


def test_approved_definition_is_ready():
    target = TargetDefinition.from_config(
        draft_config(
            definition_status="APPROVED",
            exclude_deleted=False,
            date_field="registered_date",
            reversal_policy="keep",
        )
    )
    assert target.is_production_ready()
    target.assert_production_ready()


def test_unknown_date_field_rejected():
    with pytest.raises(TargetDefinitionError):
        TargetDefinition.from_config(draft_config(date_field="paid_at"))


def test_status_and_scope_predicates():
    target = TargetDefinition.from_config(
        draft_config(exclude_deleted=True, payment_methods=["CASH", "CREDIT"])
    )
    assert target.status_predicate() == "O.status = 'SETTLE'"
    predicates = target.scope_predicates()
    assert "O.merchant_type = 'ONLINE'" in predicates
    assert "O.deleted_at IS NULL" in predicates
    assert "O.payment_method_type IN ('CASH', 'CREDIT')" in predicates


def test_scope_filters_never_include_commercial_exclusions():
    """Commercial-only exclusions must not leak into the engine target."""
    target = TargetDefinition.from_config(draft_config())
    rendered = render.render_predicates(target.scope_predicates())
    assert "Snapp-Venture" not in rendered
    assert "in_store" not in rendered


def test_approved_decisions_render_into_the_query():
    """The three business decisions must be visible in the generated SQL."""
    target = TargetDefinition.from_config(
        draft_config(
            definition_status="APPROVED",
            exclude_deleted=True,
            date_field="registered_date",
            reversal_policy="exclude",
        )
    )
    assert target.is_production_ready()
    predicates = target.scope_predicates()
    assert "O.deleted_at IS NULL" in predicates
    assert any("O.status NOT IN (" in p for p in predicates)
    assert target.date_field_for_draft() == "registered_date"


def test_reversal_keep_emits_no_reversal_predicate():
    target = TargetDefinition.from_config(
        draft_config(reversal_policy="keep", exclude_deleted=False)
    )
    assert not any("NOT IN" in p for p in target.scope_predicates())


def test_unknown_reversal_policy_is_rejected():
    with pytest.raises(TargetDefinitionError):
        TargetDefinition.from_config(draft_config(reversal_policy="maybe"))


def test_shipped_config_is_approved_and_production_ready():
    """The committed target.yaml must carry the approved business decisions."""
    from src import config

    target = TargetDefinition.from_config(config.load_yaml("target.yaml"))
    assert target.status == "APPROVED"
    assert target.is_production_ready()
    assert target.exclude_deleted is True
    assert target.date_field == "registered_date"
    assert target.reversal_policy == "exclude"


def test_vertical_list_rendering_escapes_quotes():
    assert render_vertical_list(["DIGITAL", "H&B"]) == "'DIGITAL', 'H&B'"
    with pytest.raises(TargetDefinitionError):
        render_vertical_list([])


def test_template_renders_dates_and_rejects_bad_date():
    template = "SELECT * FROM t WHERE d >= toDate('{{start_date}}') AND d < toDate('{{end_date}}')"
    sql = render.render_template(template, start_date=date(2026, 1, 1), end_date=date(2026, 2, 1))
    assert "'2026-01-01'" in sql and "'2026-02-01'" in sql
    with pytest.raises(render.SQLRenderError):
        render.render_template(template, start_date="2026-1-1", end_date="2026-02-01")


def test_missing_placeholder_raises():
    with pytest.raises(render.SQLRenderError):
        render.render_template(
            "SELECT {{unknown_thing}}", start_date="2026-01-01", end_date="2026-02-01"
        )


def test_predicate_injection_is_rejected():
    with pytest.raises(render.SQLRenderError):
        render.render_predicates(["O.status = 'X'; DROP TABLE fms_credit.tbl_order"])
    with pytest.raises(render.SQLRenderError):
        render.render_predicates(["O.secret_column = 1"])


def test_column_whitelist():
    assert render.validate_column("registered_date") == "registered_date"
    with pytest.raises(render.SQLRenderError):
        render.validate_column("amount; DROP")
    with pytest.raises(render.SQLRenderError):
        render.validate_column("user_password")
