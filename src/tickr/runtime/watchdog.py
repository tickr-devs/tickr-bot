"""Watchdog (spec 11): a separate launchd job that outlives a crashed bot.

The protective stop is a DAY order. If the bot dies and is (deliberately) not restarted, nothing
flattens at the flatten time and the position goes overnight without a broker-side stop; the
bot's own day-end check dies with the bot. The watchdog is its own process: it never takes the
bot's single-instance lock, opens the bot's database read-only (SQLite `mode=ro`), journals to
its own file, and sees the broker only through `ReadOnlyBroker`, which has the two reads and
nothing else, so it cannot send a trading request.

On an open market day (half days too: a position after the flatten time is wrong on any day):
- `[runtime] watchdog_after_flatten_seconds` after the flatten time and again
  `watchdog_before_close_seconds` before the regular close, it reads positions and open orders.
  Any position or any open order (ours or not) alerts `watchdog-not-flat`; a failed read alerts
  `watchdog-cannot-check`. Checks whose time passed before the job started collapse into one
  check at once.
- every `watchdog_bot_check_seconds` in the regular session until its last check: unless the
  bot looks alive, while the database shows an open trade, it alerts `bot-down-with-open-trade`.
  Alive means BOTH a live pid in the bot's lock file AND a status file (`[api] status_file`,
  written on every loop pass) whose `written_at` is at most `watchdog_status_stale_seconds`
  old. A pid alone is not proof: after a crash or a reboot it may belong to another process
  (or user), and a hung bot keeps its pid. A missing, unreadable or future-dated status file
  means not running.
- from `watchdog_restart_before_flatten_seconds` before the flatten time (15:40 ET on a normal
  day; config refuses a time inside the entry window), a bot that is not running while the
  database shows an open trade is started ONCE a day: the watchdog runs
  `watchdog_restart_command` (launchd's kickstart of the bot job: the same preflight and
  supervised bot as the scheduled start, never a second instance). The bot then recovers,
  reconciles, and its own `flatten_due` closes the trade; the watchdog still sends nothing.
  The attempt is recorded in `<data_dir>/<watchdog_restart_file>` (the New York date) BEFORE
  the command runs, so a day never gets a second one, also across a watchdog restart. Alerts
  `watchdog-restarted-bot`. Refused, with `watchdog-restart-refused`, while the operator's stop
  marker exists (the watchdog never overrides an operator stop; a stop with an open trade is
  for the owner to look at), when the record is unreadable or cannot be written, or when the
  clock's New York date is not the calendar day. After a restart, a broker check that is not
  flat, or that finds the bot still down with an open trade, alerts `watchdog-restart-failed`
  instead of `watchdog-not-flat`.
- earlier, at any bot check in the regular session: a bot that is not running while the
  database shows an open trade with a rest time (`trades.entry_rest_until`) whose entry may
  still work at the broker is started at once, also once a day, recorded in its own
  `<data_dir>/<watchdog_entry_restart_file>` (so the flatten restart above stays available):
  the broker order is a DAY limit, so it could fill with no stop until the bot starts; the
  started bot cancels every working entry at startup and protects any fill. Same refusals
  (operator stop marker, unreadable record, wrong day) and alerts. That run takes no new entry:
  at startup the bot reads this record and, when it is today's, sets the `watchdog-started`
  fence, which only the operator clears (runtime/service.py), so a bot started unattended
  still never opens a trade on its own. After an early restart, a later not-flat check alerts
  `watchdog-not-flat` (the "after a restart" rule above reads the flatten restart record only).
At every hour, every day (the `Sentinel`, every `watchdog_bot_check_seconds`; the job runs
continuously: launchd starts it at load, at `watchdog_start_local` and again whenever it ends):
- the bot stopped without a clean stop: not running (as above) while its last status is not
  `stopped` (a crash, a kill, a reboot or a hang leaves `starting`/`running`, or `stopping` older
  than `bot_exit_timeout_seconds`) alerts `bot-down` once per such status, when two checks in a
  row see it. A clean stop (a release, the operator, launchd's SIGTERM) writes `stopped` and
  stays quiet; a data folder where the bot never wrote a status stays quiet; an unreadable
  status file alerts. A live bot process whose status is `starting` or older than the process
  is starting, quiet for `watchdog_start_grace_seconds` after it took its lock, then hung.
- free space on the data disk below `[runtime] disk_warn_free_mb` alerts `disk-low` once, and
  once more below `min_free_disk_mb` (where preflight refuses to start the bot); back above the
  warning level re-arms it.
- the API's `device-session` alerts (a device signed in with tickr.bot, signed out, or signed
  out by the bot) are relayed to Telegram: the API holds no credential variable, so it writes
  them only to the alerts feed, which a device signed in with a stolen key could read too. Each
  check reads the feed's new lines (from the offset kept in the state file; a first run starts
  at the end, a shorter file from its start) and alerts every `device-session` line again, its
  text marked `RELAYED` so the copy the watchdog's own alert adds to the feed is never relayed.
Every broker check, every "bot down" finding and every alert is one JSON line on stdout (the
job log) and in `<data_dir>/<watchdog_journal_file>`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from tickr.api import contract
from tickr.broker.base import Broker, BrokerError
from tickr.calendar import NEW_YORK, CalendarCause, CalendarRefused, MarketDay, SessionCalendar
from tickr.config import Config
from tickr.engine.core import Leg, SendState, TradeState
from tickr.lock import recorded_holder
from tickr.runtime import stopmarker
from tickr.runtime.alerts import Alerter, AlertKind, AlertOutcome, feed_path
from tickr.store import Store
from tickr.types import OrderDetail, OrderState, Position

START = "watchdog-start"
CHECK = "watchdog-check"
BOT_DOWN = "watchdog-bot-down"
ALERT = "alert"
RESTART = "watchdog-restart"
UID_PLACEHOLDER = "{uid}"
JOURNAL_FAILED = "watchdog-journal-failed"
CHANNEL_REFUSED = "watchdog-alert-channel-refused"
DISK = "watchdog-disk"
STATE_FAILED = "watchdog-state-failed"
BYTES_PER_MB = 1024 * 1024  # unit factor
# Calendar answers that mean "no session today" (exit without an alert, like preflight).
CLOSED_CAUSES = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})


# Runs a command with a timeout in seconds and returns its exit code; replaced in tests.
Runner = Callable[[Sequence[str], float], int]
# Free bytes on the volume holding a folder; replaced in tests.
DiskFree = Callable[[Path], int]


def disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def ny_day(now: datetime) -> date:
    return now.astimezone(NEW_YORK).date()


def next_ny_midnight(now: datetime) -> datetime:
    """The start of the next New York date after `now`, in UTC: a day's run ends there."""
    day = now.astimezone(NEW_YORK).date() + timedelta(days=1)
    return datetime.combine(day, time(), NEW_YORK).astimezone(UTC)


def run_command(command: Sequence[str], timeout: float) -> int:
    """Run the restart command; its output is discarded, only the exit code counts."""
    return subprocess.run(list(command), check=False, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout).returncode


def restart_command(config: Config) -> list[str]:
    return [part.replace(UID_PLACEHOLDER, str(os.getuid()))
            for part in config.runtime.watchdog_restart_command]


def restart_file(config: Config) -> Path:
    return config.root / config.run.data_dir / config.runtime.watchdog_restart_file


def entry_restart_file(config: Config) -> Path:
    return config.root / config.run.data_dir / config.runtime.watchdog_entry_restart_file


def restarted_on(path: Path) -> date | None:
    """New York date of the last recorded restart, None when there never was one. An
    unreadable record raises (OSError, ValueError, KeyError, TypeError): never guessed."""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    return date.fromisoformat(json.loads(text)["ny_date"])


class Reads(Protocol):
    def positions(self) -> list[Position]: ...

    def open_orders(self) -> list[OrderDetail]: ...


class ReadOnlyBroker:
    """The watchdog's only view of the broker: positions and open orders, nothing that sends."""

    __slots__ = ("_broker",)

    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    def positions(self) -> list[Position]:
        return self._broker.positions()

    def open_orders(self) -> list[OrderDetail]:
        return self._broker.open_orders()


def market_day(config: Config, now: datetime) -> MarketDay:
    """Today's sessions, or CalendarRefused (WEEKEND/HOLIDAY mean no session)."""
    return SessionCalendar.load((config.root / config.session.calendar_file).parent).day_at(now)


def check_times(config: Config, day: MarketDay) -> tuple[datetime, datetime]:
    """The two broker checks of `day` (UTC): after the flatten time, and before the close."""
    flatten = day.before_close(config.session.flatten_minutes_before_close).astimezone(UTC)
    close = day.regular_close.astimezone(UTC)
    first = flatten + timedelta(seconds=config.runtime.watchdog_after_flatten_seconds)
    last = close - timedelta(seconds=config.runtime.watchdog_before_close_seconds)
    if not flatten < first < last < close:
        raise CalendarRefused(CalendarCause.INVALID_OFFSET,
                              f"watchdog checks {first.isoformat()} and {last.isoformat()} are "
                              f"not between flatten {flatten.isoformat()} and the close")
    return first, last


def open_trades(database: Path) -> int:
    """Open trades in the bot's database, read-only; 0 when it was never created."""
    if not database.exists():
        return 0
    store = Store.open_readonly(database)
    try:
        rows = store.query("SELECT COUNT(*) AS n FROM trades WHERE state != ?",
                           (TradeState.CLOSED.value,))
    finally:
        store.close()
    return int(rows[0]["n"])


# An entry that may still work at the broker: sent (or maybe sent) and not shown ended.
_WORKING = tuple(s.value for s in OrderState if not s.terminal)
_SENT = tuple(s.value for s in (SendState.ATTEMPTED, SendState.ACKNOWLEDGED,
                                SendState.UNKNOWN))


def resting_entries(database: Path) -> int:
    """Open trades with a rest time (`trades.entry_rest_until`) whose entry may still work at
    the broker, read-only; 0 when the database was never created."""
    if not database.exists():
        return 0
    store = Store.open_readonly(database)
    try:
        rows = store.query(
            "SELECT COUNT(*) AS n FROM trades t WHERE t.state != ? "
            "AND t.entry_rest_until IS NOT NULL AND EXISTS (SELECT 1 FROM orders o "
            "WHERE o.trade_id = t.trade_id AND o.leg = ? "
            f"AND o.send_state IN ({', '.join('?' * len(_SENT))}) "
            f"AND (o.broker_state IS NULL OR o.broker_state IN "
            f"({', '.join('?' * len(_WORKING))})))",
            (TradeState.CLOSED.value, Leg.ENTRY.value, *_SENT, *_WORKING))
    finally:
        store.close()
    return int(rows[0]["n"])


def not_running(config: Config, lock_path: Path, status_path: Path,
                now: datetime) -> str | None:
    """Why the bot does not count as running, or None when it does."""
    if recorded_holder(lock_path) is None:
        return "no live pid in its lock file"
    try:
        status = contract.read_status(status_path)
    except FileNotFoundError:
        return "no status file"
    except (contract.ContractError, OSError):
        return "status file unreadable"
    age = contract.seconds_between(status.written_at, now)
    stale = config.runtime.watchdog_status_stale_seconds
    # `now` is read before the file, so a write landing in between looks slightly ahead:
    # allow [risk] max_clock_skew_seconds of lead before calling it "future".
    if age < -config.risk.max_clock_skew_seconds:
        return "status file dated in the future"
    if age > stale:
        return f"status file older than {stale} s"
    return None


class Journal:
    """JSON lines to stdout and to the watchdog's own file (never the bot's database). Two
    threads write it (the watchdog's loop and the Live Activity publisher), so each line is
    written whole under one lock: lines never interleave on stdout or in the file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: str, body: dict[str, Any]) -> None:
        line = json.dumps({"record": record, **body}, sort_keys=True, default=str)
        with self._lock:
            print(line, flush=True)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                print(json.dumps({"record": JOURNAL_FAILED, "error": type(exc).__name__}),
                      flush=True)


RELAY_OFFSET = "relay_offset"  # state-file key: bytes of the alerts feed already relayed
RELAYED = "RELAYED from the API:"  # marks the watchdog's copy, so it is never relayed again


def _relayed_text(raw: bytes) -> str | None:
    """The text the watchdog relays for a feed line: the API's `device-session`, never its own
    copy; None for any other line, or one that is not JSON."""
    try:
        line = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(line, dict):
        return None
    title, text, at = line.get("title"), line.get("text"), line.get("at")
    if not isinstance(title, str) or not isinstance(text, str):
        return None
    if not title.endswith(f" {AlertKind.DEVICE_SESSION.value}") or text.startswith(RELAYED):
        return None
    # The API's time keeps two same-text sign-ins apart (the alerter drops a repeat of a text).
    return f"{text} (at {at})" if isinstance(at, str) else text


def _ny(instant: datetime) -> str:
    return instant.astimezone(NEW_YORK).strftime("%H:%M:%S ET")


def _cause(exc: Exception) -> str:
    # Adapter errors carry names and causes only; anything else is reported by type alone.
    return f"{type(exc).__name__}: {exc}" if isinstance(exc, BrokerError) else type(exc).__name__


class Sentinel:
    """The checks that run at every hour, also on days without a session: did the bot stop
    without a clean stop (or hang), is the data disk running low. Each finding alerts once
    while it lasts. What was alerted is kept in `<data_dir>/<watchdog_state_file>`, so a
    relaunched watchdog (launchd `KeepAlive`) never alerts the same finding again; when that
    file cannot be written the finding is still alerted and the process keeps running, so
    only a relaunch (at most once per `watchdog_relaunch_seconds`) could repeat it."""

    def __init__(self, config: Config, *, alerter: Alerter, journal: Journal,
                 lock_path: Path, status_path: Path, data_dir: Path,
                 free: DiskFree = disk_free) -> None:
        self.config = config
        self._alerter = alerter
        self.journal = journal
        self._lock_path = lock_path
        self._status_path = status_path
        self._data_dir = data_dir
        self._free = free
        self._state_path = data_dir / config.runtime.watchdog_state_file
        self._state = self._load_state()
        self._candidate: str | None = None  # a bot finding seen once, alerted if seen again
        self.alerted = False

    # --- the alert state that survives a relaunch -----------------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._state_path.read_text("utf-8"))
            if not isinstance(state, dict):
                raise ValueError("not an object")
            return state
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Unknown: start empty (a finding may alert once more), and say so.
            self.journal.write(STATE_FAILED, {"read": type(exc).__name__})
            return {}

    def _remember(self, key: str, value: Any) -> None:
        if self._state.get(key) == value:
            return
        self._state[key] = value
        try:
            contract.write_atomic(self._state_path, json.dumps(self._state, sort_keys=True))
        except OSError as exc:
            self.journal.write(STATE_FAILED, {"write": type(exc).__name__})

    def alert(self, kind: AlertKind, text: str) -> None:
        self.alerted = True
        self.journal.write(ALERT, self._alerter.alert(kind, text).journal_body())

    def alert_once(self, field: str, key: str, kind: AlertKind, text: str) -> None:
        """Alert unless this finding (`key`) was already alerted, by this process or by an
        earlier one."""
        if self._state.get(field) == key:
            return
        self._remember(field, key)
        self.alert(kind, text)

    def completed(self, flush: bool = False) -> list[AlertOutcome]:
        """Finished background sends (Telegram); with `flush`, after waiting for them."""
        return self._alerter.flush() if flush else self._alerter.completed()

    def check(self, now: datetime) -> None:
        self.bot(now)
        self.disk(now)
        self.relay()

    # --- the API's device-session alerts, to Telegram ---------------------------------------

    def relay(self) -> None:
        """Alert again each `device-session` line the API added to the alerts feed since the
        last check; a feed that cannot be read is journaled and tried at the next check."""
        path = feed_path(self.config.root, self.config.run.data_dir, self.config.alerts)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        except OSError as exc:
            self.journal.write(STATE_FAILED, {"feed": type(exc).__name__})
            return
        offset = self._state.get(RELAY_OFFSET)
        if not isinstance(offset, int) or isinstance(offset, bool):
            self._remember(RELAY_OFFSET, size)  # a first run relays nothing from before it
            return
        if size < offset:
            offset = 0  # the feed was replaced: read it from its start
        if size == offset:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(size - offset)
        except OSError as exc:
            self.journal.write(STATE_FAILED, {"feed": type(exc).__name__})
            return
        complete = chunk.rfind(b"\n") + 1  # a line still being written waits for the next check
        self._remember(RELAY_OFFSET, offset + complete)
        for raw in chunk[:complete].splitlines():
            text = _relayed_text(raw)
            if text is not None:
                self.alert(AlertKind.DEVICE_SESSION, f"{RELAYED} {text}")

    # --- the bot ---------------------------------------------------------------------------

    def _started_at(self) -> datetime | None:
        """When the live bot process took its lock (it writes its pid then), or None."""
        if recorded_holder(self._lock_path) is None:
            return None
        try:
            return datetime.fromtimestamp(self._lock_path.stat().st_mtime, UTC)
        except OSError:
            return None

    def _finding(self, now: datetime) -> tuple[str, str] | None:
        """(key, alert text) when the bot is down or hung, None when it runs, starts or
        stopped cleanly."""
        why = not_running(self.config, self._lock_path, self._status_path, now)
        if why is None:
            return None
        runtime = self.config.runtime
        try:
            status = contract.read_status(self._status_path)
        except FileNotFoundError:
            return None  # the bot never ran on this data folder: nothing stopped
        except (contract.ContractError, OSError) as exc:
            status, last = None, "an unreadable status file"
            key = f"unreadable {type(exc).__name__}"
        else:
            if status.bot_state is contract.BotState.STOPPED:
                return None  # a clean stop (release, operator, launchd): nothing to say
            if (status.bot_state is contract.BotState.STOPPING
                    and contract.seconds_between(status.written_at, now)
                    <= runtime.bot_exit_timeout_seconds):
                return None  # still stopping: the supervisor alerts bot-did-not-stop
            key = status.written_at.isoformat()
            last = f"its last status ({status.bot_state.value}) at {_ny(status.written_at)}"
        started = self._started_at()
        if started is not None:
            # A live bot process: starting while its status is "starting" or older than the
            # process; quiet for the start grace, then hung. Never "start the bot job": a
            # kickstart does nothing while it holds the job.
            starting = status is not None and (
                status.bot_state is contract.BotState.STARTING or status.written_at < started)
            age = contract.seconds_between(started, now)
            if starting and age <= runtime.watchdog_start_grace_seconds:
                return None
            what = (f"still starting {age:.0f} s after it started" if starting
                    else f"not answering ({why})")
            return (f"hung {started.isoformat()} {key}",
                    f"the bot process is alive but {what}: {last}. It is hung: nothing trades "
                    "and nothing watches the stops. Read bot.log, then stop the bot job "
                    "(launchctl kill TERM) and start it again when it is safe")
        return (key, f"the bot is not running ({why}) and did not stop cleanly: {last}. "
                     "Nothing trades and nothing watches the stops until it runs again. Read "
                     "bot.log for the cause, then start the bot job when it is safe")

    def bot(self, now: datetime) -> None:
        """A finding alerts when two checks in a row see it (one stale status right after the
        Mac wakes is not a crash), once per finding."""
        found = self._finding(now)
        if found is None:
            self._candidate = None
            self._remember("bot_down", None)
            return
        key, text = found
        if self._candidate != key:
            self._candidate = key
            return
        if self._state.get("bot_down") == key:
            return
        self.journal.write(BOT_DOWN, {"at": now.isoformat(), "finding": key})
        self.alert_once("bot_down", key, AlertKind.BOT_DOWN, text)

    # --- the disk --------------------------------------------------------------------------

    def disk(self, now: datetime) -> None:
        runtime = self.config.runtime
        try:
            free_mb = self._free(self._data_dir) // BYTES_PER_MB
        except OSError as exc:
            self.journal.write(DISK, {"at": now.isoformat(), "error": type(exc).__name__})
            return
        if free_mb >= runtime.disk_warn_free_mb:
            self._remember("disk_level", None)
            return
        level = (runtime.min_free_disk_mb if free_mb < runtime.min_free_disk_mb
                 else runtime.disk_warn_free_mb)
        alerted = self._state.get("disk_level")
        if isinstance(alerted, int) and alerted <= level:
            return  # already alerted at this level or a lower one
        self.journal.write(DISK, {"at": now.isoformat(), "free_mb": free_mb, "below_mb": level})
        self._remember("disk_level", level)
        self.alert(AlertKind.DISK_LOW,
                   f"{free_mb} MB free on the bot's data disk, below {level} MB. Below "
                   f"{runtime.min_free_disk_mb} MB the bot does not start, and a full disk stops "
                   "it: free space now (research data, caches)")


def watch_until(sentinel: Sentinel, until: datetime, *, clock: Callable[[], datetime],
                sleep: Callable[[float], None]) -> None:
    """The sentinel's checks alone until `until` (a day without a session or a calendar)."""
    interval = float(sentinel.config.runtime.watchdog_bot_check_seconds)
    while (now := clock()) < until:
        for outcome in sentinel.completed():
            sentinel.journal.write(ALERT, outcome.journal_body())
        sentinel.check(now)
        sleep(min(interval, (until - now).total_seconds()))
    for outcome in sentinel.completed(flush=True):
        sentinel.journal.write(ALERT, outcome.journal_body())


class Watchdog:
    def __init__(self, config: Config, day: MarketDay, *, connect: Callable[[], Reads],
                 alerter: Alerter, journal: Journal, clock: Callable[[], datetime],
                 sleep: Callable[[float], None], lock_path: Path, database: Path,
                 status_path: Path, runner: Runner, sentinel: Sentinel | None = None) -> None:
        self.config = config
        self.sentinel = sentinel
        self.day = day
        self.times = check_times(config, day)
        flatten = day.before_close(config.session.flatten_minutes_before_close).astimezone(UTC)
        self.restart_at = flatten - timedelta(
            seconds=config.runtime.watchdog_restart_before_flatten_seconds)
        self._restart_file = restart_file(config)
        self._entry_restart_file = entry_restart_file(config)
        self._runner = runner
        self.interval = float(config.runtime.watchdog_bot_check_seconds)
        self._status_path = status_path
        self._connect = connect
        self._reads: Reads | None = None
        self._alerter = alerter
        self.journal = journal
        self._clock = clock
        self._sleep = sleep
        self._lock_path = lock_path
        self._database = database
        self.alerted = False

    def alert(self, kind: AlertKind, text: str) -> None:
        self.alerted = True
        self.journal.write(ALERT, self._alerter.alert(kind, text).journal_body())

    def _background_outcomes(self, flush: bool = False) -> None:
        """Journal finished background sends (Telegram); at exit wait for them first."""
        for outcome in self._alerter.flush() if flush else self._alerter.completed():
            self.journal.write(ALERT, outcome.journal_body())

    def run(self, until: datetime | None = None) -> int:
        """Until the last broker check, or until `until` when it is later (the sentinel's
        checks go on to it); 1 if anything alerted, else 0."""
        self.journal.write(START, {"day": self.day.ny_date.isoformat(),
                                   "checks": [t.isoformat() for t in self.times]})
        pending = list(self.times)
        while True:
            self._background_outcomes()
            now = self._clock()
            if pending and now >= pending[0]:
                pending = [t for t in pending if t > now]
                self.broker_check(now)
                continue
            if pending:
                wake = pending[0]
            elif until is not None and now < until:
                wake = until
            else:
                break
            if self.sentinel is not None:
                self.sentinel.check(now)
            if self.day.in_regular_session(now):
                self.bot_check(now)
            self._sleep(min(self.interval, (wake - now).total_seconds()))
        self._background_outcomes(flush=True)
        return 1 if self.alerted or (self.sentinel is not None and self.sentinel.alerted) else 0

    def broker_check(self, now: datetime) -> None:
        try:
            if self._reads is None:
                self._reads = self._connect()
            positions = [p for p in self._reads.positions() if p.quantity != 0]
            orders = self._reads.open_orders()
        except Exception as exc:  # noqa: BLE001 - any failure means "unknown", never "flat"
            cause = _cause(exc)
            self.journal.write(CHECK, {"at": now.isoformat(), "broker_read": False,
                                       "cause": cause})
            self.alert(AlertKind.WATCHDOG_CANNOT_CHECK,
                       f"{_ny(now)}: broker read failed ({cause}). Open the Webull app and "
                       "check positions and orders yourself: the stop is DAY")
            return
        left = ([f"position {p.symbol} {p.quantity}" for p in positions]
                + [f"order {o.client_order_id} {o.symbol} {o.side} {o.order_type}"
                   for o in orders])
        self.journal.write(CHECK, {"at": now.isoformat(), "broker_read": True,
                                   "positions": len(positions), "open_orders": len(orders)})
        if self._restarted_today():
            self._after_restart(now, left)
        elif left:
            self.alert(AlertKind.WATCHDOG_NOT_FLAT,
                       f"{_ny(now)}: not flat after the flatten time: " + "; ".join(left)
                       + ". Open the Webull app: close it, or place a stop by hand; the stop "
                       f"is DAY and expires at {_ny(self.day.regular_close)}")

    def not_running(self, now: datetime) -> str | None:
        """Why the bot does not count as running, or None when it does."""
        return not_running(self.config, self._lock_path, self._status_path, now)

    def down_with_open_trades(self, now: datetime) -> tuple[str, int] | None:
        """(why not running, open trades) when the bot is down while the database shows an
        open trade, else None. Raises sqlite3.Error when the database cannot be read."""
        why = self.not_running(now)
        if why is None:
            return None
        count = open_trades(self._database)
        return (why, count) if count else None

    def bot_check(self, now: datetime) -> None:
        try:
            down = self.down_with_open_trades(now)
        except sqlite3.Error as exc:
            self.alert(AlertKind.WATCHDOG_CANNOT_CHECK,
                       f"bot not running and its database is unreadable ({type(exc).__name__})")
            return
        if down is None:
            return
        why, count = down
        self.journal.write(BOT_DOWN, {"at": now.isoformat(), "open_trades": count,
                                      "why": why})
        if now >= self.restart_at and self.restart(now, why, count):
            return
        if now < self.restart_at and self._resting(now) and self.restart(
                now, why, count, record=self._entry_restart_file, early=True):
            return
        self.alert(AlertKind.BOT_DOWN_WITH_OPEN_TRADE,
                   f"the bot is not running ({why}) and the database shows {count} open "
                   "trade(s): "
                   "nothing will flatten it. Open the Webull app: check the stop, close by hand "
                   f"before {_ny(self.day.regular_close)}")

    def _resting(self, now: datetime) -> bool:
        """An entry with a rest time may still work at the broker while the bot is down: a
        fill would stay without a stop until the bot starts. False when unreadable (the
        bot-down alert still fires)."""
        try:
            return resting_entries(self._database) > 0
        except sqlite3.Error as exc:
            self.journal.write(BOT_DOWN, {"at": now.isoformat(),
                                          "resting_entries": f"unreadable "
                                                             f"({type(exc).__name__})"})
            return False

    # --- the one restart a day (and one early one for a resting entry) ---------------------

    def _restarted_today(self) -> bool:
        try:
            return restarted_on(self._restart_file) == self.day.ny_date
        except (OSError, ValueError, KeyError, TypeError):
            return False  # unknown: the plain alerts apply (the refusal was alerted)

    def _refuse(self, now: datetime, cause: str) -> bool:
        self.journal.write(RESTART, {"at": now.isoformat(), "started": False, "cause": cause})
        self.alert(AlertKind.WATCHDOG_RESTART_REFUSED,
                   "the bot is not running with an open trade and the watchdog will not start "
                   f"it: {cause}. Open the Webull app: close the position by hand before "
                   f"{_ny(self.day.regular_close)}; the stop is DAY")
        return True

    def restart(self, now: datetime, why: str, count: int, *, record: Path | None = None,
                early: bool = False) -> bool:
        """Start the bot job once today; False when today's attempt was already made (the
        caller then alerts bot-down as usual), True when this call alerted. `early`: the
        restart for a resting entry, before the flatten restart time, recorded in its own
        file (`record`) so the flatten restart stays available."""
        path = self._restart_file if record is None else record
        if now.astimezone(NEW_YORK).date() != self.day.ny_date:
            return self._refuse(now, "the clock's New York date is not the calendar day "
                                     f"{self.day.ny_date.isoformat()}")
        try:
            last = restarted_on(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return self._refuse(now, f"restart record {path.name} unreadable "
                                     f"({type(exc).__name__})")
        if last == self.day.ny_date:
            return False
        marker = stopmarker.describe(self.config)
        if marker is not None:
            return self._refuse(now, f"{marker}; the watchdog never starts a bot the operator "
                                     "stopped")
        try:  # durable before the command: a day never gets a second attempt
            contract.write_atomic(path, json.dumps(
                {"ny_date": self.day.ny_date.isoformat(), "at": now.isoformat(),
                 "why": why, "open_trades": count, "early": early}) + "\n")
        except OSError as exc:
            return self._refuse(now, f"restart record cannot be written ({type(exc).__name__})")
        runtime = self.config.runtime
        try:
            code = self._runner(restart_command(self.config),
                                float(runtime.watchdog_restart_timeout_seconds))
            failure = None if code == 0 else f"restart command exited {code}"
        except (OSError, subprocess.SubprocessError) as exc:
            failure = f"restart command failed ({type(exc).__name__})"
        self.journal.write(RESTART, {"at": now.isoformat(), "started": failure is None,
                                     "why": why, "open_trades": count, "cause": failure,
                                     "early": early,
                                     "command": list(runtime.watchdog_restart_command)})
        if failure is not None:
            self.alert(AlertKind.WATCHDOG_RESTART_FAILED,
                       f"URGENT: the bot is not running ({why}) with {count} open trade(s) and "
                       f"the watchdog could not start it: {failure}. Open the Webull app now: "
                       f"close the position by hand before {_ny(self.day.regular_close)}")
        else:
            purpose = ("an entry with a rest time may still work at the broker; the bot "
                       "cancels it at startup and protects any fill. New entries stay "
                       "blocked (fence watchdog-started) until you look and run: tickr "
                       "clear-fence watchdog-started" if early else
                       "so it flattens at the flatten time")
            self.alert(AlertKind.WATCHDOG_RESTARTED_BOT,
                       f"the bot was not running ({why}) with {count} open trade(s): the "
                       f"watchdog started the bot job once: {purpose}. Watch for a quiet "
                       "check after the flatten time")
        return True

    def _after_restart(self, now: datetime, left: list[str]) -> None:
        """A broker check on a day the watchdog restarted the bot: anything wrong is loud."""
        problems = list(left)
        try:
            down = self.down_with_open_trades(now)
        except sqlite3.Error as exc:
            down = None
            problems.append(f"bot database unreadable ({type(exc).__name__})")
        if down is not None:
            problems.append(f"bot still not running ({down[0]}) with {down[1]} open trade(s)")
        if problems:
            self.alert(AlertKind.WATCHDOG_RESTART_FAILED,
                       f"URGENT {_ny(now)}: the watchdog restarted the bot today and it did not "
                       "flatten: " + "; ".join(problems) + ". Open the Webull app now: close "
                       f"by hand; the stop is DAY and expires at {_ny(self.day.regular_close)}")
