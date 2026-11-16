"""Strategy price-action-v3: price-action-v1 plus a spike setup on fast movers.

Values come from `strategies/price-action-v3/strategy.toml`: every v1 section plus `[spike]`.
Unknown, missing or mistyped keys raise ValueError. The owner asked for it on 2026-11-16: a
stock whose latest minute breaks sharply from the minutes before it is traded by the one
strategy, not by a separate one, with real orders, and every decision names its setup
so breakout, continuation and spike results stay apart.

The detector is `tickr.data.movers.measure` on the completed bars of the current session with
the `[spike]` values; the apps' fast-movers list uses the same detector and values.

1. v1 runs first and its decision stands unless it is IGNORE: a position (HOLD), an armed
   continuation or breakout (WATCH or ENTER), and every refusal before the setups (bars out of
   order, too little history) are v1's.
2. Otherwise, when the latest bar is an upward spike (`Mover.spike_up`: volume ratio at least
   `min_volume_ratio`, close above the previous close by at least `min_move_atr` mean true
   ranges, bar dollar volume at least `min_bar_dollar_volume_usd`), the spike setup is armed
   with trigger = the spike bar's close and invalidation = its low. v1's filters on cumulative
   volume, VWAP and the EMAs are not applied to it: a spike is by nature early, before the day's
   volume has built. Everything from the quote on is v1's unchanged: spread, bid above the
   invalidation, ask above the trigger (else WATCH), the price window of
   `entry_extension_max_atr` v1 ATR (else IGNORE: never chased), stop distance, both cost
   rules and the sizing.
3. Not a spike: v1's IGNORE as it was.

The setup is armed only while the spike bar is the latest completed bar: the next minute it is
gone unless that minute spikes again.

Liquidity: discovery admits a symbol whose session dollar volume is below
`min_dollar_volume_usd` when its latest bar is an upward spike (check `fast_mover_spike`), so
a fast mover reaches the strategy before its day's volume has built. The spread rule is v1's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tickr.data.movers import Mover, MoverSettings, measure
from tickr.strategy import indicators as ind
from tickr.strategy import price_action_v1 as v1
from tickr.strategy.base import Check, Decision, SymbolContext, Verdict
from tickr.types import Bar

NAME = "price-action-v3"
SPIKE = "spike"
SPIKE_RULE = "fast_mover_spike"

_RULES = {
    "spike": {
        "lookback_bars": v1._positive_int,
        "min_volume_ratio": v1._positive,
        "min_move_atr": v1._positive,
        "min_dollar_volume_ratio": v1._positive,
        "min_bar_dollar_volume_usd": v1._positive,
    },
}


def parse_settings(values: Mapping[str, Any]) -> tuple[v1.Settings, MoverSettings]:
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
    return v1.parse_settings(base, NAME), MoverSettings(**flat)


def spike_checks(mover: Mover) -> tuple[Check, ...]:
    """The armed spike as checks, in the order the detector reads them."""
    return (
        v1._check("spike_volume_ratio", mover.volume_ratio.value, ">=",
                  mover.volume_ratio.threshold, "latest bar volume / mean of the lookback"),
        v1._check("spike_move_atr", mover.move_atr.value, ">=", mover.move_atr.threshold,
                  "close - previous close, in mean true ranges of the lookback"),
        v1._check("spike_invalidation_below_trigger", mover.low, "<", mover.close,
                  "spike bar low below its close"),
    )


class PriceActionV3(v1.PriceActionV1):
    def __init__(self, settings: v1.Settings, movers: MoverSettings) -> None:
        super().__init__(settings)
        self.movers = movers

    def fast_mover(self, symbol: str, session_bars: Sequence[Bar]) -> Mover | None:
        """The fast mover of `session_bars` by this strategy's `[spike]` values, or None."""
        return measure(symbol, session_bars, self.movers)

    def session_liquidity(self, session_bars: Sequence[Bar]) -> tuple[Check, ...]:
        volume = v1.dollar_volume_check(session_bars, self.settings.min_dollar_volume_usd)
        if volume.passed or not session_bars:
            return (volume,)
        mover = self.fast_mover(session_bars[-1].symbol, session_bars)
        spike = mover is not None and mover.spike_up
        return (v1._flag(SPIKE_RULE, spike,
                         f"session dollar volume {volume.measured} below {volume.threshold}: "
                         "admitted only while the latest bar is an upward spike"),)

    def evaluate(self, context: SymbolContext) -> Decision:
        decision = super().evaluate(context)
        if decision.verdict is not Verdict.IGNORE or len(decision.checks) < 2:
            return decision
        consistent, history = decision.checks[0], decision.checks[1]
        if not (consistent.passed and history.passed):
            return decision
        bars = context.bars
        mover = self.fast_mover(context.symbol, ind.current_session(bars))
        if mover is None or not mover.spike_up:
            return decision
        checks = [consistent, history, *spike_checks(mover)]
        if not all(c.passed for c in checks):
            return decision
        atr = ind.atr_series(bars, self.settings.atr_period)[-1]
        assert atr is not None  # guaranteed by history_bars
        checks.append(v1._check("atr", atr, ">", v1.ZERO))
        if not checks[-1].passed:
            return decision
        setup = v1._Setup(SPIKE, tuple(checks[2:]), mover.close, mover.low)
        levels = {"setup": SPIKE, "trigger": setup.trigger, "invalidation": setup.invalidation}
        if context.quote is None:
            return self._decide(context, Verdict.WATCH, checks, **levels)
        return self._with_quote(context, context.quote, setup, atr, checks, levels)


def create(values: Mapping[str, Any]) -> PriceActionV3:
    """Build the strategy from parsed strategy.toml values (ValueError on any key problem)."""
    return PriceActionV3(*parse_settings(values))
