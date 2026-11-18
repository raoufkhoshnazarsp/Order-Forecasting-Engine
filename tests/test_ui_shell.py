"""Tests: the UI shell stays consistent (no permanently visible progress, spacing scale)."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "serving" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "serving" / "templates"


def test_hidden_attribute_always_wins_over_component_display():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css)


def test_progress_indicator_starts_hidden():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<div[^>]*id="progress"[^>]*>', html)
    assert match, "progress block missing"
    assert "hidden" in match.group(0)


def test_progress_is_only_toggled_by_setbusy():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function setBusy" in js
    # The progress element must never be un-hidden outside setBusy.
    toggles = re.findall(r"progress\.hidden\s*=\s*([^;]+);", js)
    assert toggles == ["!on"]


def test_spacing_scale_is_defined_and_used():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    for token in ("--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6"):
        assert token + ":" in css, token
    assert "gap: var(--sp-5)" in css


def test_the_overview_follows_the_design_frame():
    """Frame 00 (starts and run history) is the contract for the landing page."""
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    for text in (
        "شروع و سابقهٔ اجراها",
        "از دادهٔ امروز، برای تصمیم‌های فردا.",
        "هر پیش‌بینی، یک تصمیم مطمئن‌تر",
        "شروع با فایل اکسل",
        "یا امتحان با دادهٔ نمونه",
        "اجراهای اخیر",
        "نمایش همه",
        "نام فایل و اجرا",
        "موتور آماده",
    ):
        assert text in html, text
    assert 'class="hero-card"' in html
    assert 'id="history-table"' in html
    # The history table has the design's five columns, not the old seven.
    header = re.search(r'id="history-table".*?</thead>', html, re.S).group(0)
    assert header.count("<th scope") == 5


def test_the_design_palette_and_type_are_the_only_ones():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    for token, value in (
        ("--bg", "#f6f8fa"),
        ("--ink", "#172b32"),
        ("--text-muted", "#667a82"),
        ("--border", "#e4ebee"),
        ("--green", "#087f6b"),
        ("--green-soft", "#e8f5f0"),
        ("--rail", "#102c35"),
    ):
        assert f"{token}: {value};" in root, token
    assert "--font: \"Vazirmatn\"" in root
    # The design's 240px navigation rail.
    assert "grid-template-columns: 240px minmax(0, 1fr)" in css
