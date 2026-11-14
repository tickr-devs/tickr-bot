from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aperture.config import AlertsConfig
from aperture.runtime.alerts import (
    OSASCRIPT,
    Alerter,
    AlertKind,
    Delivery,
    MacNotifier,
    NotifyError,
    default_notifier,
)

CONFIG = AlertsConfig(dedupe_window_seconds=Decimal(600), notification_timeout_seconds=Decimal(5))
T0 = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.shown: list[tuple[str, str]] = []
        self.fail = fail

    def notify(self, title: str, text: str) -> None:
        if self.fail:
            raise NotifyError("boom")
        self.shown.append((title, text))


def test_kinds_cover_spec_section_11():
    assert {k.value for k in AlertKind} == {
        "unprotected-position", "stop-rejected", "unknown-order-status", "foreign-exposure",
        "broker-reads-failing", "stream-lost-with-position", "daily-ceiling-reached",
        "bot-crashed", "bot-did-not-start", "bot-did-not-stop", "preflight-failed",
        "token-needs-confirmation",
    }


def test_repeat_within_window_is_suppressed_then_shown_again():
    clock, notifier = Clock(), Recorder()
    alerter = Alerter(CONFIG, "Aperture x", notifier, clock)
    first = alerter.alert(AlertKind.STOP_REJECTED, "ABC stop rejected")
    assert first.delivery is Delivery.SHOWN
    assert notifier.shown == [("Aperture x: stop-rejected", "ABC stop rejected")]
    clock.now = T0 + timedelta(seconds=599)
    assert alerter.alert(AlertKind.STOP_REJECTED, "ABC stop rejected").delivery is (
        Delivery.SUPPRESSED_REPEAT)
    # Different text or kind is not a repeat.
    assert alerter.alert(AlertKind.STOP_REJECTED, "XYZ stop rejected").delivery is Delivery.SHOWN
    assert alerter.alert(AlertKind.FOREIGN_EXPOSURE, "ABC stop rejected").delivery is (
        Delivery.SHOWN)
    clock.now = T0 + timedelta(seconds=600)
    assert alerter.alert(AlertKind.STOP_REJECTED, "ABC stop rejected").delivery is Delivery.SHOWN
    assert len(notifier.shown) == 4


def test_failed_delivery_is_reported_not_raised_and_not_deduped():
    alerter = Alerter(CONFIG, "t", Recorder(fail=True), Clock())
    outcome = alerter.alert(AlertKind.BOT_CRASHED, "bot exited with code 1")
    assert outcome.delivery is Delivery.FAILED and outcome.error == "boom"
    assert alerter.alert(AlertKind.BOT_CRASHED, "bot exited with code 1").delivery is (
        Delivery.FAILED)


def test_no_notifier_still_returns_outcome_for_journal():
    outcome = Alerter(CONFIG, "t", None, Clock()).alert(AlertKind.PREFLIGHT_FAILED, "disk")
    assert outcome.journal_body() == {"kind": "preflight-failed", "text": "disk",
                                      "at": T0.isoformat(), "delivery": "no-notifier",
                                      "error": None}


def test_kind_must_be_typed():
    with pytest.raises(TypeError):
        Alerter(CONFIG, "t", None, Clock()).alert("bot-crashed", "x")  # type: ignore[arg-type]


def test_osascript_gets_text_as_arguments_not_script():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    text = 'quote " and \\ end tell'
    MacNotifier(Decimal(5), runner).notify("Title", text)
    argv, kwargs = calls[0]
    assert argv[0] == OSASCRIPT
    assert argv[-2:] == ["Title", text]
    assert all(text not in part for part in argv[:-1])
    assert kwargs["timeout"] == 5.0 and kwargs["check"] is False


@pytest.mark.parametrize("effect", ["exit", "timeout", "missing"])
def test_osascript_failures_become_notify_error(effect):
    def runner(argv, **kwargs):
        if effect == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        if effect == "missing":
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 1)

    with pytest.raises(NotifyError):
        MacNotifier(Decimal(5), runner).notify("t", "x")


def test_default_notifier_by_platform():
    assert isinstance(default_notifier(CONFIG, "darwin"), MacNotifier)
    assert default_notifier(CONFIG, "linux") is None
