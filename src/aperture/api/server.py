"""Local HTTP API: GET /status and the status page, POST operator commands.

stdlib only. Binds a loopback address and refuses any request whose Host (or Origin, when sent)
is not in the configured allowlist, which blocks DNS rebinding and cross-site requests from a
browser. Commands additionally require `Content-Type: application/json`, which a cross-site form
cannot send without a CORS preflight, and this server answers no preflight.

The API never talks to the broker and never changes bot state itself: it reads the bot's status
file and drops command files into the inbox. The bot decides, acts and journals.
"""

from __future__ import annotations

import ipaddress
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from aperture.api import contract
from aperture.api.contract import (
    CommandAcceptedV1,
    CommandKind,
    ContractError,
    ErrorV1,
    PendingCommandsV1,
    StatusResponseV1,
)
from aperture.api.history import SYMBOL_PATTERN, History, HistoryPaths, HistoryUnavailable
from aperture.calendar import ny_date_of
from aperture.config import Config, ConfigError

STATIC_DIR = Path(__file__).resolve().parent / "static"
COMMAND_SOURCE = "api"
JSON_TYPE = "application/json"
ENTRY_STOP_STATES = {"on": CommandKind.ENTRY_STOP_ON, "off": CommandKind.ENTRY_STOP_OFF}
COMMAND_ROUTES = {
    "/commands/entry-stop": None,  # kind comes from the body
    "/commands/cancel-own-orders": CommandKind.CANCEL_OWN_ORDERS,
    "/commands/flatten": CommandKind.FLATTEN,
}


@dataclass(frozen=True)
class ApiSettings:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    status_path: Path
    inbox: Path
    status_stale_seconds: Decimal
    poll_seconds: Decimal
    max_request_body_bytes: int
    max_pending_commands: int
    history: HistoryPaths


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
    data = cfg.root / cfg.run.data_dir
    return ApiSettings(
        host=api.host,
        port=api.port,
        allowed_hosts=api.allowed_hosts,
        status_path=data / api.status_file,
        inbox=data / api.commands_dir / contract.INBOX,
        status_stale_seconds=api.status_stale_seconds,
        poll_seconds=api.poll_seconds,
        max_request_body_bytes=api.max_request_body_bytes,
        max_pending_commands=api.max_pending_commands,
        history=HistoryPaths.from_config(cfg),
    )


def _now() -> datetime:
    return datetime.now(UTC)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings: ApiSettings, clock: Callable[[], datetime] = _now) -> None:
        self.settings = settings
        self.clock = clock
        self.inbox_lock = threading.Lock()
        self.page = (STATIC_DIR / "status.html").read_bytes()
        self.history = History(settings.history)
        super().__init__((settings.host, settings.port), Handler)

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
    "internal": 500, "unavailable": 503,
}
GET_ROUTES = ("/", "/status", "/commands/pending", "/schema", "/days", "/bars")
DAY_PREFIX = "/days/"


class BadQuery(ValueError):
    """The query string is not exactly what the route takes; the message is the cause."""


def _query(raw: str, names: frozenset[str]) -> dict[str, str]:
    """Exactly the parameters `names`, each once and non-empty; anything else is refused."""
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
    missing = sorted(names - set(found))
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
        if host not in self.server.allowed_host_headers:
            self._error("forbidden_host", f"Host {host!r} is not allowed")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            self._error("forbidden_origin", f"Origin {origin!r} is not allowed")
            return False
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
