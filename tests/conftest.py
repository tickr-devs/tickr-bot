"""Shared test setup for the whole suite."""

import pytest

import tickr.cli as cli
import tickr.runtime.__main__ as runtime_main
import tickr.version as version


@pytest.fixture(autouse=True)
def no_real_alert_channels(monkeypatch):
    """Tests never reach the machine's real alert channels.

    On macOS `default_notifier` returns the Mac notification and iMessage, so a test that
    raises an alert would show a real notification, try Messages.app (a real iMessage when
    the owner's recipient variable is set in the shell) and journal its delivery, which the
    tests, written for CI (no channel), do not expect. Every test runs as on CI: no channel.
    A test that needs channels builds them itself (tests/runtime/test_alerts.py)."""
    monkeypatch.setattr(cli, "NOTIFIER", lambda cfg: None)
    monkeypatch.setattr(runtime_main, "default_notifier", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def watchdog_runs_one_day(monkeypatch):
    """`python -m tickr.runtime watchdog` runs until stopped; in tests it returns after one
    New York day (and does not wait after an invalid config), and the disk it watches always
    has room (a test that checks the disk alert sets its own)."""
    monkeypatch.setattr(runtime_main, "WATCHDOG_DAYS", 1)
    monkeypatch.setattr(runtime_main, "WATCHDOG_HOLD", lambda: None)
    monkeypatch.setattr(runtime_main, "WATCHDOG_DISK_FREE", lambda path: 1 << 50)


# The code version a test bot run records (tickr/version.py): tests run the code as if from a
# folder with no `.git`, as in the Docker image, so it comes from the environment and does not
# depend on the checkout's commit or uncommitted changes.
TEST_CODE_VERSION = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(scope="session")
def _no_git_code_root(tmp_path_factory):
    return tmp_path_factory.mktemp("code-root")


@pytest.fixture(autouse=True)
def code_version_in_environment(monkeypatch, _no_git_code_root) -> str:
    """Every bot run is tagged with a code version or refuses to start; tests that check the
    refusal remove the variable themselves. Returns the version a test run records (request
    this fixture by name to compare with it)."""
    monkeypatch.setattr(version, "CODE_ROOT", _no_git_code_root)
    monkeypatch.setenv("TICKR_CODE_VERSION", TEST_CODE_VERSION)
    return TEST_CODE_VERSION
