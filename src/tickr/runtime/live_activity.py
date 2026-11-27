"""The iPhone Live Activity of a trading day, pushed over APNs (docs/api.md "Live Activity").

Runs in the watchdog process (`python -m tickr.runtime watchdog`), in a daemon thread of its
own, only while `[live_activity] enabled`. It never touches the bot: every `[api] poll_seconds`
it reads the status file the bot already writes for the API (status-v1) and the account file
(account-v1, for the broker's last price and unrealized P&L), derives the activity's
content-state, and compares what makes an event with what it last pushed:

- start (to the `activity_start` tokens) when a position is open and no activity is live: the
  day's first fill, or the first after the activity ended;
- update (to the `activity_update` tokens) when the state (open, paused, alarm), the day's
  result, the positions, a stop price or a stop's confirmation changed, and, with `[live_activity]
  pnl_refresh_seconds` above 0, at most that often for the prices and unrealized P&L alone;
- end when flat: state `flat`, with `dismissal-date` = now + `end_linger_seconds` when above 0.

What it pushed is written to `[live_activity] state_file` before the push, so a restarted
watchdog neither starts a second activity nor repeats an event. Each push is one attempt per
device, never retried (`alerts.ApnsClient`); a 410 or BadDeviceToken drops that token. A status
file that is missing, unreadable or older than `[api] status_stale_seconds` pushes nothing: the
activity keeps its last state, and `stops_confirmed_at` shows its age.

The engine never imports this module (tests/runtime/test_live_activity.py checks it).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any

from tickr.api import contract
from tickr.api.contract import AccountV1, PushTokenKind, StatusV1, StopState
from tickr.config import Config
from tickr.engine.core import OPERATOR_FENCES, Fence
from tickr.runtime.alerts import ApnsClient, NotifyError
from tickr.runtime.push_tokens import short_hash

# Apple, "Starting and updating Live Activities with ActivityKit push notifications": the push
# type, the topic suffix after the app's bundle id, and the two priorities (10 = at once, 5 =
# when the device's budget allows).
PUSH_TYPE = "liveactivity"
TOPIC_SUFFIX = ".push-type.liveactivity"
PRIORITY_NOW = "10"
PRIORITY_BUDGETED = "5"
CENTS_PER_DOLLAR = 100
STATE_VERSION = 1  # version of the state file's layout
RECORD = "live-activity"  # the watchdog journal's record name for each push and failure
# Fences that say a held position has no verified stop (the bot alerts unprotected-position).
UNPROTECTED_FENCES = frozenset({Fence.UNPROTECTED, Fence.UNPROTECTED_TIMEOUT})


class ActivityState(StrEnum):
    OPEN = "open"
    PAUSED = "paused"  # entries blocked by something that needs the owner (`EntryBlock.attention`)
    ALARM = "alarm"  # a position's stop is not confirmed while the bot alerts it
    FLAT = "flat"  # nothing open: the activity ends


class Event(StrEnum):
    START = "start"
    UPDATE = "update"
    END = "end"


@dataclass(frozen=True)
class ActivityPosition:
    symbol: str
    last: Decimal | None  # the broker's last price; null while the account read lacks it
    stop: Decimal | None  # the protective stop's price; null while it has none
    target: Decimal | None  # null: the bot has no profit target yet (backlog T13)
    unrealized_cents: int | None  # the broker's unrealized P&L; null while unread


@dataclass(frozen=True)
class ContentState:
    """The activity's content-state, field for field as the iOS app decodes it (agreed with the
    app, mockup v138). This dataclass is the one place its field set lives."""

    state: ActivityState
    day_pnl_cents: int  # the day's result as the apps show it: net of fees, gross until known
    positions: tuple[ActivityPosition, ...]
    stops_confirmed_at: int | None  # unix seconds of the last broker read with every stop verified
    reason_code: str | None  # paused: the entry block's cause; alarm: the fence's reason code
    alarm_symbol: str | None  # alarm: the position whose stop is not confirmed

    def jsonable(self) -> dict[str, Any]:
        return contract.to_jsonable(self)


@dataclass(frozen=True)
class Observation:
    """One look at the bot's files: the content-state, what a change of is an event, and the
    broker read time when every open position's stop was verified (else None)."""

    content: ContentState
    events: dict[str, Any]
    confirmed_at: int | None


def cents(usd: Decimal) -> int:
    return int((usd * CENTS_PER_DOLLAR).to_integral_value(ROUND_HALF_EVEN))


def _unix(at: datetime) -> int:
    return int(at.timestamp())


def _alarm_fence(status: StatusV1, now: datetime, alarm_after: Decimal) -> contract.Fence | None:
    """The fence the bot alerts as unprotected-position (runtime/watch.py: at once for an
    operator fence, after `[alerts] fence_alert_after_seconds` for one that clears itself)."""
    for fence in sorted(status.fences, key=lambda f: f.kind):
        base = fence.kind.split(":", 1)[0]
        if base not in UNPROTECTED_FENCES:
            continue
        if base in OPERATOR_FENCES or contract.seconds_between(fence.since, now) >= alarm_after:
            return fence
    return None


def observe(status: StatusV1, account: AccountV1 | None, now: datetime,
            alarm_after: Decimal) -> Observation:
    """The content-state of `status` (and of `account`, None when missing or stale)."""
    held = sorted(status.positions, key=lambda p: p.symbol)
    broker = {} if account is None else {p.symbol: p for p in account.positions}
    positions = []
    for p in held:
        row = broker.get(p.symbol)
        unrealized = None if row is None else row.unrealized_pl_usd
        positions.append(ActivityPosition(
            p.symbol, None if row is None else row.last_price, p.stop.price, None,
            None if unrealized is None else cents(unrealized)))
    unverified = [p.symbol for p in held if p.stop.state is not StopState.VERIFIED]
    state, reason, alarm_symbol = ActivityState.OPEN, None, None
    fence = _alarm_fence(status, now, alarm_after) if unverified else None
    if not held:
        state = ActivityState.FLAT
    elif fence is not None:
        state, reason, alarm_symbol = ActivityState.ALARM, fence.code, unverified[0]
    elif status.entry_block is not None and status.entry_block.attention:
        state, reason = ActivityState.PAUSED, status.entry_block.cause
    result = status.result_today_net_usd
    day = cents(status.result_today_gross_usd if result is None else result)
    read = status.freshness.last_broker_read_at
    confirmed = _unix(read) if held and not unverified and read is not None else None
    content = ContentState(state, day, tuple(positions), None, reason, alarm_symbol)
    events = {"state": state.value, "day_pnl_cents": day, "reason_code": reason,
              "alarm_symbol": alarm_symbol,
              "positions": [[p.symbol, contract.to_jsonable(p.stop.price),
                             p.stop.state is StopState.VERIFIED] for p in held]}
    return Observation(content, events, confirmed)


def payload(event: Event, content: ContentState, now: int, attributes_type: str,
            linger_seconds: int) -> bytes:
    """The push body: aps {timestamp, event, content-state}, plus the attributes on start and
    the dismissal date on end (none while `linger_seconds` is 0: the system default)."""
    aps: dict[str, Any] = {"timestamp": now, "event": event.value,
                           "content-state": content.jsonable()}
    if event is Event.START:
        aps["attributes-type"] = attributes_type
        aps["attributes"] = {}
    if event is Event.END and linger_seconds > 0:
        aps["dismissal-date"] = now + linger_seconds
    return json.dumps({"aps": aps}, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass(frozen=True)
class Sent:
    """What was last pushed, kept in the state file."""

    active: bool
    events: dict[str, Any]
    content: dict[str, Any]
    sent_at: int


Journal = Callable[[str, dict[str, Any]], None]


class Publisher:
    def __init__(self, config: Config, client: ApnsClient, journal: Journal,
                 clock: Callable[[], datetime]) -> None:
        data = config.root / config.run.data_dir
        self._config = config
        self._live = config.live_activity
        self._client = client
        self._journal = journal
        self._clock = clock
        self._status_path = data / config.api.status_file
        self._account_path = data / config.api.account_file
        self._state_path = data / self._live.state_file
        self._sent = self._load()

    def _load(self) -> Sent | None:
        try:
            raw = json.loads(self._state_path.read_text("utf-8"))
            if raw.get("version") != STATE_VERSION:
                raise ValueError("version")
            return Sent(bool(raw["active"]), dict(raw["events"]), dict(raw["content"]),
                        int(raw["sent_at"]))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Unknown: start as if no activity is live (one may start twice), and say so.
            self._journal(RECORD, {"state_file": f"unreadable ({type(exc).__name__})"})
            return None

    def _save(self, sent: Sent) -> None:
        contract.write_atomic(self._state_path, json.dumps(
            {"version": STATE_VERSION, "active": sent.active, "events": sent.events,
             "content": sent.content, "sent_at": sent.sent_at}, sort_keys=True))

    def _status(self, now: datetime) -> StatusV1 | None:
        try:
            status = contract.read_status(self._status_path)
        except (FileNotFoundError, contract.ContractError, OSError):
            return None
        age = contract.seconds_between(status.written_at, now)
        return status if 0 <= age <= self._config.api.status_stale_seconds else None

    def _account(self, now: datetime) -> AccountV1 | None:
        try:
            account = contract.read_account(self._account_path)
        except (FileNotFoundError, contract.ContractError, OSError):
            return None
        age = contract.seconds_between(account.read_at, now)
        return account if 0 <= age <= self._config.api.account_stale_seconds else None

    def step(self) -> Event | None:
        """One look; the event pushed, or None."""
        now = self._clock()
        status = self._status(now)
        if status is None:
            return None
        seen = observe(status, self._account(now), now,
                       self._config.alerts.fence_alert_after_seconds)
        sent = self._sent
        active = sent is not None and sent.active
        confirmed = seen.confirmed_at
        if confirmed is None and active:
            confirmed = sent.content.get("stops_confirmed_at")
        content = replace(seen.content, stops_confirmed_at=confirmed)
        unix = _unix(now)
        refresh = self._live.pnl_refresh_seconds
        if not active:
            if content.state is ActivityState.FLAT:
                return None
            event = Event.START
        elif content.state is ActivityState.FLAT:
            event = Event.END
        elif seen.events != sent.events:
            event = Event.UPDATE
        elif (refresh > 0 and unix - sent.sent_at >= refresh
              and content.jsonable() != sent.content):
            event = Event.UPDATE
        else:
            return None
        self._sent = Sent(event is not Event.END, seen.events, content.jsonable(), unix)
        self._save(self._sent)  # before the push: never pushed twice, also across a restart
        urgent = event is Event.END or content.state is ActivityState.ALARM
        self._push(event, content, unix, PRIORITY_NOW if urgent else PRIORITY_BUDGETED)
        return event

    def _push(self, event: Event, content: ContentState, now: int, priority: str) -> None:
        kind = PushTokenKind.ACTIVITY_START if event is Event.START else (
            PushTokenKind.ACTIVITY_UPDATE)
        body: dict[str, Any] = {"event": event.value, "state": content.state.value,
                                "priority": priority, "sent": 0, "failures": []}
        try:
            recipients = self._client.devices(kind)
            body["failures"].extend(recipients.failures)
            devices = [d for d in recipients.tokens if d.bundle_id == self._live.app_bundle_id]
        except NotifyError as exc:
            body["failures"].append(str(exc))
            devices = []
        data = payload(event, content, now, self._live.attributes_type,
                       self._live.end_linger_seconds)
        for device in devices:
            error = self._client.send(device, data, push_type=PUSH_TYPE, priority=priority,
                                      topic_suffix=TOPIC_SUFFIX)
            if error is None:
                body["sent"] += 1
            else:
                body["failures"].append(f"device {short_hash(device.token)}: {error}")
        self._journal(RECORD, body)

    def run(self, stop: threading.Event) -> None:
        """Look every `[api] poll_seconds` until `stop` is set. A failed look is journaled by
        its type and the next one goes on."""
        while not stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - this thread never ends the watchdog
                self._journal(RECORD, {"error": type(exc).__name__})
            stop.wait(float(self._config.api.poll_seconds))


def start(config: Config, client: ApnsClient, journal: Journal,
          clock: Callable[[], datetime]) -> threading.Thread | None:
    """The publisher's daemon thread when `[live_activity] enabled`; None otherwise."""
    if not config.live_activity.enabled:
        return None
    publisher = Publisher(config, client, journal, clock)
    thread = threading.Thread(target=publisher.run, args=(threading.Event(),),
                              name="live-activity", daemon=True)
    thread.start()
    return thread
