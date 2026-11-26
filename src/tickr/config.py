"""Strict configuration loader and the trading gate.

- Unknown or missing keys refuse to load (ConfigError).
- Every key in a TOML file must carry a comment (`uncommented_keys`), enforced by tests.
- Environments are plain config tables with identical keys; code never branches on their names.
- `trading_gate` is the single place that decides whether trading requests may be sent.
"""

from __future__ import annotations

import hashlib
import math
import os
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
HISTORY_ROUTE = "/trading/orders/historical-orders/list"
SECONDS_PER_MINUTE = 60
PERCENT_WHOLE = 100  # unit factor: a whole in percent
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
    code_version_timeout_seconds: Decimal
    backups_dir: str
    backup_retry_seconds: Decimal
    maintain_keep_sessions: int
    maintain_archive_max_age_days: int
    diagnostics_days: int
    diagnostics_logs_dir: str


@dataclass(frozen=True)
class StrategySelection:
    active: str
    observers: tuple[str, ...]
    extended_hours_strategies: tuple[str, ...]


@dataclass(frozen=True)
class RiskConfig:
    planned_loss_usd: Decimal
    position_notional_cap_usd: Decimal
    max_daily_loss_usd: Decimal
    min_daily_room_usd: Decimal
    buying_power_max_age_seconds: Decimal
    max_open_positions: int
    max_working_entries: int
    max_quote_age_seconds: Decimal
    max_clock_skew_seconds: Decimal
    symbol_cooldown_after_loss_enabled: bool
    symbol_cooldown_minutes_after_loss: int
    max_entry_rest_bars: int
    entry_snapshot_interval_seconds: Decimal


@dataclass(frozen=True)
class SessionConfig:
    timezone: str
    calendar_file: str
    last_entry_minutes_before_close: int
    flatten_minutes_before_close: int
    flatten_grace_seconds: Decimal
    close_order_cutoff_seconds: Decimal
    half_days_enabled: bool
    premarket_enabled: bool
    premarket_first_entry_minutes_after_open: int
    premarket_last_entry_minutes_before_close: int
    premarket_flatten_minutes_before_close: int
    premarket_size_fraction: Decimal
    postmarket_enabled: bool
    postmarket_first_entry_minutes_after_open: int
    postmarket_last_entry_minutes_before_close: int
    postmarket_flatten_minutes_before_close: int
    postmarket_size_fraction: Decimal
    premarket_max_quote_age_seconds: Decimal
    postmarket_max_quote_age_seconds: Decimal
    premarket_max_spread_ratio: Decimal
    postmarket_max_spread_ratio: Decimal
    extended_stop_confirm_checks: int
    extended_watch_max_gap_seconds: Decimal
    extended_blind_seconds: Decimal


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
    max_stops_per_trade: int
    stop_listing_polls: int
    stop_split_tick_share: Decimal
    history_fill_poll_seconds: Decimal
    history_fill_max_pages: int
    detail_unreadable_seconds: Decimal
    max_exit_seconds: Decimal
    shutdown_deadline_seconds: Decimal
    reconcile_interval_seconds: Decimal
    broker_read_max_age_seconds: Decimal
    unknown_absent_reads: int
    stop_held_proof_seconds: Decimal
    reconcile_proof_seconds: Decimal
    reconcile_max_gap_seconds: Decimal
    balance_read_seconds: Decimal
    fees_unknown_alert_seconds: Decimal


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
    extended_end_margin_minutes: int
    extended_max_quote_age_seconds: Decimal
    extended_far_stop_percent: Decimal
    extended_stop_limit_percent: Decimal
    extended_observe_seconds: int
    extended_trigger_seconds: int
    extended_close_percent: Decimal
    extended_position_agree_seconds: int
    attached_far_percent: Decimal
    attached_observe_seconds: int
    attached_stop_wait_seconds: int
    trailing_wide_percent: Decimal
    trailing_tight_percents: tuple[Decimal, ...]
    trailing_amount_steps_usd: tuple[Decimal, ...]
    trailing_observe_seconds: int
    trailing_trigger_seconds: int
    trailing_close_percent: Decimal
    trailing_unit_factor: Decimal
    history_wait_seconds: int
    listing_stops: int
    listing_far_percent: Decimal
    listing_wait_seconds: int
    tick_connect_wait_seconds: int
    tick_flow_wait_seconds: int
    tick_cap_steps: tuple[int, ...]
    tick_step_wait_seconds: int
    history_max_pages: int
    paging_orders: int
    paging_far_percent: Decimal
    paging_send_interval_seconds: int
    open_orders_paging_orders: int
    open_orders_end_margin_minutes: int
    trade_events_minutes: int
    trade_events_reconnect_seconds: int
    prev_day_window_start: str
    prev_day_window_end: str
    prev_day_poll_seconds: int
    prev_day_tick_prefixes: tuple[str, ...]


# [api] system_services values: the machine services GET /system can report (docs/api.md).
SYSTEM_SERVICES_MAC = "mac"
SYSTEM_SERVICES = (SYSTEM_SERVICES_MAC, "none")


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    remote_hosts_file: str
    remote_port: int
    remote_token_hash_file: str
    remote_devices_file: str
    remote_simulator_token_hash_file: str
    remote_simulator_token_file: str
    remote_token_bytes: int
    remote_pairing_file: str
    remote_pairing_code_file: str
    remote_pairing_code_ttl_seconds: int
    demo_port: int
    status_file: str
    account_file: str
    account_stale_seconds: Decimal
    commands_dir: str
    status_stale_seconds: Decimal
    broker_read_warn_seconds: Decimal
    live_quote_max_age_seconds: Decimal
    quotes_file: str
    quotes_write_seconds: Decimal
    quotes_poll_seconds: Decimal
    poll_seconds: Decimal
    max_request_body_bytes: int
    request_timeout_seconds: Decimal
    max_pending_commands: int
    command_max_age_seconds: Decimal
    status_recent_commands: int
    idempotency_key_ttl_seconds: int
    replay_settings_file: str
    settings_schema_file: str
    observer_trades_max_rows: int
    journal_rows: int
    start_bot_command: tuple[str, ...]
    start_bot_timeout_seconds: Decimal
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
    next_session_search_days: int
    system_services: str
    sleep_check_command: tuple[str, ...]
    sleep_check_timeout_seconds: Decimal
    system_restart_rows: int
    training_progress_file: str
    settled_cache_dir: str
    settled_memory_days: int
    settled_disk_days: int


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
    premarket_gainers_losers_rank_type: str
    postmarket_gainers_losers_rank_type: str
    top_actives_rank_type: str
    snapshot_symbols_per_request: int
    bars_symbols_per_request: int
    bars_per_symbol: int
    discovery_bars_reserve_requests: int
    discovery_budget_wait_seconds: Decimal
    history_bars_per_request: int
    history_max_pages_per_symbol: int
    history_max_lookback_days: int
    history_sessions: int
    history_memory_symbols: int
    candidate_subscriptions: int
    observer_subscriptions: int
    observer_resync_seconds: Decimal
    stream_reserved_subscribe_requests: int
    candidate_retry_seconds: Decimal
    refused_symbol_recheck_seconds: Decimal
    failure_detail_max_chars: int
    history_dir: str
    history_cache_max_age_days: int
    history_preload_minutes_before_open: int
    screener_record_seconds: Decimal
    profile_refresh_seconds: Decimal
    profile_refresh_gainer_move_percent: Decimal
    profile_max_requests_per_round: int
    profile_reserve_requests: int
    history_preload_retry_seconds: Decimal
    stream_reconnect_delay_seconds: Decimal
    stream_max_symbols: int
    stream_reserved_symbols: int
    quiet_after_seconds: Decimal
    capture_dir: str


@dataclass(frozen=True)
class ChartConfig:
    webull_enabled: bool
    requests_per_minute: int
    quiet_requests_per_minute: int
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
    max_slow_evaluations_in_row: int
    queue_size: int
    watching_seconds: Decimal
    decisions_dir: str
    forget_symbol_seconds: Decimal
    history_sends_per_pass: int
    ignition: str


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
class TrailingStopConfig:
    """Stop tightening by replace (backlog T1), read by `risk.trailing`; not in the loop yet."""

    enabled: bool
    atr_multiple: Decimal
    breakeven_after_r: Decimal
    min_move_usd: Decimal


@dataclass(frozen=True)
class ModelConfig:
    enabled: bool
    bundles_dir: str
    bundle: str


@dataclass(frozen=True)
class ExitChaseConfig:
    """Limit-exit chase before a market exit (backlog X3; tickr.engine.chase)."""

    enabled: bool
    ticks_through_bid: int
    reprice_seconds: Decimal
    max_reprices: int
    max_worse_atr: Decimal


@dataclass(frozen=True)
class MassiveConfig:
    api_host: str
    stream_host: str
    stream_path: str
    files_host: str
    files_bucket: str
    files_region: str
    minute_aggs_prefix: str
    api_key_variable: str
    files_key_id_variable: str
    files_secret_variable: str
    history_dir: str
    stock_types: tuple[str, ...]
    http_timeout_seconds: int
    page_limit: int
    max_pages: int
    stream_max_message_bytes: int
    stream_reconnect_seconds: Decimal
    calendar_lookback_days: int
    massive_news_path: str
    benzinga_news_path: str
    benzinga_earnings_path: str
    news_probe_poll_seconds: int
    news_probe_news_lookback_seconds: int
    news_probe_earnings_lookback_days: int
    news_probe_dir: str
    arrival_dir: str
    arrival_flush_seconds: Decimal
    arrival_loop_seconds: Decimal
    arrival_tail_seconds: int
    arrival_report_points_seconds: tuple[Decimal, ...]
    stream_day_dir: str


@dataclass(frozen=True)
class RuntimeConfig:
    min_free_disk_mb: int
    disk_clear_free_mb: int
    disk_warn_free_mb: int
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
    watchdog_state_file: str
    watchdog_start_grace_seconds: int
    watchdog_relaunch_seconds: int
    watchdog_restart_before_flatten_seconds: int
    watchdog_restart_command: tuple[str, ...]
    watchdog_restart_timeout_seconds: int
    watchdog_restart_file: str
    watchdog_entry_restart_file: str
    evaluation_budget_seconds: Decimal
    discovery_stop_seconds: Decimal
    stop_marker_file: str
    preflight_result_file: str
    release_step_timeout_seconds: int
    release_stop_wait_seconds: int
    release_poll_seconds: Decimal


@dataclass(frozen=True)
class AlertsConfig:
    dedupe_window_seconds: Decimal
    notification_timeout_seconds: Decimal
    fence_alert_after_seconds: Decimal
    imessage_recipient_variable: str
    mac_app: str
    mac_app_timeout_seconds: Decimal
    feed_file: str


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
    events_port: int
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
    history_max_pages: int
    cash_activities_max_pages: int
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
    model: ModelConfig
    massive: MassiveConfig
    exit_chase: ExitChaseConfig
    trailing_stop: TrailingStopConfig
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
    "model": ModelConfig,
    "massive": MassiveConfig,
    "exit_chase": ExitChaseConfig,
    "trailing_stop": TrailingStopConfig,
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



EXTENDED_SESSIONS = ("premarket", "postmarket")


def check_extended_sessions(session: SessionConfig) -> None:
    """Refuse an extended session turned on before the path that trades it exists (it would
    be a silent no-op), and offsets or a size the windows could never use. Whether the offsets
    fit the day's session length is checked by `risk.timers.trading_windows`, which has the
    calendar."""
    for name in EXTENDED_SESSIONS:
        if getattr(session, f"{name}_enabled"):
            raise ConfigError(f"[session] {name}_enabled = true is not supported yet: nothing "
                              f"trades that session (docs/decisions.md 2026-11-16)")
        first = getattr(session, f"{name}_first_entry_minutes_after_open")
        last = getattr(session, f"{name}_last_entry_minutes_before_close")
        flatten = getattr(session, f"{name}_flatten_minutes_before_close")
        if not (first >= 0 and last > flatten > 0):
            raise ConfigError(f"[session] {name}: needs first_entry_minutes_after_open >= 0 and "
                              f"last_entry_minutes_before_close > "
                              f"flatten_minutes_before_close > 0")
        fraction = getattr(session, f"{name}_size_fraction")
        if not 0 < fraction <= 1:
            raise ConfigError(f"[session] {name}_size_fraction must be above 0 and at most 1")
        if getattr(session, f"{name}_max_quote_age_seconds") <= 0:
            raise ConfigError(f"[session] {name}_max_quote_age_seconds must be above 0")
        if not 0 < getattr(session, f"{name}_max_spread_ratio") < 1:
            raise ConfigError(f"[session] {name}_max_spread_ratio must be above 0 and below 1")
    if session.extended_stop_confirm_checks < 1:
        raise ConfigError("[session] extended_stop_confirm_checks must be at least 1")
    if not 0 < session.extended_watch_max_gap_seconds < session.extended_blind_seconds:
        raise ConfigError("[session] needs 0 < extended_watch_max_gap_seconds < "
                          "extended_blind_seconds")


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
    if run.diagnostics_days < 1:
        raise ConfigError("[run] diagnostics_days must be at least 1")
    if not PLAIN_FOLDER.match(run.diagnostics_logs_dir):
        raise ConfigError("[run] diagnostics_logs_dir must be one plain folder name under "
                          "data_dir (letters, digits, '_' and '-'), "
                          f"not {run.diagnostics_logs_dir!r}")
    if run.code_version_timeout_seconds <= 0:
        raise ConfigError("[run] code_version_timeout_seconds must be above 0")
    if run.backup_retry_seconds <= 0:
        # 0 would try (and journal backup-failed) on every loop pass while the disk fails.
        raise ConfigError("[run] backup_retry_seconds must be above 0")
    md: MarketDataConfig = built["market_data"]
    if (md.profile_refresh_seconds <= 0 or md.profile_refresh_gainer_move_percent <= 0
            or md.profile_max_requests_per_round < 1 or md.profile_reserve_requests < 0):
        raise ConfigError("[market_data] profile_refresh_seconds and "
                          "profile_refresh_gainer_move_percent must be above 0, "
                          "profile_max_requests_per_round at least 1 and "
                          "profile_reserve_requests at least 0")
    if not 0 <= md.discovery_budget_wait_seconds < md.discovery_interval_seconds:
        raise ConfigError("[market_data] discovery_budget_wait_seconds must be at least 0 and "
                          "below discovery_interval_seconds")
    if md.refused_symbol_recheck_seconds <= 0:
        raise ConfigError("[market_data] refused_symbol_recheck_seconds must be above 0")
    if md.failure_detail_max_chars < 1:
        raise ConfigError("[market_data] failure_detail_max_chars must be at least 1")
    if md.history_memory_symbols < 1:
        raise ConfigError("[market_data] history_memory_symbols must be at least 1")
    if md.history_cache_max_age_days < 1:
        raise ConfigError("[market_data] history_cache_max_age_days must be at least 1 (today's "
                          "cache is in use)")
    if (md.history_preload_minutes_before_open < 0 or md.screener_record_seconds <= 0
            or md.history_preload_retry_seconds <= 0):
        raise ConfigError("[market_data] history_preload_minutes_before_open must be at least 0, "
                          "screener_record_seconds and history_preload_retry_seconds "
                          "above 0")
    if (md.candidate_subscriptions + md.observer_subscriptions
            > md.stream_max_symbols - md.stream_reserved_symbols):
        raise ConfigError(
            "[market_data] candidate_subscriptions plus observer_subscriptions must leave "
            "stream_reserved_symbols free within stream_max_symbols"
        )
    if built["observers"].watching_seconds <= 0:
        raise ConfigError("[observers] watching_seconds must be above 0")
    if built["observers"].forget_symbol_seconds <= 0:
        raise ConfigError("[observers] forget_symbol_seconds must be above 0")
    if built["observers"].max_slow_evaluations_in_row < 1:
        raise ConfigError("[observers] max_slow_evaluations_in_row must be at least 1")
    if built["observers"].history_sends_per_pass < 1:
        raise ConfigError("[observers] history_sends_per_pass must be at least 1")
    model: ModelConfig = built["model"]
    if model.enabled:
        raise ConfigError("[model] enabled must stay false: the trained model is not wired into "
                          "the trading loop yet (the bundle loader and the shared feature code "
                          "are; the live data feed and the decision path follow)")
    trailing: TrailingStopConfig = built["trailing_stop"]
    if trailing.enabled:
        raise ConfigError("[trailing_stop] enabled must stay false: stop replacement is not in "
                          "the trading loop yet (it waits for the Webull replace check, "
                          "docs/webull-facts.md F5)")
    if trailing.atr_multiple <= 0 or trailing.breakeven_after_r <= 0:
        raise ConfigError("[trailing_stop] atr_multiple and breakeven_after_r must be above 0")
    if trailing.min_move_usd < 0:
        raise ConfigError("[trailing_stop] min_move_usd must be at least 0 (0 = no minimum "
                          "step)")
    massive: MassiveConfig = built["massive"]
    if (massive.http_timeout_seconds < 1 or massive.page_limit < 1 or massive.max_pages < 1
            or massive.stream_max_message_bytes < 1 or massive.stream_reconnect_seconds <= 0
            or massive.calendar_lookback_days < 1
            or not massive.stock_types):
        raise ConfigError("[massive] timeouts, page sizes and the message limit must be at least "
                          "1, stream_reconnect_seconds above 0, stock_types not empty")
    points = massive.arrival_report_points_seconds
    if (massive.arrival_flush_seconds <= 0 or massive.arrival_loop_seconds <= 0
            or massive.arrival_tail_seconds < 0 or points[0] < 0
            or list(points) != sorted(set(points))):
        raise ConfigError("[massive] arrival_flush_seconds and arrival_loop_seconds must be above "
                          "0, arrival_tail_seconds at least 0, arrival_report_points_seconds "
                          "rising and not below 0")
    chase: ExitChaseConfig = built["exit_chase"]
    if (chase.ticks_through_bid < 0 or chase.reprice_seconds <= 0 or chase.max_reprices < 0
            or chase.max_worse_atr <= 0):
        raise ConfigError("[exit_chase] ticks_through_bid and max_reprices must be at least 0, "
                          "reprice_seconds and max_worse_atr above 0")
    if chase.enabled:
        raise ConfigError("[exit_chase] enabled must stay false: the chase is not wired to a "
                          "live bid and ATR yet (backlog X3), and its numbers are old-tracker "
                          "proposals the owner has not reconfirmed")
    risk: RiskConfig = built["risk"]
    if risk.symbol_cooldown_minutes_after_loss < 1:
        raise ConfigError("[risk] symbol_cooldown_minutes_after_loss must be at least 1; to "
                          "turn the cooldown off set symbol_cooldown_after_loss_enabled = false")
    probe: ProbeConfig = built["probe"]
    if (probe.trailing_wide_percent <= 0 or not probe.trailing_tight_percents
            or not probe.trailing_amount_steps_usd
            or min(probe.trailing_tight_percents + probe.trailing_amount_steps_usd) <= 0
            or probe.trailing_close_percent <= 0 or probe.trailing_observe_seconds <= 0
            or probe.trailing_trigger_seconds <= 0 or probe.trailing_unit_factor <= 1
            or probe.history_wait_seconds <= 0 or probe.listing_stops < 1
            or probe.listing_far_percent <= 0 or probe.listing_wait_seconds <= 0):
        raise ConfigError("[probe] trailing_*, history_wait_seconds and listing_* values must be "
                          "above 0 and trailing_unit_factor above 1")
    if max(probe.trailing_tight_percents) >= probe.trailing_wide_percent:
        raise ConfigError("[probe] trailing_tight_percents must be below trailing_wide_percent")
    if (probe.tick_connect_wait_seconds <= 0 or probe.tick_flow_wait_seconds <= 0
            or probe.tick_step_wait_seconds < 0 or not probe.tick_cap_steps
            or min(probe.tick_cap_steps) < 1):
        raise ConfigError("[probe] tick_* waits must be above 0 (tick_step_wait_seconds at "
                          "least 0) and tick_cap_steps at least 1")
    if probe.history_max_pages < 1:
        raise ConfigError("[probe] history_max_pages must be at least 1")
    if (probe.paging_orders < 1 or probe.open_orders_paging_orders < 1
            or probe.open_orders_end_margin_minutes < 1
            or not 0 < probe.paging_far_percent < PERCENT_WHOLE
            or probe.paging_send_interval_seconds <= 0):
        raise ConfigError("[probe] paging_orders, open_orders_paging_orders, "
                          "open_orders_end_margin_minutes and paging_send_interval_seconds "
                          "must be above "
                          "0 and paging_far_percent between 0 and 100")
    for name, values in (("trailing_tight_percents", probe.trailing_tight_percents),
                         ("trailing_amount_steps_usd", probe.trailing_amount_steps_usd),
                         ("tick_cap_steps", probe.tick_cap_steps)):
        if list(values) != sorted(set(values)):
            raise ConfigError(f"[probe] {name} must be ascending, without repeats")
    if (probe.prev_day_poll_seconds < 1 or not probe.prev_day_tick_prefixes
            or not all(re.fullmatch(r"\d{2}:\d{2}", w) for w in (
                probe.prev_day_window_start, probe.prev_day_window_end))):
        raise ConfigError("[probe] prev_day_window_start/end must be HH:MM, "
                          "prev_day_poll_seconds at least 1, prev_day_tick_prefixes not empty")
    if not 1 <= risk.max_open_positions <= md.stream_reserved_symbols:
        raise ConfigError("[risk] max_open_positions must be at least 1 and at most "
                          "[market_data] stream_reserved_symbols (a stream slot per position)")
    if risk.max_working_entries < 1:
        raise ConfigError("[risk] max_working_entries must be at least 1")
    check_extended_sessions(built["session"])
    if risk.max_entry_rest_bars < 1:
        raise ConfigError("[risk] max_entry_rest_bars must be at least 1")
    if risk.entry_snapshot_interval_seconds <= 0:
        raise ConfigError("[risk] entry_snapshot_interval_seconds must be above 0")
    if not 0 <= risk.min_daily_room_usd < risk.max_daily_loss_usd:
        raise ConfigError("[risk] min_daily_room_usd must be >= 0 and below max_daily_loss_usd")
    if risk.buying_power_max_age_seconds <= 0:
        raise ConfigError("[risk] buying_power_max_age_seconds must be above 0")
    if built["orders"].detail_unreadable_seconds <= 0:
        raise ConfigError("[orders] detail_unreadable_seconds must be above 0")
    if built["orders"].stop_held_proof_seconds <= 0:
        raise ConfigError("[orders] stop_held_proof_seconds must be above 0")
    if built["orders"].balance_read_seconds <= 0:
        raise ConfigError("[orders] balance_read_seconds must be above 0")
    if built["orders"].fees_unknown_alert_seconds <= built["orders"].balance_read_seconds:
        raise ConfigError("[orders] fees_unknown_alert_seconds must be above "
                          "balance_read_seconds")
    if built["orders"].reconcile_proof_seconds <= 0:
        raise ConfigError("[orders] reconcile_proof_seconds must be above 0")
    if not built["orders"].reconcile_max_gap_seconds > built["orders"].reconcile_proof_seconds:
        raise ConfigError("[orders] reconcile_max_gap_seconds must be above "
                          "reconcile_proof_seconds")
    if built["api"].account_stale_seconds <= built["orders"].reconcile_interval_seconds:
        raise ConfigError("[api] account_stale_seconds must be above [orders] "
                          "reconcile_interval_seconds (the account file is written per reconcile)")
    if built["api"].next_session_search_days < 1:
        raise ConfigError("[api] next_session_search_days must be at least 1 (tomorrow)")
    system = built["api"]
    if system.system_services not in SYSTEM_SERVICES:
        raise ConfigError("[api] system_services must be one of "
                          + ", ".join(SYSTEM_SERVICES))
    if system.system_services == SYSTEM_SERVICES_MAC and not system.sleep_check_command:
        raise ConfigError("[api] sleep_check_command must name a command when "
                          'system_services = "mac"')
    if system.sleep_check_timeout_seconds <= 0 or system.system_restart_rows < 1:
        raise ConfigError("[api] sleep_check_timeout_seconds must be above 0 and "
                          "system_restart_rows at least 1")
    chart: ChartConfig = built["chart"]
    if chart.requests_per_minute < 1 or chart.bars_per_request < 1:
        raise ConfigError("[chart] requests_per_minute and bars_per_request must be at least 1")
    if chart.quiet_requests_per_minute < chart.requests_per_minute:
        raise ConfigError("[chart] quiet_requests_per_minute must be at least requests_per_minute")
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
    reserve = built["market_data"].discovery_bars_reserve_requests
    for name, env in environments.items():
        count, _ = env.route_limits.get(BARS_ROUTE,
                                        (env.requests_per_route_per_minute, SECONDS_PER_MINUTE))
        # Discovery reads only while a session runs, so against the bot's daytime slice.
        bot_count = count - (chart.requests_per_minute if chart.webull_enabled else 0)
        if not 0 <= reserve < bot_count:
            raise ConfigError(f"[market_data] discovery_bars_reserve_requests must be at least 0 "
                              f"and below the bot's bars route limit of [environments.{name}] "
                              f"({bot_count}), or discovery could never read bars")
    orders, api = built["orders"], built["api"]
    if not 0 < orders.stop_split_tick_share <= 1:
        raise ConfigError("[orders] stop_split_tick_share must be above 0 and at most 1")
    if orders.history_fill_poll_seconds < orders.detail_poll_seconds \
            or orders.history_fill_max_pages < 1:
        raise ConfigError("[orders] history_fill_poll_seconds must be at least "
                          "detail_poll_seconds and history_fill_max_pages at least 1")
    for name, env in environments.items():
        check_history_share(orders, name, env)
    if not orders.reconcile_interval_seconds < api.broker_read_warn_seconds \
            < orders.broker_read_max_age_seconds:
        raise ConfigError("[api] broker_read_warn_seconds must be above [orders] "
                          "reconcile_interval_seconds and below [orders] "
                          "broker_read_max_age_seconds")
    for key in ("quotes_write_seconds", "quotes_poll_seconds", "live_quote_max_age_seconds"):
        if not getattr(api, key) > 0:
            raise ConfigError(f"[api] {key} must be above 0")
    if not api.quotes_file.strip():
        raise ConfigError("[api] quotes_file must name a file")
    if not built["alerts"].feed_file.strip():
        raise ConfigError("[alerts] feed_file must name a file")
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


def check_history_share(orders: OrdersConfig, name: str, env: EnvironmentConfig) -> None:
    """The engine's fill reads (`[orders] history_fill_max_pages` every
    history_fill_poll_seconds) must fit the order-history route limit of `env` on their own;
    a read our budget refuses (the display's reads share the route) is tried again the next
    step, nothing being sent."""
    count, window = env.route_limits.get(HISTORY_ROUTE,
                                         (env.requests_per_route_per_minute, SECONDS_PER_MINUTE))
    engine = orders.history_fill_max_pages * math.ceil(window / orders.history_fill_poll_seconds)
    if engine > count:
        raise ConfigError(
            f"[orders] history_fill_max_pages every history_fill_poll_seconds ({engine} per "
            f"{window} s) must fit the order-history route limit of [environments.{name}] "
            f"({count} per {window} s)")


def check_chart_slice(chart: ChartConfig, name: str, env: EnvironmentConfig) -> None:
    """With the chart on, the bot's bars limit in `env` loses `[chart] requests_per_minute` per
    60 s to the API (`quiet_requests_per_minute` while no session runs): refuse a bars limit over
    another window or one either slice would empty."""
    count, window = env.route_limits.get(BARS_ROUTE,
                                         (env.requests_per_route_per_minute, SECONDS_PER_MINUTE))
    if window != SECONDS_PER_MINUTE:
        raise ConfigError(f"[chart] needs the bars route limit of [environments.{name}] per "
                          f"{SECONDS_PER_MINUTE} s to carve its slice from, not per {window} s")
    if max(chart.requests_per_minute, chart.quiet_requests_per_minute) >= count:
        raise ConfigError(f"[chart] requests_per_minute and quiet_requests_per_minute must be "
                          f"below the bars route limit of [environments.{name}] ({count} per "
                          f"{SECONDS_PER_MINUTE} s)")


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


def credential_variables(config: Config) -> frozenset[str]:
    """The NAMES of every environment variable the configuration says holds a credential or a
    private detail (rule 4): processes that need none of them are started without them."""
    names = {config.advisor.api_key_variable, config.alerts.imessage_recipient_variable,
             config.massive.api_key_variable, config.massive.files_key_id_variable,
             config.massive.files_secret_variable}
    for env in config.environments.values():
        names |= {env.app_key_variable, env.app_secret_variable, env.account_variable}
    return frozenset(names)


def environ_without(names: frozenset[str]) -> dict[str, str]:
    """A copy of this process's environment without `names` (credential variables), for a child
    process that needs none of them. Never printed or logged."""
    env = os.environ.copy()
    for name in names:
        env.pop(name, None)
    return env


def trading_gate(config: Config) -> GateResult:
    """The single decision whether this process may send trading requests."""
    env = config.environment
    if not env.trading_enabled:
        return GateResult(False, f"trading is disabled for environment {config.environment_name!r}")
    if env.requires_owner_authorization:
        return _authorization_valid(config, env)
    return GateResult(True, "trading enabled by configuration")
