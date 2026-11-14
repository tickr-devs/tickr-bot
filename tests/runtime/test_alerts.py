from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aperture.config import AlertsConfig
from aperture.runtime.alerts import (
    IMESSAGE,
    MACOS,
    OSASCRIPT,
    Alerter,
    AlertKind,
    Channels,
    Delivery,
    IMessageNotifier,
    MacNotifier,
    NotifyError,
    default_notifier,
    recipient_missing,
)

CONFIG = AlertsConfig(dedupe_window_seconds=Decimal(600), notification_timeout_seconds=Decimal(5),
                      fence_alert_after_seconds=Decimal(30),
                      imessage_recipient_variable="APERTURE_TEST_TO")
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
        "token-needs-confirmation", "exit-refused", "observers-stopped",
        "not-flat-after-flatten", "watchdog-not-flat", "watchdog-cannot-check",
        "bot-down-with-open-trade", "exit-not-filled",
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
    assert isinstance(default_notifier(CONFIG, "darwin", {}), Channels)
    assert default_notifier(CONFIG, "linux") is None


RECIPIENT = "+15550001234-canary"


def _capture(code: int = 0):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code)

    return calls, runner


def test_imessage_passes_the_variable_name_and_text_as_argv_never_the_recipient():
    calls, runner = _capture()
    text = 'quote " and \\ end tell'
    IMessageNotifier(CONFIG, {"APERTURE_TEST_TO": RECIPIENT}, runner).notify("Title", text)
    (argv,) = calls
    assert argv[0] == OSASCRIPT
    assert argv[-2:] == ["APERTURE_TEST_TO", f"Title\n{text}"]
    assert all(text not in part for part in argv[:-1])
    assert RECIPIENT not in " ".join(argv)
    assert "system attribute (item 1 of argv)" in " ".join(argv)


def test_imessage_refuses_without_the_recipient_variable():
    calls, runner = _capture()
    for environ in ({}, {"APERTURE_TEST_TO": ""}):
        with pytest.raises(NotifyError, match="alert-recipient-missing: APERTURE_TEST_TO"):
            IMessageNotifier(CONFIG, environ, runner).notify("t", "x")
    assert calls == []
    assert recipient_missing(CONFIG, {}) == "APERTURE_TEST_TO"
    assert recipient_missing(CONFIG, {"APERTURE_TEST_TO": RECIPIENT}) is None


def _phone(runner):
    return IMessageNotifier(CONFIG, {"APERTURE_TEST_TO": RECIPIENT}, runner)


def _channels(mac_code: int, phone_runner) -> tuple[list, Channels]:
    mac, mac_runner = _capture(code=mac_code)
    return mac, Channels(((MACOS, MacNotifier(Decimal(5), mac_runner)),),
                         ((IMESSAGE, _phone(phone_runner)),))


def test_alert_returns_while_imessage_blocks_and_its_outcome_is_recorded_later():
    entered, release = threading.Event(), threading.Event()

    def blocking(argv, **kwargs):
        entered.set()
        release.wait()
        return subprocess.CompletedProcess(argv, 0)

    mac, channels = _channels(0, blocking)
    alerter = Alerter(CONFIG, "Aperture", channels, Clock())
    outcome = alerter.alert(AlertKind.BOT_CRASHED, "x")  # returns with Messages still "busy"
    assert entered.wait(5) and not release.is_set()
    assert outcome.delivery is Delivery.SHOWN and outcome.pending == (IMESSAGE,)
    assert outcome.journal_body()["pending"] == ["imessage"] and len(mac) == 1
    assert alerter.completed() == []  # not known yet
    release.set()
    (done,) = alerter.flush()
    assert done.delivery is Delivery.SHOWN and done.channel == IMESSAGE
    assert done.journal_body()["channel"] == "imessage"


def test_failing_imessage_is_recorded_once_and_mac_success_holds_dedupe():
    phone, phone_runner = _capture(code=1)
    mac, channels = _channels(0, phone_runner)
    clock = Clock()
    alerter = Alerter(CONFIG, "Aperture", channels, clock)
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SHOWN
    (failed,) = alerter.flush()
    assert failed.delivery is Delivery.FAILED and failed.channel == IMESSAGE
    assert failed.error == "imessage: osascript exited with code 1"
    assert RECIPIENT not in str(failed.journal_body())
    clock.now += timedelta(seconds=60)
    again = alerter.alert(AlertKind.BOT_CRASHED, "x")
    assert again.delivery is Delivery.SUPPRESSED_REPEAT
    assert alerter.flush() == []
    assert len(mac) == 1 and len(phone) == 1  # one attempt each, never retried


def test_mac_failing_still_sends_imessage_and_its_success_holds_dedupe():
    phone, phone_runner = _capture()
    mac, channels = _channels(1, phone_runner)
    clock = Clock()
    alerter = Alerter(CONFIG, "Aperture", channels, clock)
    first = alerter.alert(AlertKind.BOT_CRASHED, "x")
    assert first.delivery is Delivery.FAILED
    assert first.error == "macos: osascript exited with code 1"
    (done,) = alerter.flush()
    assert done.delivery is Delivery.SHOWN and len(phone) == 1
    clock.now += timedelta(seconds=60)
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SUPPRESSED_REPEAT
