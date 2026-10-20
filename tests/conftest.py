"""Shared test setup.

Tests build the whole app (build_app), which configures logging to the real
data/veronica.log. That mixed test runs ("Booting VERONICA", fake GPS fixes from
127.0.0.1, "Greeting the user") into the user's own log, so reading it to find out why
she didn't answer showed events that never happened. Logging is marked as already
configured for the test session, so nothing is written there; pytest captures the lines.
"""
from __future__ import annotations

import veronica.logging_setup as logging_setup

logging_setup._CONFIGURED = True


import pytest


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch, request):
    """Unit tests never look at the real desktop (windows, tabs, Explorer): the per-request
    [DESKTOP NOW] block is empty unless a test supplies its own."""
    if getattr(request.module, "FAKE_DESKTOP_TESTS", False):      # the module drives Desktop with fakes itself
        return
    monkeypatch.setattr("veronica.vision.desktop_state.Desktop.describe", lambda self, *a, **k: "")
