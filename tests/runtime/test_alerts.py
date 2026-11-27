from __future__ import annotations

import json
import threading
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr.config import AlertsConfig
from tickr.runtime.alerts import (
    FEED,
    TELEGRAM,
    Alerter,
    AlertKind,
    Channels,
    Delivery,
    FeedNotifier,
    NotifyError,
    TelegramNotifier,
    default_notifier,
    feed_path,
    telegram_missing,
    telegram_on,
)

CONFIG = AlertsConfig(dedupe_window_seconds=Decimal(600), notification_timeout_seconds=Decimal(5),
                      fence_alert_after_seconds=Decimal(30),
                      feed_file="alerts.jsonl",
                      routing_file="alert-routing.json",
                      bot_token_variable="", chat_id_variable="",
                      telegram_host="api.telegram.org")
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
        "protection-change-ignored",
        "watchdog-restarted-bot", "watchdog-restart-refused", "watchdog-restart-failed",
        "bot-down", "disk-low", "remote-auth-blocked", "risk-command-used",
        "device-session",
        "strategy-not-loaded",
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


def test_feed_appends_one_json_line_per_alert(tmp_path):
    path = feed_path(tmp_path, "data", CONFIG)
    path.parent.mkdir()
    feed = FeedNotifier(path, Clock())
    feed.notify("Tickr Sandbox: bot-crashed", "bot exited with code 1")
    feed.notify("Tickr Sandbox: disk-low", "line two")
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert rows == [
        {"at": T0.isoformat(), "title": "Tickr Sandbox: bot-crashed",
         "text": "bot exited with code 1"},
        {"at": T0.isoformat(), "title": "Tickr Sandbox: disk-low", "text": "line two"}]


def test_feed_failure_is_a_channel_failure(tmp_path):
    with pytest.raises(NotifyError, match="feed not written: FileNotFoundError"):
        FeedNotifier(tmp_path / "missing" / "alerts.jsonl").notify("t", "x")


class Blocking:
    """A channel that waits until released, as a slow network would."""

    def __init__(self, fail: bool = False) -> None:
        self.entered, self.release = threading.Event(), threading.Event()
        self.shown, self.fail = 0, fail

    def notify(self, title: str, text: str) -> None:
        self.entered.set()
        assert self.release.wait(5)
        if self.fail:
            raise NotifyError("boom")
        self.shown += 1


def test_alert_never_waits_for_the_telegram_send_and_one_send_runs_at_a_time():
    """With every channel in the background, alert() returns at once, and the same alert
    raised again while its send is still running is a repeat: one send, not one per pass."""
    mac, phone = Blocking(), Blocking()
    alerter = Alerter(CONFIG, "Tickr", Channels((), ((TELEGRAM, mac), (FEED, phone))),
                      Clock())
    first = alerter.alert(AlertKind.UNPROTECTED_POSITION, "ABC")
    assert first.delivery is Delivery.SENDING and first.pending == (TELEGRAM, FEED)
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
        for channel in (TELEGRAM, FEED))
    assert mac.shown == phone.shown == 2
    # Delivered: the usual window now suppresses it.
    assert alerter.alert(AlertKind.UNPROTECTED_POSITION, "ABC").delivery \
        is Delivery.SUPPRESSED_REPEAT


def test_an_alert_no_channel_delivered_is_sent_again_once_its_sends_have_ended():
    mac, phone = Blocking(fail=True), Blocking(fail=True)
    alerter = Alerter(CONFIG, "Tickr", Channels((), ((TELEGRAM, mac), (FEED, phone))),
                      Clock())
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SENDING
    mac.release.set()
    assert mac.entered.wait(5)
    while not alerter.completed():  # the first send has failed; the second is still running
        pass
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SUPPRESSED_REPEAT
    phone.release.set()
    (failed,) = alerter.flush()
    assert failed.delivery is Delivery.FAILED and failed.channel == FEED
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
                      Channels((), ((TELEGRAM, mac), (FEED, phone)), spawn=spawn), Clock())
    # The bot's loop gets an outcome, never the exception; the other channel still runs.
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SENDING
    outcomes = {o.channel: (o.delivery, o.error) for o in alerter.flush()}
    assert outcomes == {TELEGRAM: (Delivery.FAILED, "telegram: not started: RuntimeError"),
                        FEED: (Delivery.FAILED, "feed: boom")}
    # Not suppressed until a restart: the next occurrence is sent, on both channels.
    phone.fail = mac.fail = False
    assert alerter.alert(AlertKind.BOT_CRASHED, "x").delivery is Delivery.SENDING
    assert {o.channel: o.delivery for o in alerter.flush()} == {
        TELEGRAM: Delivery.SHOWN, FEED: Delivery.SHOWN}
    assert len(mac.shown) == len(phone.shown) == 1


def test_a_send_that_raises_leaves_no_alert_marked_as_sending():
    class Broken(Channels):
        def send(self, title, text, done, skip=frozenset()):
            raise RuntimeError("boom")

    alerter = Alerter(CONFIG, "Tickr", Broken((), ((TELEGRAM, Recorder()),)), Clock())
    for _ in range(2):  # the second call reaches send again instead of being suppressed
        with pytest.raises(RuntimeError):
            alerter.alert(AlertKind.BOT_CRASHED, "x")


TG = replace(CONFIG, bot_token_variable="TICKR_TEST_TG_TOKEN",
             chat_id_variable="TICKR_TEST_TG_CHAT")
TG_ENV = {"TICKR_TEST_TG_TOKEN": "token-canary", "TICKR_TEST_TG_CHAT": "chat-canary"}


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _opener(sent, answer=None):
    def opener(request, timeout):
        sent.append((request, timeout))
        if isinstance(answer, BaseException):
            raise answer
        return answer or FakeResponse()
    return opener


def _inline(target):
    thread = threading.Thread(target=target)
    thread.start()
    return thread


def test_telegram_is_off_while_no_variable_is_set():
    assert telegram_on(CONFIG, TG_ENV) is False
    assert telegram_on(TG, {}) is False  # names set, nothing in the environment
    assert telegram_missing(TG, {}) == []
    assert default_notifier(CONFIG, TG_ENV) is None
    assert default_notifier(TG, {}) is None  # no channel without Telegram, on any platform


def test_default_notifier_is_telegram_and_the_feed_only():
    channels = default_notifier(TG, TG_ENV)
    # Not in the caller's thread: the bot's loop waits for no alert.
    assert isinstance(channels, Channels) and channels.background == (TELEGRAM,)
    with_feed = default_notifier(TG, TG_ENV, feed=Path("a.jsonl"))
    assert isinstance(with_feed, Channels) and with_feed.background == (TELEGRAM, FEED)
    assert with_feed.names() == (TELEGRAM, FEED)
    # The feed alone is not a way to reach the owner.
    assert default_notifier(TG, {}, feed=Path("a.jsonl")) is None


def test_no_osascript_or_other_os_channel_remains():
    root = Path(__file__).resolve().parents[2]
    needles = ("osascript", "iMessage", "imessage", "MacNotifier", "IMessageNotifier")
    for path in (root / "src").rglob("*.py"):
        text = path.read_text("utf-8")
        assert not [n for n in needles if n in text], path
    import tickr.runtime.alerts as alerts
    assert not hasattr(alerts, "OSASCRIPT") and not hasattr(alerts, "IMessageNotifier")
    assert not hasattr(alerts, "MacNotifier") and not hasattr(alerts, "mac_notifier")
    assert not [name for name in dir(alerts) if "imessage" in name.lower()]


def test_telegram_sends_one_https_post_and_never_retries():
    sent = []
    TelegramNotifier(TG, TG_ENV, _opener(sent)).notify("tickr: disk-low", "10 MB free")
    ((request, timeout),) = sent
    assert request.full_url == "https://api.telegram.org/bottoken-canary/sendMessage"
    assert request.get_method() == "POST" and timeout == 5.0
    assert json.loads(request.data) == {"chat_id": "chat-canary",
                                        "text": "tickr: disk-low\n10 MB free"}


@pytest.mark.parametrize("answer, message", [
    (urllib.error.HTTPError("https://x/bottoken-canary/s", 401, "no", {}, None),
     "telegram answered HTTP 401"),
    (urllib.error.URLError("token-canary down"), "telegram did not answer: URLError"),
    (TimeoutError("token-canary"), "telegram did not answer: TimeoutError"),
    (FakeResponse(302), "telegram answered HTTP 302"),
])
def test_telegram_failure_is_one_attempt_and_leaks_no_secret(answer, message):
    sent = []
    with pytest.raises(NotifyError) as caught:
        TelegramNotifier(TG, TG_ENV, _opener(sent, answer)).notify("t", "x")
    assert str(caught.value) == message
    assert len(sent) == 1


def test_telegram_missing_variable_names_only_the_name():
    sent = []
    environ = {"TICKR_TEST_TG_TOKEN": "token-canary"}
    assert telegram_missing(TG, environ) == ["TICKR_TEST_TG_CHAT"]
    with pytest.raises(NotifyError, match="TICKR_TEST_TG_CHAT not set") as caught:
        TelegramNotifier(TG, environ, _opener(sent)).notify("t", "x")
    assert "canary" not in str(caught.value) and sent == []


def test_a_failed_telegram_send_is_recorded_as_a_failed_delivery():
    sent = []
    notifier = TelegramNotifier(TG, TG_ENV, _opener(sent, urllib.error.URLError("x")))
    alerter = Alerter(CONFIG, "tickr", Channels((), ((TELEGRAM, notifier),), spawn=_inline))
    alerter.alert(AlertKind.DISK_LOW, "low")
    (outcome,) = alerter.flush()
    assert outcome.delivery is Delivery.FAILED and outcome.channel == TELEGRAM
    assert "telegram did not answer" in (outcome.error or "")
    assert len(sent) == 1


@pytest.mark.parametrize("error", [ValueError("https://x/bottoken-canary/s chat-canary"),
                                   RuntimeError("token-canary"), KeyError("chat-canary")])
def test_an_unexpected_telegram_error_becomes_a_notify_error_naming_only_its_type(error):
    sent = []
    with pytest.raises(NotifyError) as caught:
        TelegramNotifier(TG, TG_ENV, _opener(sent, error)).notify("t", "x")
    assert str(caught.value) == f"telegram failed: {type(error).__name__}"
    # No chained exception carries the original message (it may hold the URL or chat id).
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert len(sent) == 1


def test_an_unexpected_telegram_error_never_reaches_the_alert_caller():
    sent = []
    notifier = TelegramNotifier(TG, TG_ENV, _opener(sent, ValueError("token-canary")))
    # In the caller's thread too (no background), the alert returns a failed outcome.
    alerter = Alerter(CONFIG, "tickr", Channels(((TELEGRAM, notifier),), ()))
    outcome = alerter.alert(AlertKind.DISK_LOW, "low")
    assert outcome.delivery is Delivery.FAILED and outcome.error is not None
    assert "canary" not in outcome.error and "ValueError" in outcome.error
