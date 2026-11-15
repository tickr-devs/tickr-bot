"""Strict configuration loader and the trading gate.

- Unknown or missing keys refuse to load (ConfigError).
- Every key in a TOML file must carry a comment (`uncommented_keys`), enforced by tests.
- Environments are plain config tables with identical keys; code never branches on their names.
- `trading_gate` is the single place that decides whether trading requests may be sent.
"""

from __future__ import annotations

import hashlib
import math
import re
import tomllib
import warnings
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, get_type_hints

# The Webull bars route (protocol constant, also `broker.webull.Route.BARS`): the chart's
# request slice is carved from this route's limit, which must be counted per 60 s.
BARS_ROUTE = "/market-data/stocks/bars/list"
SECONDS_PER_MINUTE = 60
# One folder name: no separator, no "..", never absolute (keeps a cache inside [run] data_dir).
PLAIN_FOLDER = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    """The configuration is invalid; the bot must not start."""


class RenamedKeyWarning(UserWarning):
    """A config key was read under its old name; rename it in `config/bot.toml`."""


# Keys renamed after release: (section, old name) -> new name. An old local
# `config/bot.toml` keeps loading with a warning; giving both names is refused. Display keys
# only: a renamed risk or order number stays a loud refusal. Drop an entry once the owner's
# file uses the new name.
RENAMED_KEYS: dict[tuple[str, str], str] = {
    ("api", "watching_check_decimal_places"): "check_decimal_places",
}


def apply_renamed_keys(name: str, table: Any, found: list[str]) -> Any:
    """Return `table` with old key names moved to their new names. Each rename is appended to
    `found` as `[section] old -> new` and warned about; giving both names is refused."""
    if not isinstance(table, dict):
        return table
    table = dict(table)
    for (section, old), new in RENAMED_KEYS.items():
        if section != name or old not in table:
            continue
        if new in table:
            raise ConfigError(f"[{name}] has both {old} (old name) and {new}; keep only {new}")
        rename = f"[{name}] {old} -> {new}"
        warnings.warn(f"old config key name {rename}: rename it in config/bot.toml",
                      RenamedKeyWarning, stacklevel=2)
        found.append(rename)
        table[new] = table.pop(old)
    return table


@dataclass(frozen=True)
class RunConfig:
    environment: str
    data_dir: str
    database_busy_timeout_seconds: Decimal
    backups_dir: str
    maintain_keep_sessions: int
    maintain_archive_max_age_days: int


@dataclass(frozen=True)
class StrategySelection:
    active: str
    observers: tuple[str, ...]


@dataclass(frozen=True)
class RiskConfig:
    planned_loss_usd: Decimal
    position_notional_cap_usd: Decimal
    max_daily_loss_usd: Decimal
    min_daily_room_usd: Decimal
    max_open_positions: int
    max_quote_age_seconds: Decimal
    max_clock_skew_seconds: Decimal
    symbol_cooldown_after_loss_enabled: bool
    symbol_cooldown_minutes_after_loss: int


@dataclass(frozen=True)
class SessionConfig:
    timezone: str
    calendar_file: str
    last_entry_minutes_before_close: int
    flatten_minutes_before_close: int
    flatten_grace_seconds: Decimal
    close_order_cutoff_seconds: Decimal
    half_days_enabled: bool


@dataclass(frozen=True)
class OrdersConfig:
    client_order_id_prefix: str
    probe_client_order_id_prefix: str
    client_order_id_max_length: int
    detail_poll_seconds: Decimal
    stop_retry_attempts: int
    stop_retry_spacing_seconds: Decimal
    detail_reads_per_step: int
    cancel_retry_attempts: int
    cancel_retry_spacing_seconds: Decimal
    cancel_not_sent_alert_seconds: Decimal
    max_unprotected_seconds: Decimal
    detail_unreadable_seconds: Decimal
    max_exit_seconds: Decimal
    shutdown_deadline_seconds: Decimal
    reconcile_interval_seconds: Decimal
    broker_read_max_age_seconds: Decimal
    unknown_absent_reads: int
    stop_held_proof_seconds: Decimal
    reconcile_proof_seconds: Decimal
    reconcile_max_gap_seconds: Decimal


@dataclass(frozen=True)
class ProbeConfig:
    quantity: int
    max_price_usd: Decimal
    stop_percent_below_fill: Decimal
    max_loss_usd: Decimal
    window_start: str
    window_end: str
    entry_wait_seconds: int
    hold_seconds: int
    replace_max_move_usd: Decimal
    replace_observe_seconds: int
    evidence_dir: str
    chart_limit_processes: int
    chart_limit_requests: int
    chart_limit_start_delay_seconds: int
    chart_client_reads: int
    chart_client_interval_seconds: int


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    remote_hosts_file: str
    remote_port: int
    remote_token_hash_file: str
    remote_token_bytes: int
    remote_pairing_file: str
    remote_pairing_code_file: str
    remote_pairing_code_ttl_seconds: int
    demo_port: int
    demo_timeout_seconds: Decimal
    status_file: str
    account_file: str
    account_stale_seconds: Decimal
    commands_dir: str
    status_stale_seconds: Decimal
    broker_read_warn_seconds: Decimal
    poll_seconds: Decimal
    max_request_body_bytes: int
    request_timeout_seconds: Decimal
    max_pending_commands: int
    command_max_age_seconds: Decimal
    status_recent_commands: int
    idempotency_key_ttl_seconds: int
    replay_settings_file: str
    observer_trades_max_rows: int
    watching_max_rows: int
    journal_rows: int
    advisor_runs: int
    watching_closeness_decimal_places: int
    check_decimal_places: int
    flow_change_pct_decimal_places: int
    performance_max_range_days: int
    performance_decimal_places: int
    price_after_minutes: tuple[int, ...]
    after_r_decimal_places: int
    lab_replay_sessions: int
    lab_replay_data_dir: str
    lab_replay_results_dir: str
    lab_replay_niceness: int
    lab_replay_outside_session_only: bool
    trade_whatif_outside_session_only: bool


@dataclass(frozen=True)
class MarketDataConfig:
    discovery_interval_seconds: Decimal
    stream_lost_exit_seconds: Decimal
    allowed_exchanges: tuple[str, ...]
    allowed_currencies: tuple[str, ...]
    screener_category: str
    allowed_security_types: tuple[str, ...]
    profile_symbols_per_request: int
    gainers_losers_rank_type: str
    top_actives_rank_type: str
    snapshot_symbols_per_request: int
    bars_symbols_per_request: int
    bars_per_symbol: int
    history_bars_per_request: int
    history_max_pages_per_symbol: int
    history_max_lookback_days: int
    history_sessions: int
    candidate_subscriptions: int
    observer_subscriptions: int
    observer_resync_seconds: Decimal
    stream_reserved_subscribe_requests: int
    candidate_retry_seconds: Decimal
    history_dir: str
    history_cache_max_age_days: int
    stream_reconnect_delay_seconds: Decimal
    stream_max_symbols: int
    stream_reserved_symbols: int
    quiet_after_seconds: Decimal
    capture_dir: str


@dataclass(frozen=True)
class ChartConfig:
    webull_enabled: bool
    requests_per_minute: int
    bars_per_request: int
    live_ttl_seconds: Decimal
    closed_day_settle_seconds: Decimal
    cache_dir: str
    cache_max_age_days: int
    session_lookback_days: int


@dataclass(frozen=True)
class ObserversConfig:
    strategy_modules: tuple[str, ...]
    max_observers: int
    max_evaluation_seconds: Decimal
    queue_size: int
    watching_seconds: Decimal
    decisions_dir: str


@dataclass(frozen=True)
class AdvisorConfig:
    api_key_variable: str
    model: str
    effort: str
    max_output_tokens: int
    request_timeout_seconds: int
    min_days: int
    max_changes: int
    max_evidence_bytes: int
    proposals_dir: str
    backtest_min_fitting_trades: int
    backtest_price_edges_usd: tuple[Decimal, ...]
    backtest_stop_distance_edges_percent: tuple[Decimal, ...]
    backtest_excursion_edges_r: tuple[Decimal, ...]


@dataclass(frozen=True)
class RuntimeConfig:
    min_free_disk_mb: int
    supervisor_poll_seconds: Decimal
    bot_exit_timeout_seconds: int
    stop_alert_after_seconds: int
    preflight_start_local: str
    bot_start_local: str
    launch_weekdays: tuple[int, ...]
    watchdog_start_local: str
    nightly_fetch_start_local: str
    nightly_fetch_out: str
    nightly_fetch_seed: str
    watchdog_after_flatten_seconds: int
    watchdog_before_close_seconds: int
    watchdog_bot_check_seconds: int
    watchdog_status_stale_seconds: int
    watchdog_journal_file: str
    watchdog_restart_before_flatten_seconds: int
    watchdog_restart_command: tuple[str, ...]
    watchdog_restart_timeout_seconds: int
    watchdog_restart_file: str
    evaluation_budget_seconds: Decimal
    stop_marker_file: str


@dataclass(frozen=True)
class AlertsConfig:
    dedupe_window_seconds: Decimal
    notification_timeout_seconds: Decimal
    fence_alert_after_seconds: Decimal
    imessage_recipient_variable: str
    mac_app: str
    mac_app_timeout_seconds: Decimal


@dataclass(frozen=True)
class EnvironmentConfig:
    trading_enabled: bool
    requires_owner_authorization: bool
    authorization_file: str
    http_host: str
    http_port: int
    mqtt_host: str
    mqtt_port: int
    events_host: str
    app_key_variable: str
    app_secret_variable: str
    account_variable: str
    token_dir: str
    requests_per_route_per_minute: int
    rate_limit_cooldown_seconds: Decimal
    region_id: str
    http_connect_timeout_seconds: int
    http_read_timeout_seconds: int
    token_check_duration_seconds: int
    token_check_interval_seconds: int
    open_orders_max_pages: int
    route_limits: dict[str, tuple[int, Decimal]]

    @property
    def hosts(self) -> frozenset[str]:
        """Every host this environment may contact (the outbound allowlist)."""
        return frozenset({self.http_host, self.mqtt_host, self.events_host})


@dataclass(frozen=True)
class Config:
    run: RunConfig
    strategy: StrategySelection
    risk: RiskConfig
    session: SessionConfig
    orders: OrdersConfig
    probe: ProbeConfig
    api: ApiConfig
    runtime: RuntimeConfig
    alerts: AlertsConfig
    market_data: MarketDataConfig
    chart: ChartConfig
    observers: ObserversConfig
    advisor: AdvisorConfig
    environments: dict[str, EnvironmentConfig]
    root: Path
    digest: str
    # Old key names read under their new names (`[section] old -> new`); preflight reports them.
    renamed_keys: tuple[str, ...] = ()

    @property
    def environment_name(self) -> str:
        return self.run.environment

    @property
    def environment(self) -> EnvironmentConfig:
        return self.environments[self.run.environment]


_SECTIONS: dict[str, type] = {
    "run": RunConfig,
    "strategy": StrategySelection,
    "risk": RiskConfig,
    "session": SessionConfig,
    "orders": OrdersConfig,
    "probe": ProbeConfig,
    "api": ApiConfig,
    "runtime": RuntimeConfig,
    "alerts": AlertsConfig,
    "market_data": MarketDataConfig,
    "chart": ChartConfig,
    "observers": ObserversConfig,
    "advisor": AdvisorConfig,
}


def _coerce(where: str, value: Any, target: Any) -> Any:
    if target is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
        return value
    if target is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        return value
    if target is Decimal:
        if (isinstance(value, bool) or not isinstance(value, int | float)
                or not math.isfinite(value)):
            raise ConfigError(f"{where}: expected a finite number, got {value!r}")
        return Decimal(str(value))
    if target is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {value!r}")
        return value
    if target == tuple[int, ...]:
        if not isinstance(value, list) or not all(
                isinstance(v, int) and not isinstance(v, bool) for v in value):
            raise ConfigError(f"{where}: expected a list of integers, got {value!r}")
        return tuple(value)
    if target == tuple[Decimal, ...]:
        if not isinstance(value, list) or not value or not all(
                isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
                for v in value):
            raise ConfigError(f"{where}: expected a non-empty list of finite numbers, got "
                              f"{value!r}")
        numbers = tuple(Decimal(str(v)) for v in value)
        if any(b <= a for a, b in zip(numbers, numbers[1:], strict=False)):
            raise ConfigError(f"{where}: expected strictly increasing numbers, got {value!r}")
        return numbers
    if target == tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
        return tuple(value)
    if target == dict[str, tuple[int, Decimal]]:
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a table of route limits, got {value!r}")
        limits: dict[str, tuple[int, Decimal]] = {}
        for route, pair in value.items():
            if (not isinstance(pair, list) or len(pair) != 2
                    or isinstance(pair[0], bool) or not isinstance(pair[0], int)
                    or isinstance(pair[1], bool) or not isinstance(pair[1], int | float)
                    or pair[0] <= 0 or pair[1] <= 0):
                raise ConfigError(
                    f"{where} {route!r}: expected [requests > 0, window seconds > 0], got {pair!r}")
            limits[route] = (pair[0], Decimal(str(pair[1])))
        return limits
    raise ConfigError(f"{where}: unsupported type {target!r}")  # pragma: no cover


def build_section(name: str, table: Any, cls: type) -> Any:
    """Build a frozen dataclass from a TOML table, refusing unknown and missing keys."""
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] must be a table")
    hints = get_type_hints(cls)
    expected = {f.name for f in fields(cls)}
    unknown = sorted(set(table) - expected)
    missing = sorted(expected - set(table))
    if unknown:
        raise ConfigError(f"[{name}] unknown keys: {', '.join(unknown)}")
    if missing:
        raise ConfigError(f"[{name}] missing keys: {', '.join(missing)}")
    values = {k: _coerce(f"[{name}] {k}", table[k], hints[k]) for k in expected}
    return cls(**values)


def load(path: Path | str, root: Path | str | None = None) -> Config:
    """Load and validate `config/bot.toml`. `root` is the repo root for relative paths."""
    path = Path(path)
    raw_bytes = path.read_bytes()
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    expected_tables = set(_SECTIONS) | {"environments"}
    unknown = sorted(set(data) - expected_tables)
    missing = sorted(expected_tables - set(data))
    if unknown:
        raise ConfigError(f"unknown sections: {', '.join(unknown)}")
    if missing:
        raise ConfigError(f"missing sections: {', '.join(missing)}")

    renamed: list[str] = []
    built = {name: build_section(name, apply_renamed_keys(name, data[name], renamed), cls)
             for name, cls in _SECTIONS.items()}

    envs_raw = data["environments"]
    if not isinstance(envs_raw, dict) or not envs_raw:
        raise ConfigError("[environments] must define at least one environment")
    environments = {
        name: build_section(f"environments.{name}", table, EnvironmentConfig)
        for name, table in envs_raw.items()
    }

    run: RunConfig = built["run"]
    if run.environment not in environments:
        raise ConfigError(
            f"[run] environment {run.environment!r} is not one of: {', '.join(environments)}"
        )
    if run.maintain_keep_sessions < 1:
        raise ConfigError("[run] maintain_keep_sessions must be at least 1 (today is always kept)")
    if run.maintain_archive_max_age_days < 0:
        raise ConfigError("[run] maintain_archive_max_age_days must be 0 (never delete archives) "
                          "or a number of days")
    md: MarketDataConfig = built["market_data"]
    if md.history_cache_max_age_days < 1:
        raise ConfigError("[market_data] history_cache_max_age_days must be at least 1 (today's "
                          "cache is in use)")
    if (md.candidate_subscriptions + md.observer_subscriptions
            > md.stream_max_symbols - md.stream_reserved_symbols):
        raise ConfigError(
            "[market_data] candidate_subscriptions plus observer_subscriptions must leave "
            "stream_reserved_symbols free within stream_max_symbols"
        )
    if built["observers"].watching_seconds <= 0:
        raise ConfigError("[observers] watching_seconds must be above 0")
    risk: RiskConfig = built["risk"]
    if risk.symbol_cooldown_minutes_after_loss < 1:
        raise ConfigError("[risk] symbol_cooldown_minutes_after_loss must be at least 1; to "
                          "turn the cooldown off set symbol_cooldown_after_loss_enabled = false")
    if not 0 <= risk.min_daily_room_usd < risk.max_daily_loss_usd:
        raise ConfigError("[risk] min_daily_room_usd must be >= 0 and below max_daily_loss_usd")
    if built["orders"].detail_unreadable_seconds <= 0:
        raise ConfigError("[orders] detail_unreadable_seconds must be above 0")
    if built["orders"].stop_held_proof_seconds <= 0:
        raise ConfigError("[orders] stop_held_proof_seconds must be above 0")
    if built["orders"].reconcile_proof_seconds <= 0:
        raise ConfigError("[orders] reconcile_proof_seconds must be above 0")
    if not built["orders"].reconcile_max_gap_seconds > built["orders"].reconcile_proof_seconds:
        raise ConfigError("[orders] reconcile_max_gap_seconds must be above "
                          "reconcile_proof_seconds")
    if built["api"].account_stale_seconds <= built["orders"].reconcile_interval_seconds:
        raise ConfigError("[api] account_stale_seconds must be above [orders] "
                          "reconcile_interval_seconds (the account file is written per reconcile)")
    chart: ChartConfig = built["chart"]
    if chart.requests_per_minute < 1 or chart.bars_per_request < 1:
        raise ConfigError("[chart] requests_per_minute and bars_per_request must be at least 1")
    if chart.closed_day_settle_seconds < 0:
        raise ConfigError("[chart] closed_day_settle_seconds must not be negative")
    if chart.live_ttl_seconds <= 0 or chart.session_lookback_days < 1:
        raise ConfigError("[chart] live_ttl_seconds must be above 0 and session_lookback_days "
                          "at least 1")
    if not PLAIN_FOLDER.match(chart.cache_dir):
        raise ConfigError("[chart] cache_dir must be one plain folder name under [run] data_dir "
                          f"(letters, digits, '_' and '-'), not {chart.cache_dir!r}")
    if chart.cache_max_age_days < chart.session_lookback_days:
        raise ConfigError("[chart] cache_max_age_days must be at least session_lookback_days (a "
                          "pruned day a 1d or 5d range still reads is read again from Webull)")
    if chart.webull_enabled:
        for name, env in environments.items():
            check_chart_slice(chart, name, env)
    orders, api = built["orders"], built["api"]
    if not orders.reconcile_interval_seconds < api.broker_read_warn_seconds \
            < orders.broker_read_max_age_seconds:
        raise ConfigError("[api] broker_read_warn_seconds must be above [orders] "
                          "reconcile_interval_seconds and below [orders] "
                          "broker_read_max_age_seconds")
    strategy: StrategySelection = built["strategy"]
    if strategy.active in strategy.observers:
        raise ConfigError("[strategy] the active strategy cannot also be an observer")
    repeated = sorted({name for name in strategy.observers if strategy.observers.count(name) > 1})
    if repeated:
        raise ConfigError(f"[strategy] observers lists {', '.join(repeated)} more than once")

    return Config(
        **built,
        environments=environments,
        root=Path(root) if root is not None else path.resolve().parent.parent,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
        renamed_keys=tuple(renamed),
    )


def check_chart_slice(chart: ChartConfig, name: str, env: EnvironmentConfig) -> None:
    """With the chart on, the bot's bars limit in `env` loses `[chart] requests_per_minute` per
    60 s to the API: refuse a bars limit over another window or one the slice would empty."""
    count, window = env.route_limits.get(BARS_ROUTE,
                                         (env.requests_per_route_per_minute, SECONDS_PER_MINUTE))
    if window != SECONDS_PER_MINUTE:
        raise ConfigError(f"[chart] needs the bars route limit of [environments.{name}] per "
                          f"{SECONDS_PER_MINUTE} s to carve its slice from, not per {window} s")
    if chart.requests_per_minute >= count:
        raise ConfigError(f"[chart] requests_per_minute must be below the bars route limit of "
                          f"[environments.{name}] ({count} per {SECONDS_PER_MINUTE} s)")


_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_\-\"\.]+)\s*=")


def _has_trailing_comment(value: str) -> bool:
    """True if a `#` appears outside any quoted string in the value part of a line."""
    quote: str | None = None
    escaped = False
    for char in value:
        if quote is None:
            if char == "#":
                return True
            if char in "\"'":
                quote = char
        elif escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif char == quote:
            quote = None
    return False


def uncommented_keys(text: str) -> list[tuple[int, str]]:
    """Return (line number, key) for every key without a comment on its line or the line above.

    A comment on the line above may be part of a block of consecutive comment lines.
    """
    problems: list[tuple[int, str]] = []
    previous = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        match = _KEY_LINE.match(line)
        if match and not stripped.startswith("#"):
            inline = _has_trailing_comment(line.split("=", 1)[1])
            if not inline and not previous.startswith("#"):
                problems.append((number, match.group(1)))
        if stripped:
            previous = stripped
    return problems


@dataclass(frozen=True)
class GateResult:
    open: bool
    cause: str


def _authorization_valid(config: Config, env: EnvironmentConfig) -> GateResult:
    if not env.authorization_file:
        return GateResult(False, "owner authorization required but no authorization_file is set")
    path = config.root / env.authorization_file
    if not path.is_file():
        return GateResult(False, f"owner authorization file not found: {env.authorization_file}")
    try:
        record = tomllib.loads(path.read_text("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return GateResult(False, "owner authorization file is not valid TOML")
    required = {"environment", "config_digest", "approved_on", "statement"}
    if set(record) != required:
        return GateResult(False, "owner authorization file must have exactly: " + ", ".join(
            sorted(required)))
    if record["environment"] != config.environment_name:
        return GateResult(False, "owner authorization is for another environment")
    if record["config_digest"] != config.digest:
        return GateResult(False, "owner authorization does not match the current config digest")
    return GateResult(True, "owner authorization valid")


def trading_gate(config: Config) -> GateResult:
    """The single decision whether this process may send trading requests."""
    env = config.environment
    if not env.trading_enabled:
        return GateResult(False, f"trading is disabled for environment {config.environment_name!r}")
    if env.requires_owner_authorization:
        return _authorization_valid(config, env)
    return GateResult(True, "trading enabled by configuration")
