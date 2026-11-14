"""Strict configuration loader and the trading gate.

- Unknown or missing keys refuse to load (ConfigError).
- Every key in a TOML file must carry a comment (`uncommented_keys`), enforced by tests.
- Environments are plain config tables with identical keys; code never branches on their names.
- `trading_gate` is the single place that decides whether trading requests may be sent.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, get_type_hints


class ConfigError(ValueError):
    """The configuration is invalid; the bot must not start."""


@dataclass(frozen=True)
class RunConfig:
    environment: str
    data_dir: str
    database_busy_timeout_seconds: Decimal
    backups_dir: str


@dataclass(frozen=True)
class StrategySelection:
    active: str
    observers: tuple[str, ...]


@dataclass(frozen=True)
class RiskConfig:
    planned_loss_usd: Decimal
    position_notional_cap_usd: Decimal
    max_daily_loss_usd: Decimal
    max_open_positions: int
    max_quote_age_seconds: Decimal
    max_clock_skew_seconds: Decimal


@dataclass(frozen=True)
class SessionConfig:
    timezone: str
    calendar_file: str
    last_entry_minutes_before_close: int
    flatten_minutes_before_close: int
    flatten_grace_seconds: Decimal
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
    max_unprotected_seconds: Decimal
    shutdown_deadline_seconds: Decimal
    reconcile_interval_seconds: Decimal
    broker_read_max_age_seconds: Decimal
    unknown_absent_reads: int


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


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    status_file: str
    commands_dir: str
    status_stale_seconds: Decimal
    poll_seconds: Decimal
    max_request_body_bytes: int
    max_pending_commands: int
    command_max_age_seconds: Decimal
    status_recent_commands: int


@dataclass(frozen=True)
class MarketDataConfig:
    discovery_interval_seconds: Decimal
    stream_lost_exit_seconds: Decimal
    allowed_exchanges: tuple[str, ...]
    allowed_currencies: tuple[str, ...]
    screener_category: str
    allowed_security_types: tuple[str, ...]
    profile_symbols_per_request: int
    max_spread_ratio: Decimal
    min_session_dollar_volume_usd: Decimal
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
    candidate_retry_seconds: Decimal
    history_dir: str
    stream_reconnect_delay_seconds: Decimal
    stream_max_symbols: int
    stream_reserved_symbols: int
    quiet_after_seconds: Decimal
    capture_dir: str


@dataclass(frozen=True)
class ObserversConfig:
    strategy_modules: tuple[str, ...]
    max_observers: int
    max_evaluation_seconds: Decimal
    queue_size: int
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


@dataclass(frozen=True)
class RuntimeConfig:
    min_free_disk_mb: int
    supervisor_poll_seconds: Decimal
    bot_exit_timeout_seconds: int
    stop_alert_after_seconds: int
    preflight_start_local: str
    bot_start_local: str
    launch_weekdays: tuple[int, ...]


@dataclass(frozen=True)
class AlertsConfig:
    dedupe_window_seconds: Decimal
    notification_timeout_seconds: Decimal
    fence_alert_after_seconds: Decimal


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
    observers: ObserversConfig
    advisor: AdvisorConfig
    environments: dict[str, EnvironmentConfig]
    root: Path
    digest: str

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
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
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

    built = {name: build_section(name, data[name], cls) for name, cls in _SECTIONS.items()}

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
    md: MarketDataConfig = built["market_data"]
    if md.candidate_subscriptions > md.stream_max_symbols - md.stream_reserved_symbols:
        raise ConfigError(
            "[market_data] candidate_subscriptions must leave stream_reserved_symbols free "
            "within stream_max_symbols"
        )
    strategy: StrategySelection = built["strategy"]
    if strategy.active in strategy.observers:
        raise ConfigError("[strategy] the active strategy cannot also be an observer")

    return Config(
        **built,
        environments=environments,
        root=Path(root) if root is not None else path.resolve().parent.parent,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


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
