"""Settings schema (backlog S1, design rule W10): one record per owner setting, plus cross-field
rules.

Read-only groundwork. Nothing at runtime reads this module yet: `config.load` and each strategy's
`parse_settings` stay the checks that refuse a bad file. Later the settings API (S4), the app
forms (S6), the TOML comments and the advisor's limits take their facts from here.

Words, kinds, units, apply timing and rules live here. A record's `minimum` is structural (0 or
1, the bound `config.load` or the strategy's `parse_settings` already enforces). The numbers that
are product choices, each setting's `step` and any `maximum`, live in config/settings_schema.toml
and `load` joins them to the records (rule 6).

First slice: the [risk] owner settings in config/bot.toml except planned_loss_usd and
position_notional_cap_usd (moving in another change), and the price-action-v3 parameters except
its [sizing] table.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

PLATFORM = "platform"
"""Owner of the settings in config/bot.toml; any other owner is a strategy folder name."""


class Kind(StrEnum):
    INT = "int"
    DECIMAL = "decimal"
    BOOL = "bool"


class Unit(StrEnum):
    """A setting's unit. `UNIT_WORDS` lists the words its TOML comment must use for it."""

    USD = "usd"
    SECONDS = "seconds"
    MINUTES = "minutes"
    COUNT = "count"
    BARS = "bars"
    SESSIONS = "sessions"
    MULTIPLE = "multiple"
    RATIO = "ratio"
    ATR = "atr"
    SPREADS = "spreads"
    PERCENT = "percent"
    SWITCH = "switch"


UNIT_WORDS: Mapping[Unit, tuple[str, ...]] = {
    Unit.USD: ("usd",),
    Unit.SECONDS: ("second",),
    Unit.MINUTES: ("minute",),
    Unit.COUNT: ("count",),
    Unit.BARS: ("bar", "m1 close"),
    Unit.SESSIONS: ("session",),
    Unit.MULTIPLE: ("multiple",),
    Unit.RATIO: ("ratio", " / ", "divided by"),
    Unit.ATR: ("atr multiple", "true range"),
    Unit.SPREADS: ("spread",),
    Unit.PERCENT: ("percent",),
    Unit.SWITCH: ("true",),
}


class Applies(StrEnum):
    """When a change that does not loosen protection takes effect (backlog S5)."""

    AT_ONCE = "at_once"
    WHEN_FLAT = "when_flat"
    NEXT_SESSION = "next_session"


class Loosens(StrEnum):
    """Which direction of change loosens protection (that change needs Face ID, S5)."""

    RAISE = "raise"
    LOWER = "lower"
    NEVER = "never"


@dataclass(frozen=True)
class Setting:
    owner: str
    key: str
    """Dotted `table.key` inside the owner's file."""
    kind: Kind
    unit: Unit
    group: str
    en: str
    es: str
    applies: Applies
    loosens: Loosens
    minimum: Decimal | None = None
    minimum_inclusive: bool = True
    maximum: Decimal | None = None
    step: Decimal | None = None
    requires_retraining: bool = False

    @property
    def needs_face_id(self) -> bool:
        """True when some change of this setting loosens protection."""
        return self.loosens is not Loosens.NEVER


class Compare(StrEnum):
    BELOW = "<"
    AT_MOST = "<="


@dataclass(frozen=True)
class Rule:
    """`left` compare `right`, dotted keys of its file (`right` may be outside the schema)."""

    owner: str
    name: str
    left: str
    compare: Compare
    right: str
    en: str
    es: str


class Cause(StrEnum):
    UNKNOWN_KEY = "UNKNOWN_KEY"
    WRONG_TYPE = "WRONG_TYPE"
    BELOW_MINIMUM = "BELOW_MINIMUM"
    ABOVE_MAXIMUM = "ABOVE_MAXIMUM"
    OFF_STEP = "OFF_STEP"
    RULE_BROKEN = "RULE_BROKEN"
    VALUE_MISSING = "VALUE_MISSING"


@dataclass(frozen=True)
class Refusal:
    key: str
    cause: Cause
    detail: str


class SchemaError(ValueError):
    """config/settings_schema.toml does not match the records."""


@dataclass(frozen=True)
class ChangeEffect:
    applies: Applies
    needs_face_id: bool


D = Decimal
PA3 = "price-action-v3"
_P = PLATFORM
_PROTECT = Applies.AT_ONCE  # tightening protection applies at once; loosening, next session (S5)
_FLAT = Applies.WHEN_FLAT  # strategy parameters apply when flat, by a safe restart (S5)
_UP = Loosens.RAISE
_DOWN = Loosens.LOWER
_NO = Loosens.NEVER

RECORDS: tuple[Setting, ...] = (
    # Platform [risk], config/bot.toml.
    Setting(_P, "risk.max_daily_loss_usd", Kind.DECIMAL, Unit.USD, "risk.daily_loss",
            "Daily loss ceiling: today's net realized loss plus open reservations plus the new "
            "trade's planned loss.",
            "Límite de pérdida diaria: pérdida neta realizada hoy más reservas abiertas más la "
            "pérdida prevista de la nueva operación.",
            _PROTECT, _UP, minimum=D(0), minimum_inclusive=False),
    Setting(_P, "risk.min_daily_room_usd", Kind.DECIMAL, Unit.USD, "risk.daily_loss",
            "Smallest daily-loss room a new trade is sized into; below it entries stop.",
            "Margen mínimo de pérdida diaria en el que se dimensiona una nueva operación; por "
            "debajo se detienen las entradas.",
            _PROTECT, _DOWN, minimum=D(0)),
    Setting(_P, "risk.buying_power_max_age_seconds", Kind.DECIMAL, Unit.SECONDS, "risk.account",
            "Oldest broker read of the day buying power an entry may rely on.",
            "Lectura más antigua del poder de compra del día en la que puede basarse una entrada.",
            _PROTECT, _UP, minimum=D(0), minimum_inclusive=False),
    Setting(_P, "risk.max_open_positions", Kind.INT, Unit.COUNT, "risk.positions",
            "Position slots: most open trades at once.",
            "Plazas de posición: máximo de operaciones abiertas a la vez.",
            _PROTECT, _UP, minimum=D(1)),
    Setting(_P, "risk.max_working_entries", Kind.INT, Unit.COUNT, "risk.positions",
            "Most entry orders working at once.",
            "Máximo de órdenes de entrada activas a la vez.",
            _PROTECT, _UP, minimum=D(1)),
    Setting(_P, "risk.max_quote_age_seconds", Kind.DECIMAL, Unit.SECONDS, "risk.data",
            "Oldest quote any decision may use, from the provider's time.",
            "Cotización más antigua que puede usar una decisión, según la hora del proveedor.",
            _PROTECT, _UP, minimum=D(0), minimum_inclusive=False),
    Setting(_P, "risk.max_clock_skew_seconds", Kind.DECIMAL, Unit.SECONDS, "risk.data",
            "How far the provider's clock may run ahead of ours before a quote is refused.",
            "Cuánto puede adelantarse el reloj del proveedor al nuestro antes de rechazar una "
            "cotización.",
            _PROTECT, _UP, minimum=D(0)),
    Setting(_P, "risk.symbol_cooldown_after_loss_enabled", Kind.BOOL, Unit.SWITCH, "risk.cooldown",
            "Pause new entries in a symbol after a losing trade in it today.",
            "Pausar nuevas entradas en un símbolo tras una operación perdedora en él hoy.",
            _PROTECT, _DOWN),
    Setting(_P, "risk.symbol_cooldown_minutes_after_loss", Kind.INT, Unit.MINUTES, "risk.cooldown",
            "Length of that pause, from the losing trade's close.",
            "Duración de esa pausa, desde el cierre de la operación perdedora.",
            _PROTECT, _DOWN, minimum=D(1)),
    Setting(_P, "risk.max_entry_rest_bars", Kind.INT, Unit.BARS, "risk.entries",
            "Longest a resting limit entry may work, in completed one-minute bars.",
            "Tiempo máximo que puede estar activa una entrada límite en espera, en barras de un "
            "minuto completas.",
            _PROTECT, _UP, minimum=D(1)),
    Setting(_P, "risk.entry_snapshot_interval_seconds", Kind.DECIMAL, Unit.SECONDS, "risk.entries",
            "Time between snapshot reads of a working entry's symbol while its stream quote is "
            "too old.",
            "Tiempo entre lecturas de instantánea del símbolo de una entrada activa mientras su "
            "cotización en streaming es demasiado antigua.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    # Strategy price-action-v3, strategies/price-action-v3/strategy.toml.
    Setting(PA3, "indicators.fast_ema_period", Kind.INT, Unit.BARS, "strategy.indicators",
            "Fast exponential moving average length.",
            "Longitud de la media móvil exponencial rápida.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "indicators.slow_ema_period", Kind.INT, Unit.BARS, "strategy.indicators",
            "Slow exponential moving average length.",
            "Longitud de la media móvil exponencial lenta.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "indicators.atr_period", Kind.INT, Unit.BARS, "strategy.indicators",
            "Average true range length.",
            "Longitud del rango verdadero medio (ATR).",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "indicators.relative_volume_sessions", Kind.INT, Unit.SESSIONS,
            "strategy.indicators",
            "Prior sessions averaged for relative volume.",
            "Sesiones anteriores promediadas para el volumen relativo.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "indicators.minimum_relative_volume", Kind.DECIMAL, Unit.RATIO,
            "strategy.indicators",
            "Least relative cumulative volume for an entry.",
            "Volumen acumulado relativo mínimo para una entrada.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "indicators.refuse_when_relative_volume_unavailable", Kind.BOOL, Unit.SWITCH,
            "strategy.indicators",
            "Refuse a symbol whose prior-session volume history is missing.",
            "Rechazar un símbolo sin historial de volumen de sesiones anteriores.",
            _FLAT, _NO),
    Setting(PA3, "indicators.warmup_period_multiple", Kind.INT, Unit.MULTIPLE,
            "strategy.indicators",
            "Indicator warm-up, as a multiple of each indicator's length.",
            "Calentamiento de los indicadores, como múltiplo de la longitud de cada uno.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "breakout.lookback_bars", Kind.INT, Unit.BARS, "strategy.breakout",
            "Prior bars whose highest high must be broken.",
            "Barras anteriores cuyo máximo más alto debe romperse.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "continuation.impulse_bars", Kind.INT, Unit.BARS, "strategy.continuation",
            "Impulse bars before the base.",
            "Barras de impulso antes de la base.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "continuation.base_bars", Kind.INT, Unit.BARS, "strategy.continuation",
            "Base bars after the impulse.",
            "Barras de base después del impulso.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "continuation.minimum_impulse_atr", Kind.DECIMAL, Unit.ATR,
            "strategy.continuation",
            "Least impulse size.",
            "Tamaño mínimo del impulso.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "continuation.maximum_base_range_atr", Kind.DECIMAL, Unit.ATR,
            "strategy.continuation",
            "Widest base range.",
            "Rango máximo de la base.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "continuation.maximum_pre_breakout_distance_atr", Kind.DECIMAL, Unit.ATR,
            "strategy.continuation",
            "Furthest the last close may sit below the trigger to arm a setup.",
            "Distancia máxima del último cierre por debajo del disparador para armar una "
            "configuración.",
            _FLAT, _NO, minimum=D(0)),
    Setting(PA3, "entry.entry_extension_max_atr", Kind.DECIMAL, Unit.ATR, "strategy.entry",
            "Price window above the trigger; beyond it the entry is missed, never chased.",
            "Ventana de precio sobre el disparador; más allá la entrada se pierde, nunca se "
            "persigue.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "entry.max_round_trip_atr_ratio", Kind.DECIMAL, Unit.RATIO, "strategy.entry",
            "Highest round-trip spread cost relative to the average true range.",
            "Coste máximo de ida y vuelta del diferencial respecto al rango verdadero medio.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "entry.max_round_trip_risk_ratio", Kind.DECIMAL, Unit.RATIO, "strategy.entry",
            "Highest round-trip spread cost relative to the risk per share.",
            "Coste máximo de ida y vuelta del diferencial respecto al riesgo por acción.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "entry.minimum_stop_spreads", Kind.DECIMAL, Unit.SPREADS, "strategy.entry",
            "Least stop distance in spreads.",
            "Distancia mínima del stop en diferenciales.",
            _FLAT, _NO, minimum=D(0)),
    Setting(PA3, "entry.minimum_stop_percent", Kind.DECIMAL, Unit.PERCENT, "strategy.entry",
            "Least stop distance in percent of price.",
            "Distancia mínima del stop en porcentaje del precio.",
            _FLAT, _NO, minimum=D(0)),
    Setting(PA3, "entry.slippage_reserve_atr", Kind.DECIMAL, Unit.ATR, "strategy.entry",
            "Slippage reserve per side added to the risk per share for sizing.",
            "Reserva de deslizamiento por lado añadida al riesgo por acción al dimensionar.",
            _FLAT, _NO, minimum=D(0)),
    Setting(PA3, "liquidity.max_spread_ratio", Kind.DECIMAL, Unit.RATIO, "strategy.liquidity",
            "Widest spread relative to the mid price.",
            "Diferencial máximo respecto al precio medio.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "liquidity.min_dollar_volume_usd", Kind.DECIMAL, Unit.USD, "strategy.liquidity",
            "Least dollar volume traded in the session so far.",
            "Volumen mínimo en dólares negociado en la sesión hasta ahora.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "spike.lookback_bars", Kind.INT, Unit.BARS, "strategy.spike",
            "Bars the latest bar is compared with to detect a spike.",
            "Barras con las que se compara la última barra para detectar un pico.",
            _FLAT, _NO, minimum=D(1)),
    Setting(PA3, "spike.min_volume_ratio", Kind.DECIMAL, Unit.RATIO, "strategy.spike",
            "Least volume of the latest bar relative to the lookback bars.",
            "Volumen mínimo de la última barra respecto a las barras de referencia.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "spike.min_move_atr", Kind.DECIMAL, Unit.ATR, "strategy.spike",
            "Least close-to-close move relative to the lookback bars' true range.",
            "Movimiento mínimo de cierre a cierre respecto al rango verdadero de las barras de "
            "referencia.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "spike.min_dollar_volume_ratio", Kind.DECIMAL, Unit.RATIO, "strategy.spike",
            "Least dollar volume of the latest bar relative to the lookback bars.",
            "Volumen mínimo en dólares de la última barra respecto a las barras de referencia.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "spike.min_bar_dollar_volume_usd", Kind.DECIMAL, Unit.USD, "strategy.spike",
            "Least dollar volume of the latest bar for it to count as a fast mover.",
            "Volumen mínimo en dólares de la última barra para contar como valor de movimiento "
            "rápido.",
            _FLAT, _NO, minimum=D(0), minimum_inclusive=False),
    Setting(PA3, "spike.signal_valid_bars", Kind.INT, Unit.BARS, "strategy.spike",
            "Bars a spike stays tradable, counting the spike bar.",
            "Barras durante las que un pico sigue siendo operable, contando la barra del pico.",
            _FLAT, _NO, minimum=D(1)),
)

RULES: tuple[Rule, ...] = (
    Rule(_P, "daily_room_below_ceiling", "risk.min_daily_room_usd", Compare.BELOW,
         "risk.max_daily_loss_usd",
         "The smallest daily room must be below the daily loss ceiling.",
         "El margen diario mínimo debe ser inferior al límite de pérdida diaria."),
    Rule(_P, "slots_fit_stream", "risk.max_open_positions", Compare.AT_MOST,
         "market_data.stream_reserved_symbols",
         "Each position keeps a stream slot, so slots may not exceed the reserved stream symbols.",
         "Cada posición ocupa una plaza de streaming, así que las plazas no pueden superar los "
         "símbolos reservados."),
    Rule(PA3, "fast_below_slow_ema", "indicators.fast_ema_period", Compare.BELOW,
         "indicators.slow_ema_period",
         "The fast average must be shorter than the slow one.",
         "La media rápida debe ser más corta que la lenta."),
)


_NUMBERS = frozenset({"step", "maximum"})


def _decimal(where: str, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise SchemaError(f"{where}: expected a finite number, got {value!r}")
    number = D(str(value))
    if number <= 0:
        raise SchemaError(f"{where}: must be above 0")
    return number


def load(path: Path | str) -> tuple[Setting, ...]:
    """The records with their `step` and `maximum` from config/settings_schema.toml.

    Refuses a numeric setting without an entry or without a step, an entry for an on/off setting
    or for no record, and an unknown field."""
    tables = tomllib.loads(Path(path).read_text("utf-8"))
    entries: dict[tuple[str, str], Any] = {
        (owner, key): entry for owner, table in tables.items() for key, entry in table.items()}
    settings: list[Setting] = []
    for record in RECORDS:
        where = f'[{record.owner}."{record.key}"]'
        entry = entries.pop((record.owner, record.key), None)
        if record.kind is Kind.BOOL:
            if entry is not None:
                raise SchemaError(f"{where}: an on/off setting has no step or maximum")
            settings.append(record)
            continue
        if not isinstance(entry, Mapping) or "step" not in entry:
            raise SchemaError(f"{where}: missing, or without a step")
        if unknown := sorted(set(entry) - _NUMBERS):
            raise SchemaError(f"{where}: unknown fields {unknown}")
        maximum = entry.get("maximum")
        settings.append(replace(
            record, step=_decimal(f"{where} step", entry["step"]),
            maximum=None if maximum is None else _decimal(f"{where} maximum", maximum)))
    if entries:
        raise SchemaError(f"entries for no record: {sorted(entries)}")
    return tuple(settings)


def settings_of(settings: tuple[Setting, ...], owner: str) -> dict[str, Setting]:
    return {s.key: s for s in settings if s.owner == owner}


def flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """A parsed TOML document as dotted keys (`risk.max_open_positions`)."""
    flat: dict[str, Any] = {}
    for name, value in table.items():
        key = f"{prefix}{name}"
        if isinstance(value, Mapping):
            flat.update(flatten(value, f"{key}."))
        else:
            flat[key] = value
    return flat


def _number(spec: Setting, value: Any) -> Decimal | bool | None:
    """The value as the setting's kind, or None when it is not of that kind."""
    if spec.kind is Kind.BOOL:
        return value if isinstance(value, bool) else None
    if isinstance(value, bool):
        return None
    if spec.kind is Kind.INT:
        return D(value) if isinstance(value, int) else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int | float) and math.isfinite(value):
        return D(str(value))
    return None


def _check_value(spec: Setting, value: Any) -> Refusal | None:
    number = _number(spec, value)
    if number is None:
        return Refusal(spec.key, Cause.WRONG_TYPE, f"expected {spec.kind.value}, got {value!r}")
    if isinstance(number, bool):
        return None
    if spec.minimum is not None and (number < spec.minimum or (
            number == spec.minimum and not spec.minimum_inclusive)):
        bound = ">=" if spec.minimum_inclusive else ">"
        return Refusal(spec.key, Cause.BELOW_MINIMUM, f"{number} must be {bound} {spec.minimum}")
    if spec.maximum is not None and number > spec.maximum:
        return Refusal(spec.key, Cause.ABOVE_MAXIMUM, f"{number} must be <= {spec.maximum}")
    if spec.step is not None and number % spec.step != 0:
        return Refusal(spec.key, Cause.OFF_STEP, f"{number} is not a multiple of {spec.step}")
    return None


def validate(settings: tuple[Setting, ...], owner: str, draft: Mapping[str, Any],
             current: Mapping[str, Any]) -> list[Refusal]:
    """Check a draft of one owner's settings (dotted key -> value) against `settings` (see `load`).

    `current` holds the owner's present values as dotted keys (see `flatten`); cross-field rules
    read the draft over it. Returns every refusal; an empty list means the draft passes.
    """
    specs = settings_of(settings, owner)
    refusals: list[Refusal] = []
    for key, value in draft.items():
        spec = specs.get(key)
        if spec is None:
            refusals.append(Refusal(key, Cause.UNKNOWN_KEY, f"not an owner setting of {owner}"))
            continue
        refusal = _check_value(spec, value)
        if refusal is not None:
            refusals.append(refusal)
    refused = {r.key for r in refusals}
    merged = {**current, **draft}
    for rule in RULES:
        if rule.owner != owner or not {rule.left, rule.right} & set(draft):
            continue
        if {rule.left, rule.right} & refused:
            continue
        missing = [k for k in (rule.left, rule.right) if k not in merged]
        if missing:
            refusals.extend(Refusal(k, Cause.VALUE_MISSING, f"rule {rule.name} needs it")
                            for k in missing)
            continue
        left, right = D(str(merged[rule.left])), D(str(merged[rule.right]))
        holds = left < right if rule.compare is Compare.BELOW else left <= right
        if not holds:
            refusals.append(Refusal(rule.left, Cause.RULE_BROKEN,
                                    f"{rule.name}: {rule.left} {left} must be "
                                    f"{rule.compare.value} {rule.right} {right}"))
    return refusals


def change_effect(spec: Setting, old: Any, new: Any) -> ChangeEffect:
    """When a change from `old` to `new` applies and whether it needs Face ID (backlog S5):
    loosening protection waits for the next session and needs Face ID; anything else follows
    the record's `applies`."""
    raised = new > old
    lowered = new < old
    loosening = ((spec.loosens is Loosens.RAISE and raised)
                 or (spec.loosens is Loosens.LOWER and lowered))
    if loosening:
        return ChangeEffect(Applies.NEXT_SESSION, needs_face_id=True)
    return ChangeEffect(spec.applies, needs_face_id=False)
