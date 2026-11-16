"""Local HTTP API: GET /status and the status page, POST operator commands.

stdlib only. Binds a loopback address and refuses any request whose Host (or Origin, when sent)
is not in the configured allowlist, which blocks DNS rebinding and cross-site requests from a
browser. Remote access (the phone, through an HTTPS proxy on this Mac such as `tailscale serve`)
has its own loopback listener, `[api] remote_port`: every request there must carry the paired
bearer token whatever its Host, and its Host must be one of the names in
`[api] remote_hosts_file`. Whether a token is needed is decided by the listener a request
arrives on, never by a header the caller chooses.
The one exception is POST /pair there: the phone redeems the one-time pairing code `pair` left
in the pairing link, once and before it expires, for a new token. The server keeps only SHA-256
digests of the code and the token.
Commands additionally require `Content-Type: application/json`, which a cross-site form
cannot send without a CORS preflight, and this server answers no preflight.

The API never talks to the broker's trading or account routes and never changes bot state
itself: it reads the bot's status file and drops command files into the inbox. The bot decides,
acts and journals. The one market-data read, GET /chart, goes through its own source and request
budget (api/chart.py), off unless `[chart] webull_enabled`.
"""

from __future__ import annotations

import contextlib
import fcntl
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from tickr.api import contract, flow, symbols, trade_whatif
from tickr.api.advisor import DecisionRefused, advisor_document, record_decision
from tickr.api.after import AfterSettings, SkippedAfter
from tickr.api.botstart import StartRefused, StartSettings, run_command, start_bot
from tickr.api.chart import Chart, ChartRefused, ChartSettings, parse_query
from tickr.api.configview import config_document
from tickr.api.contract import (
    AccountResponseV1,
    BotStartV1,
    CommandAcceptedV1,
    CommandKind,
    ContractError,
    ErrorV1,
    PairedV1,
    PendingCommandsV1,
    RiskSettings,
    SessionSettings,
    SettingsV1,
    StatusResponseV1,
)
from tickr.api.decision_cache import DecisionCache
from tickr.api.history import SYMBOL_PATTERN, History, HistoryPaths, HistoryUnavailable
from tickr.api.jobs import JobsSettings, jobs_document
from tickr.api.journal import journal_document
from tickr.api.lab_replay import LabReplay, LabReplayRefused, LabReplaySettings
from tickr.api.observer_results import ObserverResults, SimulationPaths
from tickr.calendar import ny_date_of
from tickr.config import Config, ConfigError
from tickr.data.capture import CapturedBarsDays, CaptureError
from tickr.data.discovery import DiscoverySettings
from tickr.data.history import SYMBOL_PATTERN as TICKER_PATTERN
from tickr.observers.errors import ObserverError
from tickr.observers.loader import read_identity, read_values
from tickr.observers.log import DecisionLog
from tickr.research.errors import RecordFileError
from tickr.research.records import DecisionRecord
from tickr.strategy.base import Role

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
PRIVATE_FILE_MODE = 0o600  # owner read and write only (POSIX mode bits)
LOCK_SUFFIX = ".lock"  # the pairing lock file: next to the code file, never holds data
PAIR_ROUTE = "/pair"  # POST on the remote port only: redeem the one-time pairing code
# Fewest random bytes in a token or pairing code: 128 bits, the usual floor for a bearer secret.
MIN_SECRET_BYTES = 16


@dataclass(frozen=True)
class ApiSettings:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    remote_hosts: tuple[str, ...]  # read from remote_hosts_path at start; empty = remote off
    remote_hosts_path: Path
    remote_port: int
    token_hash_path: Path
    token_bytes: int
    pairing_path: Path
    pairing_code_path: Path  # SHA-256 of the waiting one-time code and its expiry
    pairing_code_ttl_seconds: int
    status_path: Path
    inbox: Path
    idempotency: Path  # <commands_dir>/idempotency: remembered Idempotency-Key answers
    idempotency_ttl_seconds: int
    status_stale_seconds: Decimal
    poll_seconds: Decimal
    max_request_body_bytes: int
    request_timeout_seconds: Decimal  # per socket read or write, both listeners
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
    journal_rows: int  # GET /journal: latest journal rows served
    advisor_dir: Path  # [advisor] proposals_dir: one folder per AI advisor run, GET /advisor
    advisor_runs: int  # GET /advisor: newest runs served
    closeness_places: int  # decimal places of GET /watching closeness
    check_places: int  # decimal places of check values in every document that shows checks
    change_pct_places: int  # decimal places of GET /flow change_pct
    performance_max_range_days: int  # longest GET /performance range, both ends counted
    performance_decimal_places: int  # rounding of GET /performance quotients
    chart: ChartSettings  # GET /chart: source switch, cache and calendar
    after: AfterSettings  # prices after, overshoot (day-v1) and what-if (skipped-v1)
    jobs: JobsSettings  # GET /jobs: schedule, database and calendar
    lab: LabReplaySettings  # GET and POST /lab/replay: the Lab's simulated replay
    start: StartSettings  # POST /commands/start-bot: launchd's start of the bot job
    trade_whatif: trade_whatif.TradeWhatIfSettings  # GET /trades/whatif
    config_file: Path | None  # the bot.toml this API loaded, shown at GET /config
    config_root: Path  # GET /config shows the file's path relative to this
    config_digest: str  # sha256 of the file when this API loaded it


def read_remote_hosts(path: Path) -> tuple[str, ...]:
    """The Host names the remote port answers to: the non-blank lines of `path`, in order. A
    missing file means remote access is off. The names are this machine's, so they live in its
    data folder, never in the shared bot.toml."""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"remote_hosts_file {path}: unreadable ({type(exc).__name__})") from exc
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def settings_from_config(cfg: Config, config_path: Path | None = None) -> ApiSettings:
    """`config_path` is the file `cfg` was loaded from; without it POST /lab/replay refuses."""
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
    remote_hosts_path = data / api.remote_hosts_file
    remote_hosts = read_remote_hosts(remote_hosts_path)
    for name in remote_hosts:
        if not REMOTE_HOST.match(name):
            raise ConfigError(f"remote_hosts_file {remote_hosts_path}: {name!r} is not a "
                              "lowercase DNS name (optionally with :port)")
        if name.split(":", 1)[0] in api.allowed_hosts:
            raise ConfigError(f"remote_hosts_file {remote_hosts_path}: {name!r} is also a "
                              "local allowed host")
    if api.remote_port == api.port:
        raise ConfigError("[api] remote_port must differ from port")
    if api.request_timeout_seconds <= 0:
        raise ConfigError("[api] request_timeout_seconds must be positive")
    if not 0 <= api.demo_port <= MAX_PORT or api.demo_port in (api.port, api.remote_port):
        raise ConfigError(f"[api] demo_port must be 0 (none) or a port 1-{MAX_PORT} other than "
                          "port and remote_port")
    if api.remote_token_bytes < MIN_SECRET_BYTES:
        raise ConfigError(f"[api] remote_token_bytes must be at least {MIN_SECRET_BYTES}")
    if api.remote_pairing_code_ttl_seconds <= 0:
        raise ConfigError("[api] remote_pairing_code_ttl_seconds must be positive")
    if api.idempotency_key_ttl_seconds <= api.command_max_age_seconds:
        raise ConfigError("[api] idempotency_key_ttl_seconds must be above "
                          "command_max_age_seconds, so a repeat can never queue a command twice")
    if api.observer_trades_max_rows <= 0:
        raise ConfigError("[api] observer_trades_max_rows must be positive")
    if api.journal_rows <= 0:
        raise ConfigError("[api] journal_rows must be positive")
    if api.start_bot_timeout_seconds <= 0:
        raise ConfigError("[api] start_bot_timeout_seconds must be positive")
    if api.advisor_runs <= 0:
        raise ConfigError("[api] advisor_runs must be positive")
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
    if api.lab_replay_sessions <= 0:
        raise ConfigError("[api] lab_replay_sessions must be positive")
    if not 0 <= api.lab_replay_niceness <= MAX_NICENESS:
        raise ConfigError(f"[api] lab_replay_niceness must be 0-{MAX_NICENESS}")
    for key in ("lab_replay_data_dir", "lab_replay_results_dir"):
        parts = PurePosixPath(getattr(api, key)).parts
        if not parts or parts[0] == "/" or any(p in (".", "..") for p in parts) \
                or "\\" in getattr(api, key):
            raise ConfigError(f"[api] {key} must be a plain subfolder of data_dir")
    if PurePosixPath(api.lab_replay_data_dir) == PurePosixPath(api.lab_replay_results_dir):
        raise ConfigError("[api] lab_replay_data_dir and lab_replay_results_dir must be "
                          "two different folders")
    return ApiSettings(
        host=api.host,
        port=api.port,
        allowed_hosts=api.allowed_hosts,
        remote_hosts=remote_hosts,
        remote_hosts_path=remote_hosts_path,
        remote_port=api.remote_port,
        token_hash_path=data / api.remote_token_hash_file,
        token_bytes=api.remote_token_bytes,
        pairing_path=data / api.remote_pairing_file,
        pairing_code_path=data / api.remote_pairing_code_file,
        pairing_code_ttl_seconds=api.remote_pairing_code_ttl_seconds,
        status_path=data / api.status_file,
        inbox=data / api.commands_dir / contract.INBOX,
        idempotency=data / api.commands_dir / IDEMPOTENCY,
        idempotency_ttl_seconds=api.idempotency_key_ttl_seconds,
        status_stale_seconds=api.status_stale_seconds,
        poll_seconds=api.poll_seconds,
        max_request_body_bytes=api.max_request_body_bytes,
        request_timeout_seconds=api.request_timeout_seconds,
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
        journal_rows=api.journal_rows,
        advisor_dir=data / cfg.advisor.proposals_dir,
        advisor_runs=api.advisor_runs,
        closeness_places=api.watching_closeness_decimal_places,
        check_places=api.check_decimal_places,
        change_pct_places=api.flow_change_pct_decimal_places,
        performance_max_range_days=api.performance_max_range_days,
        performance_decimal_places=api.performance_decimal_places,
        chart=ChartSettings.from_config(cfg),
        after=AfterSettings(minutes, api.after_r_decimal_places,
                            (cfg.root / cfg.session.calendar_file).parent),
        jobs=JobsSettings.from_config(cfg),
        lab=LabReplaySettings.from_config(cfg, config_path),
        start=StartSettings.from_config(cfg),
        trade_whatif=trade_whatif.TradeWhatIfSettings.from_config(cfg),
        config_file=config_path,
        config_root=cfg.root,
        config_digest=cfg.digest,
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
                 remote: bool = False, inbox_lock: threading.Lock | None = None,
                 start_lock: threading.Lock | None = None,
                 lab: LabReplay | None = None) -> None:
        self.settings = settings
        self.clock = clock
        self.remote = remote
        # Shared with the other listener so both count the inbox under one lock.
        self.inbox_lock = inbox_lock or threading.Lock()
        # One start at a time across both listeners; its own lock, so a hanging start command
        # never holds back a queued command (emergency, flatten).
        self.start_lock = start_lock or threading.Lock()
        # One redemption at a time, so a pairing code can never yield two tokens.
        self.pair_lock = threading.Lock()
        # Shared too, so one replay at a time whichever listener started it.
        self.lab = lab or LabReplay(settings.lab, clock)
        self.page = (STATIC_DIR / "status.html").read_bytes()
        # Captured bars by symbol, read on as the bot appends; shared by every reader below.
        self.captured_bars = CapturedBarsDays()
        self.history = History(settings.history, settings.after, self.captured_bars)
        # Today's decision records, parsed once and read on as the bot appends.
        self.decisions = DecisionCache(DecisionLog(settings.decisions))
        self.observer_results = ObserverResults(
            settings.simulation, settings.observer_trades_max_rows, self.captured_bars)
        self.screener_chips = flow.ScreenerChips(settings.universe, settings.change_pct_places)
        self.discovery_day = symbols.DiscoveryDay()  # GET /symbols and /movers
        self.skipped = SkippedAfter(settings.simulation, settings.after.minutes,
                                    settings.check_places, self.captured_bars)
        # No source until one is attached (python -m tickr.api, or the demo's world).
        self.chart = Chart(settings.chart)
        self.start_runner = run_command  # runs [api] start_bot_command; replaced in tests
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

    def start_bot(self) -> BotStartV1 | ErrorV1:
        """POST /commands/start-bot (api/botstart.py), one at a time across both listeners."""
        with self.start_lock:
            try:
                return start_bot(self.settings.start, self.status_response(), self.clock(),
                                 self.start_runner)
            except StartRefused as exc:
                return ErrorV1(exc.error, exc.cause)

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
              "/watching", "/performance", "/chart", "/jobs", "/journal", "/config",
              "/trades/whatif", "/advisor", "/symbols", "/movers")
MAX_NICENESS = 19  # POSIX nice: 19 is the lowest priority (protocol constant)
MAX_PORT = 65535  # TCP ports are 16-bit (protocol constant)
HTML_TYPE = "text/html; charset=utf-8"
# Every HTML page this API serves carries these headers.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
LAB_REPLAY = "/lab/replay"  # GET reads, POST starts: outside the command inbox
START_BOT = "/commands/start-bot"  # POST only; runs launchd's start, outside the command inbox
TRADE_WHATIF = "/trades/whatif"  # GET: another version on one trade's day, simulated
# A strategy folder name: one path segment, never "." or "..".
FOLDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ADVISOR_DECISION = "/advisor/decision"  # POST only: the owner's note on a run, never a command
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
    server_version = "tickr-api"
    sys_version = ""

    # ------------------------------------------------------------ plumbing

    def setup(self) -> None:
        # Before the stdlib applies it to the socket: a client that stops sending (no request
        # line, headers or body) is dropped after this long instead of holding a thread forever.
        self.timeout = float(self.server.settings.request_timeout_seconds)
        super().setup()

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
            if self.command == "POST" and self._path() == PAIR_ROUTE:
                return True  # the pairing code in the body is the credential: see _redeem
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
            self._error("unauthorized", "no phone is paired; run `python -m tickr.api pair`",
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
                self._send(200, self.server.page, HTML_TYPE, dict(PAGE_HEADERS))
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
            elif path == "/config":
                self._config()
            elif path == "/jobs":
                self._json(200, jobs_document(self.server.settings.jobs,
                                              self.server.status_response()))
            elif path == "/journal":
                self._json(200, journal_document(self.server.settings.jobs.database,
                                                 self.server.settings.journal_rows))
            elif path == "/advisor":
                self._json(200, advisor_document(self.server.settings.advisor_dir,
                                                 self.server.settings.advisor_runs))
            elif path == ADVISOR_DECISION:
                self._error("method_not_allowed", "use POST", {"Allow": "POST"})
            elif path == LAB_REPLAY:
                self._json(200, self.server.lab.state())
            elif path == TRADE_WHATIF:
                self._trade_whatif()
            elif path == "/explain":
                self._explain()
            elif path in ("/observers", "/decisions", "/skipped", "/flow", "/watching",
                          "/symbols", "/movers"):
                self._recorded_decisions(path)
            elif path == "/account":
                self._account()
            elif path == "/chart":
                self._chart()
            elif path in COMMAND_ROUTES or path == START_BOT:
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
            if path == LAB_REPLAY:
                self._start_lab_replay()
                return
            if path == START_BOT:
                self._start_bot()
                return
            if path == ADVISOR_DECISION:
                self._advisor_decision()
                return
            if path == PAIR_ROUTE and self.server.remote:
                self._redeem()
                return
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

    def _start_bot(self) -> None:
        """POST /commands/start-bot with {}: 202 bot-start-v1, or the error with one cause. An
        Idempotency-Key is checked for form only: a repeat is harmless (a running job is never
        restarted, and a running bot is refused)."""
        key = self.headers.get(IDEMPOTENCY_HEADER)
        if key is not None and not IDEMPOTENCY_KEY.match(key):
            self._error("bad_request", f"{IDEMPOTENCY_HEADER} must be 1-128 characters "
                        "from A-Z a-z 0-9 . _ : -")
            return
        body = self._read_json_body()
        if body is None:
            return
        if body:
            self._error("bad_request", "body must be an empty JSON object {}")
            return
        result = self.server.start_bot()
        if isinstance(result, ErrorV1):
            self._error(result.error, result.cause)
        else:
            self._json(202, result)

    def _advisor_decision(self) -> None:
        """POST /advisor/decision with {"run": "<id>", "verdict": "accepted"|"rejected"}:
        record the owner's verdict (a note, nothing is deployed), 200 with advisor-runs-v1;
        404 without such a run, 409 when the run takes no decision."""
        body = self._read_json_body()
        if body is None:
            return
        verdicts = {v.value: v for v in contract.OwnerVerdict}
        if (set(body) != {"run", "verdict"} or not isinstance(body["run"], str)
                or not isinstance(body["verdict"], str) or body["verdict"] not in verdicts):
            self._error("bad_request", 'body must be {"run": "<run id>", "verdict": '
                        '"accepted"} or "rejected"')
            return
        settings = self.server.settings
        try:
            record_decision(settings.advisor_dir, body["run"], verdicts[body["verdict"]],
                            self.server.clock())
        except DecisionRefused as exc:
            self._error("not_found" if exc.missing else "conflict", str(exc))
            return
        self._json(200, advisor_document(settings.advisor_dir, settings.advisor_runs))

    def _redeem(self) -> None:
        """POST /pair with {"code": "<code>"} on the remote port: the one-time pairing code `pair`
        left in the pairing link buys one new bearer token, 200 paired-v1. The code is burnt on
        success and when found expired; a wrong code changes nothing. Neither the code nor the
        token is ever logged or echoed in an error."""
        body = self._read_json_body()
        if body is None:
            return
        if set(body) != {"code"} or not isinstance(body["code"], str) or not body["code"]:
            self._error("bad_request", 'body must be exactly {"code": "<pairing code>"}')
            return
        settings = self.server.settings
        with self.server.pair_lock, _pairing_lock(settings):
            try:
                waiting = json.loads(settings.pairing_code_path.read_text("ascii"))
                digest = waiting["sha256"]
                expires_at = contract.parse_time(waiting["expires_at"])
                if not isinstance(digest, str):
                    raise TypeError("sha256")
            except FileNotFoundError:
                self._error("unauthorized", "no pairing is waiting; run `python -m tickr.api "
                            "pair` on the Mac")
                return
            except (OSError, ValueError, KeyError, TypeError, ContractError) as exc:
                self._error("unavailable",
                            f"the pairing code file is unreadable ({type(exc).__name__})")
                return
            if self.server.clock() >= expires_at:
                settings.pairing_code_path.unlink(missing_ok=True)
                settings.pairing_path.unlink(missing_ok=True)
                self._error("unauthorized", "the pairing code has expired; run `python -m "
                            "tickr.api pair` on the Mac again")
                return
            presented = hashlib.sha256(body["code"].encode("utf-8")).hexdigest()
            if not hmac.compare_digest(presented, digest):
                self._error("unauthorized", "the pairing code is not the waiting one")
                return
            token = secrets.token_urlsafe(settings.token_bytes)
            _write_private(settings.token_hash_path,
                           hashlib.sha256(token.encode("utf-8")).hexdigest() + "\n")
            settings.pairing_code_path.unlink(missing_ok=True)
            settings.pairing_path.unlink(missing_ok=True)
        self._json(200, PairedV1(token=token))

    def _start_lab_replay(self) -> None:
        """POST /lab/replay with {} or {"version", "compare", "day"}: start the simulated
        replay, 202 with its state, or 409 with the single cause it cannot start now."""
        body = self._read_json_body()
        if body is None:
            return
        shape = ('body must be {} or hold only "version" and "compare" (folders under '
                 'strategies/) and "day" (YYYY-MM-DD), each a non-empty string')
        if not set(body) <= {"version", "compare", "day"} or not all(
                isinstance(v, str) and v for v in body.values()):
            self._error("bad_request", shape)
            return
        try:
            day = None if "day" not in body else _day(body["day"])
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        try:
            self._json(202, self.server.lab.start(body.get("version"), body.get("compare"), day))
        except LabReplayRefused as exc:
            self._error("conflict", str(exc))

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

    def _config(self) -> None:
        """GET /config: the configuration file now, or 503 with why it cannot be shown."""
        settings = self.server.settings
        if settings.config_file is None:
            self._error("unavailable", "this API was started without a config file path")
            return
        try:
            document = config_document(settings.config_file, settings.config_root,
                                       settings.config_digest)
        except OSError as exc:
            self._error("unavailable", f"config file unreadable ({type(exc).__name__})")
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

    def _trade_whatif(self) -> None:
        """GET /trades/whatif?date=&symbol=&version=: another strategy version on one trade's
        symbol and day, simulated; 409 with the cause during the session."""
        try:
            params = _query(urlsplit(self.path).query,
                            frozenset({"date", "symbol", "version"}))
            if not TICKER_PATTERN.match(params["symbol"]):
                raise BadQuery(f"symbol {params['symbol']!r} is not a ticker")
            if not FOLDER_PATTERN.match(params["version"]):
                raise BadQuery(f"version {params['version']!r} is not a strategy folder name")
            day = _day(params["date"])
        except BadQuery as exc:
            self._error("bad_request", str(exc))
            return
        try:
            document = trade_whatif.what_if(self.server.settings.trade_whatif,
                                            params["version"], params["symbol"], day,
                                            self.server.clock(), self.server.captured_bars)
        except trade_whatif.TradeWhatIfRefused as exc:
            self._error("conflict", str(exc))
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
            found = [r for r in self.server.decisions.records(day, day, symbol=symbol)
                     if r.role is Role.ACTIVE]  # as DecisionLog.latest_active filters
            record = found[-1] if found else None
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
        cache = self.server.decisions
        log = cache.log
        try:
            if path == "/observers":
                identities = tuple(read_identity(settings.strategies_root, folder)
                                   for folder in settings.shown.observers)
                records = cache.records(day, today, role=Role.OBSERVER)
                configured = {name for name, _ in identities}
                results = self.server.observer_results.results(
                    day, today, [r for r in records if r.decision.strategy in configured],
                    log.path(day))
                document: Any = contract.observers_document(day, identities, records, results,
                                                            self._observer_differences())
            elif path == "/skipped":
                document = self.server.skipped.document(
                    day, today, cache.records(day, today, role=Role.ACTIVE), log.path(day))
            elif path == "/watching":
                document = contract.watching_document(
                    day, cache.records(day, today, role=Role.ACTIVE), settings.closeness_places,
                    settings.check_places)
            elif path == "/flow":
                document = self._flow(day, today, cache.records(day, today, role=Role.ACTIVE))
            elif path == "/symbols":
                document = self._symbols(day, today, cache.records(day, today, role=Role.ACTIVE))
            elif path == "/movers":
                document = self._movers(day, today, cache.records(day, today, role=Role.ACTIVE))
            else:
                symbol = params["symbol"]
                document = contract.decisions_document(symbol, day,
                                                       cache.records(day, today, symbol=symbol),
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
        return flow.flow_document(
            settings.history.environment, day,
            self.server.screener_chips.chips(path, open_tail=day == today), records,
            self.server.history.entries(day, today),
            flow.reconcile(day, today, settings.status_path, settings.account_path))

    def _discovery(self, day: date, today: date) -> symbols.DiscoveryFolded:
        return self.server.discovery_day.read(
            symbols.discovery_path(self.server.settings.history.capture, day), day == today)

    def _symbols(self, day: date, today: date,
                 records: tuple[DecisionRecord, ...]) -> contract.SymbolsV1:
        settings = self.server.settings
        chips = self.server.screener_chips.chips(
            flow.screener_path(settings.history.capture, day), open_tail=day == today)
        return symbols.symbols_document(day, self._discovery(day, today), chips, records,
                                        self.server.history.entries(day, today),
                                        settings.check_places)

    def _observer_differences(self) -> dict[tuple[str, str], tuple[contract.SettingDifferenceV1,
                                                                    ...]]:
        """Display only: each configured observer folder of the active strategy's name, by the
        (name, version) its file declares now, with the settings where its file differs from
        the active strategy's file. Nothing is imported or built; ObserverError when a file is
        unreadable."""
        settings = self.server.settings
        root, active_folder = settings.strategies_root, settings.shown.active_strategy
        active_name, _ = read_identity(root, active_folder)
        active = read_values(root, active_folder)
        found: dict[tuple[str, str], tuple[contract.SettingDifferenceV1, ...]] = {}
        for folder in settings.shown.observers:
            identity = read_identity(root, folder)
            if identity[0] == active_name:
                found[identity] = contract.settings_differences(read_values(root, folder), active)
        return found

    def _movers(self, day: date, today: date,
                records: tuple[DecisionRecord, ...]) -> contract.MoversV1:
        settings = self.server.settings
        toml = read_values(settings.strategies_root, settings.shown.active_strategy)
        return symbols.movers_document(day, self._discovery(day, today),
                                       symbols.mover_rules(toml), records)

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
    return ApiServer(local.settings, local.clock, remote=True, inbox_lock=local.inbox_lock,
                     start_lock=local.start_lock, lab=local.lab)


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by the owner only (0600), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # a leftover from a crash could carry wider permissions
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def pairing_link(address: str, code: str) -> str:
    return "tickr://pair?" + urlencode({"url": address, "code": code})


@contextlib.contextmanager
def _pairing_lock(settings: ApiSettings) -> Iterator[None]:
    """Exclusive across processes: `pair` (the CLI) and POST /pair (the API) never interleave,
    so a redemption of an old code in flight cannot burn the new code or leave its own token
    after a new pairing began. The lock file sits next to the code file and holds nothing."""
    lock = settings.pairing_code_path.with_name(settings.pairing_code_path.name + LOCK_SUFFIX)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def pair(settings: ApiSettings, now: datetime | None = None) -> Path:
    """Start a new pairing: revoke the current phone token (its hash file is deleted, so the
    old phone stops working at once), make a one-time pairing code that expires after
    `[api] remote_pairing_code_ttl_seconds`, keep only its SHA-256, and leave the pairing link
    with the code in a 0600 file for the owner to move to the phone. The phone redeems the code
    once at POST /pair for its token, so a copy of the link left anywhere is worthless after
    that or after expiry. Nothing secret is printed or logged. Returns the link file's path.
    """
    if not settings.remote_hosts:
        raise ConfigError(f"no remote host: write the Tailscale name of this Mac into "
                          f"{settings.remote_hosts_path} first")
    now = now or _now()
    code = secrets.token_urlsafe(settings.token_bytes)
    expires_at = now + timedelta(seconds=settings.pairing_code_ttl_seconds)
    address = f"https://{settings.remote_hosts[0]}"
    with _pairing_lock(settings):
        settings.token_hash_path.unlink(missing_ok=True)
        _write_private(settings.pairing_code_path, json.dumps({
            "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "expires_at": contract.format_time(expires_at)}) + "\n")
        _write_private(settings.pairing_path, pairing_link(address, code) + "\n")
    return settings.pairing_path
