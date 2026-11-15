"""Tests: the three pages are one product, not three islands.

Covers the shared sidebar, the active-page marker, the cross-page links, the
no-data guidance, and the last-run memory that lets one page report what the
other page computed.
"""

from __future__ import annotations

import json
import re

import pytest

from src.serving import api, state

PAGES = ("/", "/comparison")


def page(path: str) -> str:
    return api.index().body.decode() if path == "/" else api.comparison_page().body.decode()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    yield


def test_every_page_links_to_every_other_page():
    for path in PAGES:
        html = page(path)
        assert 'href="/"' in html
        assert 'href="/comparison"' in html
        assert 'href="/report/"' in html
        assert 'href="/api/docs"' in html


def test_exactly_one_page_is_marked_current():
    for path in PAGES:
        html = page(path)
        assert html.count('aria-current="page"') == 1, path


def test_the_current_marker_is_on_the_right_link():
    home = page("/")
    assert re.search(r'<a href="/" aria-current="page"', home)
    comparison = page("/comparison")
    assert re.search(r'<a href="/comparison" aria-current="page"', comparison)


def test_sidebar_is_shared_not_duplicated():
    # The partial is expanded at render time, so the raw include must be gone.
    for path in PAGES:
        html = page(path)
        assert "{{>" not in html
        assert "{{side_foot}}" not in html
        assert "{{cur_home}}" not in html


def test_pages_guide_the_user_when_there_is_no_data():
    for path in PAGES:
        html = page(path)
        assert 'id="guide"' in html
        assert "بررسی کیفیت داده" in html


def test_pages_show_a_last_run_strip():
    for path in PAGES:
        assert 'id="last-run"' in page(path)


def test_status_is_empty_before_any_run():
    assert api.status() == {"last_run": None}


def test_status_returns_the_recorded_run():
    state.record_run("forecast", {"file": "data.xlsx", "horizon": 33, "model": "ets"})
    payload = api.status()
    assert payload["last_run"]["kind"] == "forecast"
    assert payload["last_run"]["file"] == "data.xlsx"
    assert payload["last_run"]["horizon"] == 33
    assert payload["last_run"]["at"]


def test_recording_never_raises_on_a_bad_directory(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("FORECAST_STATE_DIR", str(blocker / "nested"))
    entry = state.record_run("forecast", {"file": "a.xlsx"})
    assert entry["file"] == "a.xlsx"
    assert state.last_run() is None


def test_corrupt_state_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    (tmp_path / state.FILE_NAME).write_text("{not json", encoding="utf-8")
    assert state.last_run() is None


def test_state_file_holds_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path))
    state.record_run("comparison", {"file": "data.xlsx", "next_month": "2026-11"})
    saved = json.loads((tmp_path / state.FILE_NAME).read_text(encoding="utf-8"))
    assert set(saved) == {"kind", "at", "file", "next_month"}


def test_display_name_strips_the_timestamp_prefix():
    from pathlib import Path

    assert api._display_name(Path("20261115_101500_data-model.xlsx")) == "data-model.xlsx"
    assert api._display_name(Path("plain.xlsx")) == "plain.xlsx"
