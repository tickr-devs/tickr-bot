"""Tiny breakout strategy for replay tests only. Not a trading strategy.

ENTER when the last completed close breaks the highest high of the previous `lookback` bars:
window = [that high, that high + window_usd], stop = lowest low of those bars.
With a position, EXIT when the close falls below the entry price minus `exit_drop_usd`.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from tickr.strategy.base import Check, Decision, SymbolContext, Verdict

KEYS = {"strategy": {"name", "version"}, "rules": {"lookback", "window_usd", "exit_drop_usd"}}


class StubBreakout:
    def __init__(self, name: str, version: str, lookback: int, window: Decimal, drop: Decimal):
        self.name = name
        self.version = version
        self.lookback = lookback
        self.window = window
        self.drop = drop

    def _decision(self, context: SymbolContext, verdict: Verdict, **kw: Any) -> Decision:
        return Decision(self.name, self.version, context.symbol, verdict, (), **kw)

    def evaluate(self, context: SymbolContext) -> Decision:
        bars = context.bars
        last = bars[-1]
        if context.position is not None:
            if last.close < context.position.entry_price - self.drop:
                return self._decision(context, Verdict.EXIT)
            return self._decision(context, Verdict.HOLD)
        if len(bars) <= self.lookback:
            return self._decision(context, Verdict.IGNORE)
        prior = bars[-self.lookback - 1 : -1]
        high = max(b.high for b in prior)
        if last.close <= high:
            return self._decision(context, Verdict.IGNORE)
        check = Check("close_above_high", last.close, ">", high, True)
        return Decision(
            self.name,
            self.version,
            context.symbol,
            Verdict.ENTER,
            (check,),
            trigger=high,
            entry_low=high,
            entry_high=high + self.window,
            stop=min(b.low for b in prior),
            setup="stub-breakout",
        )


def create(values: Mapping[str, Any]) -> StubBreakout:
    if {k: set(v) for k, v in values.items()} != KEYS:
        raise ValueError(f"stub strategy keys mismatch: {values}")
    rules = values["rules"]
    return StubBreakout(
        values["strategy"]["name"],
        values["strategy"]["version"],
        rules["lookback"],
        Decimal(str(rules["window_usd"])),
        Decimal(str(rules["exit_drop_usd"])),
    )
