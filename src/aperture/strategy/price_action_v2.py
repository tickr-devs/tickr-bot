"""Strategy price-action-v2: price-action-v1 plus two rules (candidate, never the active one
unless the owner selects it).

Values come from `strategies/price-action-v2/strategy.toml`: every v1 section plus
`[confirmation]` and `[no_progress]`. Unknown, missing or mistyped keys raise ValueError.

1. Trigger confirmation. v1's ENTER is kept only when this symbol's evaluation on the previous
   completed bar of the same session also had an executable ask through its trigger (passed
   `ask_within_price_window`), `confirmation_quotes` evaluations in a row. Otherwise the
   decision becomes WATCH with the failed `trigger_confirmation` check last. Any evaluation
   whose ask is not through (or that never reaches the quote) resets the count. The platform
   evaluates once per completed minute, so "consecutive quotes" means the quotes seen by
   consecutive minute evaluations.
2. No-progress exit. With a position open: EXIT once `window_minutes` have passed since the
   bar on which the position was first seen and the best high of the bars after that one
   (floored at the entry) is less than `minimum_progress_r` R above the entry (R = entry - stop
   at first sight); otherwise HOLD
   (also HOLD, with the cause, when the bars are not this symbol's in order or R <= 0).
   The fill time is not in `PositionView`, so the entry minute is the first bar evaluated with
   the position (that bar's start, at most a minute early). That bar can hold highs from
   before the fill, so it never counts as progress. Reaching the progress once
   satisfies the rule for the rest of the trade.

State: the strategy interface gives one evaluation at a time, so both rules keep the minimum
per-symbol memory in the instance (last evaluated bar and count; entry price, stop and first
bar of the open position). It lives in memory only: after a restart the count starts at zero
and the no-progress clock starts at the first bar seen with the position. Observers never see a
position, so as an observer only rule 1 shows; replay and `research decide` show both.
The strategy never sends orders and never reads the clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from aperture.strategy import indicators as ind
from aperture.strategy import price_action_v1 as v1
from aperture.strategy.base import Decision, SymbolContext, Verdict

NAME = "price-action-v2"
M1 = timedelta(minutes=1)
THROUGH_RULE = "ask_within_price_window"

_RULES = {
    "confirmation": {"confirmation_quotes": v1._positive_int},
    "no_progress": {"minimum_progress_r": v1._positive, "window_minutes": v1._positive_int},
}


@dataclass(frozen=True)
class Rules:
    confirmation_quotes: int
    minimum_progress_r: Decimal
    window_minutes: int


def parse_settings(values: Mapping[str, Any]) -> tuple[v1.Settings, Rules]:
    if not isinstance(values, Mapping):
        raise ValueError("strategy values must be a table")
    flat: dict[str, Any] = {}
    for section, keys in _RULES.items():
        table = values.get(section)
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
    base = {k: v for k, v in values.items() if k not in _RULES}
    return v1.parse_settings(base, NAME), Rules(**flat)


class PriceActionV2(v1.PriceActionV1):
    def __init__(self, settings: v1.Settings, rules: Rules) -> None:
        super().__init__(settings)
        self.rules = rules
        self._streaks: dict[str, tuple[datetime, int, int]] = {}  # bar, carried in, count
        self._held: dict[str, tuple[Decimal, Decimal, datetime]] = {}  # entry, stop, first bar

    def evaluate(self, context: SymbolContext) -> Decision:
        symbol, bars = context.symbol, context.bars
        if context.position is not None and bars:
            self._streaks.pop(symbol, None)
            return self._no_progress(context)
        self._held.pop(symbol, None)
        decision = super().evaluate(context)
        if not bars:
            self._streaks.pop(symbol, None)
            return decision
        through = any(c.rule == THROUGH_RULE and c.passed for c in decision.checks)
        start, carried = bars[-1].start, 0
        last = self._streaks.get(symbol)
        if last is not None and last[0] == start:  # the same bar again: same count carried in
            carried = last[1]
        elif last is not None and len(ind.current_session(bars)) > 1 and last[0] == bars[-2].start:
            carried = last[2]
        count = carried + 1 if through else 0
        self._streaks[symbol] = (start, carried, count)
        if decision.verdict is not Verdict.ENTER:
            return decision
        check = v1._check("trigger_confirmation", Decimal(count), ">=",
                          Decimal(self.rules.confirmation_quotes),
                          "consecutive evaluations with an executable ask through the trigger")
        checks = (*decision.checks, check)
        if check.passed:
            return replace(decision, checks=checks)
        return replace(decision, verdict=Verdict.WATCH, checks=checks, entry_low=None,
                       entry_high=None, stop=None, quantity=None)

    def _no_progress(self, context: SymbolContext) -> Decision:
        position, bars, rules = context.position, context.bars, self.rules
        assert position is not None
        held = self._held.get(context.symbol)
        if held is None or held[0] != position.entry_price:
            held = (position.entry_price, position.stop, bars[-1].start)
            self._held[context.symbol] = held
        entry, stop, since = held
        consistent = all(b.symbol == context.symbol for b in bars) and all(
            a.start < b.start for a, b in pairwise(bars))
        checks = [v1._flag("bars_consistent", consistent, "own symbol, increasing start")]
        if consistent:
            checks.append(v1._check("risk_per_share", entry - stop, ">", v1.ZERO, "entry - stop"))
        if checks[-1].passed:
            minutes = (bars[-1].start - since) // M1 + 1
            checks.append(v1._check("minutes_held", Decimal(minutes), ">=",
                                    Decimal(rules.window_minutes), "from the first bar seen"))
        if all(c.passed for c in checks):
            # The first bar seen may start before the fill, so its high never counts.
            best = max([entry, *(b.high for b in bars if b.start > since)])
            checks.append(v1._check("progress_r", (best - entry) / (entry - stop), "<",
                                    rules.minimum_progress_r, "best high after entry bar, in R"))
        verdict = Verdict.EXIT if all(c.passed for c in checks) else Verdict.HOLD
        return self._decide(context, verdict, checks)


def create(values: Mapping[str, Any]) -> PriceActionV2:
    """Build the strategy from parsed strategy.toml values (ValueError on any key problem)."""
    return PriceActionV2(*parse_settings(values))
