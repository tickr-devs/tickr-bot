"""Versioned contract between the bot, the local API and its clients (status page, macOS app).

Two files on disk decouple the bot from the API process:
- the bot writes `StatusV1` to `<data_dir>/<status_file>` with `write_status` (temp file + rename);
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
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

API_VERSION = 1
STATUS_SCHEMA_VERSION = 1
COMMAND_SCHEMA_VERSION = 1

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


class CommandOutcome(StrEnum):
    DONE = "done"
    REFUSED = "refused"


# ---------------------------------------------------------------- status written by the bot


@dataclass(frozen=True)
class Gate:
    open: bool
    cause: str


@dataclass(frozen=True)
class StrategyId:
    name: str
    version: str


@dataclass(frozen=True)
class EntryBlock:
    """The single reason entries are blocked: a stable cause code plus plain words for people."""

    cause: str
    text: str


@dataclass(frozen=True)
class Fence:
    kind: str
    since: datetime
    text: str


@dataclass(frozen=True)
class Stop:
    state: StopState
    price: Decimal | None
    client_order_id: str | None


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    opened_at: datetime
    stop: Stop


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
    position: Position | None
    trades_today: tuple[Trade, ...]
    result_today_gross_usd: Decimal
    freshness: Freshness
    recent_commands: tuple[CommandRecord, ...]
    schema_version: int = _const(STATUS_SCHEMA_VERSION)


# ---------------------------------------------------------------- commands


@dataclass(frozen=True)
class CommandV1:
    command_id: str
    kind: CommandKind
    requested_at: datetime
    source: str
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
class DayTrade:
    """A closed trade with fills. `stop_price` is the first protective stop sent (null if none
    was); 1 R = `planned_loss_usd`."""

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


@dataclass(frozen=True)
class DaySummary:
    """One New York date with records: closed trades, a bot run, or captured market data."""

    day: date
    trades: int
    wins: int
    losses: int
    result_gross_usd: Decimal
    has_bars: bool


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


# ---------------------------------------------------------------- settings (read-only)


@dataclass(frozen=True)
class RiskSettings:
    planned_loss_usd: Decimal
    position_notional_cap_usd: Decimal
    max_daily_loss_usd: Decimal
    max_open_positions: int
    max_quote_age_seconds: Decimal


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
    api_version: int = _const(API_VERSION)


SCHEMA_TYPES: dict[str, type] = {
    "status-v1": StatusV1,
    "command-v1": CommandV1,
    "status-response-v1": StatusResponseV1,
    "command-accepted-v1": CommandAcceptedV1,
    "pending-commands-v1": PendingCommandsV1,
    "error-v1": ErrorV1,
    "days-v1": DaysV1,
    "day-v1": DayV1,
    "bars-v1": BarsV1,
    "settings-v1": SettingsV1,
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


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
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
    if get_origin(hint) is tuple:
        (item,) = [a for a in get_args(hint) if a is not Ellipsis]
        if not isinstance(raw, list):
            raise ContractError(f"{where}: expected a list, got {type(raw).__name__}")
        return tuple(_decode(f"{where}[{i}]", v, item) for i, v in enumerate(raw))
    if is_dataclass(hint):
        return from_jsonable(hint, raw, where)
    raise ContractError(f"{where}: unsupported type {hint!r}")  # pragma: no cover


def from_jsonable(cls: type, raw: Any, where: str = "$") -> Any:
    """Build `cls` from decoded JSON, refusing unknown, missing and mistyped keys."""
    if not isinstance(raw, dict):
        raise ContractError(f"{where}: expected an object, got {type(raw).__name__}")
    hints = get_type_hints(cls)
    expected = [f.name for f in fields(cls)]
    unknown = sorted(set(raw) - set(expected))
    missing = [name for name in expected if name not in raw]
    if unknown:
        raise ContractError(f"{where}: unknown keys: {', '.join(unknown)}")
    if missing:
        raise ContractError(f"{where}: missing keys: {', '.join(missing)}")
    meta = {f.name: f.metadata or None for f in fields(cls)}
    return cls(**{n: _decode(f"{where}.{n}", raw[n], hints[n], meta[n]) for n in expected})


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
            "$id": f"aperture:{name}", "title": cls.__name__, **_schema(cls)}


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


def new_command(kind: CommandKind, now: datetime, source: str) -> CommandV1:
    return CommandV1(command_id=str(uuid.uuid4()), kind=kind, requested_at=now, source=source)


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
    """Regenerate the committed schema files: `python -m aperture.api.contract`."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, cls in SCHEMA_TYPES.items():
        text = json.dumps(json_schema(cls), indent=2) + "\n"
        (directory / f"{name}.schema.json").write_text(text, "utf-8")


if __name__ == "__main__":
    write_schemas()
