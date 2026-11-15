"""Strategy price-action-v1: long breakout and impulse-continuation setups on M1 bars.

Values come from `strategies/price-action-v1/strategy.toml`; `create(values)` refuses unknown,
missing or mistyped keys. The strategy is pure: no clock, no I/O, no orders. Checks run in a
fixed order and stop at the first failure, so the last check of a non-ENTER decision is its
single cause.

Rule order:
1. Open position: HOLD (v1 has no strategy exit rule; the resting stop and the session
   flatten exit the position).
2. Data integrity: bars of this symbol, strictly increasing start times.
3. History: `warmup_period_multiple` periods for the slow EMA and ATR (prior sessions may
   supply them; ATR deliberately spans overnight gaps) and both setup patterns.
4. Filters: relative cumulative volume, session dollar volume, last close above session
   VWAP, fast EMA above slow EMA, ATR positive.
5. Setup (continuation first, then breakout; the first armed setup is used):
   - continuation: `impulse_bars` impulse bars then `base_bars` base bars (the last bars);
     impulse (last impulse close - first impulse open) >= `minimum_impulse_atr` ATR, base
     high-low range <= `maximum_base_range_atr` ATR, last close no further than
     `maximum_pre_breakout_distance_atr` ATR below the base high; trigger = base high,
     invalidation = base low.
   - breakout: trigger = highest high of the `lookback_bars` bars before the last one,
     invalidation = their lowest low (must be below the trigger). Armed when the last close
     is above the trigger (confirmed) or, before the break, no further than
     `maximum_pre_breakout_distance_atr` ATR below it.
6. Without a quote: WATCH(trigger, invalidation).
7. Quote: valid, spread ratio within the liquidity limit, bid above invalidation (else
   IGNORE), ask above trigger (else WATCH), ask inside the price window
   trigger + `entry_extension_max_atr` ATR (else IGNORE: missed, never chased), stop
   distance (ask - invalidation) at least max(`minimum_stop_spreads` spreads,
   `minimum_stop_percent` percent of ask), cost rule on the round trip (2 x spread)
   against ATR and against per-share risk (top of window - stop), quantity at least 1
   share: min(floor(`planned_loss_usd` / sizing risk), floor(`max_position_notional_usd` /
   top of window)), where sizing risk = top of window - stop + spread + slippage reserve
   on both legs. ENTER carries it in `Decision.quantity`. Then ENTER with entry window
   [trigger, trigger + extension], stop = invalidation.

The spread and session dollar-volume rules ([liquidity]) are also `quote_liquidity` and
`session_liquidity`, which discovery applies to the active strategy's candidates (the platform
has no liquidity threshold of its own). Quote freshness is the platform's rule (the strategy
never reads the clock). Prices are
not rounded to the tick here; the order layer does that.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from itertools import pairwise
from typing import Any

from tickr.strategy import indicators as ind
from tickr.strategy.base import Check, Decision, SymbolContext, Verdict
from tickr.types import Bar, Quote

NAME = "price-action-v1"
ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
PERCENT = Decimal(100)

BREAKOUT = "breakout"
CONTINUATION = "continuation"

_OPERATORS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


# ---------------------------------------------------------------- configuration


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("must be a positive integer")
    return value


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("must be a number")
    return Decimal(str(value))


def _positive(value: Any) -> Decimal:
    number = _number(value)
    if number <= ZERO:
        raise ValueError("must be positive")
    return number


def _non_negative(value: Any) -> Decimal:
    number = _number(value)
    if number < ZERO:
        raise ValueError("must not be negative")
    return number


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("must be a non-empty string")
    return value


_SCHEMA: dict[str, dict[str, Callable[[Any], Any]]] = {
    "strategy": {"name": _text, "version": _text},
    "indicators": {
        "fast_ema_period": _positive_int,
        "slow_ema_period": _positive_int,
        "atr_period": _positive_int,
        "relative_volume_sessions": _positive_int,
        "minimum_relative_volume": _positive,
        "refuse_when_relative_volume_unavailable": _boolean,
        "warmup_period_multiple": _positive_int,
    },
    "breakout": {"lookback_bars": _positive_int},
    "continuation": {
        "impulse_bars": _positive_int,
        "base_bars": _positive_int,
        "minimum_impulse_atr": _positive,
        "maximum_base_range_atr": _positive,
        "maximum_pre_breakout_distance_atr": _non_negative,
    },
    "entry": {
        "entry_extension_max_atr": _positive,
        "max_round_trip_atr_ratio": _positive,
        "max_round_trip_risk_ratio": _positive,
        "minimum_stop_spreads": _non_negative,
        "minimum_stop_percent": _non_negative,
        "slippage_reserve_atr": _non_negative,
    },
    "liquidity": {"max_spread_ratio": _positive, "min_dollar_volume_usd": _positive},
    "sizing": {"planned_loss_usd": _positive, "max_position_notional_usd": _positive},
}


@dataclass(frozen=True)
class Settings:
    name: str
    version: str
    fast_ema_period: int
    slow_ema_period: int
    atr_period: int
    relative_volume_sessions: int
    minimum_relative_volume: Decimal
    refuse_when_relative_volume_unavailable: bool
    warmup_period_multiple: int
    lookback_bars: int
    impulse_bars: int
    base_bars: int
    minimum_impulse_atr: Decimal
    maximum_base_range_atr: Decimal
    maximum_pre_breakout_distance_atr: Decimal
    entry_extension_max_atr: Decimal
    max_round_trip_atr_ratio: Decimal
    max_round_trip_risk_ratio: Decimal
    minimum_stop_spreads: Decimal
    minimum_stop_percent: Decimal
    slippage_reserve_atr: Decimal
    max_spread_ratio: Decimal
    min_dollar_volume_usd: Decimal
    planned_loss_usd: Decimal
    max_position_notional_usd: Decimal

    @property
    def required_bars(self) -> int:
        """Bars needed before any rule can be evaluated: the indicator warm-up and both
        setup patterns."""
        warmup = self.warmup_period_multiple
        return max(
            self.slow_ema_period * warmup,
            self.fast_ema_period * warmup,
            self.atr_period * warmup + 1,
            self.lookback_bars + 1,
            self.impulse_bars + self.base_bars,
        )


def parse_settings(values: Mapping[str, Any], name: str = NAME) -> Settings:
    """Validate the parsed strategy.toml. Unknown, missing or mistyped keys raise ValueError.
    `name` is the strategy name the file must carry (a derived strategy passes its own)."""
    if not isinstance(values, Mapping):
        raise ValueError("strategy values must be a table")
    unknown = sorted(set(values) - set(_SCHEMA))
    missing = sorted(set(_SCHEMA) - set(values))
    if unknown or missing:
        raise ValueError(f"strategy sections: unknown {unknown}, missing {missing}")
    flat: dict[str, Any] = {}
    for section, keys in _SCHEMA.items():
        table = values[section]
        if not isinstance(table, Mapping):
            raise ValueError(f"[{section}] must be a table")
        unknown = sorted(set(table) - set(keys))
        missing = sorted(set(keys) - set(table))
        if unknown or missing:
            raise ValueError(f"[{section}]: unknown keys {unknown}, missing keys {missing}")
        for key, convert in keys.items():
            try:
                flat[key] = convert(table[key])
            except ValueError as exc:
                raise ValueError(f"[{section}] {key} {exc}") from None
    settings = Settings(**flat)
    if settings.name != name:
        raise ValueError(f"[strategy] name must be {name!r}")
    if settings.fast_ema_period >= settings.slow_ema_period:
        raise ValueError("[indicators] fast_ema_period must be below slow_ema_period")
    return settings


# ---------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class _Setup:
    name: str
    checks: tuple[Check, ...]
    trigger: Decimal
    invalidation: Decimal

    @property
    def armed(self) -> bool:
        return all(c.passed for c in self.checks)


def _check(rule: str, measured: Decimal, op: str, threshold: Decimal, note: str = "") -> Check:
    return Check(rule, measured, op, threshold, _OPERATORS[op](measured, threshold), note)


def _flag(rule: str, passed: bool, note: str) -> Check:
    return Check(rule, None, "is", None, passed, note)


def spread_check(quote: Quote, max_spread_ratio: Decimal) -> Check:
    """(ask - bid) / mid of a valid quote (0 < bid <= ask) against the strategy's limit."""
    mid = (quote.ask + quote.bid) / TWO
    return _check("spread_ratio", (quote.ask - quote.bid) / mid, "<=", max_spread_ratio)


def dollar_volume_check(session_bars: Sequence[Bar], min_dollar_volume_usd: Decimal) -> Check:
    """Sum of close x volume of the session's completed bars against the strategy's floor."""
    return _check("dollar_volume_usd", ind.dollar_volume(session_bars), ">=",
                  min_dollar_volume_usd)


class PriceActionV1:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.name = settings.name
        self.version = settings.version
        # Relative volume is the only rule that reads prior sessions.
        self.prior_sessions_needed = settings.relative_volume_sessions

    def quote_liquidity(self, quote: Quote) -> tuple[Check, ...]:
        return (spread_check(quote, self.settings.max_spread_ratio),)

    def session_liquidity(self, session_bars: Sequence[Bar]) -> tuple[Check, ...]:
        return (dollar_volume_check(session_bars, self.settings.min_dollar_volume_usd),)

    # A decision is built from the checks gathered so far.
    def _decide(
        self, context: SymbolContext, verdict: Verdict, checks: Sequence[Check], **fields: Any
    ) -> Decision:
        return Decision(
            strategy=self.name,
            version=self.version,
            symbol=context.symbol,
            verdict=verdict,
            checks=tuple(checks),
            **fields,
        )

    def evaluate(self, context: SymbolContext) -> Decision:
        s = self.settings
        checks: list[Check] = []

        def ignore() -> Decision:
            return self._decide(context, Verdict.IGNORE, checks)

        if context.position is not None:
            checks.append(
                _flag("position_open", True, "v1 exits by resting stop or session flatten")
            )
            return self._decide(context, Verdict.HOLD, checks)

        bars = context.bars
        consistent = all(b.symbol == context.symbol for b in bars) and all(
            a.start < b.start for a, b in pairwise(bars)
        )
        checks.append(_flag("bars_consistent", consistent, "own symbol, increasing start"))
        if not consistent:
            return ignore()

        checks.append(_check("history_bars", Decimal(len(bars)), ">=", Decimal(s.required_bars)))
        if not checks[-1].passed:
            return ignore()

        session = ind.current_session(bars)
        closes = [b.close for b in bars]
        last = bars[-1]

        rvol = ind.relative_volume(session, context.prior_sessions, s.relative_volume_sessions)
        if rvol is None:
            checks.append(
                _flag(
                    "relative_volume_available",
                    not s.refuse_when_relative_volume_unavailable,
                    f"{len(context.prior_sessions)} prior sessions given, "
                    f"{s.relative_volume_sessions} needed with non-zero volume",
                )
            )
            if not checks[-1].passed:
                return ignore()
        else:
            checks.append(_check("relative_volume", rvol, ">=", s.minimum_relative_volume))
            if not checks[-1].passed:
                return ignore()

        checks.append(dollar_volume_check(session, s.min_dollar_volume_usd))
        if not checks[-1].passed:
            return ignore()

        vwap = ind.vwap_series(session)[-1]
        if vwap is None:
            checks.append(_flag("session_vwap_available", False, "session volume is zero"))
            return ignore()
        checks.append(_check("close_above_vwap", last.close, ">", vwap))
        if not checks[-1].passed:
            return ignore()

        fast = ind.ema_series(closes, s.fast_ema_period)[-1]
        slow = ind.ema_series(closes, s.slow_ema_period)[-1]
        assert fast is not None and slow is not None  # guaranteed by history_bars
        checks.append(_check("fast_ema_above_slow_ema", fast, ">", slow))
        if not checks[-1].passed:
            return ignore()

        atr = ind.atr_series(bars, s.atr_period)[-1]
        assert atr is not None  # guaranteed by history_bars
        checks.append(_check("atr", atr, ">", ZERO))
        if not checks[-1].passed:
            return ignore()

        continuation = self._continuation(bars, atr)
        breakout = self._breakout(bars, atr)
        setup = next((x for x in (continuation, breakout) if x.armed), None)
        if setup is None:
            checks.extend(continuation.checks)
            checks.extend(breakout.checks)
            return ignore()
        checks.extend(setup.checks)

        levels = {"setup": setup.name, "trigger": setup.trigger, "invalidation": setup.invalidation}
        if context.quote is None:
            return self._decide(context, Verdict.WATCH, checks, **levels)
        return self._with_quote(context, context.quote, setup, atr, checks, levels)

    def _with_quote(
        self,
        context: SymbolContext,
        quote: Quote,
        setup: _Setup,
        atr: Decimal,
        checks: list[Check],
        levels: dict[str, Any],
    ) -> Decision:
        s = self.settings

        def ignore() -> Decision:
            return self._decide(context, Verdict.IGNORE, checks, **levels)

        valid = quote.symbol == context.symbol and ZERO < quote.bid <= quote.ask
        checks.append(_flag("quote_valid", valid, "own symbol, 0 < bid <= ask"))
        if not valid:
            return ignore()

        spread = quote.ask - quote.bid
        checks.append(spread_check(quote, s.max_spread_ratio))
        if not checks[-1].passed:
            return ignore()

        checks.append(_check("bid_above_invalidation", quote.bid, ">", setup.invalidation))
        if not checks[-1].passed:
            return ignore()

        checks.append(_check("ask_above_trigger", quote.ask, ">", setup.trigger))
        if not checks[-1].passed:
            return self._decide(context, Verdict.WATCH, checks, **levels)

        entry_high = setup.trigger + s.entry_extension_max_atr * atr
        checks.append(_check("ask_within_price_window", quote.ask, "<=", entry_high))
        if not checks[-1].passed:
            return ignore()

        stop = setup.invalidation
        minimum = max(s.minimum_stop_spreads * spread, s.minimum_stop_percent / PERCENT * quote.ask)
        checks.append(_check("stop_distance", quote.ask - stop, ">=", minimum))
        if not checks[-1].passed:
            return ignore()

        risk = entry_high - stop
        round_trip = TWO * spread
        checks.append(
            _check("round_trip_atr_ratio", round_trip / atr, "<=", s.max_round_trip_atr_ratio)
        )
        if not checks[-1].passed:
            return ignore()
        checks.append(
            _check("round_trip_risk_ratio", round_trip / risk, "<=", s.max_round_trip_risk_ratio)
        )
        if not checks[-1].passed:
            return ignore()

        # Per-share risk for sizing: worst fill (top of window) to the stop, plus the spread
        # and the slippage reserve on both legs. Recorded so the platform can size from it.
        sizing_risk = risk + spread + TWO * s.slippage_reserve_atr * atr
        checks.append(
            _check(
                "sizing_risk_per_share",
                sizing_risk,
                ">",
                ZERO,
                "entry_high - stop + spread + 2 legs x slippage reserve",
            )
        )
        # Quantity: whole shares within the planned loss and the notional cap. Near the
        # platform's daily ceiling the planned loss shrinks to the room it leaves.
        planned_loss = s.planned_loss_usd
        if context.loss_budget_usd is not None and context.loss_budget_usd < planned_loss:
            planned_loss = max(context.loss_budget_usd, ZERO)
            checks.append(_check("daily_loss_budget_usd", planned_loss, "<", s.planned_loss_usd,
                                 "sized down to the room the daily loss ceiling leaves"))
        by_loss = (planned_loss / sizing_risk).to_integral_value(rounding=ROUND_FLOOR)
        by_notional = (s.max_position_notional_usd / entry_high).to_integral_value(
            rounding=ROUND_FLOOR
        )
        quantity = min(by_loss, by_notional)
        checks.append(
            _check(
                "quantity",
                quantity,
                ">=",
                ONE,
                f"min(floor(planned loss {planned_loss} / risk) = {by_loss}, "
                f"floor(notional cap / entry_high) = {by_notional})",
            )
        )
        if not checks[-1].passed:
            return ignore()
        return self._decide(
            context,
            Verdict.ENTER,
            checks,
            entry_low=setup.trigger,
            entry_high=entry_high,
            stop=stop,
            quantity=quantity,
            **levels,
        )

    def _breakout(self, bars: Sequence[Bar], atr: Decimal) -> _Setup:
        s = self.settings
        reference = bars[-s.lookback_bars - 1 : -1]
        trigger = max(b.high for b in reference)
        invalidation = min(b.low for b in reference)
        close = bars[-1].close
        if close > trigger:
            arming = _check("breakout_close_above_trigger", close, ">", trigger, "confirmed")
        else:
            arming = _check(
                "breakout_pre_breakout_distance_atr",
                (trigger - close) / atr,
                "<=",
                s.maximum_pre_breakout_distance_atr,
                "close at or below trigger: arm before the break",
            )
        checks = _until_failure(
            (_check("breakout_invalidation_below_trigger", invalidation, "<", trigger), arming)
        )
        return _Setup(BREAKOUT, checks, trigger, invalidation)

    def _continuation(self, bars: Sequence[Bar], atr: Decimal) -> _Setup:
        s = self.settings
        base = bars[-s.base_bars :]
        impulse = bars[-s.base_bars - s.impulse_bars : -s.base_bars]
        base_high = max(b.high for b in base)
        base_low = min(b.low for b in base)
        checks = _until_failure(
            (
                _check(
                    "impulse_atr",
                    (impulse[-1].close - impulse[0].open) / atr,
                    ">=",
                    s.minimum_impulse_atr,
                ),
                _check(
                    "base_range_atr", (base_high - base_low) / atr, "<=", s.maximum_base_range_atr
                ),
                _check("continuation_invalidation_below_trigger", base_low, "<", base_high),
                _check(
                    "pre_breakout_distance_atr",
                    (base_high - bars[-1].close) / atr,
                    "<=",
                    s.maximum_pre_breakout_distance_atr,
                ),
            )
        )
        return _Setup(CONTINUATION, checks, base_high, base_low)


def _until_failure(checks: Sequence[Check]) -> tuple[Check, ...]:
    """Checks up to and including the first failure, like every other rule."""
    kept: list[Check] = []
    for c in checks:
        kept.append(c)
        if not c.passed:
            break
    return tuple(kept)


def create(values: Mapping[str, Any]) -> PriceActionV1:
    """Build the strategy from parsed strategy.toml values (ValueError on any key problem)."""
    return PriceActionV1(parse_settings(values))
