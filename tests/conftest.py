"""Shared test setup for the whole suite."""

import shutil
from pathlib import Path

import pytest

import tickr.cli as cli
import tickr.runtime.__main__ as runtime_main
import tickr.version as version


@pytest.fixture(autouse=True)
def no_real_alert_channels(monkeypatch):
    """Tests never reach the machine's real alert channels.

    Once the owner's Telegram variables are set in the shell, `default_notifier` returns the
    Telegram channel, so a test that raises an alert would send a real message and journal its
    delivery, which the tests, written for CI (no channel), do not expect. Every test runs as
    on CI: no channel.
    A test that needs channels builds them itself (tests/runtime/test_alerts.py)."""
    monkeypatch.setattr(cli, "NOTIFIER", lambda cfg: None)
    monkeypatch.setattr(runtime_main, "default_notifier", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def watchdog_runs_one_day(monkeypatch):
    """`python -m tickr.runtime watchdog` runs until stopped; in tests it returns after one
    New York day (and does not wait after an invalid config), and the disk it watches always
    has room (a test that checks the disk alert sets its own)."""
    monkeypatch.setattr(runtime_main, "WATCHDOG_DAYS", 1)
    monkeypatch.setattr(runtime_main, "WATCHDOG_HOLD", lambda again: None)
    monkeypatch.setattr(runtime_main, "WATCHDOG_DISK_FREE", lambda path: 1 << 50)
    monkeypatch.setattr(cli, "DISK_FREE", lambda path: 1 << 50)  # `tickr run` too


# The code version a test bot run records (tickr/version.py): tests run the code as if from a
# folder with no `.git`, as in the Docker image, so it comes from the environment and does not
# depend on the checkout's commit or uncommitted changes.
TEST_CODE_VERSION = "0123456789abcdef0123456789abcdef01234567"


# The pinned sign-in provider key, a file of the checkout ([api] remote_signin_key_file).
SIGNIN_KEY = Path("config") / "signin" / "tickr-bot-provider.pem"


@pytest.fixture(scope="session")
def _no_git_code_root(tmp_path_factory):
    """A checkout with no `.git`, as in the Docker image, that carries the files the API reads
    from the code's checkout: the pinned sign-in key."""
    root = tmp_path_factory.mktemp("code-root")
    (root / SIGNIN_KEY).parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / SIGNIN_KEY, root / SIGNIN_KEY)
    return root


@pytest.fixture(autouse=True)
def code_version_in_environment(monkeypatch, _no_git_code_root) -> str:
    """Every bot run is tagged with a code version or refuses to start; tests that check the
    refusal remove the variable themselves. Returns the version a test run records (request
    this fixture by name to compare with it)."""
    monkeypatch.setattr(version, "CODE_ROOT", _no_git_code_root)
    monkeypatch.setenv("TICKR_CODE_VERSION", TEST_CODE_VERSION)
    return TEST_CODE_VERSION
