"""Local HTTP API: GET /status and the status page, POST operator commands.

stdlib only. Binds a loopback address and refuses any request whose Host (or Origin, when sent)
is not in the configured allowlist, which blocks DNS rebinding and cross-site requests from a
browser. Remote access (the phone, through an HTTPS proxy on this Mac such as `tailscale serve`)
has its own loopback listener, `[api] remote_port`: every request there must carry the paired
bearer token of a paired device whatever its Host, and its Host must be one of the names in
`[api] remote_hosts_file`. Several devices can be paired, each with its own token, revoked one by
one (api/devices.py). Whether a token is needed is decided by the listener a request
arrives on, never by a header the caller chooses.
The one exception is POST /pair there: the phone redeems the one-time pairing code `pair` left
in the pairing link, once and before it expires, for a new device token. The server keeps only
SHA-256 digests of the code and the tokens.
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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from tickr.api import authlimit, contract, devices, flow, settled, symbols, trade_whatif
from tickr.api.advisor import DecisionRefused, advisor_document, record_decision
from tickr.api.after import AfterSettings, SkippedAfter
from tickr.api.alerts import AlertsSettings, alerts_document
from tickr.api.botstart import StartRefused, StartSettings, run_command, start_bot
from tickr.api.causes import safe_cause
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
    QuotesResponseV1,
    RiskSettings,
    SessionSettings,
    SettingsV1,
    StatusResponseV1,
)
from tickr.api.decision_cache import DecisionCache
from tickr.api.history import SYMBOL_PATTERN, History, HistoryPaths, HistoryUnavailable
from tickr.api.jobs import JobsSettings, jobs_document
from tickr.api.journal import day_events, journal_document
from tickr.api.lab_replay import LabReplay, LabReplayRefused, LabReplaySettings
from tickr.api.observer_results import ObserverResults, SimulationPaths
from tickr.api.settingsview import (
    BadDraft,
    SettingsUnavailable,
    draft_document,
    load_classes,
    load_schema,
    parse_draft,
    schema_document,
    values_document,
)
from tickr.api.system import SystemSettings, system_document
from tickr.api.warm import PastDayWarmer
from tickr.calendar import ny_date_of
from tickr.config import Config, ConfigError
from tickr.data.capture import CapturedBarsDays, CaptureError
from tickr.data.discovery import DiscoverySettings
from tickr.data.history import SYMBOL_PATTERN as TICKER_PATTERN
from tickr.observers.errors import ObserverError
from tickr.observers.loader import STRATEGIES_DIR, read_identity, read_values
from tickr.observers.log import DecisionLog
from tickr.research.errors import RecordFileError
from tickr.research.records import DecisionRecord
from tickr.runtime.alert_routing import RefusalCause, RoutingRefused
from tickr.runtime.alerts import Alerter, AlertKind
from tickr.strategy.base import Role

STATIC_DIR = Path(__file__).resolve().parent / "static"
# What a failed read of a past day's files raises while warming it (api/warm.py).
WARM_ERRORS = (HistoryUnavailable, CaptureError, RecordFileError, ObserverError, OSError)
COMMAND_SOURCE = "api"
JSON_TYPE = "application/json"
LOCAL_DEVICE = "local"  # the device name of a request on the local port (no token there)
SIMULATOR_DEVICE = "simulator"  # the iOS Simulator's token on the remote port's loopback address
LOCAL_PORT, REMOTE_PORT = "local", "remote"  # the port named in a risk-command-used alert
ENTRY_STOP_STATES = {"on": CommandKind.ENTRY_STOP_ON, "off": CommandKind.ENTRY_STOP_OFF}
STOP_MODES = {m.value: m for m in contract.StopMode}  # POST /commands/stop-bot {"mode": ...}
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
# GET with an Idempotency-Key: the answer that key got, or that it queued nothing (no write).
RECEIPT_ROUTE = "/commands/receipt"


# A remote Host header: a lowercase DNS name with at least one dot, optionally ":port".
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
REMOTE_HOST = re.compile(rf"^{_LABEL}(\.{_LABEL})+(:[0-9]+)?$")
BEARER = "Bearer "
PRIVATE_FILE_MODE = 0o600  # owner read and write only (POSIX mode bits)
LOCK_SUFFIX = ".lock"  # the pairing lock file: next to the code file, never holds data
PAIR_ROUTE = "/pair"  # POST on the remote port only: redeem the one-time pairing code
# Fewest random bytes in a token or pairing code: 128 bits, the usual floor for a bearer secret.
MIN_SECRET_BYTES = 16
IPV6_BITS = 128  # length of an IPv6 address (protocol constant)
TOKEN_ROUTE_KIND = "token"  # the budget names in the block log line and the alert
PAIR_ROUTE_KIND = "pair"


@dataclass(frozen=True)
class ApiSettings:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    remote_hosts: tuple[str, ...]  # read from remote_hosts_path at start; empty = remote off
    remote_hosts_path: Path
    remote_port: int
    token_hash_path: Path  # the single phone token's hash before devices: migrated, then gone
    devices_path: Path  # the paired devices: name, token SHA-256, created and last-used time
    simulator_token_hash_path: Path  # the Simulator's own token (`pair-simulator`), beside it
    simulator_token_path: Path  # the Simulator's token itself, 0600, for the app to read
    token_bytes: int
    pairing_path: Path
    pairing_code_path: Path  # SHA-256 of the waiting one-time code and its expiry
    pairing_code_ttl_seconds: int
    auth_trusted_proxy: str  # the proxy's TCP peer address: only its X-Forwarded-For counts
    auth_window_seconds: int  # device-token budget: failed checks per address per window
    auth_failures: int
    pair_window_seconds: int  # POST /pair budget, stricter
    pair_failures: int
    auth_max_addresses: int  # addresses each budget tracks; full of live ones = fail closed
    auth_ipv6_prefix_length: int  # an IPv6 client counts as its network of this length
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
    quotes_path: Path  # the bot's quotes-v1 file, served at GET /quotes
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
    system: SystemSettings  # GET /system: link, preflight record, training, machine services
    alerts: AlertsSettings  # GET /alerts and PUT /alerts/routing: the notification settings
    lab: LabReplaySettings  # GET and POST /lab/replay: the Lab's simulated replay
    start: StartSettings  # POST /commands/start-bot: launchd's start of the bot job
    trade_whatif: trade_whatif.TradeWhatIfSettings  # GET /trades/whatif
    config_file: Path | None  # the bot.toml this API loaded, shown at GET /config
    config_root: Path  # GET /config shows the file's path relative to this
    config_digest: str  # sha256 of the file when this API loaded it
    settings_schema_file: Path  # [api] settings_schema_file: GET /settings/schema and siblings
    settled_dir: Path  # <data_dir>/<[api] settled_cache_dir>/<environment>: past days' answers
    settled_memory_days: int  # [api] settled_memory_days
    settled_disk_days: int  # [api] settled_disk_days


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
    try:
        ipaddress.ip_address(api.remote_auth_trusted_proxy)
    except ValueError:
        raise ConfigError("[api] remote_auth_trusted_proxy must be an IP address") from None
    if min(api.remote_auth_window_seconds, api.remote_auth_failures, api.remote_pair_failures,
           api.remote_auth_max_addresses) <= 0:
        raise ConfigError("[api] remote_auth_window_seconds, remote_auth_failures, "
                          "remote_pair_failures and remote_auth_max_addresses must be positive")
    if api.remote_pair_window_seconds < api.remote_pairing_code_ttl_seconds:
        raise ConfigError("[api] remote_pair_window_seconds must be at least "
                          "remote_pairing_code_ttl_seconds, so one window covers a code's life")
    if not 0 < api.remote_auth_ipv6_prefix_length <= IPV6_BITS:
        raise ConfigError(f"[api] remote_auth_ipv6_prefix_length must be 1-{IPV6_BITS}")
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
    if min(api.settled_memory_days, api.settled_disk_days) < 1:
        raise ConfigError("[api] settled_memory_days and settled_disk_days must be at least 1")
    for key in ("lab_replay_data_dir", "lab_replay_results_dir", "settled_cache_dir"):
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
        devices_path=data / api.remote_devices_file,
        simulator_token_hash_path=data / api.remote_simulator_token_hash_file,
        simulator_token_path=data / api.remote_simulator_token_file,
        token_bytes=api.remote_token_bytes,
        pairing_path=data / api.remote_pairing_file,
        pairing_code_path=data / api.remote_pairing_code_file,
        pairing_code_ttl_seconds=api.remote_pairing_code_ttl_seconds,
        auth_trusted_proxy=api.remote_auth_trusted_proxy,
        auth_window_seconds=api.remote_auth_window_seconds,
        auth_failures=api.remote_auth_failures,
        pair_window_seconds=api.remote_pair_window_seconds,
        pair_failures=api.remote_pair_failures,
        auth_max_addresses=api.remote_auth_max_addresses,
        auth_ipv6_prefix_length=api.remote_auth_ipv6_prefix_length,
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
        quotes_path=data / api.quotes_file,
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
        system=SystemSettings.from_config(cfg),
        alerts=AlertsSettings.from_config(cfg),
        lab=LabReplaySettings.from_config(cfg, config_path),
        start=StartSettings.from_config(cfg),
        trade_whatif=trade_whatif.TradeWhatIfSettings.from_config(cfg),
        config_file=config_path,
        config_root=cfg.root,
        config_digest=cfg.digest,
        settings_schema_file=cfg.root / api.settings_schema_file,
        settled_dir=data / api.settled_cache_dir / cfg.environment_name,
        settled_memory_days=api.settled_memory_days,
        settled_disk_days=api.settled_disk_days,
    )


def shown_settings(cfg: Config) -> SettingsV1:
    """The config numbers clients show, exactly as loaded, and the active strategy's identity
    as its strategy.toml declares it (null with a cause code when that file is unreadable)."""
    risk, session = cfg.risk, cfg.session
    try:
        decider, decider_cause = (contract.strategy_decider(
            *read_identity(cfg.root, cfg.strategy.active)), None)
    except ObserverError:
        decider, decider_cause = None, contract.DECIDER_UNREADABLE
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
        live_quote_max_age_seconds=cfg.api.live_quote_max_age_seconds,
        quotes_poll_seconds=cfg.api.quotes_poll_seconds,
        command_max_age_seconds=cfg.api.command_max_age_seconds,
        decider=decider,
        decider_cause=decider_cause,
    )


def _now() -> datetime:
    return datetime.now(UTC)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # The readers and their caches; the phone's listener shares the local one's.
    captured_bars: CapturedBarsDays
    history: History
    decisions: DecisionCache
    observer_results: ObserverResults
    screener_chips: flow.ScreenerChips
    discovery_day: symbols.DiscoveryDay
    skipped: SkippedAfter
    warmer: PastDayWarmer
    settled: settled.SettledAnswers
    code_key: str  # api/settled.py code_key: the running code, part of every settled key

    def __init__(self, settings: ApiSettings, clock: Callable[[], datetime] = _now, *,
                 remote: bool = False, inbox_lock: threading.Lock | None = None,
                 start_lock: threading.Lock | None = None,
                 lab: LabReplay | None = None, shared: ApiServer | None = None) -> None:
        self.settings = settings
        self.clock = clock
        # GET /status api_started_at: clients read /settings again when it changes.
        self.started_at = clock()
        self.remote = remote
        # Shared with the other listener so both count the inbox under one lock.
        self.inbox_lock = inbox_lock or threading.Lock()
        # One start at a time across both listeners; its own lock, so a hanging start command
        # never holds back a queued command (emergency, flatten).
        self.start_lock = start_lock or threading.Lock()
        # One redemption at a time, so a pairing code can never yield two tokens.
        self.pair_lock = threading.Lock()
        # The alert for a command that raises risk (see _risk_used); one Alerter for both
        # listeners (set by python -m tickr.api; None = no alert, as in most tests).
        self.alerter: Alerter | None = shared.alerter if shared is not None else None
        self.alert_lock = threading.Lock()
        # The remote port's failed-authentication budgets per client address (api/authlimit.py).
        self.auth_limits = {
            TOKEN_ROUTE_KIND: authlimit.AuthLimiter(
                settings.auth_failures, settings.auth_window_seconds,
                settings.auth_max_addresses),
            PAIR_ROUTE_KIND: authlimit.AuthLimiter(
                settings.pair_failures, settings.pair_window_seconds,
                settings.auth_max_addresses),
        }
        # Called with the alert text when an address is blocked; set by `python -m tickr.api`
        # (the `remote-auth-blocked` alert), None in tests and the demo.
        self.auth_alert: Callable[[str], None] | None = None
        self._table_full_logged = {kind: False for kind in self.auth_limits}
        # Shared too, so one replay at a time whichever listener started it.
        self.lab = lab or LabReplay(settings.lab, clock)
        self.page = (STATIC_DIR / "status.html").read_bytes()
        if shared is not None:
            # The phone's listener reads through the local one's caches: one parse per file.
            self.captured_bars = shared.captured_bars
            self.history = shared.history
            self.decisions = shared.decisions
            self.observer_results = shared.observer_results
            self.screener_chips = shared.screener_chips
            self.discovery_day = shared.discovery_day
            self.skipped = shared.skipped
            self.warmer = shared.warmer
            self.settled = shared.settled
            self.code_key = shared.code_key
            # One chart source and one request budget for both listeners: the source is
            # attached to the local listener (python -m tickr.api), and the phone reads it too.
            self.chart = shared.chart
        else:
            # Captured bars by symbol, read on as the bot appends; shared by every reader below.
            self.captured_bars = CapturedBarsDays()
            self.code_key = settled.code_key()
            # A past day's bars by symbol (GET /days/<date>, /bars) indexed on disk once.
            self.history = History(settings.history, settings.after, self.captured_bars,
                                   settled.DayBarsIndex(settings.settled_dir, self.code_key))
            # Decision records: today's read on as the bot appends, the last past day's kept.
            self.decisions = DecisionCache(DecisionLog(settings.decisions))
            self.observer_results = ObserverResults(
                settings.simulation, settings.observer_trades_max_rows, self.captured_bars)
            self.screener_chips = flow.ScreenerChips(settings.universe,
                                                     settings.change_pct_places)
            self.discovery_day = symbols.DiscoveryDay()  # GET /symbols and /movers
            self.skipped = SkippedAfter(settings.simulation, settings.after.minutes,
                                        settings.check_places, self.captured_bars)
            # The latest past day's files, read once per New York date (api/warm.py).
            self.warmer = PastDayWarmer(self.history.latest_past_day, self._warm_past_day,
                                        WARM_ERRORS)
            # Past days' answers built once and kept (api/settled.py).
            self.settled = settled.SettledAnswers(
                settings.settled_dir, settings.settled_memory_days, settings.settled_disk_days)
            # No source until one is attached (python -m tickr.api, or the demo's world).
            self.chart = Chart(settings.chart)
        self.start_runner = run_command  # runs [api] start_bot_command; replaced in tests
        port = settings.remote_port if remote else settings.port
        super().__init__((settings.host, port), Handler)

    def auth_blocked(self, kind: str, key: str) -> None:
        """One log line and the `remote-auth-blocked` alert when `key` used up its `kind`
        budget. The address is the caller's (never a token or code); `kind` names the budget."""
        limiter = self.auth_limits[kind]
        self._report_block(f"address {key} refused on the remote port for up to "
                           f"{limiter.window} s after {limiter.failures} failed {kind} checks")

    def auth_table_full(self, kind: str, refused: bool) -> None:
        """Once per spell: the `kind` table is full of addresses that still count, so new
        addresses are refused (fail closed) until one ages out."""
        limiter = self.auth_limits[kind]
        if not refused:
            if self._table_full_logged[kind] and len(limiter) < limiter.max_addresses:
                self._table_full_logged[kind] = False
            return
        if self._table_full_logged[kind]:
            return
        self._table_full_logged[kind] = True
        self._report_block(f"every new address refused on the remote port: the {kind} table "
                           f"holds {limiter.max_addresses} addresses with failed checks inside "
                           f"{limiter.window} s")

    def _report_block(self, text: str) -> None:
        sys.stderr.write(f"api: auth limit: {text}\n")
        if self.auth_alert is not None:
            self.auth_alert(text)

    def _warm_past_day(self, day: date, today: date) -> None:
        """Builds what a pick of past day `day` reads: its trades (GET /days/<date>, which
        indexes its captured bars by symbol on disk) and every answer of `settled.ROUTES`, kept
        on disk too (api/settled.py). Once kept, a later warming (after a restart) only stamps
        the day's files. A route's failure is left to its own request, which answers the cause."""
        for route in settled.ROUTES:
            with contextlib.suppress(*WARM_ERRORS):
                self.settled_body(route, day, today)
        self.history.day(day, today)

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
            cause = f"status file is invalid ({safe_cause(exc)})"
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
            api_started_at=self.started_at,
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
            return ErrorV1("unavailable", f"account file is invalid ({safe_cause(exc)})")
        age = contract.seconds_between(account.read_at, now)
        if age < 0:
            return ErrorV1("unavailable", "account file is dated in the future")
        return AccountResponseV1(served_at=now, account=account, age_seconds=age,
                                 stale=age > self.settings.account_stale_seconds)

    def quotes_response(self) -> QuotesResponseV1 | ErrorV1:
        """The bot's quotes-v1 file; an `unavailable` error when it is missing or invalid."""
        try:
            quotes = contract.read_quotes(self.settings.quotes_path)
        except FileNotFoundError:
            return ErrorV1("unavailable", "the bot has not written a quotes file")
        except (ContractError, OSError) as exc:
            return ErrorV1("unavailable", f"quotes file is invalid ({safe_cause(exc)})")
        return QuotesResponseV1(served_at=self.clock(), quotes=quotes)

    def start_bot(self) -> BotStartV1 | ErrorV1:
        """POST /commands/start-bot (api/botstart.py), one at a time across both listeners."""
        with self.start_lock:
            try:
                return start_bot(self.settings.start, self.status_response(), self.clock(),
                                 self.start_runner)
            except StartRefused as exc:
                return ErrorV1(exc.error, exc.cause)

    # ------------------------------------------------------------ one day's recorded decisions

    def day_document(self, path: str, day: date, today: date,
                     records: Callable[[Role], tuple[DecisionRecord, ...]]) -> tuple[Any, bool]:
        """(the answer of `path`, one of `settled.ROUTES`, for `day`, built from `records` (the
        day's records of one role), whether it may be kept: False when a transient failure
        decided it). Raises what its reads raise: ObserverError, CaptureError,
        HistoryUnavailable, RecordFileError, OSError."""
        settings = self.settings
        decision_file = self.decisions.log.path(day)
        if path == "/observers":
            identities = tuple(read_identity(settings.strategies_root, folder)
                               for folder in settings.shown.observers)
            observed = records(Role.OBSERVER)
            configured = {name for name, _ in identities}
            results, kept = self.observer_results.results(
                day, today, [r for r in observed if r.decision.strategy in configured],
                decision_file)
            return contract.observers_document(day, identities, observed, results,
                                               self._observer_differences()), kept
        if path == "/skipped":
            return self.skipped.document(day, today, records(Role.ACTIVE), decision_file)
        active = records(Role.ACTIVE)
        if path == "/watching":
            return contract.watching_document(day, active, settings.closeness_places,
                                              settings.check_places), True
        if path == "/flow":
            return self._flow(day, today, active), True
        if path == "/symbols":
            return self._symbols(day, today, active), True
        if path == "/movers":
            return self._movers(day, today, active), True
        raise ValueError(f"{path} is not a route of one day's recorded decisions")

    def settled_key(self, day: date, today: date) -> str | None:
        """The key of a past day's answers (api/settled.py): None for today or a later day, and
        when the inputs cannot be stamped (the answer is then built as for today, not kept)."""
        if day >= today:
            return None
        settings = self.settings
        named = day.isoformat()
        try:
            folders = tuple(settled.folder_stamps(settings.strategies_root / STRATEGIES_DIR / f)
                            for f in (settings.shown.active_strategy, *settings.shown.observers))
            return settled.day_key((
                self.code_key, settings.config_digest, named,
                settled.folder_stamps(settings.decisions / named),
                settled.folder_stamps(settings.history.capture / named),
                settled.file_stamp(settings.simulation.replay_settings),
                settled.folder_stamps(settings.simulation.calendar), folders,
                self.history.entries(day, today)))
        except (HistoryUnavailable, OSError):
            return None

    def settled_body(self, path: str, day: date, today: date) -> bytes | None:
        """The encoded answer of `path` (one of `settled.ROUTES`) for a past `day`: kept, or
        built now together with the day's other routes and kept. None when `day` is not past
        or its inputs cannot be stamped: the caller then builds it as for today. Raises what
        `day_document` raises for `path`."""
        key = self.settled_key(day, today)
        if key is None:
            return None
        found = self.settled.get(day, key, path)
        if found is not None:
            return found
        with self.settled.day_lock(day):
            found = self.settled.get(day, key, path)  # built while this request waited
            if found is not None:
                return found
            return self._settle(path, day, today, key)

    def _settle(self, path: str, day: date, today: date, key: str) -> bytes:
        """Build every route of `day` from one read of its records per role, keep the answers
        that may be kept and return `path`'s. Another route's failure is not this request's:
        it is not kept, and that route's own request builds it again and answers its cause."""
        read: dict[Role, tuple[DecisionRecord, ...] | Exception] = {}

        def records(role: Role) -> tuple[DecisionRecord, ...]:
            if role not in read:
                try:
                    read[role] = self.decisions.records(day, today, role=role)
                except (RecordFileError, OSError) as exc:
                    read[role] = exc
            found = read[role]
            if isinstance(found, Exception):
                raise found
            return found

        kept: dict[str, bytes] = {}
        answer: bytes | None = None
        failure: Exception | None = None
        for route in settled.ROUTES:
            try:
                document, keep = self.day_document(route, day, today, records)
            except (ObserverError, CaptureError, HistoryUnavailable, RecordFileError,
                    OSError) as exc:
                if route == path:
                    failure = exc
                continue
            body = contract.dumps(document).encode("utf-8")
            if keep:
                kept[route] = body
            if route == path:
                answer = body
        try:
            self.settled.put(day, key, kept)
        except OSError as exc:  # memory holds them; the disk copy is only for the next start
            sys.stderr.write(f"settled answers of {day.isoformat()} not written "
                             f"({type(exc).__name__})\n")
        if failure is not None:
            raise failure
        if answer is None:
            raise ValueError(f"{path} is not a route of one day's recorded decisions")
        return answer

    def _flow(self, day: date, today: date,
              records: tuple[DecisionRecord, ...]) -> contract.FlowV1:
        settings = self.settings
        path = flow.screener_path(settings.history.capture, day)
        return flow.flow_document(
            settings.history.environment, day,
            self.screener_chips.chips(path, open_tail=day == today), records,
            self.history.entries(day, today),
            flow.reconcile(day, today, settings.status_path, settings.account_path))

    def _discovery(self, day: date, today: date) -> symbols.DiscoveryFolded:
        return self.discovery_day.read(
            symbols.discovery_path(self.settings.history.capture, day), day == today)

    def _symbols(self, day: date, today: date,
                 records: tuple[DecisionRecord, ...]) -> contract.SymbolsV1:
        settings = self.settings
        chips = self.screener_chips.chips(
            flow.screener_path(settings.history.capture, day), open_tail=day == today)
        return symbols.symbols_document(day, self._discovery(day, today), chips, records,
                                        self.history.entries(day, today),
                                        settings.check_places)

    def _observer_differences(self) -> dict[tuple[str, str], tuple[contract.SettingDifferenceV1,
                                                                    ...]]:
        """Display only: each configured observer folder of the active strategy's name, by the
        (name, version) its file declares now, with the settings where its file differs from
        the active strategy's file. Nothing is imported or built; ObserverError when a file is
        unreadable."""
        settings = self.settings
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
        settings = self.settings
        toml = read_values(settings.strategies_root, settings.shown.active_strategy)
        return symbols.movers_document(day, self._discovery(day, today),
                                       symbols.mover_rules(toml), records)

    def enqueue(self, kind: CommandKind, symbol: str | None = None, key: str | None = None,
                mode: contract.StopMode | None = None) -> CommandAcceptedV1 | ErrorV1 | bytes:
        """Queue one command. With an Idempotency-Key already seen within its lifetime the
        first answer comes back as stored bytes and nothing is queued; the same key for a
        different command is a conflict. The command file is written before the key, so a
        crash between the two can at worst queue a repeat, never answer for a command that
        was not queued (every command is idempotent in its effect on the bot)."""
        with self.inbox_lock:
            now = self.clock()
            request: dict[str, Any] = {"kind": kind.value, "symbol": symbol}
            if mode is not None:  # absent without one, so a key stored before modes still matches
                request["mode"] = mode.value
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
            command = contract.new_command(kind, now, COMMAND_SOURCE, symbol, mode)
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

    def receipt(self, key: str) -> contract.CommandReceiptV1:
        """What a POST /commands/* with `key` was answered, read under the inbox lock so a send
        of the same key in progress is either wholly before or after it; nothing is written.
        A record that cannot be read raises (`internal`), never "not queued"."""
        with self.inbox_lock:
            stored = self._remembered(key, self.clock())
        if stored is None:
            return contract.CommandReceiptV1(queued=False, accepted=None)
        accepted = contract.loads(CommandAcceptedV1, stored["answer"].encode("utf-8"))
        return contract.CommandReceiptV1(queued=True, accepted=accepted)

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


# HTTP statuses of a refused caller (bad token, foreign Host or Origin): their access lines
# also carry the Host the caller sent (`Handler.log_request`).
REFUSAL_CODES = frozenset({401, 403})

_ERROR_CODES = {
    "forbidden_host": 403, "forbidden_origin": 403, "not_found": 404,
    "method_not_allowed": 405, "length_required": 411, "payload_too_large": 413,
    "unsupported_media_type": 415, "bad_request": 400, "too_many_pending": 429,
    "internal": 500, "unavailable": 503, "unauthorized": 401, "conflict": 409,
    "rate_limited": 429, "auth_rate_limited": 429,
}
GET_ROUTES = ("/", "/status", "/commands/pending", RECEIPT_ROUTE, "/schema", "/days", "/bars",
              "/settings",
              "/explain", "/observers", "/decisions", "/skipped", "/account", "/flow",
              "/watching", "/performance", "/chart", "/jobs", "/system", "/journal", "/config",
              "/trades/whatif", "/advisor", "/symbols", "/movers", "/quotes", "/events",
              "/settings/schema", "/settings/values", "/alerts")
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
SETTINGS_VALIDATE = "/settings/validate"  # POST only: checks a draft, writes nothing
ALERT_ROUTING = "/alerts/routing"  # PUT only: one cell of the owner's alert routing
DAY_PREFIX = "/days/"
# Headers a proxy adds to a request it forwards (Go's ReverseProxy, which `tailscale serve` is,
# adds X-Forwarded-For; Tailscale adds its identity headers). The remote port's loopback Host is
# for a client on this Mac only: a request carrying any of these came through the proxy, whose
# Host the remote caller chose, so the Simulator's Host is refused for it (protocol constants).
FORWARDING_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                      "X-Real-IP", "Tailscale-User-Login", "Tailscale-User-Name",
                      "Tailscale-User-Profile-Pic", "Tailscale-Headers-Info")


class BadQuery(ValueError):
    """The query string is not exactly what the route takes; the message is the cause."""


def _query(raw: str, names: frozenset[str],
           optional: frozenset[str] = frozenset()) -> dict[str, str]:
    """Exactly the parameters `names` (those in `optional` may be left out), each once and
    non-empty; anything else is refused."""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=bool(raw))
    except ValueError as exc:
        raise BadQuery(f"malformed query string: {type(exc).__name__}") from exc
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
        raise BadQuery(safe_cause(exc)) from exc


class Handler(BaseHTTPRequestHandler):
    server: ApiServer
    server_version = "tickr-api"
    sys_version = ""

    # ------------------------------------------------------------ plumbing

    _client = ""  # the remote port's client address key (api/authlimit.py), set by _guard
    _last_error: str | None = None  # the error code last answered, read by _simulator_authorized

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
        self._last_error = error
        body = contract.dumps(ErrorV1(error, cause)).encode("utf-8")
        self._send(_ERROR_CODES[error], body, f"{JSON_TYPE}; charset=utf-8", extra or {})

    def _guard(self) -> bool:
        self.device_name = ""  # who the token belongs to; set once the request is authorized
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if self.server.remote:
            settings = self.server.settings
            try:
                self._client = authlimit.client_key(
                    self.client_address[0], self.headers.get_all(authlimit.FORWARDED_FOR) or [],
                    settings.auth_trusted_proxy, settings.auth_ipv6_prefix_length)
            except authlimit.BadForwardedFor:
                # A proxy that names no client: counted under the proxy itself, never guessed.
                self._client = authlimit.address_key(self.client_address[0],
                                                     settings.auth_ipv6_prefix_length)
                with self.server.pair_lock:
                    refused = self._admit(TOKEN_ROUTE_KIND)
                    if refused is None:
                        self._settle(TOKEN_ROUTE_KIND, failed=True)
                if refused is not None:
                    self._refuse(TOKEN_ROUTE_KIND, refused)
                else:
                    self._error("bad_request", authlimit.BAD_FORWARDED_FOR)
                return False
            # The remote listener: the token is always required; Host guards against rebinding.
            if host is None or not (host in self.server.settings.remote_hosts
                                    or self._simulator_host(host)):
                self._error("forbidden_host", f"Host {host!r} is not allowed")
                return False
            if origin is not None and origin != f"https://{host}":
                self._error("forbidden_origin", f"Origin {origin!r} is not allowed")
                return False
            if self.command == "POST" and self._path() == PAIR_ROUTE:
                return True  # the pairing code in the body is the credential: see _redeem
            return self._authorized(host)
        if host in self.server.allowed_host_headers:
            if origin is not None and origin not in self.server.allowed_origins:
                self._error("forbidden_origin", f"Origin {origin!r} is not allowed")
                return False
            self.device_name = LOCAL_DEVICE
            return True
        self._error("forbidden_host", f"Host {host!r} is not allowed")
        return False

    def _admit(self, kind: str) -> authlimit.Refused | None:
        """Reserve one `kind` check for this request's client address (None), or the refusal to
        answer with `_refuse` before any credential is looked at. The caller holds `pair_lock`
        (checks are one at a time) and settles every admission with `_settle`."""
        refused = self.server.auth_limits[kind].acquire(self._client)
        self.server.auth_table_full(kind, refused is not None and refused.table_full)
        return refused

    def _refuse(self, kind: str, refused: authlimit.Refused) -> None:
        """429 `auth_rate_limited` with Retry-After: names no credential and tests none."""
        cause = ("too many addresses failed authentication recently" if refused.table_full
                 else f"too many failed {kind} checks from this address")
        self._error("auth_rate_limited", f"{cause}; retry after {refused.retry_after} s",
                    {"Retry-After": str(refused.retry_after)})

    def _settle(self, kind: str, failed: bool) -> None:
        if self.server.auth_limits[kind].release(self._client, failed):
            self.server.auth_blocked(kind, self._client)

    def _simulator_host(self, host: str) -> bool:
        """The remote port's own loopback address (host:port it listens on), which the iOS
        Simulator on this Mac reads: answered only while a Simulator token is paired, only for a
        request no proxy forwarded (`FORWARDING_HEADERS`: through `tailscale serve` the caller
        chooses the Host), and a paired token is still required."""
        settings = self.server.settings
        return (host == f"{settings.host}:{self.server.server_port}"
                and settings.simulator_token_hash_path.exists()
                and not any(self.headers.get(name) is not None for name in FORWARDING_HEADERS))

    def _authorized(self, host: str) -> bool:
        """A remote request must present a token paired for its Host: a paired device's on the
        Tailscale names, the Simulator's on this port's loopback address only, so a copy of the
        Simulator's token (kept in a file on this Mac) opens nothing from the tailnet. The token
        is never logged or echoed."""
        if host not in self.server.settings.remote_hosts:
            return self._simulator_authorized()
        challenge = {"WWW-Authenticate": "Bearer"}
        settings = self.server.settings
        header = self.headers.get("Authorization") or ""
        with self.server.pair_lock, _pairing_lock(settings):
            refused = self._admit(TOKEN_ROUTE_KIND)
            if refused is None:
                paired: tuple[devices.Device, ...] | Exception = ()
                device = None
                try:
                    paired, device = self._match_device(header)
                finally:  # every answer below for a readable list without a match is a 401
                    self._settle(TOKEN_ROUTE_KIND, isinstance(paired, tuple) and device is None)
        if refused is not None:
            self._refuse(TOKEN_ROUTE_KIND, refused)
            return False
        if not isinstance(paired, tuple):
            self._device_list_unreadable(paired)
            return False
        if not paired:
            self._error("unauthorized", "nothing is paired here; run `python -m tickr.api "
                        "pair`", challenge)
            return False
        if not header.startswith(BEARER):
            self._error("unauthorized", "a bearer token is required", challenge)
            return False
        if device is None:
            self._error("unauthorized", "the bearer token is not the paired one", challenge)
            return False
        self.device_name = device.name
        return True

    def _match_device(self, header: str) -> tuple[tuple[devices.Device, ...] | Exception,
                                                  devices.Device | None]:
        """The paired devices (or why they cannot be read) and the one whose token `header`
        carries, its last-used time recorded; the caller holds the pairing locks."""
        settings = self.server.settings
        try:
            paired = load_devices(settings)
        except (OSError, devices.DeviceFileError) as exc:
            return exc, None
        if not header.startswith(BEARER):
            return paired, None
        device = devices.match(paired, header[len(BEARER):])
        if device is not None:
            marked = devices.used(paired, device.name, self.server.clock())
            try:
                if marked is not None:
                    _write_private(settings.devices_path, devices.render(marked))
            except OSError as exc:  # the request is still the device's: only the time is lost
                sys.stderr.write(f"api: last-used time of device {device.name!r} not "
                                 f"recorded ({type(exc).__name__})\n")
        return paired, device

    def _device_list_unreadable(self, exc: Exception) -> None:
        """503 for a device list that cannot be read. The cause and the log name the exception's
        type only: its text carries the local path of the file, which never leaves this Mac."""
        kind = type(exc).__name__
        sys.stderr.write(f"api: the paired devices are unreadable ({kind})\n")
        self._error("unavailable", f"device_list_unreadable:{kind}")

    def _simulator_authorized(self) -> bool:
        with self.server.pair_lock:
            refused = self._admit(TOKEN_ROUTE_KIND)
            if refused is None:
                self._last_error = None
                try:
                    return self._simulator_token_matches()
                finally:
                    self._settle(TOKEN_ROUTE_KIND, self._last_error == "unauthorized")
        self._refuse(TOKEN_ROUTE_KIND, refused)
        return False

    def _simulator_token_matches(self) -> bool:
        challenge = {"WWW-Authenticate": "Bearer"}
        path = self.server.settings.simulator_token_hash_path
        try:
            expected = path.read_text("ascii").strip()
        except FileNotFoundError:
            self._error("unauthorized", "nothing is paired here; run `python -m tickr.api "
                        "pair-simulator`", challenge)
            return False
        except (OSError, UnicodeDecodeError) as exc:
            self._error("unavailable",
                        f"the paired token's hash file is unreadable ({type(exc).__name__})")
            return False
        header = self.headers.get("Authorization") or ""
        if not header.startswith(BEARER):
            self._error("unauthorized", "a bearer token is required", challenge)
            return False
        presented = devices.digest(header[len(BEARER):])
        if not hmac.compare_digest(presented, expected):
            self._error("unauthorized", "the bearer token is not the paired one", challenge)
            return False
        self.device_name = SIMULATOR_DEVICE
        return True

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write(f"{self.log_date_time_string()} api {format % args}\n")

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """The access line names the listener's port, and on a refusal (401, 403) the Host the
        caller sent: which client was refused, and why, is then readable from the log alone.
        The Host is untrusted text, so it is written with repr; no token or header value
        other than the Host is ever written."""
        if isinstance(code, HTTPStatus):
            code = code.value
        line = f'"{self.requestline}" {code} {size} port={self.server.server_port}'
        if code in REFUSAL_CODES:
            line += f" host={self.headers.get('Host')!r}"
        self.log_message("%s", line)

    # ------------------------------------------------------------ methods

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._guard():
            return
        self.server.warmer.on_request(ny_date_of(self.server.clock()))
        try:
            path = self._path()
            if path == "/":
                self._send(200, self.server.page, HTML_TYPE, dict(PAGE_HEADERS))
            elif path == "/status":
                self._json(200, self.server.status_response())
            elif path == RECEIPT_ROUTE:
                key = self.headers.get(IDEMPOTENCY_HEADER)
                if key is None or not IDEMPOTENCY_KEY.match(key):
                    self._error("bad_request", f"{IDEMPOTENCY_HEADER} must be 1-128 characters "
                                "from A-Z a-z 0-9 . _ : -")
                else:
                    self._json(200, self.server.receipt(key))
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
            elif path in ("/settings/schema", "/settings/values"):
                self._owner_settings(path)
            elif path == SETTINGS_VALIDATE:
                self._error("method_not_allowed", "use POST", {"Allow": "POST"})
            elif path == "/jobs":
                self._json(200, jobs_document(self.server.settings.jobs,
                                              self.server.status_response()))
            elif path == "/system":
                self._json(200, system_document(self.server.settings.system,
                                                self.server.status_response()))
            elif path == "/alerts":
                self._json(200, alerts_document(self.server.settings.alerts))
            elif path == ALERT_ROUTING:
                self._error("method_not_allowed", "use PUT", {"Allow": "PUT"})
            elif path == "/journal":
                self._json(200, journal_document(self.server.settings.jobs.database,
                                                 self.server.settings.journal_rows))
            elif path == "/events":
                self._events()
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
            elif path == "/quotes":
                self._quotes()
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
            if path == SETTINGS_VALIDATE:
                self._validate_settings()
                return
            if path == ALERT_ROUTING:
                self._error("method_not_allowed", "use PUT", {"Allow": "PUT"})
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
            symbol, mode = None, None
            if kind is None:
                if set(body) != {"state"} or body["state"] not in ENTRY_STOP_STATES:
                    self._error("bad_request", 'body must be exactly {"state": "on"} or '
                                '{"state": "off"}')
                    return
                kind = ENTRY_STOP_STATES[body["state"]]
            elif kind in (CommandKind.FLATTEN, CommandKind.CANCEL_OWN_ORDERS) and body:
                # null is refused, never read as "everything": a client bug must not widen it.
                if (set(body) != {"symbol"} or not isinstance(body["symbol"], str)
                        or not SYMBOL_PATTERN.match(body["symbol"])):
                    self._error("bad_request", 'body must be {} or {"symbol": "<TICKER>"} '
                                "with the ticker in upper case")
                    return
                symbol = body["symbol"]
            elif kind is CommandKind.STOP_BOT and body:
                # Only the two modes: anything else is a client bug, never a guess.
                if set(body) != {"mode"} or body["mode"] not in STOP_MODES:
                    self._error("bad_request", 'body must be {}, {"mode": "close_and_stop"} '
                                'or {"mode": "leave_open"}')
                    return
                mode = STOP_MODES[body["mode"]]
            elif body:
                self._error("bad_request", "body must be an empty JSON object {}")
                return
            result = self.server.enqueue(kind, symbol, key, mode)
            if isinstance(result, ErrorV1):
                self._error(result.error, result.cause)
                return
            if kind is CommandKind.ENTRY_STOP_OFF:
                self._risk_used("entry-stop off (entries allowed again)")
            if isinstance(result, bytes):
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
            self._risk_used("start-bot")
            self._json(202, result)

    def _risk_used(self, command: str) -> None:
        """One `risk-command-used` alert for an accepted command that raises risk: the device
        (its name from the device list, never its token or hash), the port and the time. A
        stolen token shows up at once. Never raises: the command is already accepted."""
        alerter = self.server.alerter
        if alerter is None:
            return
        port = REMOTE_PORT if self.server.remote else LOCAL_PORT
        text = (f"{command} by device {self.device_name!r} on the {port} port at "
                f"{contract.format_time(self.server.clock())}; if this was not you, revoke the "
                "device (docs/operations.md, risk-command-used)")
        try:
            with self.server.alert_lock:
                outcomes = [alerter.alert(AlertKind.RISK_COMMAND_USED, text), *alerter.completed()]
            for outcome in outcomes:
                body = outcome.journal_body()
                if body["error"]:
                    sys.stderr.write(f"api: alert {body['kind']} {body['delivery']} "
                                     f"({body['error']})\n")
        except Exception as exc:  # noqa: BLE001 - a failing alert never undoes the command
            sys.stderr.write(f"api: alert {AlertKind.RISK_COMMAND_USED.value} failed "
                             f"({type(exc).__name__})\n")

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
            self._error("not_found" if exc.missing else "conflict", safe_cause(exc))
            return
        self._json(200, advisor_document(settings.advisor_dir, settings.advisor_runs))

    def do_PUT(self) -> None:  # noqa: N802 - stdlib naming
        if not self._guard():
            return
        try:
            if self._path() != ALERT_ROUTING:
                self._error("method_not_allowed", "only GET and POST are supported",
                            {"Allow": "GET, POST"})
                return
            self._alert_routing()
        except Exception as exc:  # noqa: BLE001 - one typed answer instead of a dropped socket
            self._error("internal", type(exc).__name__)

    def _alert_routing(self) -> None:
        """PUT /alerts/routing with {"kind": "<alert kind>", "channel": "mac"|"imessage"|"push",
        "on": true|false}: change that one cell of the owner's alert routing, 200 alerts-v1;
        400 for an unknown kind or channel, 409 for a critical kind turned off or a routing
        file that cannot be used. A preference about where to be told, never a bot command."""
        body = self._read_json_body()
        if body is None:
            return
        if (set(body) != {"kind", "channel", "on"} or not isinstance(body["kind"], str)
                or not isinstance(body["channel"], str) or not isinstance(body["on"], bool)):
            self._error("bad_request", 'body must be exactly {"kind": "<alert kind>", '
                        '"channel": "mac" | "imessage" | "push", "on": true | false}')
            return
        settings = self.server.settings.alerts
        try:
            routing = settings.routing.set(body["kind"], body["channel"], body["on"])
        except RoutingRefused as exc:
            bad = exc.cause in (RefusalCause.UNKNOWN_KIND, RefusalCause.UNKNOWN_CHANNEL)
            self._error("bad_request" if bad else "conflict", safe_cause(exc))
            return
        self._json(200, alerts_document(settings, routing))

    def _redeem(self) -> None:
        """POST /pair with {"code": "<code>"} on the remote port: the one-time pairing code `pair`
        left in the pairing link buys one new bearer token, 200 paired-v1, for the device named
        at `pair` (added, or its earlier token replaced). The code is burnt on success and when
        found expired; a wrong code changes nothing. Neither the code nor the
        token is ever logged or echoed in an error."""
        body = self._read_json_body()
        if body is None:
            return
        if set(body) != {"code"} or not isinstance(body["code"], str) or not body["code"]:
            self._error("bad_request", 'body must be exactly {"code": "<pairing code>"}')
            return
        settings = self.server.settings
        with self.server.pair_lock, _pairing_lock(settings):
            refused = self._admit(PAIR_ROUTE_KIND)
            if refused is None:
                self._last_error = None
                try:
                    token = self._redeem_locked(body["code"])
                finally:  # every 401 here is a code that failed; a 503 tested none
                    self._settle(PAIR_ROUTE_KIND, self._last_error == "unauthorized")
        if refused is not None:
            self._refuse(PAIR_ROUTE_KIND, refused)
        elif token is not None:
            self._json(200, PairedV1(token=token))

    def _redeem_locked(self, code: str) -> str | None:
        """`_redeem` under the pairing locks, once admitted by the /pair budget: the new token,
        or None once the refusal is answered."""
        settings = self.server.settings
        try:
            waiting = json.loads(settings.pairing_code_path.read_text("ascii"))
            digest = waiting["sha256"]
            name = waiting["name"]
            expires_at = contract.parse_time(waiting["expires_at"])
            if not isinstance(digest, str):
                raise TypeError("sha256")
            if not isinstance(name, str) or not devices.valid_name(name):
                raise ValueError("name")
        except FileNotFoundError:
            self._error("unauthorized", "no pairing is waiting; run `python -m tickr.api "
                        "pair` on the Mac")
            return None
        except (OSError, ValueError, KeyError, TypeError, ContractError) as exc:
            self._error("unavailable",
                        f"the pairing code file is unreadable ({type(exc).__name__})")
            return None
        if self.server.clock() >= expires_at:
            settings.pairing_code_path.unlink(missing_ok=True)
            settings.pairing_path.unlink(missing_ok=True)
            self._error("unauthorized", "the pairing code has expired; run `python -m "
                        "tickr.api pair` on the Mac again")
            return None
        presented = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(presented, digest):
            self._error("unauthorized", "the pairing code is not the waiting one")
            return None
        try:
            paired = load_devices(settings)
        except (OSError, devices.DeviceFileError) as exc:
            self._device_list_unreadable(exc)
            return None
        token = secrets.token_urlsafe(settings.token_bytes)
        device = devices.Device(name, devices.digest(token), self.server.clock(), None)
        _write_private(settings.devices_path,
                       devices.render(devices.with_device(paired, device)))
        settings.pairing_code_path.unlink(missing_ok=True)
        settings.pairing_path.unlink(missing_ok=True)
        return token

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
            self._error("bad_request", safe_cause(exc))
            return
        try:
            self._json(202, self.server.lab.start(body.get("version"), body.get("compare"), day))
        except LabReplayRefused as exc:
            self._error("conflict", safe_cause(exc))

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
            self._error("bad_request", safe_cause(exc))
            return
        except HistoryUnavailable as exc:
            self._error("unavailable", safe_cause(exc))
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

    def _owner_settings(self, path: str) -> None:
        """GET /settings/schema and GET /settings/values (api/settingsview.py), read-only."""
        settings = self.server.settings
        try:
            _query(urlsplit(self.path).query, frozenset())
        except BadQuery as exc:
            self._error("bad_request", safe_cause(exc))
            return
        try:
            specs = load_schema(settings.settings_schema_file)
            if path == "/settings/schema":
                self._json(200, schema_document(specs))
                return
            if settings.config_file is None:
                self._error("unavailable", "this API was started without a config file path")
                return
            self._json(200, values_document(specs, settings.config_file, settings.config_root))
        except SettingsUnavailable as exc:
            self._error("unavailable", safe_cause(exc))

    def _validate_settings(self) -> None:
        """POST /settings/validate with {"changes": [...]}: 200 settings-draft-v1, the verdict
        on the draft. Nothing is written, journaled or applied."""
        body = self._read_json_body()
        if body is None:
            return
        settings = self.server.settings
        try:
            changes = parse_draft(body)
        except BadDraft as exc:
            self._error("bad_request", safe_cause(exc))
            return
        if settings.config_file is None:
            self._error("unavailable", "this API was started without a config file path")
            return
        try:
            self._json(200, draft_document(load_schema(settings.settings_schema_file), changes,
                                           settings.config_file, settings.config_root,
                                           load_classes(settings.settings_schema_file)))
        except SettingsUnavailable as exc:
            self._error("unavailable", safe_cause(exc))

    def _account(self) -> None:
        """GET /account: the bot's latest broker read of positions and open orders."""
        try:
            _query(urlsplit(self.path).query, frozenset())
        except BadQuery as exc:
            self._error("bad_request", safe_cause(exc))
            return
        result = self.server.account_response()
        if isinstance(result, ErrorV1):
            self._error(result.error, result.cause)
        else:
            self._json(200, result)

    def _events(self) -> None:
        """GET /events?date=: what the bot did on one New York date (today without `date`),
        from its journal opened read-only (display only)."""
        try:
            params = _query(urlsplit(self.path).query, frozenset({"date"}),
                            optional=frozenset({"date"}))
            day = (_day(params["date"]) if "date" in params
                   else ny_date_of(self.server.clock()))
        except BadQuery as exc:
            self._error("bad_request", safe_cause(exc))
            return
        self._json(200, day_events(self.server.settings.jobs.database, day))

    def _quotes(self) -> None:
        """GET /quotes: the newest quote of every symbol on the bot's stream (display only)."""
        try:
            _query(urlsplit(self.path).query, frozenset())
        except BadQuery as exc:
            self._error("bad_request", safe_cause(exc))
            return
        result = self.server.quotes_response()
        if isinstance(result, ErrorV1):
            self._error(result.error, result.cause)
        else:
            self._json(200, result)

    def _chart(self) -> None:
        """GET /chart?symbol=&interval=&range=: candles from market data (api/chart.py). Every
        refusal's cause is logged next to its status line: a 503 alone does not say why."""
        try:
            params = _query(urlsplit(self.path).query, frozenset({"symbol", "interval", "range"}))
            symbol, interval, span = parse_query(params)
            document = self.server.chart.answer(symbol, interval, span, self.server.clock())
        except BadQuery as exc:
            self.log_message("GET /chart refused: bad_request: %s", exc)
            self._error("bad_request", safe_cause(exc))
            return
        except ChartRefused as exc:
            self.log_message("GET /chart refused: %s: %s", exc.error, exc.cause)
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
            self._error("bad_request", safe_cause(exc))
            return
        try:
            document = trade_whatif.what_if(self.server.settings.trade_whatif,
                                            params["version"], params["symbol"], day,
                                            self.server.clock(), self.server.captured_bars)
        except trade_whatif.TradeWhatIfRefused as exc:
            self._error("conflict", safe_cause(exc))
            return
        self._json(200, document)

    def _explain(self) -> None:
        """GET /explain?symbol=: the latest active-strategy decision recorded today."""
        try:
            symbol = _query(urlsplit(self.path).query, frozenset({"symbol"}))["symbol"]
            if not SYMBOL_PATTERN.match(symbol):
                raise BadQuery(f"symbol {symbol!r} is not a ticker")
        except BadQuery as exc:
            self._error("bad_request", safe_cause(exc))
            return
        day = ny_date_of(self.server.clock())
        try:
            found = [r for r in self.server.decisions.records(day, day, symbol=symbol)
                     if r.role is Role.ACTIVE]  # as DecisionLog.latest_active filters
            record = found[-1] if found else None
        except (RecordFileError, OSError) as exc:
            self._error("unavailable", f"decision file unreadable: {safe_cause(exc)}")
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
        simulated day result, /flow the captured screeners and the database. A past day's
        answers of `settled.ROUTES` are built once and kept (api/settled.py)."""
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
            self._error("bad_request", safe_cause(exc))
            return
        cache = self.server.decisions
        try:
            if path == "/decisions":
                symbol = params["symbol"]
                document: Any = contract.decisions_document(
                    symbol, day, cache.records(day, today, symbol=symbol), settings.check_places)
            else:
                body = self.server.settled_body(path, day, today)
                if body is not None:
                    self._send(200, body, f"{JSON_TYPE}; charset=utf-8", {})
                    return
                document, _ = self.server.day_document(
                    path, day, today, lambda role: cache.records(day, today, role=role))
        except ObserverError as exc:
            self._error("unavailable", f"observer strategy unreadable: {safe_cause(exc)}")
            return
        except CaptureError as exc:
            self._error("unavailable", f"capture file unreadable: {safe_cause(exc)}")
            return
        except HistoryUnavailable as exc:
            self._error("unavailable", safe_cause(exc))
            return
        except (RecordFileError, OSError) as exc:
            self._error("unavailable", f"decision file unreadable: {safe_cause(exc)}")
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
            self._error("bad_request", safe_cause(exc))
            return None
        if not isinstance(body, dict):
            self._error("bad_request", "body must be a JSON object")
            return None
        return body

    def _not_allowed(self) -> None:
        if self._guard():
            self._error("method_not_allowed", "only GET and POST are supported",
                        {"Allow": "GET, POST"})

    do_HEAD = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815 - stdlib naming


def make_server(settings: ApiSettings, clock: Callable[[], datetime] = _now, *,
                warm: bool = False) -> ApiServer:
    """`warm`: read the latest past day into the caches once per New York date (api/warm.py);
    `python -m tickr.api serve` turns it on."""
    server = ApiServer(settings, clock)
    server.warmer.enabled = warm
    return server


def make_remote_server(local: ApiServer) -> ApiServer:
    """The phone-facing listener next to `local`, sharing its settings, clock and inbox lock."""
    return ApiServer(local.settings, local.clock, remote=True, inbox_lock=local.inbox_lock,
                     start_lock=local.start_lock, lab=local.lab, shared=local)


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


def pair_simulator(settings: ApiSettings) -> Path:
    """Give the iOS Simulator on this Mac its own bearer token, beside the phone's: revoke the
    Simulator's previous one, keep the new one's SHA-256 for the api server, and leave the token
    with the address it opens (the remote port's loopback, from `[api] host` and `remote_port`:
    the one source of that port) in a 0600 JSON file for a Simulator build to read at launch.
    The token opens that loopback address only. The phone's token is untouched. Nothing secret
    is printed or logged. Returns the file's path."""
    if not settings.remote_hosts:
        raise ConfigError(f"no remote host: write the Tailscale name of this Mac into "
                          f"{settings.remote_hosts_path} first (the remote port is off without it)")
    token = secrets.token_urlsafe(settings.token_bytes)
    address = f"http://{settings.host}:{settings.remote_port}"
    with _pairing_lock(settings):
        _write_private(settings.simulator_token_path,
                       json.dumps({"address": address, "token": token}) + "\n")
        _write_private(settings.simulator_token_hash_path,
                       hashlib.sha256(token.encode("utf-8")).hexdigest() + "\n")
    return settings.simulator_token_path


def unpair_simulator(settings: ApiSettings) -> None:
    """Revoke the Simulator's token: delete its hash (the api server refuses it at once) and the
    file holding it. The phone's token is untouched."""
    with _pairing_lock(settings):
        settings.simulator_token_hash_path.unlink(missing_ok=True)
        settings.simulator_token_path.unlink(missing_ok=True)


def pair(settings: ApiSettings, name: str, now: datetime | None = None) -> Path:
    """Start pairing the device `name`: make a one-time pairing code that expires after
    `[api] remote_pairing_code_ttl_seconds`, keep only its SHA-256 with the name, and leave the
    pairing link with the code in a 0600 file for the owner to move to the device. The device
    redeems the code once at POST /pair for its token, so a copy of the link left anywhere is
    worthless after that or after expiry. Other paired devices keep working; a device already
    paired under `name` keeps its token until the new code is redeemed, which replaces it. A new
    `pair` replaces a code still waiting. Nothing secret is printed or logged. Returns the link
    file's path."""
    if not devices.valid_name(name):
        raise ValueError(f"device name {name!r}: letters, digits, space, dot, dash and underscore "
                         "only, starting and ending with a letter or digit")
    if not settings.remote_hosts:
        raise ConfigError(f"no remote host: write the Tailscale name of this Mac into "
                          f"{settings.remote_hosts_path} first")
    now = now or _now()
    code = secrets.token_urlsafe(settings.token_bytes)
    expires_at = now + timedelta(seconds=settings.pairing_code_ttl_seconds)
    address = f"https://{settings.remote_hosts[0]}"
    with _pairing_lock(settings):
        _write_private(settings.pairing_code_path, json.dumps({
            "sha256": devices.digest(code),
            "name": name,
            "expires_at": contract.format_time(expires_at)}) + "\n")
        _write_private(settings.pairing_path, pairing_link(address, code) + "\n")
    return settings.pairing_path


def load_devices(settings: ApiSettings) -> tuple[devices.Device, ...]:
    """The paired devices; the caller holds the pairing lock. The single phone token of before
    (`token_hash_path`) is migrated here, once, as the device "default": the device list is
    written first, then the old file goes, so a crash in between leaves the same token in both
    and the next read only finishes the deletion. An old file whose token the list does not hold
    is refused, never merged by guess. Raises DeviceFileError or OSError."""
    try:
        text: str | None = settings.devices_path.read_text("ascii")
    except FileNotFoundError:
        text = None
    except UnicodeDecodeError as exc:
        raise devices.DeviceFileError(f"not ASCII ({type(exc).__name__})") from exc
    paired = () if text is None else devices.parse(text)
    if not settings.token_hash_path.exists():
        return paired
    legacy = devices.legacy_device(settings.token_hash_path)
    if text is None:
        text = devices.render((legacy,))
        _write_private(settings.devices_path, text)
        paired = devices.parse(text)  # as stored: times to the millisecond
    elif not any(d.sha256 == legacy.sha256 for d in paired):
        raise devices.DeviceFileError(f"{settings.token_hash_path.name} holds a token the device "
                                      "list does not: delete one of the two by hand")
    settings.token_hash_path.unlink()
    return paired


def list_devices(settings: ApiSettings) -> tuple[devices.Device, ...]:
    """The paired devices, for `python -m tickr.api devices` (migrating an old single token)."""
    with _pairing_lock(settings):
        return load_devices(settings)


def revoke_device(settings: ApiSettings, name: str) -> bool:
    """Revoke one device's token: the api server refuses it from the next request on; the other
    devices keep working. False when no device has that name (nothing changes)."""
    with _pairing_lock(settings):
        kept = devices.without(load_devices(settings), name)
        if kept is None:
            return False
        _write_private(settings.devices_path, devices.render(kept))
        return True
