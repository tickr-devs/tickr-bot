"""A strategy assembled from building blocks named in its strategy.toml (owner 2026-11-17: a
new strategy should be a new strategy.toml, not new code).

The file names, besides `[strategy]`:

- `[liquidity]`: the spread ceiling and the session dollar volume floor and ceiling, which
  discovery applies to the active strategy's candidates and `evaluate` applies again;
- `[[signal]]`: one table per signal block, all of which must pass on the last completed M1
  bar of the regular session (`block` names it, the other keys are its values);
- `[entry]`: the entry block (how the entry price window is set);
- `[stop]`: the stop block (where the broker stop sits);
- `[[exit]]`: one table per exit block; any one firing on an open position answers EXIT.

The planned loss and notional cap per position are the platform's (`SymbolContext.sizing`,
config/bot.toml [risk]), never the file's.

Signals are decided on the last bar only, so a signal can fire again after an exit (re-entry);
the platform's own rules (symbol pause after a loss, slots, daily loss ceiling, last entry
time, session flatten) still apply. Unknown blocks, unknown or missing keys and bad values
raise ValueError, so a strategy.toml that names something this module cannot do is refused at
load. Adding a block is a code change made once, here; strategies that use it need none.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tickr.calendar import ny_date_of
from tickr.strategy import indicators as ind
from tickr.strategy import price_action_v1 as v1
from tickr.strategy import simple
from tickr.strategy.base import Check, Decision, SymbolContext, Verdict
from tickr.types import Bar, Quote

ZERO, ONE, PERCENT, M1 = simple.ZERO, simple.ONE, simple.PERCENT, simple.M1
BLOCK = "block"

def _keys(where: str, table: Any, keys: Mapping[str, Callable[[Any], Any]]) -> dict[str, Any]:
    """Exactly `keys` (besides `block`), each converted; ValueError otherwise."""
    if not isinstance(table, Mapping):
        raise ValueError(f"{where} must be a table")
    given = set(table) - {BLOCK}
    if given != set(keys):
        raise ValueError(f"{where}: unknown keys {sorted(given - set(keys))}, "
                         f"missing keys {sorted(set(keys) - given)}")
    out = {}
    for key, convert in keys.items():
        try:
            out[key] = convert(table[key])
        except (ValueError, ArithmeticError) as exc:
            raise ValueError(f"{where} {key} {exc}") from None
        if isinstance(out[key], Decimal) and not out[key].is_finite():
            raise ValueError(f"{where} {key} must be finite")
    return out


def _below_hundred(value: Any) -> Decimal:
    number = v1._positive(value)
    if number >= PERCENT:
        raise ValueError("must be below 100")
    return number


# --- signal blocks: (session bars, regular session from 09:30, oldest first) -> Check --------

SignalBlock = Callable[[Sequence[Bar], datetime], Check]
"""A signal block decides on the session's bars and the session's regular open."""


def minutes_after_open(minutes: int) -> SignalBlock:
    """The last bar started at least `minutes` after the regular open (by the clock: minutes
    without trades have no bar)."""

    def check(session: Sequence[Bar], session_open: datetime) -> Check:
        since = (session[-1].start - session_open) // M1
        return v1._check("minutes_after_open", Decimal(since), ">=", Decimal(minutes))
    return check


def new_session_high() -> SignalBlock:
    """The last bar closed above every earlier bar's high of the session."""

    def check(session: Sequence[Bar], session_open: datetime) -> Check:
        if len(session) < 2:
            return v1._flag("new_session_high", False, "no earlier bar this session")
        earlier = max(b.high for b in session[:-1])
        return v1._check("new_session_high", session[-1].close, ">", earlier,
                         "close against the highest earlier high")
    return check


def rise_from_low(window_minutes: int, min_percent: Decimal) -> SignalBlock:
    """The last close is at least `min_percent` above the lowest low of the bars that started
    within the last `window_minutes` minutes (the last bar included)."""
    window, least = window_minutes, min_percent

    def check(session: Sequence[Bar], session_open: datetime) -> Check:
        last = session[-1]
        low = min(b.low for b in session if b.start > last.start - window * M1)
        rise = (last.close / low - ONE) * PERCENT
        return v1._check("rise_from_low_percent", rise, ">=", least,
                         f"close {last.close} over the {window}-minute low {low}")
    return check


def session_whole(session: Sequence[Bar], session_open: datetime | None) -> Check:
    """The platform proved that the bars hold the whole session from its regular open, and
    they are that session's. A missing 09:30 bar is ordinary (no trade that minute), so the
    proof comes from the read, never from the first bar."""
    if session_open is None:
        return v1._flag("session_whole", False, "bars not proven to reach the regular open")
    return v1._flag("session_whole", ind.ny_date(session[-1]) == ny_date_of(session_open)
                    and session[0].start >= session_open,
                    f"session bars from {session[0].start.isoformat()}, open "
                    f"{session_open.isoformat()}")


SIGNALS: dict[str, tuple[Mapping[str, Callable[[Any], Any]], Callable[..., SignalBlock]]] = {
    "minutes_after_open": ({"minutes": v1._positive_int}, minutes_after_open),
    "new_session_high": ({}, new_session_high),
    "rise_from_low": ({"window_minutes": v1._positive_int, "min_percent": v1._positive},
                      rise_from_low),
}


# --- entry, stop and exit blocks ---------------------------------------------------------------

@dataclass(frozen=True)
class MarketableWindow:
    """Entry window `window_percent` below and above the signal close: a marketable entry is
    admitted while the ask is inside it."""

    window_percent: Decimal

    def window(self, signal_close: Decimal) -> tuple[Decimal, Decimal]:
        part = self.window_percent / PERCENT
        return signal_close * (ONE - part), signal_close * (ONE + part)


ENTRIES: dict[str, tuple[Mapping[str, Callable[[Any], Any]], Callable[..., Any]]] = {
    "marketable_window": ({"window_percent": _below_hundred}, MarketableWindow),
}


@dataclass(frozen=True)
class PercentBelowEntry:
    """The broker stop `percent` below the top of the entry window, and below the bid."""

    percent: Decimal

    def stop(self, entry_high: Decimal, quote: Quote) -> Decimal:
        keep = ONE - self.percent / PERCENT
        return min(entry_high * keep, quote.bid * keep)


STOPS: dict[str, tuple[Mapping[str, Callable[[Any], Any]], Callable[..., Any]]] = {
    "percent_below_entry": ({"percent": _below_hundred}, PercentBelowEntry),
}


@dataclass(frozen=True)
class TrailingPercent:
    """EXIT once the price is `percent` or more below the highest price since the entry (the
    entry price, then the highs of the bars from the fill's minute on). The price is the last
    close, or the bid when that is lower and the book is sane: a valid quote (the platform
    passes only a fresh one) with a spread ratio at most `bid_max_spread_ratio`. A broken book,
    such as an emptied bid side in a thin stock, never fires the trail: the last close decides."""

    percent: Decimal
    bid_max_spread_ratio: Decimal

    def bid_refused(self, quote: Quote | None) -> str | None:
        """Why the bid may not fire the trail, or None when it may."""
        if quote is None or not ZERO < quote.bid <= quote.ask:
            return "no valid fresh quote"
        spread = v1.spread_check(quote, self.bid_max_spread_ratio)
        if not spread.passed:
            return f"spread ratio {spread.measured} over {self.bid_max_spread_ratio}"
        return None

    def check(self, entry: Decimal, since: Sequence[Bar], quote: Quote | None) -> Check:
        """One check, so a decision's decisive rule is always the trail itself; why the bid
        did not count is in its note."""
        peak = max([entry, *(b.high for b in since)])
        floor = peak * (ONE - self.percent / PERCENT)
        refused = self.bid_refused(quote)
        prices = [b.close for b in since[-1:]]
        if refused is None:
            assert quote is not None
            prices.append(quote.bid)
        price = min(prices, default=entry)
        source = ("lowest of last close and bid" if refused is None
                  else f"last close (bid not used: {refused})")
        return v1._check("trailing_stop", price, "<=", floor,
                         f"{source} against {self.percent}% under the high since entry {peak}")


EXITS: dict[str, tuple[Mapping[str, Callable[[Any], Any]], Callable[..., Any]]] = {
    "trailing_percent": ({"percent": _below_hundred, "bid_max_spread_ratio": v1._positive},
                         TrailingPercent),
}


def _one(where: str, table: Any, registry: Mapping[str, tuple[Mapping[str, Any], Any]]) -> Any:
    if not isinstance(table, Mapping) or table.get(BLOCK) not in registry:
        raise ValueError(f"{where} needs block = one of {sorted(registry)}")
    keys, build = registry[table[BLOCK]]
    return build(**_keys(f"{where} {table[BLOCK]}", table, keys))


def _many(where: str, tables: Any, registry: Mapping[str, tuple[Mapping[str, Any], Any]],
          ) -> list[tuple[str, Any]]:
    if not isinstance(tables, list) or not tables:
        raise ValueError(f"[[{where}]] needs at least one table")
    out = []
    for number, table in enumerate(tables, 1):
        name = table.get(BLOCK) if isinstance(table, Mapping) else None
        if name not in registry:
            raise ValueError(f"[[{where}]] {number} needs block = one of {sorted(registry)}")
        keys, build = registry[name]
        out.append((name, build(**_keys(f"[[{where}]] {name}", table, keys))))
    return out


# --- the strategy ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Liquidity:
    max_spread_ratio: Decimal
    min_session_dollar_volume_usd: Decimal
    max_session_dollar_volume_usd: Decimal


SECTIONS = {"strategy", "liquidity", "signal", "entry", "stop", "exit"}


class Composed:
    prior_sessions_needed = 0

    def __init__(self, name: str, version: str, liquidity: Liquidity,
                 signals: list[tuple[str, SignalBlock]], entry: MarketableWindow,
                 stop: PercentBelowEntry, exits: list[tuple[str, TrailingPercent]]) -> None:
        self.name, self.version = name, version
        self.liquidity, self.signals, self.entry = liquidity, signals, entry
        self.stop_block, self.exits = stop, exits
        # When no fill time is known (replay): the first bar seen with each position.
        self._held: dict[str, tuple[Decimal, datetime]] = {}

    # Liquidity (strategy.base.Liquidity)
    def quote_liquidity(self, quote: Quote) -> tuple[Check, ...]:
        return (v1.spread_check(quote, self.liquidity.max_spread_ratio),)

    def session_liquidity(self, session_bars: Sequence[Bar]) -> tuple[Check, ...]:
        volume = ind.dollar_volume(session_bars)
        return (v1._check("dollar_volume_usd", volume, ">=",
                          self.liquidity.min_session_dollar_volume_usd),
                v1._check("dollar_volume_max_usd", volume, "<=",
                          self.liquidity.max_session_dollar_volume_usd))

    def _decide(self, context: SymbolContext, verdict: Verdict, checks: Sequence[Check],
                **fields: Any) -> Decision:
        return simple.decide(self, context, verdict, checks, **fields)

    def evaluate(self, context: SymbolContext) -> Decision:
        checks = [simple.consistent(context)]
        if not checks[-1].passed:
            return self._decide(context, Verdict.IGNORE, checks)
        if context.position is not None:
            return self._hold_or_exit(context, checks)
        self._held.pop(context.symbol, None)
        session = ind.current_session(context.bars)
        opened = context.session_open
        checks.append(session_whole(session, opened))
        if checks[-1].passed:
            for check in self.session_liquidity(session):
                checks.append(check)
                if not check.passed:
                    break
        for _, block in self.signals:
            if not checks[-1].passed:
                break
            assert opened is not None  # session_whole passed
            checks.append(block(session, opened))
        if not checks[-1].passed:
            return self._decide(context, Verdict.IGNORE, checks)
        return self._enter(context, checks, session[-1].close)

    def _enter(self, context: SymbolContext, checks: list[Check],
               signal_close: Decimal) -> Decision:
        entry_low, entry_high = self.entry.window(signal_close)
        levels = {"setup": self.name, "trigger": signal_close}
        quote = context.quote
        if quote is None:
            return self._decide(context, Verdict.WATCH, checks, **levels)
        valid = quote.symbol == context.symbol and ZERO < quote.bid <= quote.ask
        checks.append(v1._flag("quote_valid", valid, "own symbol, 0 < bid <= ask"))
        if valid:
            checks.append(v1.spread_check(quote, self.liquidity.max_spread_ratio))
        if checks[-1].passed:
            checks.append(v1._check("ask_within_price_window", quote.ask, "<=", entry_high))
        if checks[-1].passed:
            checks.append(v1._check("ask_not_below_price_window", quote.ask, ">=", entry_low))
        if not checks[-1].passed:
            return self._decide(context, Verdict.IGNORE, checks, **levels)
        stop = self.stop_block.stop(entry_high, quote)
        risk = entry_high - stop + (quote.ask - quote.bid)
        checks.append(v1._check("sizing_risk_per_share", risk, ">", ZERO,
                                "entry_high - stop + spread"))
        quantity = v1.size(checks, context, entry_high, risk, "(entry_high - stop + spread)")
        if quantity is None:
            return self._decide(context, Verdict.IGNORE, checks, **levels)
        return self._decide(context, Verdict.ENTER, checks, entry_low=entry_low,
                            entry_high=entry_high, stop=stop, quantity=quantity,
                            invalidation=stop, **levels)

    def _hold_or_exit(self, context: SymbolContext, checks: list[Check]) -> Decision:
        position, bars = context.position, context.bars
        assert position is not None
        if position.filled_at is not None:
            start = position.filled_at.replace(second=0, microsecond=0)
        else:
            held = self._held.get(context.symbol)
            if held is None or held[0] != position.entry_price:
                held = (position.entry_price, bars[-1].start + M1)  # bars after this one
                self._held[context.symbol] = held
            start = held[1]
        since = [b for b in bars if b.start >= start]
        fired = False
        for _, block in self.exits:
            checks.append(block.check(position.entry_price, since, context.quote))
            fired = fired or checks[-1].passed
        return self._decide(context, Verdict.EXIT if fired else Verdict.HOLD, checks)


def create(values: Mapping[str, Any]) -> Composed:
    """Build the strategy from parsed strategy.toml values (ValueError on any problem)."""
    if not isinstance(values, Mapping) or set(values) != SECTIONS:
        given = set(values) if isinstance(values, Mapping) else set()
        raise ValueError(f"strategy sections: unknown {sorted(given - SECTIONS)}, "
                         f"missing {sorted(SECTIONS - given)}")
    head = _keys("[strategy]", values["strategy"], {"name": v1._text, "version": v1._text})
    liquidity = Liquidity(**_keys("[liquidity]", values["liquidity"], {
        "max_spread_ratio": v1._positive, "min_session_dollar_volume_usd": v1._non_negative,
        "max_session_dollar_volume_usd": v1._positive}))
    if liquidity.min_session_dollar_volume_usd >= liquidity.max_session_dollar_volume_usd:
        raise ValueError("[liquidity] the dollar volume floor must be below its ceiling")
    return Composed(head["name"], head["version"], liquidity,
                    _many("signal", values["signal"], SIGNALS),
                    _one("[entry]", values["entry"], ENTRIES),
                    _one("[stop]", values["stop"], STOPS),
                    _many("exit", values["exit"], EXITS))

