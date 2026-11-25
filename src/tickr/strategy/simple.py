"""Shared pieces for simple bar-signal strategies (volume-shock-v1, noise-band-v1).

A simple strategy decides on the last completed M1 bar whether its signal fires for the first
time this session, and then turns it into an ENTER with one stop through `enter`. Exits are
the platform's: the resting stop or the session flatten. Nothing here sends orders or reads the
clock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from tickr.strategy import indicators as ind
from tickr.strategy import price_action_v1 as v1
from tickr.strategy.base import Check, Decision, SymbolContext, Verdict
from tickr.types import Bar, Quote

ZERO = v1.ZERO
ONE = v1.ONE
TWO = v1.TWO
PERCENT = v1.PERCENT
REGULAR_OPEN = time(9, 30)  # New York start of the regular session's first bar
M1 = timedelta(minutes=1)

Schema = dict[str, dict[str, Callable[[Any], Any]]]

# Sections every simple strategy carries, next to its own [signal] section.
COMMON: Schema = {
    "strategy": {"name": v1._text, "version": v1._text},
    "entry": {"entry_window_percent": v1._positive, "minimum_stop_percent": v1._positive,
              "signal_valid_minutes": v1._positive_int},
    "liquidity": {"max_spread_ratio": v1._positive, "min_dollar_volume_usd": v1._positive,
                  "min_previous_close_usd": v1._positive},
}


@dataclass(frozen=True)
class Common:
    name: str
    version: str
    entry_window_percent: Decimal
    minimum_stop_percent: Decimal
    signal_valid_minutes: int
    max_spread_ratio: Decimal
    min_dollar_volume_usd: Decimal
    min_previous_close_usd: Decimal


def parse(values: Mapping[str, Any], schema: Schema, name: str) -> dict[str, Any]:
    """Validate a parsed strategy.toml against COMMON plus `schema`. Unknown, missing or
    mistyped keys raise ValueError. Returns every key flattened."""
    full = {**COMMON, **schema}
    if not isinstance(values, Mapping):
        raise ValueError("strategy values must be a table")
    if set(values) != set(full):
        raise ValueError(f"strategy sections: unknown {sorted(set(values) - set(full))}, "
                         f"missing {sorted(set(full) - set(values))}")
    flat: dict[str, Any] = {}
    for section, keys in full.items():
        table = values[section]
        if not isinstance(table, Mapping):
            raise ValueError(f"[{section}] must be a table")
        if set(table) != set(keys):
            raise ValueError(f"[{section}]: unknown keys {sorted(set(table) - set(keys))}, "
                             f"missing keys {sorted(set(keys) - set(table))}")
        for key, convert in keys.items():
            try:
                flat[key] = convert(table[key])
            except (ValueError, ArithmeticError) as exc:  # Decimal('nan') comparisons raise
                raise ValueError(f"[{section}] {key} {exc}") from None
    for key, value in flat.items():
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError(f"{key} must be finite")
    for key in ("entry_window_percent", "minimum_stop_percent"):
        if flat[key] >= PERCENT:
            raise ValueError(f"[entry] {key} must be below 100")
    if flat["name"] != name:
        raise ValueError(f"[strategy] name must be {name!r}")
    return flat


def common(flat: Mapping[str, Any]) -> Common:
    return Common(**{k: flat[k] for k in Common.__dataclass_fields__})


class Liquid:
    """The [liquidity] rules every simple strategy shares (strategy.base.Liquidity):
    discovery applies them to candidates, `evaluate` applies them again."""

    common: Common

    def quote_liquidity(self, quote: Quote) -> tuple[Check, ...]:
        return (v1.spread_check(quote, self.common.max_spread_ratio),)

    def session_liquidity(self, session_bars: Sequence[Bar]) -> tuple[Check, ...]:
        return (v1.dollar_volume_check(session_bars, self.common.min_dollar_volume_usd),)


def consistent(context: SymbolContext) -> Check:
    bars = context.bars
    ok = bool(bars) and all(b.symbol == context.symbol for b in bars) and all(
        a.start < b.start for a, b in pairwise(bars))
    return v1._flag("bars_consistent", ok, "own symbol, increasing start, at least one bar")


def previous_close(context: SymbolContext) -> Decimal | None:
    """The last close of the most recent prior session, or None when there is none."""
    prior = [s for s in context.prior_sessions if s]
    return prior[-1][-1].close if prior else None


def session_from_open(session: Sequence[Bar]) -> Check:
    """The session's bars must start with the 09:30 bar, so "first signal of the session" and
    the session open are decided over the whole session (bars_per_symbol covers it)."""
    t = ind.ny_time(session[0])
    return v1._flag("session_from_open", (t.hour, t.minute) == (REGULAR_OPEN.hour,
                                                                REGULAR_OPEN.minute),
                    f"first session bar starts {t.isoformat()}")


def first_signal(session: Sequence[Bar], fires: Callable[[int], bool],
                 valid_minutes: int) -> tuple[Check, int | None]:
    """The first bar of the session where the signal fired, decided from the bars alone so a
    restart changes nothing, and still valid: it started at most `valid_minutes` - 1 minutes
    before the last bar (minutes, not bars, so a halt or data gap does not stretch it). The
    validity window gives a quote that arrives a little after the signal bar a chance; the
    entry window around the signal close keeps a late entry from chasing. Returns the check
    and the signal bar's index (None when there is no valid signal)."""
    first = next((i for i in range(len(session)) if fires(i)), None)
    if first is None:
        return v1._flag("signal", False, "no signal bar this session"), None
    age = (session[-1].start - session[first].start) // M1
    check = v1._check("signal_age_minutes", Decimal(age), "<", Decimal(valid_minutes),
                      f"first signal on bar {first} of the session")
    return check, (first if check.passed else None)


def not_invalidated(session: Sequence[Bar], first: int, stop_level: Decimal) -> Check:
    """Every bar after the signal bar stayed above the signal's stop level: a signal whose stop
    level has already traded is not entered late."""
    later = session[first + 1:]
    lowest = min((b.low for b in later), default=None)
    return v1._flag("signal_not_invalidated", lowest is None or lowest > stop_level,
                    f"lowest low after the signal {lowest}, stop level {stop_level}")


def decide(strategy: Any, context: SymbolContext, verdict: Verdict, checks: Sequence[Check],
           **fields: Any) -> Decision:
    return Decision(strategy=strategy.name, version=strategy.version, symbol=context.symbol,
                    verdict=verdict, checks=tuple(checks), **fields)


def enter(strategy: Any, c: Common, context: SymbolContext, checks: list[Check],
          setup: str, signal_close: Decimal, stop_level: Decimal) -> Decision:
    """WATCH without a quote; otherwise ENTER when the ask is inside the window around the
    signal close (entry_window_percent below and above it), the spread is acceptable and
    the stop sizes to at least one share. The stop is the lowest of `stop_level` and the
    minimum stop distance below both the window's floor and the bid, so stop < entry_low
    and stop < bid always hold (the platform's admission shape). The recorded invalidation
    is `stop_level`, which can be above the stop used."""
    window = c.entry_window_percent / PERCENT
    entry_low, entry_high = signal_close * (ONE - window), signal_close * (ONE + window)
    levels = {"setup": setup, "trigger": signal_close, "invalidation": stop_level}
    quote = context.quote
    if quote is None:
        return decide(strategy, context, Verdict.WATCH, checks, **levels)

    def ignore() -> Decision:
        return decide(strategy, context, Verdict.IGNORE, checks, **levels)

    valid = quote.symbol == context.symbol and ZERO < quote.bid <= quote.ask
    checks.append(v1._flag("quote_valid", valid, "own symbol, 0 < bid <= ask"))
    if not valid:
        return ignore()
    spread = quote.ask - quote.bid
    checks.append(v1.spread_check(quote, c.max_spread_ratio))
    if not checks[-1].passed:
        return ignore()
    checks.append(v1._check("ask_within_price_window", quote.ask, "<=", entry_high))
    if checks[-1].passed:
        checks.append(v1._check("ask_not_below_price_window", quote.ask, ">=", entry_low))
    if not checks[-1].passed:
        return ignore()
    distance = ONE - c.minimum_stop_percent / PERCENT
    stop = min(stop_level, entry_low * distance, quote.bid * distance)
    risk = entry_high - stop + spread
    checks.append(v1._check("sizing_risk_per_share", risk, ">", ZERO, "entry_high - stop + spread"))
    quantity = v1.size(checks, context, entry_high, risk, "(entry_high - stop + spread)")
    if quantity is None:
        return ignore()
    return decide(strategy, context, Verdict.ENTER, checks, entry_low=entry_low,
                  entry_high=entry_high, stop=stop, quantity=quantity, **levels)
