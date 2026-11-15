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

The API never talks to the broker and never changes bot state itself: it reads the bot's status
file and drops command files into the inbox. The bot decides, acts and journals.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
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

from aperture.api import contract
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
from aperture.calendar import ny_date_of
from aperture.config import Config, ConfigError
from aperture.observers.errors import ObserverError
from aperture.observers.loader import read_identity
from aperture.observers.log import DecisionLog
from aperture.research.errors import RecordFileError
from aperture.strategy.base import Role

STATIC_DIR = Path(__file__).resolve().parent / "static"
COMMAND_SOURCE = "api"
JSON_TYPE = "application/json"
ENTRY_STOP_STATES = {"on": CommandKind.ENTRY_STOP_ON, "off": CommandKind.ENTRY_STOP_OFF}
COMMAND_ROUTES = {
    "/commands/entry-stop": None,  # kind comes from the body
    "/commands/cancel-own-orders": CommandKind.CANCEL_OWN_ORDERS,
    "/commands/flatten": CommandKind.FLATTEN,
}


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
        self.history = History(settings.history)
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

    def enqueue(self, kind: CommandKind) -> CommandAcceptedV1 | ErrorV1:
        with self.inbox_lock:
            if contract.count_inbox(self.settings.inbox) >= self.settings.max_pending_commands:
                return ErrorV1("too_many_pending",
                               f"{self.settings.max_pending_commands} commands are already "
                               "waiting for the bot")
            command = contract.new_command(kind, self.clock(), COMMAND_SOURCE)
            contract.write_command(self.settings.inbox, command)
        state = self.status_response()
        warning = None
        if state.status is None or state.stale:
            warning = ("the bot does not look alive (" + state.summary + "); the command runs "
                       "only if the bot reads it before it expires")
        return CommandAcceptedV1(command=command, warning=warning)


_ERROR_CODES = {
    "forbidden_host": 403, "forbidden_origin": 403, "not_found": 404,
    "method_not_allowed": 405, "length_required": 411, "payload_too_large": 413,
    "unsupported_media_type": 415, "bad_request": 400, "too_many_pending": 429,
    "internal": 500, "unavailable": 503, "unauthorized": 401,
}
GET_ROUTES = ("/", "/status", "/commands/pending", "/schema", "/days", "/bars", "/settings",
              "/explain", "/observers", "/decisions", "/account")
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
            elif path in ("/observers", "/decisions"):
                self._recorded_decisions(path)
            elif path == "/account":
                self._account()
            elif path in COMMAND_ROUTES:
                self._error("method_not_allowed", "use POST", {"Allow": "POST"})
            elif path == "/days" or path.startswith(DAY_PREFIX) or path == "/bars":
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
            body = self._read_json_body()
            if body is None:
                return
            kind = COMMAND_ROUTES[path]
            if kind is None:
                if set(body) != {"state"} or body["state"] not in ENTRY_STOP_STATES:
                    self._error("bad_request", 'body must be exactly {"state": "on"} or '
                                '{"state": "off"}')
                    return
                kind = ENTRY_STOP_STATES[body["state"]]
            elif body:
                self._error("bad_request", "body must be an empty JSON object {}")
                return
            result = self.server.enqueue(kind)
            if isinstance(result, ErrorV1):
                self._error(result.error, result.cause)
            else:
                self._json(202, result)
        except Exception as exc:  # noqa: BLE001 - one typed answer instead of a dropped socket
            self._error("internal", type(exc).__name__)

    def _history(self, path: str) -> None:
        """GET /days, /days/<YYYY-MM-DD>, /bars?symbol=&date=: read-only history."""
        history = self.server.history
        today = ny_date_of(self.server.clock())
        query = urlsplit(self.path).query
        try:
            if path == "/days":
                _query(query, frozenset())
                document: Any = history.days()
            elif path == "/bars":
                params = _query(query, frozenset({"symbol", "date"}))
                if not SYMBOL_PATTERN.match(params["symbol"]):
                    raise BadQuery(f"symbol {params['symbol']!r} is not a ticker")
                document = history.bars(params["symbol"], _day(params["date"]), today)
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
        self._json(200, contract.explain(self.server.settings.history.environment, day, record))

    def _recorded_decisions(self, path: str) -> None:
        """GET /observers?date= and /decisions?symbol=&date=: decisions already recorded in the
        decision file of one New York date (plain or archived). Never reads the broker."""
        settings = self.server.settings
        query = urlsplit(self.path).query
        try:
            if path == "/observers":
                params = _query(query, frozenset({"date"}), optional=frozenset({"date"}))
            else:
                params = _query(query, frozenset({"symbol", "date"}))
                if not SYMBOL_PATTERN.match(params["symbol"]):
                    raise BadQuery(f"symbol {params['symbol']!r} is not a ticker")
            day = _day(params["date"]) if "date" in params else ny_date_of(self.server.clock())
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        log = DecisionLog(settings.decisions)
        try:
            if path == "/observers":
                identities = tuple(read_identity(settings.strategies_root, folder)
                                   for folder in settings.shown.observers)
                document: Any = contract.observers_document(
                    day, identities, log.records(day, role=Role.OBSERVER))
            else:
                symbol = params["symbol"]
                document = contract.decisions_document(symbol, day,
                                                       log.records(day, symbol=symbol))
        except ObserverError as exc:
            self._error("unavailable", f"observer strategy unreadable: {exc}")
            return
        except (RecordFileError, OSError) as exc:
            self._error("unavailable", f"decision file unreadable: {exc}")
            return
        self._json(200, document)

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
