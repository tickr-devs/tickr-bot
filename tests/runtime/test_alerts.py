from __future__ import annotations

import plistlib
import subprocess
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tickr.config import AlertsConfig
from tickr.runtime.alerts import (
    APP_ALERT_FLAG,
    IMESSAGE,
    MACOS,
    OSASCRIPT,
    Alerter,
    AlertKind,
    AppNotifier,
    Channels,
    Delivery,
    FirstOf,
    IMessageNotifier,
    MacNotifier,
    NotifyError,
    default_notifier,
    mac_notifier,
    recipient_missing,
)

CONFIG = AlertsConfig(dedupe_window_seconds=Decimal(600), notification_timeout_seconds=Decimal(5),
                      fence_alert_after_seconds=Decimal(30),
                      imessage_recipient_variable="TICKR_TEST_TO", mac_app="",
                      mac_app_timeout_seconds=Decimal(2))
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
        "bot-down-with-open-trade", "exit-not-filled", "flatten-after-close",
        "fees-unknown", "fees-corrected", "api-credentials-unexpected",
        "watchdog-restarted-bot", "watchdog-restart-refused", "watchdog-restart-failed",
    }


def test_repeat_within_window_is_suppressed_then_shown_again():
    clock, notifier = Clock(), Recorder()
    alerter = Alerter(CONFIG, "Tickr x", notifier, clock)
    first = alerter.alert(AlertKind.STOP_REJECTED, "ABC stop rejected")
    assert first.delivery is Delivery.SHOWN
    assert notifier.shown == [("Tickr x: stop-rejected", "ABC stop rejected")]
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


def test_imessage_script_variables_do_not_shadow_messages_terms():
    # AppleScript is case-insensitive: `set imessage to ...` collided with the iMessage
    # service type and failed with -10003 on every send.
    import re

    from tickr.runtime.alerts import _IMESSAGE_SCRIPT

    terms = {"imessage", "sms", "rcs", "account", "participant", "chat", "service", "buddy"}
    names = {m.group(1).lower() for line in _IMESSAGE_SCRIPT
             for m in [re.match(r"set (\w+) to ", line)] if m}
    assert names and not names & terms


def test_osascript_error_keeps_only_the_applescript_number():
    secret = "+15550000000"

    def runner(argv, **kwargs):
        stderr = f"184:196: execution error: {secret} not allowed. (-10003)\n".encode()
        return subprocess.CompletedProcess(argv, 1, stderr=stderr)

    with pytest.raises(NotifyError) as caught:
        MacNotifier(Decimal(5), runner).notify("t", "x")
    assert str(caught.value) == "osascript exited with code 1 (AppleScript error -10003)"
    assert secret not in str(caught.value)


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
    channels = default_notifier(CONFIG, "darwin", {})
    assert isinstance(channels, Channels)
    # Neither channel runs in the caller's thread: the bot's loop waits for no alert.
    assert channels.background == (MACOS, IMESSAGE)
    assert default_notifier(CONFIG, "linux") is None


class Blocking:
    """A channel that waits until released, as a slow Tickr app or osascript would."""

    def __init__(self, fail: bool = False) -> None:
        self.entered, self.release = threading.Event(), threading.Event()
        self.shown, self.fail = 0, fail

    def notify(self, title: str, text: str) -> None:
        self.entered.set()
        assert self.release.wait(5)
        if self.fail:
            raise NotifyError("boom")
        self.shown += 1


def test_alert_never_waits_for_the_mac_notification_and_one_send_runs_at_a_time():
    """With every channel in the background, alert() returns at once, and the same alert
    raised again while its send is still running is a repeat: one send, not one per pass."""
    mac, phone = Blocking(), Blocking()
    alerter = Alerter(CONFIG, "Tickr", Channels((), ((MACOS, mac), (IMESSAGE, phone))),
                      Clock())
    first = alerter.alert(AlertKind.UNPROTECTED_POSITION, "ABC")
    assert first.delivery is Delivery.SENDING and first.pending == (MACOS, IMESSAGE)
    assert mac.entered.wait(5) and phone.entered.wait(5)  # both still "busy"
    for _ in range(3):  # the next loop passes raise it again
        again = alerter.alert(AlertKind.UNPROTECTED_POSITION, "ABC")
        assert again.delivery is Delivery.SUPPRESSED_REPEAT
    other = alerter.alert(AlertKind.UNPROTECTED_POSITION, "XYZ")  # another alert is not held
    assert other.delivery is Delivery.SENDING
    mac.release.set()
    phone.release.set()
    done = alerter.flush()
    assert sorted((o.text, o.channel, o.delivery) for o in done) == sorted(
        (text, channel, Delivery.SHOWN) for text in ("ABC", "XYZ")
        for channel in (MACOS, IMESSAGE))
    assert mac.shown == phone.shown == 2
    # Delivered: the usual window now suppresses it.
    assert alerter.alert(AlertKind.UNPROTECTED_POSITION, "ABC").delivery \
        is Delivery.SUPPRESSED_REPEAT


def test_an_alert_no_channel_delivered_is_sent_again_once_its_sends_have_ended():
    mac, phone = Blocking(fail=True), Blocking(fail=True)
    alerter = Alerter(CONFIG, "Tickr", Channels((), ((MACOS, mac), (IMESSAGE, phone))),
                      Clock())
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SENDING
    mac.release.set()
    assert mac.entered.wait(5)
    while not alerter.completed():  # the Mac send has failed; iMessage is still running
        pass
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SUPPRESSED_REPEAT
    phone.release.set()
    (failed,) = alerter.flush()
    assert failed.delivery is Delivery.FAILED and failed.channel == IMESSAGE
    # Nothing was delivered and nothing is running: the next occurrence tries again.
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SENDING
    alerter.flush()


def test_an_alert_whose_thread_cannot_start_is_reported_and_sent_again_next_time():
    starts = []

    def spawn(target):
        starts.append(target)
        if len(starts) == 1:
            raise RuntimeError("can't start new thread")
        thread = threading.Thread(target=target)
        thread.start()
        return thread

    mac, phone = Recorder(fail=True), Recorder(fail=True)
    alerter = Alerter(CONFIG, "Tickr",
                      Channels((), ((MACOS, mac), (IMESSAGE, phone)), spawn=spawn), Clock())
    # The bot's loop gets an outcome, never the exception; the other channel still runs.
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SENDING
    outcomes = {o.channel: (o.delivery, o.error) for o in alerter.flush()}
    assert outcomes == {MACOS: (Delivery.FAILED, "macos: not started: RuntimeError"),
                        IMESSAGE: (Delivery.FAILED, "imessage: boom")}
    # Not suppressed until a restart: the next occurrence is sent, on both channels.
    phone.fail = mac.fail = False
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SENDING
    assert {o.channel: o.delivery for o in alerter.flush()} == {
        MACOS: Delivery.SHOWN, IMESSAGE: Delivery.SHOWN}
    assert len(mac.shown) == len(phone.shown) == 1


def test_a_send_that_raises_leaves_no_alert_marked_as_sending():
    class Broken(Channels):
        def send(self, title, text, done):
            raise RuntimeError("boom")

    alerter = Alerter(CONFIG, "Tickr", Broken((), ((MACOS, Recorder()),)), Clock())
    for _ in range(2):  # the second call reaches send again instead of being suppressed
        with pytest.raises(RuntimeError):
            alerter.alert(AlertKind.BOT_CRASHED, "x")


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
    IMessageNotifier(CONFIG, {"TICKR_TEST_TO": RECIPIENT}, runner).notify("Title", text)
    (argv,) = calls
    assert argv[0] == OSASCRIPT
    assert argv[-2:] == ["TICKR_TEST_TO", f"Title\n{text}"]
    assert all(text not in part for part in argv[:-1])
    assert RECIPIENT not in " ".join(argv)
    assert "system attribute (item 1 of argv)" in " ".join(argv)


def test_imessage_refuses_without_the_recipient_variable():
    calls, runner = _capture()
    for environ in ({}, {"TICKR_TEST_TO": ""}):
        with pytest.raises(NotifyError, match="alert-recipient-missing: TICKR_TEST_TO"):
            IMessageNotifier(CONFIG, environ, runner).notify("t", "x")
    assert calls == []
    assert recipient_missing(CONFIG, {}) == "TICKR_TEST_TO"
    assert recipient_missing(CONFIG, {"TICKR_TEST_TO": RECIPIENT}) is None


def _phone(runner):
    return IMessageNotifier(CONFIG, {"TICKR_TEST_TO": RECIPIENT}, runner)


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
    alerter = Alerter(CONFIG, "Tickr", channels, Clock())
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
    alerter = Alerter(CONFIG, "Tickr", channels, clock)
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
    alerter = Alerter(CONFIG, "Tickr", channels, clock)
    first = alerter.alert(AlertKind.BOT_CRASHED, "x")
    assert first.delivery is Delivery.FAILED
    assert first.error == "macos: osascript exited with code 1"
    (done,) = alerter.flush()
    assert done.delivery is Delivery.SHOWN and len(phone) == 1
    clock.now += timedelta(seconds=60)
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SUPPRESSED_REPEAT


def _app(tmp_path, executable="Tickr"):
    app = tmp_path / "Tickr.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleExecutable": executable}, handle)
    return app


def test_app_notifier_runs_the_bundle_executable_with_title_and_text_as_argv(tmp_path):
    app = _app(tmp_path)
    calls, runner = _capture()
    text = 'stop "rejected"; do shell script "x"'
    AppNotifier(str(app), Decimal(5), runner).notify("Title", text)
    assert calls == [[str(app / "Contents" / "MacOS" / "Tickr"), APP_ALERT_FLAG, "Title", text]]


def test_app_notifier_refuses_a_missing_app_without_running_anything(tmp_path):
    calls, runner = _capture()
    with pytest.raises(NotifyError, match="app-missing"):
        AppNotifier(str(tmp_path / "Nowhere.app"), Decimal(5), runner).notify("t", "x")
    assert calls == []


@pytest.mark.parametrize("effect", [
    subprocess.CompletedProcess([], 2, b"", b""),
    subprocess.TimeoutExpired("app", 5),
    OSError("exec format"),
])
def test_app_notifier_failures_become_notify_error(tmp_path, effect):
    def runner(argv, **kwargs):
        if isinstance(effect, Exception):
            raise effect
        return effect
    with pytest.raises(NotifyError):
        AppNotifier(str(_app(tmp_path)), Decimal(5), runner).notify("t", "x")


class Channel:
    def __init__(self, error: str | None) -> None:
        self.error = error
        self.calls = 0

    def notify(self, title: str, text: str) -> None:
        self.calls += 1
        if self.error is not None:
            raise NotifyError(self.error)


def test_first_of_stops_at_the_first_channel_that_delivers():
    app, script = Channel(None), Channel(None)
    FirstOf((app, script)).notify("t", "x")
    assert (app.calls, script.calls) == (1, 0)


def test_first_of_falls_back_when_the_app_fails_so_no_alert_is_lost():
    app, script = Channel("app exited with code 2"), Channel(None)
    FirstOf((app, script)).notify("t", "x")
    assert (app.calls, script.calls) == (1, 1)


def test_first_of_names_every_failure_when_none_delivered():
    with pytest.raises(NotifyError, match="app-missing: x; osascript exited with code 1"):
        channels = (Channel("app-missing: x"), Channel("osascript exited with code 1"))
        FirstOf(channels).notify("t", "x")


def test_mac_notifier_is_the_app_then_osascript_or_osascript_alone(tmp_path):
    assert isinstance(mac_notifier(CONFIG), MacNotifier)
    chained = mac_notifier(replace(CONFIG, mac_app=str(tmp_path / "Tickr.app")))
    assert isinstance(chained, FirstOf)


def test_the_app_gets_its_own_short_timeout_and_osascript_keeps_its_own(tmp_path):
    timeouts = []

    def runner(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0)

    mac_notifier(replace(CONFIG, mac_app=str(_app(tmp_path))), runner).notify("t", "x")
    assert timeouts == [2.0, 5.0]


def test_missing_app_falls_back_to_osascript_end_to_end(tmp_path):
    calls, runner = _capture()
    mac_notifier(replace(CONFIG, mac_app=str(tmp_path / "Missing.app")), runner).notify("t", "x")
    assert [argv[0] for argv in calls] == [OSASCRIPT]
