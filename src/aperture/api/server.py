"""Local HTTP API: GET /status and the status page, POST operator commands.

stdlib only. Binds a loopback address and refuses any request whose Host (or Origin, when sent)
is not in the configured allowlist, which blocks DNS rebinding and cross-site requests from a
browser. Remote access (the phone, through an HTTPS proxy on this Mac such as `tailscale serve`)
has its own loopback listener, `[api] remote_port`: every request there must carry the paired
bearer token whatever its Host, and its Host must be one of `[api] remote_hosts`. Whether a token
is needed is decided by the listener a request arrives on, never by a header the caller chooses.
The server keeps only the token's SHA-256, written by `pair`.
Commands additionally require `Content-Type: application/json`, which a cross-site form
cannot send without a CORS preflight, and this server answers no preflight.

The API never talks to the broker's trading or account routes and never changes bot state
itself: it reads the bot's status file and drops command files into the inbox. The bot decides,
acts and journals. The one market-data read, GET /chart, goes through its own source and request
budget (api/chart.py), off unless `[chart] webull_enabled`.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from aperture.api import contract, flow
from aperture.api.after import AfterSettings, SkippedAfter
from aperture.api.chart import Chart, ChartRefused, ChartSettings, parse_query
from aperture.api.contract import (
    AccountResponseV1,
    CommandAcceptedV1,
    CommandKind,
    ContractError,
    ErrorV1,
    PendingCommandsV1,
    RiskSettings,
    SessionSettings,
    SettingsV1,
    StatusResponseV1,
)
from aperture.api.history import SYMBOL_PATTERN, History, HistoryPaths, HistoryUnavailable
from aperture.api.observer_results import ObserverResults, SimulationPaths
from aperture.calendar import ny_date_of
from aperture.config import Config, ConfigError
from aperture.data.capture import CaptureError
from aperture.data.discovery import DiscoverySettings
from aperture.observers.errors import ObserverError
from aperture.observers.loader import read_identity
from aperture.observers.log import DecisionLog
from aperture.research.errors import RecordFileError
from aperture.research.records import DecisionRecord
from aperture.strategy.base import Role

STATIC_DIR = Path(__file__).resolve().parent / "static"
COMMAND_SOURCE = "api"
JSON_TYPE = "application/json"
ENTRY_STOP_STATES = {"on": CommandKind.ENTRY_STOP_ON, "off": CommandKind.ENTRY_STOP_OFF}
COMMAND_ROUTES = {
    "/commands/entry-stop": None,  # kind comes from the body
    "/commands/cancel-own-orders": CommandKind.CANCEL_OWN_ORDERS,
    "/commands/flatten": CommandKind.FLATTEN,
    "/commands/stop-bot": CommandKind.STOP_BOT,
    "/commands/emergency": CommandKind.EMERGENCY,
}
# Optional on every POST /commands/*: a repeat with the same key returns the first answer.
IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
IDEMPOTENCY = "idempotency"  # folder under <commands_dir> holding one file per key


# A remote Host header: a lowercase DNS name with at least one dot, optionally ":port".
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
REMOTE_HOST = re.compile(rf"^{_LABEL}(\.{_LABEL})+(:[0-9]+)?$")
BEARER = "Bearer "


@dataclass(frozen=True)
class ApiSettings:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    remote_hosts: tuple[str, ...]
    remote_port: int
    token_hash_path: Path
    token_bytes: int
    pairing_path: Path
    status_path: Path
    inbox: Path
    idempotency: Path  # <commands_dir>/idempotency: remembered Idempotency-Key answers
    idempotency_ttl_seconds: int
    status_stale_seconds: Decimal
    poll_seconds: Decimal
    max_request_body_bytes: int
    max_pending_commands: int
    history: HistoryPaths
    decisions: Path  # <decisions_dir>/<environment>, read by GET /explain
    shown: SettingsV1  # served at GET /settings
    strategies_root: Path  # holds strategies/<folder>/strategy.toml, read by GET /observers
    account_path: Path  # the bot's account-v1 file, served at GET /account
    account_stale_seconds: Decimal
    simulation: SimulationPaths  # inputs of the observers' simulated day results
    observer_trades_max_rows: int  # most simulated trades per observer at GET /observers
    universe: DiscoverySettings  # the universe filter GET /flow applies to captured passes
    watching_max_rows: int
    closeness_places: int  # decimal places of GET /watching closeness
    check_places: int  # decimal places of check values in every document that shows checks
    change_pct_places: int  # decimal places of GET /flow change_pct
    performance_max_range_days: int  # longest GET /performance range, both ends counted
    performance_decimal_places: int  # rounding of GET /performance quotients
    chart: ChartSettings  # GET /chart: source switch, cache and calendar
    after: AfterSettings  # prices after, overshoot (day-v1) and what-if (skipped-v1)


def settings_from_config(cfg: Config) -> ApiSettings:
    api = cfg.api
    try:
        loopback = ipaddress.ip_address(api.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError(f"[api] host {api.host!r} is not a loopback IP address")
    if not api.allowed_hosts:
        raise ConfigError("[api] allowed_hosts is empty")
    for name in api.remote_hosts:
        if not REMOTE_HOST.match(name):
            raise ConfigError(f"[api] remote_hosts entry {name!r} is not a lowercase DNS name "
                              "(optionally with :port)")
        if name.split(":", 1)[0] in api.allowed_hosts:
            raise ConfigError(f"[api] remote_hosts entry {name!r} is also a local allowed host")
    if api.remote_port == api.port:
        raise ConfigError("[api] remote_port must differ from port")
    if api.remote_token_bytes <= 0:
        raise ConfigError("[api] remote_token_bytes must be positive")
    if api.idempotency_key_ttl_seconds <= api.command_max_age_seconds:
        raise ConfigError("[api] idempotency_key_ttl_seconds must be above "
                          "command_max_age_seconds, so a repeat can never queue a command twice")
    if api.observer_trades_max_rows <= 0:
        raise ConfigError("[api] observer_trades_max_rows must be positive")
    if api.watching_max_rows <= 0:
        raise ConfigError("[api] watching_max_rows must be positive")
    if min(api.watching_closeness_decimal_places, api.flow_change_pct_decimal_places,
           api.check_decimal_places) < 0:
        raise ConfigError("[api] decimal places must not be negative")
    if api.performance_max_range_days <= 0:
        raise ConfigError("[api] performance_max_range_days must be positive")
    if api.performance_decimal_places < 0:
        raise ConfigError("[api] performance_decimal_places must not be negative")
    minutes = api.price_after_minutes
    if not minutes or minutes[0] <= 0 or list(minutes) != sorted(set(minutes)):
        raise ConfigError("[api] price_after_minutes must be strictly increasing minutes > 0")
    if api.after_r_decimal_places < 0:
        raise ConfigError("[api] after_r_decimal_places must not be negative")
    data = cfg.root / cfg.run.data_dir
    return ApiSettings(
        host=api.host,
        port=api.port,
        allowed_hosts=api.allowed_hosts,
        remote_hosts=api.remote_hosts,
        remote_port=api.remote_port,
        token_hash_path=data / api.remote_token_hash_file,
        token_bytes=api.remote_token_bytes,
        pairing_path=data / api.remote_pairing_file,
        status_path=data / api.status_file,
        inbox=data / api.commands_dir / contract.INBOX,
        idempotency=data / api.commands_dir / IDEMPOTENCY,
        idempotency_ttl_seconds=api.idempotency_key_ttl_seconds,
        status_stale_seconds=api.status_stale_seconds,
        poll_seconds=api.poll_seconds,
        max_request_body_bytes=api.max_request_body_bytes,
        max_pending_commands=api.max_pending_commands,
        history=HistoryPaths.from_config(cfg),
        decisions=DecisionLog.from_config(cfg).directory,
        shown=shown_settings(cfg),
        strategies_root=cfg.root,
        account_path=data / api.account_file,
        account_stale_seconds=api.account_stale_seconds,
        simulation=SimulationPaths.from_config(cfg),
        observer_trades_max_rows=api.observer_trades_max_rows,
        universe=DiscoverySettings.from_config(cfg),
        watching_max_rows=api.watching_max_rows,
        closeness_places=api.watching_closeness_decimal_places,
        check_places=api.check_decimal_places,
        change_pct_places=api.flow_change_pct_decimal_places,
        performance_max_range_days=api.performance_max_range_days,
        performance_decimal_places=api.performance_decimal_places,
        chart=ChartSettings.from_config(cfg),
        after=AfterSettings(minutes, api.after_r_decimal_places,
                            (cfg.root / cfg.session.calendar_file).parent),
    )


def shown_settings(cfg: Config) -> SettingsV1:
    """The config numbers clients show, exactly as loaded."""
    risk, session = cfg.risk, cfg.session
    return SettingsV1(
        environment=cfg.environment_name,
        active_strategy=cfg.strategy.active,
        observers=tuple(cfg.strategy.observers),
        risk=RiskSettings(
            planned_loss_usd=Decimal(risk.planned_loss_usd),
            position_notional_cap_usd=Decimal(risk.position_notional_cap_usd),
            max_daily_loss_usd=Decimal(risk.max_daily_loss_usd),
            max_open_positions=risk.max_open_positions,
            max_quote_age_seconds=Decimal(risk.max_quote_age_seconds),
            min_daily_room_usd=Decimal(risk.min_daily_room_usd)),
        session=SessionSettings(
            timezone=session.timezone,
            last_entry_minutes_before_close=session.last_entry_minutes_before_close,
            flatten_minutes_before_close=session.flatten_minutes_before_close,
            half_days_enabled=session.half_days_enabled),
        status_stale_seconds=cfg.api.status_stale_seconds,
        poll_seconds=cfg.api.poll_seconds,
        broker_read_max_age_seconds=cfg.orders.broker_read_max_age_seconds,
        broker_read_warn_seconds=cfg.api.broker_read_warn_seconds,
    )


def _now() -> datetime:
    return datetime.now(UTC)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings: ApiSettings, clock: Callable[[], datetime] = _now, *,
                 remote: bool = False, inbox_lock: threading.Lock | None = None) -> None:
        self.settings = settings
        self.clock = clock
        self.remote = remote
        # Shared with the other listener so both count the inbox under one lock.
        self.inbox_lock = inbox_lock or threading.Lock()
        self.page = (STATIC_DIR / "status.html").read_bytes()
        self.history = History(settings.history, settings.after)
        self.observer_results = ObserverResults(settings.simulation,
                                                settings.observer_trades_max_rows)
        self.skipped = SkippedAfter(settings.simulation, settings.after.minutes,
                                    settings.check_places)
        # No source until one is attached (python -m aperture.api, or the demo's world).
        self.chart = Chart(settings.chart)
        port = settings.remote_port if remote else settings.port
        super().__init__((settings.host, port), Handler)

    @property
    def allowed_host_headers(self) -> frozenset[str]:
        return frozenset(f"{h}:{self.server_port}" for h in self.settings.allowed_hosts)

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{h}:{self.server_port}" for h in self.settings.allowed_hosts)

    def status_response(self) -> StatusResponseV1:
        now = self.clock()
        status = None
        age = None
        cause = None
        try:
            status = contract.read_status(self.settings.status_path)
        except FileNotFoundError:
            cause = "the bot has not written a status file"
        except (ContractError, OSError) as exc:
            cause = f"status file is invalid ({exc})"
        if status is not None:
            age = contract.seconds_between(status.written_at, now)
            if age < 0:
                status, age, cause = None, None, "status file is dated in the future"
        stale = age is not None and age > self.settings.status_stale_seconds
        return StatusResponseV1(
            served_at=now,
            status=status,
            age_seconds=age,
            stale=stale,
            unavailable_cause=cause,
            summary=contract.summarize(status, age, stale, cause),
            poll_seconds=self.settings.poll_seconds,
        )

    def account_response(self) -> AccountResponseV1 | ErrorV1:
        """The bot's account-v1 document with its age; an `unavailable` error when the file
        is missing, invalid or dated in the future."""
        now = self.clock()
        try:
            account = contract.read_account(self.settings.account_path)
        except FileNotFoundError:
            return ErrorV1("unavailable", "the bot has not written an account file")
        except (ContractError, OSError) as exc:
            return ErrorV1("unavailable", f"account file is invalid ({exc})")
        age = contract.seconds_between(account.read_at, now)
        if age < 0:
            return ErrorV1("unavailable", "account file is dated in the future")
        return AccountResponseV1(served_at=now, account=account, age_seconds=age,
                                 stale=age > self.settings.account_stale_seconds)

    def enqueue(self, kind: CommandKind, symbol: str | None = None,
                key: str | None = None) -> CommandAcceptedV1 | ErrorV1 | bytes:
        """Queue one command. With an Idempotency-Key already seen within its lifetime the
        first answer comes back as stored bytes and nothing is queued; the same key for a
        different command is a conflict. The command file is written before the key, so a
        crash between the two can at worst queue a repeat, never answer for a command that
        was not queued (every command is idempotent in its effect on the bot)."""
        with self.inbox_lock:
            now = self.clock()
            request = {"kind": kind.value, "symbol": symbol}
            if key is not None:
                stored = self._remembered(key, now)
                if stored is not None:
                    if stored["request"] != request:
                        return ErrorV1("conflict", f"{IDEMPOTENCY_HEADER} was already used for "
                                       f"another command ({stored['request']['kind']})")
                    return stored["answer"].encode("utf-8")
            if contract.count_inbox(self.settings.inbox) >= self.settings.max_pending_commands:
                return ErrorV1("too_many_pending",
                               f"{self.settings.max_pending_commands} commands are already "
                               "waiting for the bot")
            command = contract.new_command(kind, now, COMMAND_SOURCE, symbol)
            contract.write_command(self.settings.inbox, command)
            state = self.status_response()
            warning = None
            if state.status is None or state.stale:
                warning = ("the bot does not look alive (" + state.summary + "); the command "
                           "runs only if the bot reads it before it expires")
            accepted = CommandAcceptedV1(command=command, warning=warning)
            if key is not None:
                self._remember(key, now, request, contract.dumps(accepted))
        return accepted

    def _key_path(self, key: str) -> Path:
        # Hashed: file names stay safe and distinct on a case-insensitive file system.
        return self.settings.idempotency / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _remembered(self, key: str, now: datetime) -> dict[str, Any] | None:
        """The stored record of `key` while it is within its lifetime, else None. A record
        that cannot be read raises: the answer is then `internal`, never a second command."""
        try:
            record = json.loads(self._key_path(key).read_text("utf-8"))
        except FileNotFoundError:
            return None
        if record["key"] != key:
            raise ContractError("idempotency record does not match its key")
        return None if self._expired(record, now) else record

    def _expired(self, record: dict[str, Any], now: datetime) -> bool:
        stored_at = contract.parse_time(record["stored_at"])
        return contract.seconds_between(stored_at, now) > self.settings.idempotency_ttl_seconds

    def _remember(self, key: str, now: datetime, request: dict[str, Any], answer: str) -> None:
        contract.write_atomic(self._key_path(key), json.dumps({
            "key": key, "stored_at": contract.format_time(now), "request": request,
            "answer": answer}))
        self._forget_expired(now)

    def _forget_expired(self, now: datetime) -> None:
        """Bound the folder by the configured lifetime, judged by the API's own clock. A file
        that cannot be judged is left alone (a later repeat of its key answers `internal`)."""
        for path in self.settings.idempotency.glob("*.json"):
            try:
                if self._expired(json.loads(path.read_text("utf-8")), now):
                    path.unlink()
            except (OSError, ValueError, KeyError, TypeError):
                continue


_ERROR_CODES = {
    "forbidden_host": 403, "forbidden_origin": 403, "not_found": 404,
    "method_not_allowed": 405, "length_required": 411, "payload_too_large": 413,
    "unsupported_media_type": 415, "bad_request": 400, "too_many_pending": 429,
    "internal": 500, "unavailable": 503, "unauthorized": 401, "conflict": 409,
    "rate_limited": 429,
}
GET_ROUTES = ("/", "/status", "/commands/pending", "/schema", "/days", "/bars", "/settings",
              "/explain", "/observers", "/decisions", "/skipped", "/account", "/flow",
              "/watching", "/performance", "/chart")
DAY_PREFIX = "/days/"


class BadQuery(ValueError):
    """The query string is not exactly what the route takes; the message is the cause."""


def _query(raw: str, names: frozenset[str],
           optional: frozenset[str] = frozenset()) -> dict[str, str]:
    """Exactly the parameters `names` (those in `optional` may be left out), each once and
    non-empty; anything else is refused."""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=bool(raw))
    except ValueError as exc:
        raise BadQuery(f"malformed query string: {exc}") from exc
    found: dict[str, str] = {}
    for name, value in pairs:
        if name not in names:
            raise BadQuery(f"unknown query parameter {name!r}")
        if name in found:
            raise BadQuery(f"query parameter {name!r} given twice")
        if not value:
            raise BadQuery(f"query parameter {name!r} is empty")
        found[name] = value
    missing = sorted(names - optional - set(found))
    if missing:
        raise BadQuery(f"missing query parameters: {', '.join(missing)}")
    return found


def _day(text: str) -> date:
    try:
        return contract.parse_date(text)
    except ContractError as exc:
        raise BadQuery(str(exc)) from exc


class Handler(BaseHTTPRequestHandler):
    server: ApiServer
    server_version = "aperture-api"
    sys_version = ""

    # ------------------------------------------------------------ plumbing

    def _send(self, code: int, body: bytes, content_type: str, extra: dict[str, str]) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, document: Any) -> None:
        self._send(code, contract.dumps(document).encode("utf-8"),
                   f"{JSON_TYPE}; charset=utf-8", {})

    def _error(self, error: str, cause: str, extra: dict[str, str] | None = None) -> None:
        body = contract.dumps(ErrorV1(error, cause)).encode("utf-8")
        self._send(_ERROR_CODES[error], body, f"{JSON_TYPE}; charset=utf-8", extra or {})

    def _guard(self) -> bool:
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if self.server.remote:
            # The remote listener: the token is always required; Host guards against rebinding.
            if host is None or host not in self.server.settings.remote_hosts:
                self._error("forbidden_host", f"Host {host!r} is not allowed")
                return False
            if origin is not None and origin != f"https://{host}":
                self._error("forbidden_origin", f"Origin {origin!r} is not allowed")
                return False
            return self._authorized()
        if host in self.server.allowed_host_headers:
            if origin is not None and origin not in self.server.allowed_origins:
                self._error("forbidden_origin", f"Origin {origin!r} is not allowed")
                return False
            return True
        self._error("forbidden_host", f"Host {host!r} is not allowed")
        return False

    def _authorized(self) -> bool:
        """A remote request must present the paired token. The token is never logged or echoed."""
        challenge = {"WWW-Authenticate": "Bearer"}
        settings = self.server.settings
        try:
            expected = settings.token_hash_path.read_text("ascii").strip()
        except FileNotFoundError:
            self._error("unauthorized", "no phone is paired; run `python -m aperture.api pair`",
                        challenge)
            return False
        except (OSError, UnicodeDecodeError) as exc:
            self._error("unavailable",
                        f"the paired token's hash file is unreadable ({type(exc).__name__})")
            return False
        header = self.headers.get("Authorization") or ""
        if not header.startswith(BEARER):
            self._error("unauthorized", "a bearer token is required", challenge)
            return False
        presented = hashlib.sha256(header[len(BEARER):].encode("utf-8")).hexdigest()
        if not hmac.compare_digest(presented, expected):
            self._error("unauthorized", "the bearer token is not the paired one", challenge)
            return False
        # The phone has the token now: the pairing link left for the owner is no longer needed.
        settings.pairing_path.unlink(missing_ok=True)
        return True

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write(f"{self.log_date_time_string()} api {format % args}\n")

    # ------------------------------------------------------------ methods

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._guard():
            return
        try:
            path = self._path()
            if path == "/":
                self._send(200, self.server.page, "text/html; charset=utf-8", {
                    "Content-Security-Policy": (
                        "default-src 'none'; script-src 'unsafe-inline'; "
                        "style-src 'unsafe-inline'; connect-src 'self'; "
                        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                })
            elif path == "/status":
                self._json(200, self.server.status_response())
            elif path == "/commands/pending":
                pending = contract.pending_commands(self.server.settings.inbox)
                self._json(200, PendingCommandsV1(commands=tuple(pending)))
            elif path == "/schema":
                self._json(200, {name: contract.json_schema(cls)
                                 for name, cls in contract.SCHEMA_TYPES.items()})
            elif path == "/settings":
                self._json(200, self.server.settings.shown)
            elif path == "/explain":
                self._explain()
            elif path in ("/observers", "/decisions", "/skipped", "/flow", "/watching"):
                self._recorded_decisions(path)
            elif path == "/account":
                self._account()
            elif path == "/chart":
                self._chart()
            elif path in COMMAND_ROUTES:
                self._error("method_not_allowed", "use POST", {"Allow": "POST"})
            elif path in ("/days", "/bars", "/performance") or path.startswith(DAY_PREFIX):
                self._history(path)
            else:
                self._error("not_found", f"no resource {path!r}")
        except Exception as exc:  # noqa: BLE001 - one typed answer instead of a dropped socket
            self._error("internal", type(exc).__name__)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self._guard():
            return
        try:
            path = self._path()
            if path not in COMMAND_ROUTES:
                if path in GET_ROUTES or path.startswith(DAY_PREFIX):
                    self._error("method_not_allowed", "use GET", {"Allow": "GET"})
                else:
                    self._error("not_found", f"no resource {path!r}")
                return
            key = self.headers.get(IDEMPOTENCY_HEADER)
            if key is not None and not IDEMPOTENCY_KEY.match(key):
                self._error("bad_request", f"{IDEMPOTENCY_HEADER} must be 1-128 characters "
                            "from A-Z a-z 0-9 . _ : -")
                return
            body = self._read_json_body()
            if body is None:
                return
            kind = COMMAND_ROUTES[path]
            symbol = None
            if kind is None:
                if set(body) != {"state"} or body["state"] not in ENTRY_STOP_STATES:
                    self._error("bad_request", 'body must be exactly {"state": "on"} or '
                                '{"state": "off"}')
                    return
                kind = ENTRY_STOP_STATES[body["state"]]
            elif kind is CommandKind.FLATTEN and body:
                # null is refused, never read as "everything": a client bug must not widen it.
                if (set(body) != {"symbol"} or not isinstance(body["symbol"], str)
                        or not SYMBOL_PATTERN.match(body["symbol"])):
                    self._error("bad_request", 'body must be {} or {"symbol": "<TICKER>"} '
                                "with the ticker in upper case")
                    return
                symbol = body["symbol"]
            elif body:
                self._error("bad_request", "body must be an empty JSON object {}")
                return
            result = self.server.enqueue(kind, symbol, key)
            if isinstance(result, ErrorV1):
                self._error(result.error, result.cause)
            elif isinstance(result, bytes):
                self._send(202, result, f"{JSON_TYPE}; charset=utf-8", {})
            else:
                self._json(202, result)
        except Exception as exc:  # noqa: BLE001 - one typed answer instead of a dropped socket
            self._error("internal", type(exc).__name__)

    def _history(self, path: str) -> None:
        """GET /days, /days/<YYYY-MM-DD>, /bars?symbol=&date=, /performance?from=&to=:
        read-only history."""
        history = self.server.history
        today = ny_date_of(self.server.clock())
        query = urlsplit(self.path).query
        try:
            if path == "/days":
                _query(query, frozenset())
                document: Any = history.days(today)
            elif path == "/bars":
                params = _query(query, frozenset({"symbol", "date"}))
                if not SYMBOL_PATTERN.match(params["symbol"]):
                    raise BadQuery(f"symbol {params['symbol']!r} is not a ticker")
                document = history.bars(params["symbol"], _day(params["date"]), today)
            elif path == "/performance":
                params = _query(query, frozenset({"from", "to"}))
                first, last = _day(params["from"]), _day(params["to"])
                if first > last:
                    raise BadQuery(f"from {first.isoformat()} is after to {last.isoformat()}")
                limit = self.server.settings.performance_max_range_days
                if (last - first).days + 1 > limit:
                    raise BadQuery(f"range of {(last - first).days + 1} days is longer than "
                                   f"[api] performance_max_range_days ({limit})")
                document = history.performance(
                    first, last, self.server.settings.performance_decimal_places)
            else:
                _query(query, frozenset())
                document = history.day(_day(path[len(DAY_PREFIX):]), today)
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        except HistoryUnavailable as exc:
            self._error("unavailable", str(exc))
            return
        self._json(200, document)

    def _account(self) -> None:
        """GET /account: the bot's latest broker read of positions and open orders."""
        try:
            _query(urlsplit(self.path).query, frozenset())
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        result = self.server.account_response()
        if isinstance(result, ErrorV1):
            self._error(result.error, result.cause)
        else:
            self._json(200, result)

    def _chart(self) -> None:
        """GET /chart?symbol=&interval=&range=: candles from market data (api/chart.py)."""
        try:
            params = _query(urlsplit(self.path).query, frozenset({"symbol", "interval", "range"}))
            symbol, interval, span = parse_query(params)
            document = self.server.chart.answer(symbol, interval, span, self.server.clock())
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        except ChartRefused as exc:
            extra = {}
            if exc.retry_after is not None and math.isfinite(exc.retry_after):
                extra["Retry-After"] = str(math.ceil(exc.retry_after))
            self._error(exc.error, exc.cause, extra)
            return
        self._json(200, document)

    def _explain(self) -> None:
        """GET /explain?symbol=: the latest active-strategy decision recorded today."""
        try:
            symbol = _query(urlsplit(self.path).query, frozenset({"symbol"}))["symbol"]
            if not SYMBOL_PATTERN.match(symbol):
                raise BadQuery(f"symbol {symbol!r} is not a ticker")
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        day = ny_date_of(self.server.clock())
        try:
            record = DecisionLog(self.server.settings.decisions).latest_active(day, symbol)
        except (RecordFileError, OSError) as exc:
            self._error("unavailable", f"decision file unreadable: {exc}")
            return
        if record is None:
            self._error("not_found", f"no decision recorded for {symbol} on {day.isoformat()}")
            return
        self._json(200, contract.explain(self.server.settings.history.environment, day, record,
                                            self.server.settings.check_places))

    def _recorded_decisions(self, path: str) -> None:
        """GET /observers?date=, /skipped?date=, /watching?date=, /flow?date= and
        /decisions?symbol=&date=: decisions already recorded in the decision file of one New
        York date (plain or archived). Never reads the broker; /observers adds each observer's
        simulated day result, /flow the captured screeners and the database."""
        settings = self.server.settings
        query = urlsplit(self.path).query
        today = ny_date_of(self.server.clock())
        try:
            if path != "/decisions":
                params = _query(query, frozenset({"date"}), optional=frozenset({"date"}))
            else:
                params = _query(query, frozenset({"symbol", "date"}))
                if not SYMBOL_PATTERN.match(params["symbol"]):
                    raise BadQuery(f"symbol {params['symbol']!r} is not a ticker")
            day = _day(params["date"]) if "date" in params else today
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        log = DecisionLog(settings.decisions)
        try:
            if path == "/observers":
                identities = tuple(read_identity(settings.strategies_root, folder)
                                   for folder in settings.shown.observers)
                records = log.records(day, role=Role.OBSERVER)
                configured = {name for name, _ in identities}
                results = self.server.observer_results.results(
                    day, today, [r for r in records if r.decision.strategy in configured],
                    log.path(day))
                document: Any = contract.observers_document(day, identities, records, results)
            elif path == "/skipped":
                document = self.server.skipped.document(
                    day, today, log.records(day, role=Role.ACTIVE), log.path(day))
            elif path == "/watching":
                document = contract.watching_document(
                    day, log.records(day, role=Role.ACTIVE), settings.watching_max_rows,
                    settings.closeness_places, settings.check_places)
            elif path == "/flow":
                document = self._flow(day, today, log.records(day, role=Role.ACTIVE))
            else:
                symbol = params["symbol"]
                document = contract.decisions_document(symbol, day,
                                                       log.records(day, symbol=symbol),
                                                       settings.check_places)
        except ObserverError as exc:
            self._error("unavailable", f"observer strategy unreadable: {exc}")
            return
        except CaptureError as exc:
            self._error("unavailable", f"capture file unreadable: {exc}")
            return
        except HistoryUnavailable as exc:
            self._error("unavailable", str(exc))
            return
        except (RecordFileError, OSError) as exc:
            self._error("unavailable", f"decision file unreadable: {exc}")
            return
        self._json(200, document)

    def _flow(self, day: date, today: date,
              records: tuple[DecisionRecord, ...]) -> contract.FlowV1:
        settings = self.server.settings
        path = flow.screener_path(settings.history.capture, day)
        try:
            passes = [] if path is None else flow.read_passes(path, open_tail=day == today)
        except OSError as exc:
            raise CaptureError(str(exc)) from exc
        return flow.flow_document(
            settings.history.environment, day,
            flow.chips(passes, settings.universe, settings.change_pct_places), records,
            self.server.history.entries(day, today),
            flow.reconcile(day, today, settings.status_path, settings.account_path))

    def _read_json_body(self) -> dict[str, Any] | None:
        media = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media != JSON_TYPE:
            self._error("unsupported_media_type", f"Content-Type must be {JSON_TYPE}")
            return None
        length_header = self.headers.get("Content-Length")
        if length_header is None or not length_header.isdigit():
            self._error("length_required", "Content-Length is required")
            return None
        length = int(length_header)
        if length > self.server.settings.max_request_body_bytes:
            self._error("payload_too_large",
                        f"body is limited to {self.server.settings.max_request_body_bytes} bytes")
            return None
        try:
            body = contract.parse_json(self.rfile.read(length))
        except ContractError as exc:
            self._error("bad_request", str(exc))
            return None
        if not isinstance(body, dict):
            self._error("bad_request", "body must be a JSON object")
            return None
        return body

    def _not_allowed(self) -> None:
        if self._guard():
            self._error("method_not_allowed", "only GET and POST are supported",
                        {"Allow": "GET, POST"})

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815 - stdlib naming


def make_server(settings: ApiSettings, clock: Callable[[], datetime] = _now) -> ApiServer:
    return ApiServer(settings, clock)


def make_remote_server(local: ApiServer) -> ApiServer:
    """The phone-facing listener next to `local`, sharing its settings, clock and inbox lock."""
    return ApiServer(local.settings, local.clock, remote=True, inbox_lock=local.inbox_lock)


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by the owner only (0600), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # a leftover from a crash could carry wider permissions
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def pairing_link(address: str, token: str) -> str:
    return "aperture://pair?" + urlencode({"url": address, "token": token})


def pair(settings: ApiSettings) -> Path:
    """Make a new bearer token for the phone, store only its SHA-256 (replacing any earlier
    pairing, so the old phone token stops working) and leave the pairing link in a 0600 file for
    the owner to move to the phone. The token is never printed or logged; the API deletes the
    link file when the phone first uses the token. Returns the link file's path.
    """
    if not settings.remote_hosts:
        raise ConfigError("[api] remote_hosts is empty: set the Tailscale name of this Mac first")
    token = secrets.token_urlsafe(settings.token_bytes)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    _write_private(settings.token_hash_path, digest + "\n")
    address = f"https://{settings.remote_hosts[0]}"
    _write_private(settings.pairing_path, pairing_link(address, token) + "\n")
    return settings.pairing_path
