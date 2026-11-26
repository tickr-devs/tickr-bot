"""Owner alerts (spec section 11): a macOS notification, an iMessage to the owner's phone and a
line in the alerts feed file (`[alerts] feed_file`), which a Claude session on the Mac waits on.

The Mac notification is shown by the Tickr app (`[alerts] mac_app`), so it carries the app's
icon; when the app is missing, not allowed to notify or fails, osascript shows it instead.

`Alerter.alert(kind, text)` never raises and never retries. It returns an `AlertOutcome` that the
caller always writes to its journal, whether the notification was shown, suppressed as a repeat,
or could not be delivered. Repeats of the same kind and text inside the configured window are
suppressed so that a condition that persists does not flood the owner. The owner's routing
(`runtime/alert_routing.py`) may turn a kind off on some channels; critical kinds are never off,
and what was skipped is named in the outcome.

Alert text must never contain a secret value: callers pass variable NAMES, never contents.
The iMessage recipient is personal data: Tickr code only knows the NAME of the environment
variable that holds it (`[alerts] imessage_recipient_variable`) and checks its presence; the
AppleScript reads the value itself (`system attribute`), so it is never in Python, argv or logs.
"""

from __future__ import annotations

import json
import os
import plistlib
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from tickr.config import AlertsConfig

# Protocol constants of macOS: the AppleScript runner and a script that takes its title and
# text as arguments, so alert text is never parsed as AppleScript.
OSASCRIPT = "/usr/bin/osascript"
_APPLESCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)
# Sends argv item 2 by iMessage to the address held by the environment variable NAMED in argv
# item 1. A missing recipient is an AppleScript error (non-zero exit), never a silent skip.
# AppleScript names are case-insensitive: a variable must not share a name with a Messages
# term (`imessage` is the `iMessage` service type and fails with -10003 before sending).
_IMESSAGE_SCRIPT = (
    "on run argv",
    "set recipient to system attribute (item 1 of argv)",
    'if recipient is "" then error "recipient variable not set"',
    'tell application "Messages"',
    "set acct to 1st account whose service type = iMessage",
    "send (item 2 of argv) to participant recipient of acct",
    "end tell",
    "end run",
)
# The Tickr app's command line (apps/macos AlertCommand): `<executable> --notify-alert TITLE TEXT`
# exits 0 once the notification is handed to Notification Center.
APP_ALERT_FLAG = "--notify-alert"
APP_INFO_PLIST = ("Contents", "Info.plist")
APP_EXECUTABLES = ("Contents", "MacOS")
APP_EXECUTABLE_KEY = "CFBundleExecutable"
DARWIN = "darwin"
MACOS = "macos"
IMESSAGE = "imessage"
FEED = "feed"
NOTIFIER = "notifier"  # name of a single channel given to Alerter


class AlertKind(StrEnum):
    """Every alert the owner can receive (spec section 11). Values are stable journal names."""

    UNPROTECTED_POSITION = "unprotected-position"
    STOP_REJECTED = "stop-rejected"
    # A closing order was refused; the stop is back and the operator must look.
    EXIT_REFUSED = "exit-refused"
    UNKNOWN_ORDER_STATUS = "unknown-order-status"
    FOREIGN_EXPOSURE = "foreign-exposure"
    BROKER_READS_FAILING = "broker-reads-failing"
    STREAM_LOST_WITH_POSITION = "stream-lost-with-position"
    DAILY_CEILING_REACHED = "daily-ceiling-reached"
    BOT_CRASHED = "bot-crashed"
    BOT_DID_NOT_START = "bot-did-not-start"
    # Added to the spec list by review of #13: SIGTERM forwarded, bot still running.
    BOT_DID_NOT_STOP = "bot-did-not-stop"
    PREFLIGHT_FAILED = "preflight-failed"
    TOKEN_NEEDS_CONFIRMATION = "token-needs-confirmation"
    # The observer queue overflowed: observers are switched off for the run (trading goes on).
    OBSERVERS_STOPPED = "observers-stopped"
    # Broker reads after the flatten time (plus grace) still show our position or order.
    NOT_FLAT_AFTER_FLATTEN = "not-flat-after-flatten"
    # Raised by the separate watchdog job (runtime/watchdog.py), which outlives a crashed bot:
    # broker reads after the flatten time show any position or any open order;
    WATCHDOG_NOT_FLAT = "watchdog-not-flat"
    # the watchdog could not tell (broker read, database or calendar failed);
    WATCHDOG_CANNOT_CHECK = "watchdog-cannot-check"
    # during the session the bot process is not running while the database shows an open trade.
    BOT_DOWN_WITH_OPEN_TRADE = "bot-down-with-open-trade"
    # at any hour the bot is not running (or hung) although its last status was not a clean stop;
    BOT_DOWN = "bot-down"
    # at any hour the bot's data disk has less free space than [runtime] disk_warn_free_mb;
    DISK_LOW = "disk-low"
    # near the flatten time the watchdog started the bot job (once a day) so it flattens;
    WATCHDOG_RESTARTED_BOT = "watchdog-restarted-bot"
    # it would have, but may not (stop marker present, restart record unreadable, day unknown);
    WATCHDOG_RESTART_REFUSED = "watchdog-restart-refused"
    # the restart failed: the command failed, or at a later check the bot is still down or the
    # broker is still not flat. The loudest watchdog alert: act by hand now.
    WATCHDOG_RESTART_FAILED = "watchdog-restart-failed"
    # A closing order is still not filled [orders] max_exit_seconds after it was sent.
    EXIT_NOT_FILLED = "exit-not-filled"
    # A close (session flatten or any other exit) was due outside the regular session: no
    # MARKET order was sent and the stop stays; the owner closes by hand (fence market-closed).
    FLATTEN_AFTER_CLOSE = "flatten-after-close"
    # A finished order with fills still has no recorded broker fees after
    # [orders] fees_unknown_alert_seconds: today's money is unknown, so entries are refused.
    FEES_UNKNOWN = "fees-unknown"
    # The account's cash activity proved an order's recorded fees wrong; they were replaced.
    FEES_CORRECTED = "fees-corrected"
    # Raised by the API at its start: it holds a credential variable its launchd job must not
    # give it (any, while [chart] webull_enabled is false; any but the active environment's app
    # key and secret while it is true): its plist is stale or hand-edited (runtime/launchd.py).
    API_CREDENTIALS_UNEXPECTED = "api-credentials-unexpected"


class Delivery(StrEnum):
    SHOWN = "shown"
    SUPPRESSED_REPEAT = "suppressed-repeat"
    NO_NOTIFIER = "no-notifier"
    SENDING = "sending"  # background channels only; their outcomes follow
    FAILED = "failed"
    # The owner's routing turned this kind off on every channel this process has.
    ROUTED_OFF = "routed-off"


class NotifyError(Exception):
    """The notification could not be shown; the message says why."""


class Notifier(Protocol):
    """One alert delivery channel (`MacNotifier`, `IMessageNotifier`), or several at once
    (`Channels`). `default_notifier` chooses them for the bot and the watchdog alike."""

    def notify(self, title: str, text: str) -> None:
        """Show one notification or raise NotifyError."""


Runner = Callable[..., subprocess.CompletedProcess[bytes]]
# osascript ends an error line with its number in brackets, e.g. "... Access not allowed. (-10003)".
_APPLESCRIPT_ERROR = re.compile(rb"\((-\d+)\)\s*$")


def _osascript(runner: Runner, timeout: float, script: tuple[str, ...],
               args: list[str]) -> None:
    """Run one AppleScript with `args` as its argv (an argument list, never a shell string).
    The error names the exit code and the AppleScript error number only: the rest of stderr
    could echo the arguments."""
    argv = [OSASCRIPT]
    for line in script:
        argv += ["-e", line]
    argv += args
    try:
        result = runner(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotifyError(f"osascript did not run: {type(exc).__name__}") from exc
    if result.returncode != 0:
        number = _APPLESCRIPT_ERROR.search(result.stderr or b"")
        detail = f" (AppleScript error {number.group(1).decode()})" if number else ""
        raise NotifyError(f"osascript exited with code {result.returncode}{detail}")


class MacNotifier:
    """macOS notification through osascript. Title and text go in argv, never into the script."""

    def __init__(self, timeout_seconds: Decimal, runner: Runner = subprocess.run) -> None:
        self._timeout = float(timeout_seconds)
        self._runner = runner

    def notify(self, title: str, text: str) -> None:
        _osascript(self._runner, self._timeout, _APPLESCRIPT, [title, text])


class AppNotifier:
    """macOS notification shown by the Tickr app, with its icon. The app runs once per alert in
    its notify mode; title and text go in argv. Raises NotifyError when the app is missing, not
    allowed to notify (System Settings > Notifications), times out or fails."""

    def __init__(self, app: str, timeout_seconds: Decimal,
                 runner: Runner = subprocess.run) -> None:
        self._app = app
        self._timeout = float(timeout_seconds)
        self._runner = runner

    def _executable(self) -> str:
        info = os.path.join(self._app, *APP_INFO_PLIST)
        try:
            with open(info, "rb") as handle:
                name = plistlib.load(handle).get(APP_EXECUTABLE_KEY)
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            raise NotifyError(f"app-missing: {type(exc).__name__}") from exc
        if not isinstance(name, str) or not name:
            raise NotifyError("app-missing: no executable in Info.plist")
        return os.path.join(self._app, *APP_EXECUTABLES, name)

    def notify(self, title: str, text: str) -> None:
        argv = [self._executable(), APP_ALERT_FLAG, title, text]
        try:
            result = self._runner(argv, capture_output=True, timeout=self._timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotifyError(f"app did not run: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise NotifyError(f"app exited with code {result.returncode}")


class FirstOf:
    """One notification through the first channel that delivers it: the next is tried only
    when the one before raised. Raises NotifyError naming every failure when none delivered.
    A channel that timed out may still have shown it, so a repeat is possible; a loss is not."""

    def __init__(self, channels: tuple[Notifier, ...]) -> None:
        self._channels = channels

    def notify(self, title: str, text: str) -> None:
        errors = []
        for channel in self._channels:
            try:
                channel.notify(title, text)
            except NotifyError as exc:
                errors.append(str(exc))
            else:
                return
        raise NotifyError("; ".join(errors))


def mac_notifier(config: AlertsConfig, runner: Runner = subprocess.run) -> Notifier:
    """The Tickr app when configured, osascript after it (or alone)."""
    script = MacNotifier(config.notification_timeout_seconds, runner)
    if not config.mac_app:
        return script
    app = AppNotifier(config.mac_app, config.mac_app_timeout_seconds, runner)
    return FirstOf((app, script))


def recipient_missing(config: AlertsConfig, environ: Mapping[str, str]) -> str | None:
    """The recipient variable's NAME when it is not set, else None. Presence only: the value is
    compared with "" and never stored, printed or returned."""
    name = config.imessage_recipient_variable
    return None if environ.get(name) else name


class IMessageNotifier:
    """iMessage through Messages.app, driven by osascript. Messages must be signed in to
    iMessage on this Mac. The recipient never passes through Python: the script reads the
    variable named `variable`. One attempt per alert, never retried."""

    def __init__(self, config: AlertsConfig, environ: Mapping[str, str] = os.environ,
                 runner: Runner = subprocess.run) -> None:
        self._config = config
        self._environ = environ
        self._timeout = float(config.notification_timeout_seconds)
        self._runner = runner

    def notify(self, title: str, text: str) -> None:
        missing = recipient_missing(self._config, self._environ)
        if missing is not None:
            raise NotifyError(f"alert-recipient-missing: {missing} not set")
        _osascript(self._runner, self._timeout, _IMESSAGE_SCRIPT,
                   [self._config.imessage_recipient_variable, f"{title}\n{text}"])


class FeedNotifier:
    """Appends one JSON line per alert ({at, title, text}) to the alerts feed file, so whatever
    waits on that file (a Claude session on the Mac) learns of the alert at once. Every process
    that alerts appends to the same file; one short `write` in append mode per line. Raises
    NotifyError when the file cannot be written (a full disk, a missing folder)."""

    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self._path = path
        self._clock = clock or _utc_now

    def notify(self, title: str, text: str) -> None:
        line = json.dumps({"at": self._clock().isoformat(), "title": title, "text": text},
                          sort_keys=True) + "\n"
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            raise NotifyError(f"feed not written: {type(exc).__name__}") from exc


def feed_path(root: Path, data_dir: str, config: AlertsConfig) -> Path:
    """The alerts feed file: `[alerts] feed_file` under `[run] data_dir`."""
    return root / data_dir / config.feed_file


Done = Callable[[str, "str | None"], None]


def _spawn_daemon(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, name="alert-channel", daemon=True)
    thread.start()
    return thread


class Channels:
    """Every alert through every channel; a failing channel never stops the others.

    `channels` run in the caller's thread. `background` channels each run once on a daemon
    thread, so the bot's loop never waits on the Tickr app, osascript or Messages.app; each
    reports its result through `done` exactly once. Never retried."""

    def __init__(self, channels: tuple[tuple[str, Notifier], ...],
                 background: tuple[tuple[str, Notifier], ...] = (),
                 spawn: Callable[[Callable[[], None]], threading.Thread] = _spawn_daemon,
                 ) -> None:
        self._channels = channels
        self.background = tuple(name for name, _ in background)
        self._background = background
        self._spawn = spawn
        self._threads: list[threading.Thread] = []

    def background_except(self, skip: frozenset[str]) -> tuple[str, ...]:
        """Names of the background channels a send that skips `skip` starts."""
        return tuple(name for name in self.background if name not in skip)

    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self._channels) + self.background

    def send(self, title: str, text: str, done: Done,
             skip: frozenset[str] = frozenset()) -> list[tuple[str, str | None]]:
        """Start the background channels, run the others, all but those named in `skip`;
        (name, error or None) of the ones run in this thread."""
        self._threads = [t for t in self._threads if t.is_alive()]
        for name, channel in self._background:
            if name in skip:
                continue
            try:
                self._threads.append(self._spawn(_once(channel, name, title, text, done)))
            except Exception as exc:  # noqa: BLE001 - a thread that cannot start
                # Reported like any failed send, so the caller never raises into the bot's
                # loop and never waits for an outcome that cannot come.
                done(name, f"not started: {type(exc).__name__}")
        results: list[tuple[str, str | None]] = []
        for name, channel in self._channels:
            if name in skip:
                continue
            try:
                channel.notify(title, text)
            except NotifyError as exc:
                results.append((name, str(exc)))
            else:
                results.append((name, None))
        return results

    def notify(self, title: str, text: str) -> None:
        failed = [f"{n}: {e}" for n, e in self.send(title, text, _ignore) if e is not None]
        if failed:
            raise NotifyError("; ".join(failed))

    def wait(self) -> None:
        """Until the background sends started so far have finished (each is bounded by its own
        osascript timeout). For short-lived commands, which would otherwise exit first."""
        for thread in self._threads:
            thread.join()
        self._threads = []


def _ignore(_name: str, _error: str | None) -> None:
    return None


def _once(channel: Notifier, name: str, title: str, text: str, done: Done) -> Callable[[], None]:
    def run() -> None:
        try:
            channel.notify(title, text)
        except NotifyError as exc:
            done(name, str(exc))
        except Exception as exc:  # noqa: BLE001 - a background channel never raises upward
            done(name, f"{type(exc).__name__}")
        else:
            done(name, None)
    return run


def default_notifier(config: AlertsConfig, platform: str = sys.platform,
                     environ: Mapping[str, str] = os.environ,
                     feed: Path | None = None) -> Notifier | None:
    """The channels for this machine (Mac notification, iMessage and, given its path, the
    alerts feed file, all in the background: the Tickr app and osascript can each take
    seconds, and the bot's loop must not wait for them), or None where there are none (CI,
    Linux)."""
    if platform == DARWIN:
        feeds: tuple[tuple[str, Notifier], ...] = (
            () if feed is None else ((FEED, FeedNotifier(feed)),))
        return Channels((), ((MACOS, mac_notifier(config)),
                             (IMESSAGE, IMessageNotifier(config, environ)), *feeds))
    return None


@dataclass(frozen=True)
class AlertOutcome:
    """What happened to one alert. The caller journals `journal_body()`.

    `pending` names background channels still sending; each later yields its own outcome
    (from `Alerter.completed`) with `channel` set. `routed_off` names the channels the owner's
    routing skipped for this kind."""

    kind: AlertKind
    text: str
    at: datetime
    delivery: Delivery
    error: str | None
    pending: tuple[str, ...] = ()
    channel: str | None = None
    routed_off: tuple[str, ...] = ()

    def journal_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": self.kind.value,
            "text": self.text,
            "at": self.at.isoformat(),
            "delivery": self.delivery.value,
            "error": self.error,
        }
        if self.pending:
            body["pending"] = list(self.pending)
        if self.channel is not None:
            body["channel"] = self.channel
        if self.routed_off:
            body["routed_off"] = list(self.routed_off)
        return body


def _utc_now() -> datetime:
    return datetime.now(UTC)


# The channels (by their names in `Channels`) the owner's routing turned off for one kind;
# `alert_routing.Routing.skipped` reads the routing file on each call.
Routing = Callable[[AlertKind], frozenset[str]]


def route_all(_kind: AlertKind) -> frozenset[str]:
    return frozenset()


class Alerter:
    """Shows alerts with repeat suppression. One instance per process.

    An alert counts as shown (for repeat suppression) once any channel delivered it, so a
    channel that keeps failing does not repeat the others on every occurrence. While a
    background send of the same alert is still running it is a repeat too, so a condition
    raised on every loop pass starts one send, not one per pass. Background
    results arrive through `completed()`, which the owner of the journal calls regularly (the
    bot once per loop pass), and `flush()` before a short-lived process exits.

    `routing` names the channels to skip for a kind (the owner's routing; never a critical
    kind). An alert every channel of which is skipped is `routed-off`, never counted as shown."""

    def __init__(
        self,
        config: AlertsConfig,
        title: str,
        notifier: Notifier | None,
        clock: Callable[[], datetime] = _utc_now,
        routing: Routing = route_all,
    ) -> None:
        self._window = timedelta(seconds=float(config.dedupe_window_seconds))
        self._title = title
        self._channels = (notifier if isinstance(notifier, Channels) or notifier is None
                          else Channels(((NOTIFIER, notifier),)))
        self._clock = clock
        self._routing = routing
        self._last_shown: dict[tuple[AlertKind, str], datetime] = {}
        # Background sends of each alert not yet collected by completed().
        self._sending: dict[tuple[AlertKind, str], int] = {}
        self._done: queue.SimpleQueue[tuple[AlertKind, str, str, str | None]] = \
            queue.SimpleQueue()

    def alert(self, kind: AlertKind, text: str) -> AlertOutcome:
        if not isinstance(kind, AlertKind):
            raise TypeError(f"kind must be an AlertKind, got {kind!r}")
        now = self._clock()
        key = (kind, text)
        last = self._last_shown.get(key)
        if (last is not None and now - last < self._window) or key in self._sending:
            return AlertOutcome(kind, text, now, Delivery.SUPPRESSED_REPEAT, None)
        if self._channels is None:
            return AlertOutcome(kind, text, now, Delivery.NO_NOTIFIER, None)

        def done(name: str, error: str | None) -> None:  # on a background thread
            self._done.put((kind, text, name, error))

        try:
            skip = self._routing(kind)
        except Exception:  # noqa: BLE001 - an injected routing never drops an alert
            skip = frozenset()
        routed_off = tuple(name for name in self._channels.names() if name in skip)
        if routed_off and len(routed_off) == len(self._channels.names()):
            return AlertOutcome(kind, text, now, Delivery.ROUTED_OFF, None,
                                routed_off=routed_off)
        pending = self._channels.background_except(skip)
        if pending:
            self._sending[key] = len(pending)
        try:
            results = self._channels.send(f"{self._title}: {kind.value}", text, done, skip)
        except BaseException:
            # No outcome will follow for sends that never started: the alert must not stay
            # suppressed until the process restarts.
            self._sending.pop(key, None)
            raise
        failed = [error if name == NOTIFIER else f"{name}: {error}"
                  for name, error in results if error is not None]
        if len(failed) < len(results):
            self._last_shown[key] = now
        if failed:
            # The next occurrence tries again (never an automatic retry).
            return AlertOutcome(kind, text, now, Delivery.FAILED, "; ".join(failed), pending,
                                routed_off=routed_off)
        if not results:  # background channels only: their outcomes follow
            return AlertOutcome(kind, text, now, Delivery.SENDING, None, pending,
                                routed_off=routed_off)
        return AlertOutcome(kind, text, now, Delivery.SHOWN, None, pending,
                            routed_off=routed_off)

    def completed(self) -> list[AlertOutcome]:
        """Outcomes of background sends that finished since the last call."""
        outcomes = []
        while True:
            try:
                kind, text, name, error = self._done.get_nowait()
            except queue.Empty:
                return outcomes
            now = self._clock()
            left = self._sending.get((kind, text), 1) - 1
            if left > 0:
                self._sending[(kind, text)] = left
            else:
                self._sending.pop((kind, text), None)
            if error is None:
                self._last_shown[(kind, text)] = now
            outcomes.append(AlertOutcome(kind, text, now,
                                         Delivery.SHOWN if error is None else Delivery.FAILED,
                                         None if error is None else f"{name}: {error}",
                                         channel=name))

    def flush(self) -> list[AlertOutcome]:
        """Wait for the background sends, then their outcomes (short-lived commands only)."""
        if self._channels is not None:
            self._channels.wait()
        return self.completed()


def token_pending_text(wait_seconds: int) -> str:
    """Text of the TOKEN_NEEDS_CONFIRMATION alert: the SDK now waits for the owner."""
    return f"approve the API token in the Webull app within {wait_seconds} s"
