"""Point-in-time gainer gate around a strategy: the strategy is asked only from the minute a
pool symbol would have been a discovery candidate, as live, never earlier.

For a (symbol, session) in the pool, the symbol is eligible from the first completed M1 bar at
which BOTH hold, using bars up to that bar only:
- close >= previous session's last M1 close x (1 + `gainer_change_percent` / 100) (on the
  gainers list; Webull ranks by change against the previous close), and
- the session's dollar volume so far (sum of close x volume) >= bot.toml
  `min_session_dollar_volume_usd` (discovery's platform floor).
Once eligible, it stays eligible for the rest of the session (live, a WATCH symbol stays on
the stream). An open simulated position is always passed through, so the strategy can manage
it. Every other bar gets IGNORE with one recorded check and the strategy is not called.

The wrapper is a backtest device, not a strategy: it never changes what the strategy decides
when it is asked, and its decisions carry the wrapped strategy's name and version.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from decimal import Decimal

from aperture.strategy import indicators as ind
from aperture.strategy.base import Check, Decision, Strategy, SymbolContext, Verdict

PERCENT = Decimal(100)
ONE = Decimal(1)
RULE_POOL = "backtest_pool_day"
RULE_PREVIOUS = "backtest_previous_close"
RULE_GAINER = "backtest_gainer_eligible"


class GainerGate:
    def __init__(self, inner: Strategy, pool_days: Collection[tuple[str, date]],
                 change_percent: Decimal, dollar_volume_floor: Decimal) -> None:
        self.inner = inner
        self.name = inner.name
        self.version = inner.version
        self._pool = frozenset(pool_days)
        self._factor = ONE + change_percent / PERCENT
        self._floor = dollar_volume_floor
        self._since: dict[date, datetime] = {}

    def _ignore(self, context: SymbolContext, check: Check) -> Decision:
        return Decision(self.name, self.version, context.symbol, Verdict.IGNORE, (check,))

    def eligible_since(self, context: SymbolContext) -> datetime | None:
        """Start of the first bar of today's session at which the symbol became eligible."""
        last = context.bars[-1]
        day = ind.ny_date(last)
        known = self._since.get(day)
        if known is not None:
            return known
        previous_close = context.prior_sessions[-1][-1].close
        level = previous_close * self._factor
        dollar_volume = Decimal(0)
        for bar in ind.current_session(context.bars):
            dollar_volume += bar.close * bar.volume
            if bar.close >= level and dollar_volume >= self._floor:
                self._since[day] = bar.start
                return bar.start
        return None

    def evaluate(self, context: SymbolContext) -> Decision:
        if context.position is not None or not context.bars:
            return self.inner.evaluate(context)
        day = ind.ny_date(context.bars[-1])
        if (context.symbol, day) not in self._pool:
            return self._ignore(context, Check(RULE_POOL, None, "is", None, False,
                                               "not a pool day"))
        if not context.prior_sessions or not context.prior_sessions[-1]:
            return self._ignore(context, Check(RULE_PREVIOUS, None, "is", None, False,
                                               "no previous session bars"))
        if self.eligible_since(context) is None:
            previous_close = context.prior_sessions[-1][-1].close
            change = (context.bars[-1].close / previous_close - ONE) * PERCENT
            return self._ignore(context, Check(RULE_GAINER, change, ">=",
                                               (self._factor - ONE) * PERCENT, False,
                                               "change vs previous close; dollar-volume floor"))
        return self.inner.evaluate(context)
