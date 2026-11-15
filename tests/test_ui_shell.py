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
