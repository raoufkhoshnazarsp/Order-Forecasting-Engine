"""Shared test setup.

The last-run memory is written to disk by the API. Tests must never touch the
real artifacts directory, so every test gets its own throwaway state dir.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_run_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path / "state"))
