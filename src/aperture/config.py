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


@dataclass(frozen=True)
class SessionConfig:
    timezone: str
    calendar_file: str
    last_entry_minutes_before_close: int
    flatten_minutes_before_close: int
    half_days_enabled: bool


@dataclass(frozen=True)
class OrdersConfig:
    client_order_id_prefix: str
    probe_client_order_id_prefix: str
    client_order_id_max_length: int
    detail_poll_seconds: Decimal
    stop_retry_attempts: int
    stop_retry_spacing_seconds: Decimal
    reconcile_interval_seconds: Decimal
    broker_read_max_age_seconds: Decimal


@dataclass(frozen=True)
class ProbeConfig:
    quantity: int
    max_price_usd: Decimal
    entry_offset_usd: Decimal
    stop_percent_below_fill: Decimal
    max_loss_usd: Decimal
    window_start: str
    window_end: str


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int


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
    if target == tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
        return tuple(value)
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
