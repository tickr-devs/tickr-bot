"""Shared test setup for the whole suite."""

import pytest

import tickr.cli as cli
import tickr.runtime.__main__ as runtime_main


@pytest.fixture(autouse=True)
def no_real_alert_channels(monkeypatch):
    """Tests never reach the machine's real alert channels.

    On macOS `default_notifier` returns the Mac notification and iMessage, so a test that
    raises an alert would show a real notification, try Messages.app (a real iMessage when
    the owner's recipient variable is set in the shell) and journal its delivery, which the
    tests, written for CI (no channel), do not expect. Every test runs as on CI: no channel.
    A test that needs channels builds them itself (tests/runtime/test_alerts.py)."""
    monkeypatch.setattr(cli, "NOTIFIER", lambda alerts: None)
    monkeypatch.setattr(runtime_main, "default_notifier", lambda *args, **kwargs: None)
