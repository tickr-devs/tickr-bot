"""Shared pieces for simple bar-signal strategies (volume-shock-v1, noise-band-v1).

A simple strategy decides on the last completed M1 bar whether its signal fires for the first
time this session, and then turns it into an ENTER with one stop through `enter`. Exits are
the platform's: the resting stop or the session flatten. Nothing here sends orders or reads the
clock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from itertools import pairwise
from typing import Any

from aperture.strategy import price_action_v1 as v1
from aperture.strategy.base import Check, Decision, SymbolContext, Verdict
from aperture.types import Bar

ZERO = v1.ZERO
ONE = v1.ONE
TWO = v1.TWO
PERCENT = v1.PERCENT

Schema = dict[str, dict[str, Callable[[Any], Any]]]

# Sections every simple strategy carries, next to its own [signal] section.
COMMON: Schema = {
    "strategy": {"name": v1._text, "version": v1._text},
    "entry": {"entry_window_percent": v1._positive, "minimum_stop_percent": v1._positive},
    "liquidity": {"max_spread_ratio": v1._positive, "min_previous_close_usd": v1._positive},
    "sizing": {"planned_loss_usd": v1._positive, "max_position_notional_usd": v1._positive},
}


@dataclass(frozen=True)
class Common:
    name: str
    version: str
    entry_window_percent: Decimal
    minimum_stop_percent: Decimal
    max_spread_ratio: Decimal
    min_previous_close_usd: Decimal
    planned_loss_usd: Decimal
    max_position_notional_usd: Decimal


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
            except ValueError as exc:
                raise ValueError(f"[{section}] {key} {exc}") from None
    if flat["name"] != name:
        raise ValueError(f"[strategy] name must be {name!r}")
    return flat


def common(flat: Mapping[str, Any]) -> Common:
    return Common(**{k: flat[k] for k in Common.__dataclass_fields__})


def consistent(context: SymbolContext) -> Check:
    bars = context.bars
    ok = bool(bars) and all(b.symbol == context.symbol for b in bars) and all(
        a.start < b.start for a, b in pairwise(bars))
    return v1._flag("bars_consistent", ok, "own symbol, increasing start, at least one bar")


def previous_close(context: SymbolContext) -> Decimal | None:
    """The last close of the most recent prior session, or None when there is none."""
    prior = [s for s in context.prior_sessions if s]
    return prior[-1][-1].close if prior else None


def first_signal(session: Sequence[Bar], fires: Callable[[int], bool]) -> Check:
    """The signal must fire on the last bar and on no earlier bar of the session, decided from
    the bars alone so a restart changes nothing. This is one signal bar per symbol and
    session: if the quote does not allow an entry during that minute, the session is spent."""
    last = len(session) - 1
    earlier = next((i for i in range(last) if fires(i)), None)
    if earlier is not None:
        return v1._flag("first_signal_of_session", False,
                        f"already fired on bar {earlier} of the session")
    return v1._flag("signal", fires(last), "on the last completed bar")


def decide(strategy: Any, context: SymbolContext, verdict: Verdict, checks: Sequence[Check],
           **fields: Any) -> Decision:
    return Decision(strategy=strategy.name, version=strategy.version, symbol=context.symbol,
                    verdict=verdict, checks=tuple(checks), **fields)


def enter(strategy: Any, c: Common, context: SymbolContext, checks: list[Check],
          setup: str, signal_close: Decimal, stop_level: Decimal) -> Decision:
    """WATCH without a quote; otherwise ENTER when the ask is inside the window around the
    signal close (entry_window_percent below and above it), the spread is acceptable and
    the stop sizes to at least one share. The stop is the lower of `stop_level` and the
    minimum stop distance below the window's floor, so stop < entry_low always holds (the
    platform's admission shape)."""
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
    checks.append(v1._check("spread_ratio", spread / ((quote.ask + quote.bid) / TWO), "<=",
                            c.max_spread_ratio))
    if not checks[-1].passed:
        return ignore()
    checks.append(v1._check("ask_within_price_window", quote.ask, "<=", entry_high))
    if checks[-1].passed:
        checks.append(v1._check("ask_not_below_price_window", quote.ask, ">=", entry_low))
    if not checks[-1].passed:
        return ignore()
    stop = min(stop_level, entry_low * (ONE - c.minimum_stop_percent / PERCENT))
    checks.append(v1._check("stop_below_bid", stop, "<", quote.bid))
    if not checks[-1].passed:
        return ignore()
    risk = entry_high - stop + spread
    planned_loss = c.planned_loss_usd
    if context.loss_budget_usd is not None and context.loss_budget_usd < planned_loss:
        planned_loss = max(context.loss_budget_usd, ZERO)
        checks.append(v1._check("daily_loss_budget_usd", planned_loss, "<", c.planned_loss_usd,
                                "sized down to the room the daily loss ceiling leaves"))
    by_loss = (planned_loss / risk).to_integral_value(rounding=ROUND_FLOOR)
    by_notional = (c.max_position_notional_usd / entry_high).to_integral_value(
        rounding=ROUND_FLOOR)
    quantity = min(by_loss, by_notional)
    checks.append(v1._check("quantity", quantity, ">=", ONE,
                            f"min(floor({planned_loss} / (entry_high - stop + spread)) = "
                            f"{by_loss}, floor(notional cap / entry_high) = {by_notional})"))
    if not checks[-1].passed:
        return ignore()
    return decide(strategy, context, Verdict.ENTER, checks, entry_low=entry_low,
                  entry_high=entry_high, stop=stop, quantity=quantity, **levels)
