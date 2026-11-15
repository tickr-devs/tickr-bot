"""Versioned contract between the bot, the local API and its clients (status page, macOS app).

Two files on disk decouple the bot from the API process:
- the bot writes `StatusV1` to `<data_dir>/<status_file>` with `write_status` (temp file + rename);
- the bot writes `AccountV1` to `<data_dir>/<account_file>` with `write_account` after each
  successful broker read of positions and open orders (reconciliation), the same way;
- the API writes one `CommandV1` per operator command into `<data_dir>/<commands_dir>/inbox/`
  with `write_command`; the bot reads them with `read_commands`, journals each one, then moves it
  out of the inbox with `retire_command`.

Wire rules (stable for version 1; see docs/api.md):
- money, prices and quantities are decimal strings ("12.34"), never JSON numbers;
- times are UTC strings with milliseconds, "2026-11-14T18:44:39.123Z";
- every object has exactly the documented keys: unknown or missing keys are refused.
A change that breaks these rules or removes/renames a key is a new version, never an edit.
"""

from __future__ import annotations

import json
import os
import re
import types
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from tickr.api.reasons import UNRECOGNIZED as UNRECOGNIZED_CODE
from tickr.api.reasons import Reason, prose_reasons, refusal_params
from tickr.data.base import ScreenerKind
from tickr.replay.engine import ExitReason
from tickr.research.records import Admission, DecisionRecord
from tickr.risk.causes import ENGINE_REFUSED, AdmissionCause
from tickr.risk.ticks import round_to_tick
from tickr.strategy.base import Check, Decision, Role, Verdict

API_VERSION = 1
STATUS_SCHEMA_VERSION = 1
COMMAND_SCHEMA_VERSION = 1
ACCOUNT_SCHEMA_VERSION = 1

INBOX = "inbox"
PROCESSED = "processed"

DECIMAL_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"
TIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_DECIMAL = re.compile(DECIMAL_PATTERN)
_TIME = re.compile(TIME_PATTERN)
_UUID = re.compile(UUID_PATTERN)
_DATE = re.compile(DATE_PATTERN)
_COMMAND_FILE = re.compile(UUID_PATTERN[:-1] + r"\.json$")


class ContractError(ValueError):
    """A document does not match the contract; the single cause is the message."""


def _const(value: int) -> Any:
    return field(default=value, metadata={"const": value})


ADDED_AFTER_V1 = "added_after_v1"


def _added_later(default: Any) -> Any:
    """A key added to version 1 after it shipped: a reader takes a document without it as
    `default`; writers always write it. Every other key stays required on read, whatever
    default its dataclass field has."""
    return field(default=default, metadata={ADDED_AFTER_V1: True})


def _params_added_later() -> Any:
    """`params` (docs/api.md, "Reason codes") added after version 1 shipped: empty when absent."""
    return field(default_factory=dict, metadata={ADDED_AFTER_V1: True})


# A reason's parameters: name -> plain text value (docs/api.md, "Reason codes").
Params = dict[str, str]

# A cause's code and parameters ({code, params}; docs/api.md, "Reason codes").
ReasonV1 = Reason
REASONS_OF = "reasons_of"


def _reasons_of(source: str) -> Any:
    """`<source>_codes`: the codes of the cause in field `source`. Computed only when a document
    is encoded (`to_jsonable`), never when one is built or read, so no path that builds these
    records (the daily-loss check reads `DayTrade`s) ever parses a cause; the attribute itself
    stays empty. Added after version 1 shipped."""
    return field(init=False, default=(), metadata={REASONS_OF: source, ADDED_AFTER_V1: True})


def _codes_for(document: Any, source: str) -> tuple[Reason, ...]:
    """Display only: a reason table that fails on a text shows it unrecognized, never raises."""
    try:
        return prose_reasons(getattr(document, source))
    except Exception:  # noqa: BLE001 - see docstring
        return (Reason(UNRECOGNIZED_CODE),)

# The code of a reason the bot wrote before it recorded codes; clients show the text.
UNRECORDED = "unrecorded"


# ---------------------------------------------------------------- enums


class BotState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class StopState(StrEnum):
    """Broker-side protective stop of the open position, as last verified with the broker."""

    VERIFIED = "verified"  # Detail shows the stop working
    PENDING = "pending"  # sent, not yet verified
    MISSING = "missing"  # no working stop: an alert condition
    UNKNOWN = "unknown"  # outcome unknown: broker read in progress


class SendOutcome(StrEnum):
    """The engine's send states (engine.core.SendState), mirrored value for value."""

    INTENT = "intent"  # journaled, not attempted yet
    ATTEMPTED = "attempted"  # attempt recorded, no outcome yet (in flight, or crashed)
    ACKNOWLEDGED = "acknowledged"  # the broker accepted the order
    REFUSED = "refused"  # refused by the broker or our request budget: no order exists
    UNKNOWN = "unknown"  # may or may not exist: never resent, resolved by broker reads
    ABSENT = "absent"  # never at the broker (not attempted, or not found by reads)


class StreamHealth(StrEnum):
    FRESH = "fresh"
    QUIET = "quiet"
    LOST = "lost"
    OFF = "off"  # no subscription


class CommandKind(StrEnum):
    ENTRY_STOP_ON = "entry-stop-on"
    ENTRY_STOP_OFF = "entry-stop-off"
    CANCEL_OWN_ORDERS = "cancel-own-orders"
    FLATTEN = "flatten"
    STOP_BOT = "stop-bot"  # graceful stop, only when flat; start refused until cleared
    EMERGENCY = "emergency"  # entry-stop on, then cancel own orders, then flatten all


class CommandOutcome(StrEnum):
    DONE = "done"
    REFUSED = "refused"


# ---------------------------------------------------------------- status written by the bot


@dataclass(frozen=True)
class Gate:
    """The trading gate. `code` and `params` name `cause` for translation (api/reasons.py)."""

    open: bool
    cause: str
    code: str = _added_later(UNRECORDED)
    params: Params = _params_added_later()


@dataclass(frozen=True)
class StrategyId:
    name: str
    version: str


# EntryBlock cause while a position or a working order of the bot's own exists.
OWN_TRADE_OPEN = "own_trade_open"


@dataclass(frozen=True)
class EntryBlock:
    """The single reason entries are blocked: a stable cause code plus plain words for people.
    `attention` is false for an ordinary state that passes on its own (a position is already
    open, the time is outside the entry window): clients show it calmly, not as a warning."""

    cause: str
    text: str
    attention: bool = _added_later(True)
    params: Params = _params_added_later()  # the values in `text`, for a translated sentence


@dataclass(frozen=True)
class Fence:
    """An engine fence. `kind` is the fence name; `code` and `params` name `text` for
    translation (api/reasons.py)."""

    kind: str
    since: datetime
    text: str
    code: str = _added_later(UNRECORDED)
    params: Params = _params_added_later()


@dataclass(frozen=True)
class Stop:
    state: StopState
    price: Decimal | None
    client_order_id: str | None


@dataclass(frozen=True)
class EntrySend:
    """The first link of the protection chain: the entry order's single send, from the journal
    (docs/api.md, "Entry send"). Display only; each null is explained by `entry_cause`."""

    client_order_id: str | None  # the bot's own id for the entry order
    intent_at: datetime | None  # `order-intent` row: written before the send
    sent_at: datetime | None  # first `order-attempted` row: just before the HTTP call
    send_outcome: SendOutcome | None  # the state the send itself recorded
    latest_outcome: SendOutcome | None  # the latest send state (a broker read may resolve it)
    sends: int | None  # `order-attempted` rows of the entry order: 1, never clamped
    filled_at: datetime | None  # first `order-observed` row showing filled quantity > 0


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    opened_at: datetime
    stop: Stop
    entry: EntrySend
    entry_cause: str | None  # why a field of `entry` is null; null when none is

    entry_cause_codes: tuple[ReasonV1, ...] = _reasons_of("entry_cause")

@dataclass(frozen=True)
class Trade:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    exit_reason: str
    result_gross_usd: Decimal
    planned_loss_usd: Decimal  # 1 R, as recorded when the trade was opened
    # First entry fill observed -> first stop seen working at the broker; null = never.
    stop_confirmed_after_seconds: Decimal | None
    entry: EntrySend
    entry_cause: str | None  # why a field of `entry` is null; null when none is
    # The strategy whose decision opened the trade; null for a manual trade or one opened
    # before the bot journaled it. Added after version 1 shipped.
    strategy: StrategyId | None = _added_later(None)

    entry_cause_codes: tuple[ReasonV1, ...] = _reasons_of("entry_cause")

@dataclass(frozen=True)
class Freshness:
    last_broker_read_at: datetime | None
    last_quote_at: datetime | None
    stream: StreamHealth


@dataclass(frozen=True)
class CommandRecord:
    """A command the bot has consumed and journaled, with what it did."""

    command_id: str
    kind: CommandKind
    requested_at: datetime
    processed_at: datetime
    outcome: CommandOutcome
    cause: str | None


@dataclass(frozen=True)
class StatusV1:
    """Everything the status screen and the app show. Written by the bot, never by the API."""

    written_at: datetime
    bot_state: BotState
    run_id: str | None
    environment: str
    trading_gate: Gate
    strategy: StrategyId
    entry_stop: bool
    entry_block: EntryBlock | None  # None = entries allowed
    fences: tuple[Fence, ...]
    position: Position | None  # the first of `positions`, kept for older clients
    trades_today: tuple[Trade, ...]
    result_today_gross_usd: Decimal
    freshness: Freshness
    recent_commands: tuple[CommandRecord, ...]
    # Daily loss rule (spec 7, check 6) as the bot computes it; null = unknown, never zero.
    realized_loss_today_usd: Decimal | None  # net realized loss today, >= 0
    open_reservations_usd: Decimal | None  # planned loss of every open trade
    daily_room_usd: Decimal | None  # ceiling - realized - reserved; may be negative
    positions: tuple[Position, ...]  # every open position the bot tracks
    schema_version: int = _const(STATUS_SCHEMA_VERSION)


# ---------------------------------------------------------------- account written by the bot


@dataclass(frozen=True)
class AccountPosition:
    symbol: str
    quantity: Decimal
    avg_price: Decimal | None  # null when the broker row does not show it


@dataclass(frozen=True)
class AccountOrder:
    """One open order as the broker listed it. `ours`: its client id is one of the bot's."""

    client_order_id: str | None
    symbol: str
    side: str
    type: str
    status: str
    quantity: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    ours: bool


@dataclass(frozen=True)
class AccountV1:
    """What the bot's latest successful broker read of positions and open orders showed, and
    whether it agreed with the bot's records. Written by the bot, never by the API; no
    account ids."""

    read_at: datetime
    equity_usd: Decimal | None
    buying_power_usd: Decimal | None
    positions: tuple[AccountPosition, ...]
    orders: tuple[AccountOrder, ...]
    matches_records: bool
    mismatch: str | None  # what disagrees, in plain words; null when matches_records
    schema_version: int = _const(ACCOUNT_SCHEMA_VERSION)


# ---------------------------------------------------------------- commands


@dataclass(frozen=True)
class CommandV1:
    command_id: str
    kind: CommandKind
    requested_at: datetime
    source: str
    symbol: str | None = None  # flatten only: close just this symbol; null = everything
    schema_version: int = _const(COMMAND_SCHEMA_VERSION)


# ---------------------------------------------------------------- API responses


@dataclass(frozen=True)
class StatusResponseV1:
    """GET /status. Always this shape; `status` is null when the bot's file is missing or bad."""

    served_at: datetime
    status: StatusV1 | None
    age_seconds: Decimal | None
    stale: bool
    unavailable_cause: str | None
    summary: str  # the single entry-blocking reason in plain words, or "Entries allowed."
    poll_seconds: Decimal
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class AccountResponseV1:
    """GET /account: the bot's account-v1 document with its age. A missing or invalid file is
    503 unavailable instead."""

    served_at: datetime
    account: AccountV1
    age_seconds: Decimal
    stale: bool
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class PairedV1:
    """POST /pair on the remote port: the bearer token the phone keeps from now on. Answered
    once per pairing code; the server keeps only the token's SHA-256."""

    token: str
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class CommandAcceptedV1:
    """POST /commands/*: the command is queued for the bot, not yet executed."""

    command: CommandV1
    warning: str | None
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class PendingCommandsV1:
    """GET /commands/pending: commands written but not yet consumed by the bot."""

    commands: tuple[CommandV1, ...]
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class ErrorV1:
    error: str
    cause: str
    api_version: int = _const(API_VERSION)


# ---------------------------------------------------------------- history (read-only)


@dataclass(frozen=True)
class PriceAfterV1:
    """The price `minutes` after a closed trade's exit or a skipped signal (docs/api.md, "Prices
    after"): the close of the captured M1 bar covering `at`. `change_usd` is per share, `price`
    minus the reference (a trade's `exit_price`; a signal's decision bar close). `cause` says
    why `price` or `change_usd` is null, else null. Display only."""

    minutes: int
    at: datetime
    price: Decimal | None
    change_usd: Decimal | None
    cause: str | None

    cause_codes: tuple[ReasonV1, ...] = _reasons_of("cause")

@dataclass(frozen=True)
class WhatIfV1:
    """A skipped signal entered alone in the replay simulation (docs/api.md, "What if"):
    gross USD for the recorded quantity, R per share, and why the simulated trade ended.
    SIMULATED, display only. Every number null with `cause` when it was not simulated."""

    result_gross_usd: Decimal | None
    result_r: Decimal | None
    exit_reason: ExitReason | None
    cause: str | None

    cause_codes: tuple[ReasonV1, ...] = _reasons_of("cause")

@dataclass(frozen=True)
class DayTrade:
    """A closed trade with fills. `stop_price` is the first protective stop sent (null if none
    was); 1 R = `planned_loss_usd`. `stop_confirmed_after_seconds` as in `Trade`. `mae_usd` /
    `mfe_usd`: lower bounds of the excursions in USD: the fill prices and the captured M1 bars
    strictly between the minute of the first entry fill read and the minute up to which the
    journal proves no share was sold yet (a close's send intent, a stop's last unfilled read),
    times the quantity the first fill read showed (docs/api.md); null without capture of the
    trade's edge minutes. `prices_after`: one point per `[api] price_after_minutes` after
    `closed_at` (empty with `prices_after_cause` when not computed). `overshoot_usd` (per
    share) / `overshoot_r`: highest high after the exit minute within the longest offset,
    minus `exit_price`; null with `overshoot_cause`. Display only, never on the risk path."""

    trade_id: str
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    exit_reason: str
    stop_price: Decimal | None
    planned_loss_usd: Decimal
    result_gross_usd: Decimal
    stop_confirmed_after_seconds: Decimal | None
    mae_usd: Decimal | None
    mfe_usd: Decimal | None
    entry: EntrySend  # as in `Position`
    entry_cause: str | None
    prices_after: tuple[PriceAfterV1, ...]
    prices_after_cause: str | None
    overshoot_usd: Decimal | None
    overshoot_r: Decimal | None
    overshoot_cause: str | None
    # As in `Trade`: the strategy whose decision opened it, null when not journaled. Always
    # written; the default only spares paths that never show it (the daily-loss check).
    strategy: StrategyId | None = None

    entry_cause_codes: tuple[ReasonV1, ...] = _reasons_of("entry_cause")
    prices_after_cause_codes: tuple[ReasonV1, ...] = _reasons_of("prices_after_cause")
    overshoot_cause_codes: tuple[ReasonV1, ...] = _reasons_of("overshoot_cause")

class CleanVerdict(StrEnum):
    """Did the broker and the bot's records agree all that New York day (docs/api.md)."""

    CLEAN = "clean"
    NOT_CLEAN = "not_clean"  # `causes` says why, each with its journal rows
    UNKNOWN = "unknown"  # `cause` says why: not recorded, day not over, no day-end record...


class CleanCauseKind(StrEnum):
    RECONCILE_MISMATCH = "reconcile_mismatch"  # a reconcile pass or a fence: records differ
    UNKNOWN_ORDER_OUTCOME = "unknown_order_outcome"  # still unresolved at the day's end
    UNPROTECTED_POSITION = "unprotected_position"  # stop rejected, or no verified stop too long
    SAFETY_EXIT = "safety_exit"  # an exit forced by a safety fault, or an emergency command
    DAY_END_NOT_CLEAN = "day_end_not_clean"  # the day-end check: not flat, or not agreeing


@dataclass(frozen=True)
class CleanCause:
    kind: CleanCauseKind
    text: str  # plain words, from the rows
    journal_seqs: tuple[int, ...]  # the journal rows (seq) that prove it, oldest first

    text_codes: tuple[ReasonV1, ...] = _reasons_of("text")

@dataclass(frozen=True)
class CleanDay:
    """The clean-day verdict of one New York date, from the journal only (docs/api.md,
    "Clean day"). Never a guess: what the journal does not prove is `unknown`."""

    verdict: CleanVerdict
    cause: str | None  # why `unknown`; null otherwise
    causes: tuple[CleanCause, ...]  # `not_clean`: one per kind of fault; empty otherwise
    reconcile_rows: int  # `reconcile-checked` rows that date
    first_reconcile_at: datetime | None  # the first and last of them
    last_reconcile_at: datetime | None
    day_end_seq: int | None  # the date's `day-end` row, if any

    cause_codes: tuple[ReasonV1, ...] = _reasons_of("cause")

@dataclass(frozen=True)
class DaySummary:
    """One New York date with records: closed trades, a bot run, or captured market data."""

    day: date
    trades: int
    wins: int
    losses: int
    result_gross_usd: Decimal
    has_bars: bool
    clean: CleanDay


@dataclass(frozen=True)
class DaysV1:
    """GET /days: every New York date with records in this environment, newest first."""

    environment: str
    days: tuple[DaySummary, ...]
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class DayV1:
    """GET /days/<YYYY-MM-DD>: the trades closed that New York date and the symbols with
    captured M1 bars. A date without records answers with empty lists."""

    environment: str
    day: date
    trades: tuple[DayTrade, ...]
    result_gross_usd: Decimal
    bar_symbols: tuple[str, ...]
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class BarV1:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class BarsV1:
    """GET /bars?symbol=&date=: the captured M1 bars of one symbol on one New York date, oldest
    first, one per start time (the last version received when the provider revised a bar)."""

    environment: str
    symbol: str
    day: date
    bars: tuple[BarV1, ...]
    api_version: int = _const(API_VERSION)


class ChartInterval(StrEnum):
    """Candle size of GET /chart."""

    M1 = "1m"
    M5 = "5m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"
    W1 = "1w"
    MO1 = "1mo"


class ChartRange(StrEnum):
    """Span of GET /chart: the latest 1 or 5 sessions, or the last 1, 3 or 12 months."""

    D1 = "1d"
    D5 = "5d"
    M1 = "1m"
    M3 = "3m"
    Y1 = "1y"


@dataclass(frozen=True)
class ChartBarV1:
    """One candle. `start` is the candle's first instant: for 1d/1w/1mo, 00:00 New York time
    of its first calendar day (the date, the Monday, the 1st). `extended` is true for a pre- or
    post-market candle, decided by the session calendar; always false for 1d/1w/1mo."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    extended: bool


@dataclass(frozen=True)
class ChartV1:
    """GET /chart?symbol=&interval=&range=: candles of any US stock from market data, oldest
    first. `source` names the provider; `cached_at` is when the oldest data in the answer was
    read from it; `cause` says why `bars` is empty (null when it is not)."""

    symbol: str
    interval: ChartInterval
    range: ChartRange
    source: str
    cached_at: datetime
    cause: str | None
    bars: tuple[ChartBarV1, ...]
    api_version: int = _const(API_VERSION)


# ---------------------------------------------------------------- settings (read-only)


@dataclass(frozen=True)
class RiskSettings:
    planned_loss_usd: Decimal
    position_notional_cap_usd: Decimal
    max_daily_loss_usd: Decimal
    max_open_positions: int
    max_quote_age_seconds: Decimal
    min_daily_room_usd: Decimal


@dataclass(frozen=True)
class SessionSettings:
    timezone: str
    last_entry_minutes_before_close: int
    flatten_minutes_before_close: int
    half_days_enabled: bool


@dataclass(frozen=True)
class SettingsV1:
    """GET /settings: the numbers from config/bot.toml that clients show (limits, session
    times). Read-only, as the API process loaded them at its start; changing them means editing
    the file and restarting."""

    environment: str
    active_strategy: str
    observers: tuple[str, ...]
    risk: RiskSettings
    session: SessionSettings
    status_stale_seconds: Decimal
    poll_seconds: Decimal
    # Age of the last broker read past which entries are blocked ([orders]
    # broker_read_max_age_seconds) and past which clients show a warning ([api]
    # broker_read_warn_seconds, always below the block age).
    broker_read_max_age_seconds: Decimal
    broker_read_warn_seconds: Decimal
    api_version: int = _const(API_VERSION)


# ---------------------------------------------------------------- jobs (read-only)


@dataclass(frozen=True)
class LaunchdJobV1:
    """One launchd job on the Mac, as configured in [runtime]: it starts at `local_time` ("HH:MM",
    the Mac's own time zone) on `weekdays` (launchd numbering: 1 = Monday ... 5 = Friday,
    0 and 7 = Sunday). The session calendar still decides whether the day trades."""

    name: str  # preflight, bot, watchdog or nightly-fetch
    local_time: str
    weekdays: tuple[int, ...]


@dataclass(frozen=True)
class ReconcileJobV1:
    """The bot's broker reconciliation, every `interval_seconds` while it runs."""

    interval_seconds: Decimal
    last_at: datetime | None  # the bot's last successful broker read (status freshness)
    next_at: datetime | None  # last_at + interval while the bot is running, else null


@dataclass(frozen=True)
class BackupJobV1:
    """The bot's verified daily database backup, made on its first loop pass of each New York
    date. `last_failed_*` only when a failure is newer than the last backup. `cause`: why
    `last_at` is null."""

    last_at: datetime | None
    last_failed_at: datetime | None
    last_failed_error: str | None  # the error's type name, never its text (it may hold paths)
    cause: str | None


@dataclass(frozen=True)
class SessionTimesV1:
    """Today's New York session and the bot's cutoffs from the calendar and [session]."""

    day: date
    regular_open: datetime
    last_entry_at: datetime
    flatten_at: datetime
    regular_close: datetime


@dataclass(frozen=True)
class JournalRowV1:
    """One row of the bot's durable journal. `kind` is the bot's own event name
    (`trade-opened`, `order-intent`, `reconcile-checked` ...); `symbol` when the row's body names
    one. The body itself is not served."""

    seq: int  # the journal's own row number: grows by one per row, never reused
    at: datetime
    kind: str
    symbol: str | None


@dataclass(frozen=True)
class JournalV1:
    """GET /journal: the latest `[api] journal_rows` journal rows, oldest first, from the
    database opened read-only. `last_seq`: the newest row's number ("line #N"), null with
    `cause` when there is no database or it cannot be read."""

    last_seq: int | None
    rows: tuple[JournalRowV1, ...]
    cause: str | None
    api_version: int = _const(API_VERSION)


@dataclass(frozen=True)
class NightlyFetchJobV1:
    """The nightly backtest-data refresh's last outcome (its `nightly.json`): `finished_at`;
    `last_day`, where the Lab's data now ends; `new_sessions` added that night (0 = already up to
    date); `cause` when it was refused or stopped (the data stays as it was). All null with
    `cause` when it never ran or its file cannot be read."""

    finished_at: datetime | None
    last_day: date | None
    new_sessions: int | None
    cause: str | None


@dataclass(frozen=True)
class JobsV1:
    """GET /jobs: when the bot's scheduled work last ran and runs next, for the Settings jobs
    strip. Read-only: from the status file, the database (read-only), config and calendar.
    `bot_state` and `bot_started_at` are null without a valid status file; `session` is null
    with `session_cause` when today has no session the bot trades."""

    served_at: datetime
    bot_state: BotState | None
    status_stale: bool
    bot_started_at: datetime | None
    reconcile: ReconcileJobV1
    backup: BackupJobV1
    launchd: tuple[LaunchdJobV1, ...]
    session: SessionTimesV1 | None
    session_cause: str | None
    nightly_fetch: NightlyFetchJobV1
    api_version: int = _const(API_VERSION)


# ---------------------------------------------------------------- lab replay


class LabReplayState(StrEnum):
    IDLE = "idle"  # no run yet
    RUNNING = "running"
    DONE = "done"  # finished with a report
    FAILED = "failed"  # finished without a report: `cause` says why


@dataclass(frozen=True)
class LabReplaySummaryV1:
    """The run's SIMULATED report (backtest report.json), only the numbers the Lab shows.
    Every signal: `trades` ... `max_drawdown_r`, the mean R interval resampling whole days
    (null when too few days). Bot limits: what the bot could have taken under its risk checks.
    Null numbers mean no trades to compute them from."""

    first_day: date  # the replayed window: the last `sessions` sessions of the fetched data
    last_day: date
    sessions: int
    sessions_without_replayed_days: int  # no gainer that day, or incomplete fetched data
    replayed_symbol_days: int
    trades: int
    win_rate_percent: Decimal | None
    mean_r: Decimal | None
    total_r: Decimal | None
    profit_factor: Decimal | None
    mean_r_interval_low: Decimal | None
    mean_r_interval_high: Decimal | None
    confidence_percent: Decimal
    max_drawdown_r: Decimal | None
    bot_trades: int
    bot_total_usd: Decimal
    bot_max_drawdown_usd: Decimal | None


@dataclass(frozen=True)
class LabReplayV1:
    """GET and POST /lab/replay: the Lab's SIMULATED replay of the active strategy over the last
    sessions of the already fetched backtest data (it never fetches). One run at a time; only
    the latest is kept. `summary` when `state` is done; `cause` when failed."""

    state: LabReplayState
    run_id: str | None
    strategy: str | None
    version: str | None
    sessions: int  # [api] lab_replay_sessions asked for
    started_at: datetime | None
    finished_at: datetime | None
    cause: str | None
    summary: LabReplaySummaryV1 | None
    api_version: int = _const(API_VERSION)


# ---------------------------------------------------------------- explain (read-only)


@dataclass(frozen=True)
class CheckV1:
    """One rule the strategy evaluated: `measured` `operator` `threshold`. Yes/no rules have
    operator "is" and null measured and threshold."""

    rule: str
    measured: Decimal | None
    operator: str
    threshold: Decimal | None
    passed: bool
    note: str


@dataclass(frozen=True)
class ExplainV1:
    """GET /explain?symbol=: the latest decision of the active strategy recorded today for
    one symbol, with every check in the order evaluated, as the decision file holds it."""

    environment: str
    day: date
    symbol: str
    decided_at: datetime
    bar_start: datetime
    strategy: str
    version: str
    verdict: Verdict
    setup: str | None
    admission: Admission
    refusal_cause: str | None
    first_failed_check: str | None
    checks: tuple[CheckV1, ...]
    api_version: int = _const(API_VERSION)


def check_v1(c: Check) -> CheckV1:
    """The explain-v1 check shape, shared by every document that shows checks."""
    return CheckV1(c.rule, c.measured, c.operator, c.threshold, c.passed, c.note)


def checks_of(decision: Decision, places: int | None = None) -> tuple[CheckV1, ...]:
    """Every check of `decision`; with `places`, each rounded by `shown_check` (the API);
    without, as recorded (the CLI's explain)."""
    if places is None:
        return tuple(check_v1(c) for c in decision.checks)
    return tuple(shown_check(c, places) for c in decision.checks)


def explain(environment: str, day: date, record: DecisionRecord,
            places: int | None = None) -> ExplainV1:
    d = record.decision
    checks = checks_of(d, places)
    return ExplainV1(
        environment=environment, day=day, symbol=d.symbol, decided_at=record.time,
        bar_start=record.bar_start, strategy=d.strategy, version=d.version, verdict=d.verdict,
        setup=d.setup, admission=record.admission, refusal_cause=record.refusal_cause,
        first_failed_check=d.decisive.rule if d.decisive else None, checks=checks)


# ---------------------------------------------------------------- recorded decisions (read-only)


# `action` is the recorded Verdict, the same enum and values as explain-v1 `verdict`.


@dataclass(frozen=True)
class ObserverDecisionV1:
    """One observer decision: `at` is the decision bar's start; `reasons` are the rules of the
    checks that failed, in the order evaluated (empty when every check passed)."""

    at: datetime
    symbol: str
    action: Verdict
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ObserverTradeV1:
    """One ENTER of an observer as its day simulation replayed it, SIMULATED, display only.
    `at` is the decision bar's start. A filled trade has its entry, exit, `exit_reason` and
    `result_r` (per share); `quantity` and `result_gross_usd` only with a recorded whole
    quantity. Every null has `cause` (missed, rejected, not simulated, skipped because an
    earlier simulated trade of the symbol was open or the same signal already traded, no
    recorded whole quantity); else null."""

    symbol: str
    at: datetime
    opened_at: datetime | None
    entry_price: Decimal | None
    closed_at: datetime | None
    exit_price: Decimal | None
    quantity: Decimal | None
    exit_reason: ExitReason | None
    result_gross_usd: Decimal | None
    result_r: Decimal | None
    cause: str | None

    cause_codes: tuple[ReasonV1, ...] = _reasons_of("cause")

@dataclass(frozen=True)
class ObserverV1:
    """One configured observer. `result_gross_usd` is its simulated gross day result (the
    research observers report's what-if simulation of its ENTERs on that day's captured bars);
    null when it cannot be computed, and then `result_cause` says why (else null). `trades`:
    every ENTER of that simulation, oldest first, at most `[api] observer_trades_max_rows`
    (`trades_truncated` true when the cap left some out); null with `trades_cause` when the
    simulation could not run (else `trades_cause` is null)."""

    name: str
    version: str
    decisions: tuple[ObserverDecisionV1, ...]
    result_gross_usd: Decimal | None
    result_cause: str | None
    trades: tuple[ObserverTradeV1, ...] | None
    trades_cause: str | None
    trades_truncated: bool

    result_cause_codes: tuple[ReasonV1, ...] = _reasons_of("result_cause")
    trades_cause_codes: tuple[ReasonV1, ...] = _reasons_of("trades_cause")

@dataclass(frozen=True)
class ObserversV1:
    """GET /observers?date=: the observers configured in [strategy] observers, one entry per
    (name, version) recorded on one New York date, with its decisions oldest first."""

    day: date
    observers: tuple[ObserverV1, ...]
    api_version: int = _const(API_VERSION)


# The recorded cause code of a refused ENTER: every admission cause, the engine's refusal after
# admission, and UNKNOWN for a recorded cause the API cannot read exactly.
UNKNOWN_CAUSE = "UNKNOWN"
RefusalCode = StrEnum("RefusalCode", [(c.value, c.value) for c in AdmissionCause]
                      + [(ENGINE_REFUSED, ENGINE_REFUSED), (UNKNOWN_CAUSE, UNKNOWN_CAUSE)])


@dataclass(frozen=True)
class RefusalV1:
    """Why admission refused an ENTER, read from the recorded `CODE: detail` (docs/api.md).
    `message` is the detail without timestamps; `since` is when the refusal began to apply and
    `until` when it stops applying, each null when the cause has no such time."""

    code: RefusalCode
    message: str
    since: datetime | None
    until: datetime | None
    params: Params  # the values in the recorded detail (api/reasons.py); {} when none


@dataclass(frozen=True)
class DecisionV1:
    """One recorded decision of the active strategy or an observer (`observer` true). `checks`
    as in explain-v1. `entry_price` (top of the entry window) and `stop_price` only for an ENTER
    that was not taken (refused, or never evaluated) and only when the record holds them, both
    rounded down to the tick like the order the bot would have sent. `refusal_cause` is the
    recorded text, kept for older clients; `refusal` is the same cause read into fields."""

    at: datetime
    action: Verdict
    strategy: StrategyId
    observer: bool
    checks: tuple[CheckV1, ...]
    entry_price: Decimal | None
    stop_price: Decimal | None
    admission: Admission
    refusal_cause: str | None
    refusal: RefusalV1 | None


@dataclass(frozen=True)
class DecisionsV1:
    """GET /decisions?symbol=&date=: every decision recorded for one symbol on one New York
    date, oldest first."""

    symbol: str
    day: date
    decisions: tuple[DecisionV1, ...]
    api_version: int = _const(API_VERSION)


def _oldest_first(records: tuple[DecisionRecord, ...]) -> list[DecisionRecord]:
    return sorted(records, key=lambda r: (r.bar_start, r.time))


@dataclass(frozen=True)
class ObserverResult:
    """One observer identity's simulated day (not a document): gross USD or None with its
    cause, and its trades or None with their cause."""

    gross_usd: Decimal | None
    cause: str | None
    trades: tuple[ObserverTradeV1, ...] | None = ()
    trades_cause: str | None = None
    trades_truncated: bool = False


NO_DECISIONS = "no decisions recorded that day"


def observers_document(day: date, identities: tuple[tuple[str, str], ...],
                       records: tuple[DecisionRecord, ...],
                       results: Mapping[tuple[str, str], ObserverResult]) -> ObserversV1:
    """`identities`: (name, current version) of each configured observer, in configuration
    order; `records`: observer records only. Observers are matched by name: one entry per
    version recorded that day (config order, then version), or, when a name has no records,
    one empty entry per current version, so a version bump never hides earlier records.
    `results`: the simulated result of every (name, version) recorded that day; an entry
    without records gets null result and trades with the cause NO_DECISIONS."""
    ordered = _oldest_first(records)
    names = list(dict.fromkeys(name for name, _ in identities))
    entries: list[ObserverV1] = []
    for name in names:
        recorded = {r.decision.version for r in ordered if r.decision.strategy == name}
        current = [v for n, v in identities if n == name]
        for version in sorted(recorded or set(current), key=_version_order):
            if (name, version) in results:
                result = results[(name, version)]
            elif version in recorded:  # a caller bug: refuse rather than mislabel it
                raise ContractError(f"no simulated result for observer {name} v{version}")
            else:
                result = ObserverResult(None, NO_DECISIONS, None, NO_DECISIONS)
            entries.append(ObserverV1(
                name=name, version=version, result_gross_usd=result.gross_usd,
                result_cause=result.cause, trades=result.trades,
                trades_cause=result.trades_cause, trades_truncated=result.trades_truncated,
                decisions=tuple(
                ObserverDecisionV1(r.bar_start, r.symbol, r.decision.verdict,
                                   tuple(c.rule for c in r.decision.checks if not c.passed))
                for r in ordered
                if (r.decision.strategy, r.decision.version) == (name, version))))
    return ObserversV1(day=day, observers=tuple(entries))


def _version_order(version: str) -> tuple[bool, int, str]:
    """Numeric versions in numeric order ("2" before "10"), then any other text."""
    numeric = version.isdigit()
    return (not numeric, int(version) if numeric else 0, version)


_SKIPPED = (Admission.REFUSED, Admission.NOT_EVALUATED)

# Recorded details that carry times (risk/admission.py); each time is an ISO-8601 text with an
# offset. A detail of any other code is shown as recorded, unless it holds such a time: then the
# cause is UNKNOWN (a new detail shape is read on purpose, never by accident).
_COOLDOWN = re.compile(r"(?P<symbol>\S+) closed at a loss at (?P<since>\S+); "
                       r"no entry before (?P<until>\S+)")
_AFTER_LAST_ENTRY = re.compile(r"last entry was (?P<since>\S+)")
_OUTSIDE_SESSION = re.compile(r"(?P<now>\S+) is outside (?P<open>[0-9]{2}:[0-9]{2}:[0-9]{2})-"
                              r"(?P<close>[0-9]{2}:[0-9]{2}:[0-9]{2}) New York")
_ISO_TIME_IN_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}")


def _recorded_time(text: str) -> datetime | None:
    """An aware ISO-8601 time as admission writes it, or None."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.utcoffset() is not None else None


def refusal_of(cause: str | None) -> RefusalV1 | None:
    """The recorded `CODE: detail` of a refusal in fields. Strict: a cause that is not exactly
    one of the shapes admission and the entry path write is UNKNOWN with the recorded text as
    its message, never a guess."""
    if cause is None:
        return None
    unknown = RefusalV1(RefusalCode(UNKNOWN_CAUSE), cause, None, None, {})
    code_text, separator, detail = cause.partition(": ")
    if (not separator or not detail.strip() or code_text == UNKNOWN_CAUSE
            or code_text not in RefusalCode.__members__):
        return unknown
    code = RefusalCode(code_text)
    since = until = None
    if code_text == AdmissionCause.SYMBOL_COOLDOWN:
        match = _COOLDOWN.fullmatch(detail)
        if match is None:
            return unknown
        since, until = _recorded_time(match["since"]), _recorded_time(match["until"])
        if since is None or until is None:
            return unknown
        message = f"{match['symbol']} closed at a loss; no new entry in it during the cooldown"
    elif code_text == AdmissionCause.AFTER_LAST_ENTRY:
        match = _AFTER_LAST_ENTRY.fullmatch(detail)
        if match is None or (since := _recorded_time(match["since"])) is None:
            return unknown
        message = "the session's last entry time had passed"
    elif code_text == AdmissionCause.OUTSIDE_REGULAR_SESSION:
        match = _OUTSIDE_SESSION.fullmatch(detail)
        if match is None or _recorded_time(match["now"]) is None:
            return unknown
        message = f"outside the regular session {match['open']}-{match['close']} New York"
    elif code_text == AdmissionCause.SESSION_TIMES_INVALID:
        # The detail quotes the computed instants; the code alone says what is wrong.
        message = "the [session] offsets give no valid last entry and flatten times for the day"
    elif _ISO_TIME_IN_TEXT.search(detail):
        return unknown
    else:
        message = detail
    return RefusalV1(code, message, since, until, refusal_params(code_text, detail, since, until))


def _what_if(price: Decimal | None) -> Decimal | None:
    """A recorded strategy price as the order path would have sent it."""
    return None if price is None else round_to_tick(price)


def decisions_document(symbol: str, day: date, records: tuple[DecisionRecord, ...],
                       places: int | None = None) -> DecisionsV1:
    out = []
    for r in _oldest_first(records):
        d = r.decision
        skipped = d.verdict is Verdict.ENTER and r.admission in _SKIPPED
        out.append(DecisionV1(
            at=r.bar_start, action=d.verdict,
            strategy=StrategyId(d.strategy, d.version), observer=r.role is Role.OBSERVER,
            checks=checks_of(d, places), entry_price=_what_if(d.entry_high) if skipped else None,
            stop_price=_what_if(d.stop) if skipped else None, admission=r.admission,
            refusal_cause=r.refusal_cause, refusal=refusal_of(r.refusal_cause)))
    return DecisionsV1(symbol=symbol, day=day, decisions=tuple(out))


@dataclass(frozen=True)
class SkippedSignalV1:
    """One ENTER of the active strategy that did not become a position: admission `refused`
    (`refusal` says why; `refusal_cause` is the recorded `CAUSE: detail`, kept for older
    clients) or `not_evaluated` (made too late to act on). `entry_price` / `stop_price` and
    `checks` as in decisions-v1. `prices_after` as in day-v1 from the decision bar (`at`),
    the change from its close; `what_if`: the signal entered alone, simulated."""

    at: datetime
    symbol: str
    strategy: StrategyId
    admission: Admission
    refusal_cause: str | None
    refusal: RefusalV1 | None
    entry_price: Decimal | None
    stop_price: Decimal | None
    checks: tuple[CheckV1, ...]
    prices_after: tuple[PriceAfterV1, ...]
    prices_after_cause: str | None
    what_if: WhatIfV1

    prices_after_cause_codes: tuple[ReasonV1, ...] = _reasons_of("prices_after_cause")

@dataclass(frozen=True)
class SkippedV1:
    """GET /skipped?date=: every skipped ENTER of the active strategy on one New York date,
    oldest first."""

    day: date
    signals: tuple[SkippedSignalV1, ...]
    api_version: int = _const(API_VERSION)


NOT_COMPUTED = "not computed on this path"
NO_WHAT_IF = WhatIfV1(None, None, None, NOT_COMPUTED)


def skipped_records(records: tuple[DecisionRecord, ...]) -> list[DecisionRecord]:
    """The ENTERs admission did not take, oldest first: the rows of `skipped_document`."""
    return [r for r in _oldest_first(records)
            if r.decision.verdict is Verdict.ENTER and r.admission in _SKIPPED]


def skipped_document(day: date, records: tuple[DecisionRecord, ...],
                     places: int | None = None) -> SkippedV1:
    """`records`: active-strategy records of `day`; keeps the ENTERs admission did not take,
    without prices after or what-if (api/after.py adds them)."""
    return SkippedV1(day=day, signals=tuple(
        SkippedSignalV1(
            at=r.bar_start, symbol=r.symbol,
            strategy=StrategyId(r.decision.strategy, r.decision.version),
            admission=r.admission, refusal_cause=r.refusal_cause,
            refusal=refusal_of(r.refusal_cause), entry_price=_what_if(r.decision.entry_high),
            stop_price=_what_if(r.decision.stop),
            checks=checks_of(r.decision, places), prices_after=(), prices_after_cause=NOT_COMPUTED,
            what_if=NO_WHAT_IF)
        for r in skipped_records(records)))


# ---------------------------------------------------------------- overview flow (read-only)


class FlowStage(StrEnum):
    SCREENERS = "screeners"  # listed by a screener that day
    UNIVERSE = "universe"  # passed the universe filter in the latest pass that listed it
    STRATEGY = "strategy"  # the active strategy recorded a decision
    RISK = "risk"  # an ENTER that admission took
    ORDER = "order"  # an entry order of a trade opened that day filled
    STOP = "stop"  # that trade's broker stop was seen working (verified)


class FlowStopState(StrEnum):
    """Not status-v1's StopState: a day's trades, closed ones included, from the journal."""

    VERIFIED = "verified"  # a broker read showed one of its stops working after the fill
    PENDING = "pending"  # open today, no such read yet
    NEVER_VERIFIED = "never_verified"  # closed, or a past day, without any such read
    UNKNOWN = "unknown"  # its journal rows cannot be read


class Agreement(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class FlowChipV1:
    """One symbol a screener listed that day. `sources`: every screener that listed it that
    day. `change_pct`: (price - pre_close) / pre_close x 100 from its latest captured row, null
    with `change_cause`. `excluded_cause`: the universe filter's refusal in the latest pass
    that listed it, `CAUSE: measured`; null when it passed."""

    symbol: str
    sources: tuple[ScreenerKind, ...]
    change_pct: Decimal | None
    change_cause: str | None
    excluded_cause: str | None


@dataclass(frozen=True)
class FlowStageV1:
    stage: FlowStage
    count: int
    symbols: tuple[str, ...]  # sorted


@dataclass(frozen=True)
class FlowStopV1:
    """The stop of one trade whose entry filled that day (FlowStopState)."""

    trade_id: str
    symbol: str
    state: FlowStopState


@dataclass(frozen=True)
class FlowReconcileV1:
    """The bot's latest reconciliation (account file) and fences (status file), only when
    they are of the day asked for. Each part is null when unknown and its cause says why."""

    agreement: Agreement | None
    agreement_cause: str | None
    read_at: datetime | None
    mismatch: str | None
    fences: tuple[Fence, ...] | None
    fences_cause: str | None


@dataclass(frozen=True)
class FlowV1:
    """GET /flow?date=: the day's pipeline from screeners to broker stops (docs/api.md)."""

    environment: str
    day: date
    chips: tuple[FlowChipV1, ...]
    stages: tuple[FlowStageV1, ...]
    stops: tuple[FlowStopV1, ...]
    reconcile: FlowReconcileV1
    api_version: int = _const(API_VERSION)


# ---------------------------------------------------------------- watching closely (read-only)


@dataclass(frozen=True)
class WatchingRowV1:
    """The latest decision of one symbol. `decisive_check`: the first failed check, the one
    standing between the symbol and an ENTER (null when every recorded check passed).
    `closeness`: how near that check is to passing, 0..1 (see `closeness`), null with
    `closeness_cause`. `quote_age_seconds`: null with `quote_age_cause` (not recorded)."""

    symbol: str
    at: datetime
    strategy: StrategyId
    verdict: Verdict
    decisive_check: CheckV1 | None
    closeness: Decimal | None
    closeness_cause: str | None
    quote_age_seconds: Decimal | None
    quote_age_cause: str | None

    closeness_cause_codes: tuple[ReasonV1, ...] = _reasons_of("closeness_cause")
    quote_age_cause_codes: tuple[ReasonV1, ...] = _reasons_of("quote_age_cause")

class SizeBinding(StrEnum):
    DAILY_ROOM = "daily_room"  # sized down to the room the daily loss ceiling leaves


@dataclass(frozen=True)
class LatestSizeV1:
    """The size the active strategy gave its latest sized decision of the day: the measured
    value of its recorded `quantity` check (whole shares; below 1 = too small to enter), on any
    verdict: the last size given, not a forecast of the next one. `binding`: the limit that cut
    it, only where the record proves it (`daily_room`: a `daily_loss_budget_usd` check and
    floor(its room / the recorded `sizing_risk_per_share`) equal to the quantity); otherwise null
    with `binding_cause`. Read from the decision record, never resized."""

    symbol: str
    at: datetime  # the decision's bar start
    strategy: StrategyId
    verdict: Verdict
    quantity: Decimal
    binding: SizeBinding | None
    binding_cause: str | None


@dataclass(frozen=True)
class WatchingV1:
    """GET /watching?date=: the symbols the active strategy is closest to entering on, closest
    first, at most `[api] watching_max_rows`; `total` counts them before the cap.
    `latest_size`: see LatestSizeV1, null when no decision of the day was sized."""

    day: date
    total: int
    rows: tuple[WatchingRowV1, ...]
    latest_size: LatestSizeV1 | None
    api_version: int = _const(API_VERSION)


ALL_CHECKS_PASSED = "every recorded check passed"
QUOTE_AGE_NOT_RECORDED = "not recorded: decision records hold no quote time"
_RISE = (">", ">=")  # passes once measured rises to the threshold
_FALL = ("<", "<=")  # passes once measured falls to the threshold
_NOT_WATCHING = (Verdict.ENTER, Verdict.HOLD, Verdict.EXIT)
# Rule names the strategies record for their sizing (strategy/price_action_v1.py, simple.py).
QUANTITY_RULE = "quantity"
DAILY_ROOM_RULE = "daily_loss_budget_usd"
SIZING_RISK_RULE = "sizing_risk_per_share"
BINDING_NOT_RECORDED = ("not proven by the record: the planned loss, the notional cap or "
                        "the daily room")


def closeness(check: Check | None, places: int) -> tuple[Decimal | None, str | None]:
    """(closeness, None) or (None, cause) for the decisive (failed) check, rounded down to
    `places`: `>` `>=`: measured / threshold (needs threshold > 0 and measured >= 0);
    `<` `<=`: threshold / measured (needs measured > 0 and threshold >= 0). 1 is at the
    threshold; a value outside 0..1 means the record contradicts itself and is refused."""
    if check is None:
        return None, ALL_CHECKS_PASSED
    measured, threshold = check.measured, check.threshold
    if measured is None or threshold is None:
        return None, f"{check.rule}: a yes/no check has no measured value"
    if check.operator in _RISE:
        if threshold <= 0 or measured < 0:
            return None, f"{check.rule}: needs threshold > 0 and measured >= 0"
        value = measured / threshold
    elif check.operator in _FALL:
        if measured <= 0 or threshold < 0:
            return None, f"{check.rule}: needs measured > 0 and threshold >= 0"
        value = threshold / measured
    else:
        return None, f"{check.rule}: operator {check.operator!r} has no direction"
    if not 0 <= value <= 1:
        return None, f"{check.rule}: recorded as failed but measured meets the threshold"
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), None


def shown_check(check: Check, places: int) -> CheckV1:
    """`check` with measured and threshold rounded to `places` so the shown pair keeps the
    recorded verdict: a failed check rounds each away from passing (for `>` `>=` measured down
    and threshold up, for `<` `<=` the reverse), a passed one toward passing (the opposite).
    Closeness is computed from the recorded values. A value with no more than `places`
    decimals is shown as recorded. Other operators keep the recorded values."""
    if check.operator in _RISE:
        measured_way, threshold_way = ROUND_FLOOR, ROUND_CEILING
    elif check.operator in _FALL:
        measured_way, threshold_way = ROUND_CEILING, ROUND_FLOOR
    else:
        return check_v1(check)
    if check.passed:
        measured_way, threshold_way = threshold_way, measured_way
    step = Decimal(1).scaleb(-places)

    def shown(value: Decimal | None, way: str) -> Decimal | None:
        if value is None or value.as_tuple().exponent >= -places:
            return value  # already short enough: shown as recorded, no padding zeros
        return value.quantize(step, rounding=way)

    return CheckV1(check.rule, shown(check.measured, measured_way), check.operator,
                   shown(check.threshold, threshold_way), check.passed, check.note)


def watching_document(day: date, records: tuple[DecisionRecord, ...], max_rows: int,
                      places: int, check_places: int) -> WatchingV1:
    """`records`: active-strategy records of `day`. The latest decision per symbol unless it is
    ENTER, HOLD or EXIT (entering or in a position); closest first, then by symbol."""
    latest = {r.symbol: r for r in _oldest_first(records)}
    rows = []
    for r in latest.values():
        d = r.decision
        if d.verdict in _NOT_WATCHING:
            continue
        decisive = d.decisive
        value, cause = closeness(decisive, places)
        rows.append(WatchingRowV1(
            symbol=r.symbol, at=r.bar_start, strategy=StrategyId(d.strategy, d.version),
            verdict=d.verdict,
            decisive_check=None if decisive is None else shown_check(decisive, check_places),
            closeness=value, closeness_cause=cause, quote_age_seconds=None,
            quote_age_cause=QUOTE_AGE_NOT_RECORDED))
    rows.sort(key=lambda w: (w.closeness is None, -(w.closeness or 0), w.symbol))
    return WatchingV1(day=day, total=len(rows), rows=tuple(rows[:max_rows]),
                      latest_size=latest_size(records))


def _room_binds(checks: dict[str, Check], quantity: Decimal) -> bool:
    """True only when the record proves the daily room set the quantity: the planned loss was
    lowered to the room AND floor(room / recorded sizing risk per share) is the quantity. The
    room check alone does not prove it: the notional cap can still cut below that."""
    room, risk = checks.get(DAILY_ROOM_RULE), checks.get(SIZING_RISK_RULE)
    if room is None or risk is None or room.measured is None or risk.measured is None:
        return False
    if risk.measured <= 0:
        return False
    return (room.measured / risk.measured).to_integral_value(rounding=ROUND_FLOOR) == quantity


def latest_size(records: tuple[DecisionRecord, ...]) -> LatestSizeV1 | None:
    """The newest record of `records` with a measured `quantity` check, as LatestSizeV1."""
    for r in reversed(_oldest_first(records)):
        checks = {c.rule: c for c in r.decision.checks}
        quantity = checks.get(QUANTITY_RULE)
        if quantity is None or quantity.measured is None:
            continue
        binding = SizeBinding.DAILY_ROOM if _room_binds(checks, quantity.measured) else None
        d = r.decision
        return LatestSizeV1(r.symbol, r.bar_start, StrategyId(d.strategy, d.version), d.verdict,
                            quantity.measured, binding,
                            None if binding else BINDING_NOT_RECORDED)
    return None


# ---------------------------------------------------------------- performance (read-only)


@dataclass(frozen=True)
class ExclusionV1:
    """Closed trades left out of every performance number, counted by their single cause."""

    cause: str
    trades: int

    cause_codes: tuple[ReasonV1, ...] = _reasons_of("cause")

@dataclass(frozen=True)
class PerformanceDayV1:
    """One New York date with counted trades: its gross result and the running R total."""

    day: date
    trades: int
    result_gross_r: Decimal
    result_gross_usd: Decimal
    cumulative_gross_r: Decimal  # from the first counted trade of the range to this day's end


@dataclass(frozen=True)
class PerformanceV1:
    """GET /performance?from=&to=: gross (no fees) results of the trades closed in a range of
    New York dates, in R (result / planned loss) and USD (docs/api.md). Each nullable number
    has a `<name>_cause` that is null exactly when the number is not."""

    environment: str
    from_day: date
    to_day: date
    trades: int  # counted trades: closed in range, with a positive planned loss
    wins: int
    losses: int
    scratches: int  # result exactly 0
    exclusions: tuple[ExclusionV1, ...]
    win_rate: Decimal | None  # wins / trades
    win_rate_cause: str | None
    expectancy_gross_r: Decimal | None
    expectancy_gross_r_cause: str | None
    expectancy_gross_usd: Decimal | None
    expectancy_gross_usd_cause: str | None
    average_win_gross_r: Decimal | None
    average_win_gross_r_cause: str | None
    average_loss_gross_r: Decimal | None  # negative
    average_loss_gross_r_cause: str | None
    profit_factor_gross: Decimal | None  # USD won / USD lost
    profit_factor_gross_cause: str | None
    max_drawdown_gross_r: Decimal  # largest fall from a running peak (start = 0), >= 0
    max_drawdown_gross_usd: Decimal
    result_gross_r: Decimal
    result_gross_usd: Decimal
    days: tuple[PerformanceDayV1, ...]
    api_version: int = _const(API_VERSION)

    win_rate_cause_codes: tuple[ReasonV1, ...] = _reasons_of("win_rate_cause")
    expectancy_gross_r_cause_codes: tuple[ReasonV1, ...] = _reasons_of("expectancy_gross_r_cause")
    expectancy_gross_usd_cause_codes: tuple[ReasonV1, ...] = (
        _reasons_of("expectancy_gross_usd_cause"))
    average_win_gross_r_cause_codes: tuple[ReasonV1, ...] = _reasons_of("average_win_gross_r_cause")
    average_loss_gross_r_cause_codes: tuple[ReasonV1, ...] = (
        _reasons_of("average_loss_gross_r_cause"))
    profit_factor_gross_cause_codes: tuple[ReasonV1, ...] = _reasons_of("profit_factor_gross_cause")

SCHEMA_TYPES: dict[str, type] = {
    "status-v1": StatusV1,
    "command-v1": CommandV1,
    "status-response-v1": StatusResponseV1,
    "command-accepted-v1": CommandAcceptedV1,
    "paired-v1": PairedV1,
    "pending-commands-v1": PendingCommandsV1,
    "error-v1": ErrorV1,
    "days-v1": DaysV1,
    "day-v1": DayV1,
    "bars-v1": BarsV1,
    "chart-v1": ChartV1,
    "settings-v1": SettingsV1,
    "jobs-v1": JobsV1,
    "journal-v1": JournalV1,
    "lab-replay-v1": LabReplayV1,
    "explain-v1": ExplainV1,
    "observers-v1": ObserversV1,
    "decisions-v1": DecisionsV1,
    "skipped-v1": SkippedV1,
    "flow-v1": FlowV1,
    "watching-v1": WatchingV1,
    "performance-v1": PerformanceV1,
    "account-v1": AccountV1,
    "account-response-v1": AccountResponseV1,
}


# ---------------------------------------------------------------- encoding


def format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ContractError("naive datetime: times must be timezone-aware")
    utc = value.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_date(text: str) -> date:
    """A wire date "2026-11-13"; anything else raises ContractError."""
    return _decode("date", text, date)


def parse_time(text: str) -> datetime:
    """A wire time "2026-11-13T15:04:05.000Z"; anything else raises ContractError."""
    return _decode("time", text, datetime)


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(_codes_for(value, f.metadata[REASONS_OF])
                                    if REASONS_OF in f.metadata else getattr(value, f.name))
                for f in fields(value)}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, bool | int | str) or value is None:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractError(f"non-finite decimal {value}")
        return format(value, "f")
    if isinstance(value, datetime):
        return format_time(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    raise ContractError(f"cannot encode {type(value).__name__}")


_MILLISECOND = Decimal("0.001")
_MICROSECONDS_PER_SECOND = 1_000_000


def seconds_between(earlier: datetime, later: datetime) -> Decimal:
    """Exact elapsed seconds, millisecond precision (wire times carry milliseconds)."""
    delta = later - earlier
    micro = (delta.days * 86400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds
    return (Decimal(micro) / _MICROSECONDS_PER_SECOND).quantize(_MILLISECOND)


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- strict decoding


def _optional_inner(hint: Any) -> Any | None:
    if get_origin(hint) in (Union, types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) == 1 and len(get_args(hint)) == 2:
            return args[0]
    return None


def _decode(where: str, raw: Any, hint: Any, meta: Any = None) -> Any:
    inner = _optional_inner(hint)
    if inner is not None:
        return None if raw is None else _decode(where, raw, inner)
    if meta is not None and "const" in meta:
        if isinstance(raw, bool) or raw != meta["const"]:
            raise ContractError(f"{where}: expected {meta['const']!r}, got {raw!r}")
        return raw
    if hint is bool:
        if not isinstance(raw, bool):
            raise ContractError(f"{where}: expected true/false, got {raw!r}")
        return raw
    if hint is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ContractError(f"{where}: expected an integer, got {raw!r}")
        return raw
    if hint is str:
        if not isinstance(raw, str):
            raise ContractError(f"{where}: expected a string, got {raw!r}")
        return raw
    if hint is Decimal:
        if not isinstance(raw, str) or not _DECIMAL.match(raw):
            raise ContractError(f"{where}: expected a decimal string, got {raw!r}")
        try:
            return Decimal(raw)
        except InvalidOperation as exc:  # pragma: no cover - the pattern already guards
            raise ContractError(f"{where}: bad decimal {raw!r}") from exc
    if hint is datetime:
        if not isinstance(raw, str) or not _TIME.match(raw):
            raise ContractError(f"{where}: expected a UTC time like 2026-01-02T15:04:05.000Z, "
                                f"got {raw!r}")
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ContractError(f"{where}: invalid time {raw!r}") from exc
    if hint is date:
        if not isinstance(raw, str) or not _DATE.match(raw):
            raise ContractError(f"{where}: expected a date like 2026-01-02, got {raw!r}")
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise ContractError(f"{where}: invalid date {raw!r}") from exc
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        try:
            return hint(raw)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in hint)
            raise ContractError(f"{where}: {raw!r} is not one of: {allowed}") from exc
    if hint == Params:
        if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
            raise ContractError(f"{where}: expected an object of strings, got {raw!r}")
        return dict(raw)
    if get_origin(hint) is tuple:
        (item,) = [a for a in get_args(hint) if a is not Ellipsis]
        if not isinstance(raw, list):
            raise ContractError(f"{where}: expected a list, got {type(raw).__name__}")
        return tuple(_decode(f"{where}[{i}]", v, item) for i, v in enumerate(raw))
    if is_dataclass(hint):
        return from_jsonable(hint, raw, where)
    raise ContractError(f"{where}: unsupported type {hint!r}")  # pragma: no cover


def from_jsonable(cls: type, raw: Any, where: str = "$") -> Any:
    """Build `cls` from decoded JSON, refusing unknown, missing and mistyped keys. Only a key
    marked `_added_later` may be absent, so a document written by an older bot (a status file
    left on disk across an update) still reads; a field's plain default never makes its key
    optional (a flatten command without `symbol` is refused, not read as "everything")."""
    if not isinstance(raw, dict):
        raise ContractError(f"{where}: expected an object, got {type(raw).__name__}")
    hints = get_type_hints(cls)
    expected = [f.name for f in fields(cls)]
    unknown = sorted(set(raw) - set(expected))
    optional = {f.name for f in fields(cls) if f.metadata.get(ADDED_AFTER_V1)}
    missing = [name for name in expected if name not in raw and name not in optional]
    if unknown:
        raise ContractError(f"{where}: unknown keys: {', '.join(unknown)}")
    if missing:
        raise ContractError(f"{where}: missing keys: {', '.join(missing)}")
    meta = {f.name: f.metadata or None for f in fields(cls)}
    computed = {f.name for f in fields(cls) if not f.init}  # rebuilt from their source field
    return cls(**{n: _decode(f"{where}.{n}", raw[n], hints[n], meta[n])
                  for n in expected if n in raw and n not in computed})


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise ContractError(f"{name} is not allowed")


def parse_json(text: str | bytes) -> Any:
    """Strict JSON: no duplicate keys, no NaN/Infinity."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError(f"not valid JSON: {exc}") from exc


def loads(cls: type, text: str | bytes) -> Any:
    return from_jsonable(cls, parse_json(text))


# ---------------------------------------------------------------- JSON Schema


def _schema(hint: Any, meta: Any = None) -> dict[str, Any]:
    inner = _optional_inner(hint)
    if inner is not None:
        return {"anyOf": [_schema(inner), {"type": "null"}]}
    if meta is not None and "const" in meta:
        return {"type": "integer", "const": meta["const"]}
    if hint is bool:
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    if hint is str:
        return {"type": "string"}
    if hint is Decimal:
        return {"type": "string", "pattern": DECIMAL_PATTERN}
    if hint is datetime:
        return {"type": "string", "pattern": TIME_PATTERN}
    if hint is date:
        return {"type": "string", "pattern": DATE_PATTERN}
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        return {"type": "string", "enum": [m.value for m in hint]}
    if hint == Params:
        return {"type": "object", "additionalProperties": {"type": "string"}}
    if get_origin(hint) is tuple:
        (item,) = [a for a in get_args(hint) if a is not Ellipsis]
        return {"type": "array", "items": _schema(item)}
    if is_dataclass(hint):
        hints = get_type_hints(hint)
        names = [f.name for f in fields(hint)]
        return {
            "type": "object",
            "properties": {f.name: _schema(hints[f.name], f.metadata or None)
                           for f in fields(hint)},
            "required": names,
            "additionalProperties": False,
        }
    raise ContractError(f"no schema for {hint!r}")  # pragma: no cover


def json_schema(cls: type) -> dict[str, Any]:
    name = next(k for k, v in SCHEMA_TYPES.items() if v is cls)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"tickr:{name}", "title": cls.__name__, **_schema(cls)}


SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


# ---------------------------------------------------------------- summary in plain words


def summarize(status: StatusV1 | None, age_seconds: Decimal | None, stale: bool,
              unavailable_cause: str | None) -> str:
    """The one line a person should read first: why entries are blocked, or that they are not."""
    if status is None:
        return f"Bot status unavailable: {unavailable_cause}. Entries are not possible."
    if stale:
        return (f"Bot status is {age_seconds} s old: the bot may be down. "
                "Do not trust the rest of this page.")
    if status.entry_block is not None:
        return status.entry_block.text
    return "Entries allowed."


# ---------------------------------------------------------------- atomic files


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` so readers see the old file or the new one, never a partial one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    _fsync_dir(path.parent)


def write_status(path: Path | str, status: StatusV1) -> None:
    """Called by the bot after each loop step that changes what /status shows."""
    write_atomic(Path(path), dumps(status))


def read_status(path: Path | str) -> StatusV1:
    """Raises FileNotFoundError when the bot has not written a status yet, else ContractError."""
    return loads(StatusV1, Path(path).read_bytes())


def write_account(path: Path | str, account: AccountV1) -> None:
    """Called by the bot after each successful broker read of positions and open orders."""
    write_atomic(Path(path), dumps(account))


def read_account(path: Path | str) -> AccountV1:
    """Raises FileNotFoundError when the bot has not written one yet, else ContractError."""
    return loads(AccountV1, Path(path).read_bytes())


def new_command(kind: CommandKind, now: datetime, source: str,
                symbol: str | None = None) -> CommandV1:
    return CommandV1(command_id=str(uuid.uuid4()), kind=kind, requested_at=now, source=source,
                     symbol=symbol)


def write_command(inbox: Path | str, command: CommandV1) -> Path:
    """Write one command atomically as <inbox>/<command_id>.json."""
    if not _UUID.match(command.command_id):
        raise ContractError(f"command_id {command.command_id!r} is not a lowercase uuid")
    path = Path(inbox) / f"{command.command_id}.json"
    write_atomic(path, dumps(command))
    return path


@dataclass(frozen=True)
class InboxItem:
    """One file in the inbox: a command to act on, or a refusal (bad file, expired, future)."""

    path: Path
    command: CommandV1 | None
    refusal: str | None


def read_commands(inbox: Path | str, now: datetime, max_age_seconds: Decimal) -> list[InboxItem]:
    """Read every command file in the inbox, oldest request first; refusals come first.

    The bot's contract for each item: journal it (command_id, kind, outcome or refusal) in one
    transaction, act on it if it has a command, then `retire_command`. After a crash a retired-less
    file is read again: a command_id already in the journal must be retired without acting.
    """
    inbox = Path(inbox)
    if not inbox.is_dir():
        return []
    refused: list[InboxItem] = []
    valid: list[InboxItem] = []
    for path in sorted(inbox.iterdir()):
        if not _COMMAND_FILE.match(path.name):
            continue  # temp files and anything else are not commands
        try:
            command: CommandV1 = loads(CommandV1, path.read_bytes())
        except (ContractError, OSError) as exc:
            refused.append(InboxItem(path, None, f"unreadable command file: {exc}"))
            continue
        age = seconds_between(command.requested_at, now)
        if path.name != f"{command.command_id}.json":
            refused.append(InboxItem(path, None, "file name does not match command_id"))
        elif age < 0:
            refused.append(InboxItem(path, None, f"requested {-age} s in the future"))
        elif age > max_age_seconds:
            refused.append(InboxItem(
                path, None, f"expired: requested {age} s ago (limit {max_age_seconds} s)"))
        else:
            valid.append(InboxItem(path, command, None))
    valid.sort(key=lambda item: (item.command.requested_at, item.command.command_id))
    return refused + valid


def pending_commands(inbox: Path | str) -> list[CommandV1]:
    """Valid commands still waiting in the inbox (no age check), oldest first."""
    inbox = Path(inbox)
    if not inbox.is_dir():
        return []
    found: list[CommandV1] = []
    for path in sorted(inbox.iterdir()):
        if _COMMAND_FILE.match(path.name):
            try:
                found.append(loads(CommandV1, path.read_bytes()))
            except (ContractError, OSError):
                continue
    return sorted(found, key=lambda c: (c.requested_at, c.command_id))


def count_inbox(inbox: Path | str) -> int:
    inbox = Path(inbox)
    if not inbox.is_dir():
        return 0
    return sum(1 for p in inbox.iterdir() if _COMMAND_FILE.match(p.name))


def retire_command(item: InboxItem, processed_dir: Path | str) -> Path:
    """Move a journaled command out of the inbox (atomic rename on the same file system)."""
    processed = Path(processed_dir)
    processed.mkdir(parents=True, exist_ok=True)
    target = processed / item.path.name
    os.replace(item.path, target)
    _fsync_dir(item.path.parent)
    _fsync_dir(processed)
    return target


def write_schemas(directory: Path = SCHEMA_DIR) -> None:
    """Regenerate the committed schema files: `python -m tickr.api.contract`."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, cls in SCHEMA_TYPES.items():
        text = json.dumps(json_schema(cls), indent=2) + "\n"
        (directory / f"{name}.schema.json").write_text(text, "utf-8")


if __name__ == "__main__":
    write_schemas()
