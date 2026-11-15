"""Tests: the pre-flight data quality gate."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.serving.quality import (
    FAIL,
    PASS,
    WARN,
    QualityGateError,
    check_workbook,
    require_quality,
)

VERTICALS = {
    "DIGITAL": ["Laptop", "Mobile"],
    "FASHION": ["RW"],
    "FMCG": ["Pet Care"],
    "GOLD": ["Gold"],
    "H&B": ["Pharmacy"],
    "Home": ["AT"],
    "Services": ["Service"],
}


def write_workbook(path, days=150, start=date(2026, 1, 1), mutate=None):
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        for vertical, categories in VERTICALS.items():
            for category in categories:
                rows.append(
                    {
                        "vertical": vertical,
                        "Date": day.isoformat(),
                        "category": category,
                        "payment_method_type": "INSTALLMENT",
                        "Orders": 100,
                        "GMV": 50_000_000,
                    }
                )
    frame = pd.DataFrame(rows)
    if mutate is not None:
        frame = mutate(frame)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Sheet1", index=False)
    return path


def by_id(report):
    return {c.id: c for c in report.checks}


def test_clean_workbook_passes(tmp_path):
    path = write_workbook(tmp_path / "clean.xlsx")
    report = check_workbook(path, horizon=30)
    assert report.status == PASS
    assert not report.blocked
    assert report.days == 150
    assert report.origin == date(2026, 5, 30)


def test_negative_orders_block_the_run(tmp_path):
    def mutate(frame):
        frame.loc[0, "Orders"] = -5
        return frame

    path = write_workbook(tmp_path / "neg.xlsx", mutate=mutate)
    report = check_workbook(path)
    assert report.status == FAIL
    assert report.blocked
    assert by_id(report)["negatives"].status == FAIL


def test_duplicate_keys_warn_but_do_not_block(tmp_path):
    def mutate(frame):
        return pd.concat([frame, frame.iloc[[0]]], ignore_index=True)

    path = write_workbook(tmp_path / "dup.xlsx", mutate=mutate)
    report = check_workbook(path)
    assert by_id(report)["duplicates"].status == WARN
    assert not report.blocked


def test_large_gap_blocks_the_run(tmp_path):
    def mutate(frame):
        return pd.concat(
            [frame[frame["Date"] < "2026-02-01"], frame[frame["Date"] > "2026-03-15"]],
            ignore_index=True,
        )

    path = write_workbook(tmp_path / "gap.xlsx", mutate=mutate)
    report = check_workbook(path)
    assert by_id(report)["continuity"].status == FAIL


def test_short_history_warns(tmp_path):
    path = write_workbook(tmp_path / "short.xlsx", days=60)
    report = check_workbook(path, horizon=30)
    assert by_id(report)["history"].status == WARN
    assert not report.blocked


def test_missing_vertical_warns(tmp_path):
    def mutate(frame):
        return frame[frame["vertical"] != "GOLD"]

    path = write_workbook(tmp_path / "novgold.xlsx", mutate=mutate)
    report = check_workbook(path)
    check = by_id(report)["verticals"]
    assert check.status == WARN
    assert "GOLD" in check.detail


def test_partial_last_day_is_detected(tmp_path):
    def mutate(frame):
        last = frame["Date"].max()
        frame.loc[frame["Date"] == last, "Orders"] = 10
        return frame

    path = write_workbook(tmp_path / "partial.xlsx", days=60, mutate=mutate)
    report = check_workbook(path)
    assert by_id(report)["partial_day"].status == WARN


def test_swapped_orders_and_gmv_warns(tmp_path):
    def mutate(frame):
        frame.loc[0, "GMV"] = 1
        return frame

    path = write_workbook(tmp_path / "swap.xlsx", mutate=mutate)
    report = check_workbook(path)
    assert by_id(report)["aov"].status == WARN


def test_unreadable_workbook_fails_cleanly(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not an excel file")
    report = check_workbook(path)
    assert report.status == FAIL
    assert report.checks[0].id == "load"


def test_require_quality_needs_the_matching_token(tmp_path):
    path = write_workbook(tmp_path / "ok.xlsx")
    report = check_workbook(path)

    with pytest.raises(QualityGateError):
        require_quality(path, None)
    with pytest.raises(QualityGateError):
        require_quality(path, "wrong-token")

    approved = require_quality(path, report.token)
    assert approved.token == report.token


def test_require_quality_refuses_a_blocked_file(tmp_path):
    def mutate(frame):
        frame.loc[0, "Orders"] = -1
        return frame

    path = write_workbook(tmp_path / "bad.xlsx", mutate=mutate)
    report = check_workbook(path)
    with pytest.raises(QualityGateError):
        require_quality(path, report.token)


def test_token_changes_when_the_file_changes(tmp_path):
    first = write_workbook(tmp_path / "a.xlsx")
    second = write_workbook(tmp_path / "b.xlsx", days=151)
    assert check_workbook(first).token != check_workbook(second).token
