"""Strategy volume-shock-v1: buy a volume shock in a stock that is up a little (an
observer candidate from docs/strategy-ledger.md L4, never the active strategy unless the owner
selects it).

Values come from `strategies/volume-shock-v1/strategy.toml`. On the last completed M1 bar:

1. The previous session's last close is at least `min_previous_close_usd`, the session's
   dollar volume so far is at least `min_dollar_volume_usd` ([liquidity], also applied by
   discovery), and the bar's close is between `change_min_percent` and `change_max_percent`
   above it.
2. The volume of the last `volume_window_bars` bars is at least `volume_shock_multiple` times
   the median volume of the same New York minutes over the `prior_sessions` prior sessions.
   Every prior session must hold every one of those minutes; a missing minute (half day,
   halt, data gap) means no signal on that bar, and on the last bar a failed
   `prior_window_complete` check. Today's window must be consecutive minutes too.
3. The bar closes in the top of its range: (close - low) / (high - low) >= `close_location_min`.
4. This is the first bar of the session where 1-3 hold, it started at most
   `signal_valid_minutes` - 1 minutes ago ([entry]), and no later bar traded below its low.
   After that the session is spent.

There is no time-of-day limit (the history test had none); the platform's last-entry time
applies. Then ENTER with the stop at the signal bar's low (or lower, at the minimum stop
distance below the entry window's floor and the bid) and the exit left to the platform's
session flatten. The history test (L4) is positive only on days that later became gainers,
which is look-ahead; this module exists to test it live, where the universe is the bot's own
and nothing can look ahead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from statistics import median
from typing import Any

from tickr.strategy import indicators as ind
from tickr.strategy import price_action_v1 as v1
from tickr.strategy import simple
from tickr.strategy.base import Decision, SymbolContext, Verdict
from tickr.types import Bar

NAME = "volume-shock-v1"
SETUP = "volume_shock"
M1 = simple.M1

_SIGNAL: simple.Schema = {
    "signal": {
        "change_min_percent": v1._positive,
        "change_max_percent": v1._positive,
        "volume_window_bars": v1._positive_int,
        "volume_shock_multiple": v1._positive,
        "prior_sessions": v1._positive_int,
        "close_location_min": v1._non_negative,
    },
}


@dataclass(frozen=True)
class Signal:
    change_min_percent: Decimal
    change_max_percent: Decimal
    volume_window_bars: int
    volume_shock_multiple: Decimal
    prior_sessions: int
    close_location_min: Decimal


def _volume_by_minute(bars: Sequence[Bar]) -> dict[time, Decimal]:
    return {ind.ny_time(b): b.volume for b in bars}


class VolumeShockV1(simple.Liquid):
    def __init__(self, common: simple.Common, signal: Signal) -> None:
        self.common, self.signal = common, signal
        self.name, self.version = common.name, common.version
        self.prior_sessions_needed = signal.prior_sessions

    def evaluate(self, context: SymbolContext) -> Decision:
        c, s = self.common, self.signal
        checks = [simple.consistent(context)]

        def ignore() -> Decision:
            return simple.decide(self, context, Verdict.IGNORE, checks)

        if context.position is not None:
            checks[-1] = v1._flag("position_open", True, "exit by resting stop or session flatten")
            return simple.decide(self, context, Verdict.HOLD, checks)
        if not checks[-1].passed:
            return ignore()
        prior = [p for p in context.prior_sessions[-s.prior_sessions:] if p]
        checks.append(v1._check("prior_sessions", Decimal(len(prior)), ">=",
                                Decimal(s.prior_sessions), "non-empty prior sessions given"))
        if not checks[-1].passed:
            return ignore()
        previous = simple.previous_close(context)
        assert previous is not None  # guaranteed by prior_sessions >= 1
        checks.append(v1._check("previous_close_usd", previous, ">=", c.min_previous_close_usd))
        if not checks[-1].passed:
            return ignore()
        session = ind.current_session(context.bars)
        checks.append(simple.session_from_open(session))
        if not checks[-1].passed:
            return ignore()
        checks.append(v1.dollar_volume_check(session, c.min_dollar_volume_usd))
        if not checks[-1].passed:
            return ignore()
        low = previous * (simple.ONE + s.change_min_percent / simple.PERCENT)
        high = previous * (simple.ONE + s.change_max_percent / simple.PERCENT)

        window = s.volume_window_bars
        minutes = [ind.ny_time(b) for b in session]
        prior_volume = [_volume_by_minute(p) for p in prior]

        def normal(i: int) -> Decimal | None:
            """Median prior volume of the window ending at bar i; None if any minute is
            missing in any prior session."""
            span = minutes[i + 1 - window:i + 1]
            totals = []
            for volumes in prior_volume:
                if any(m not in volumes for m in span):
                    return None
                totals.append(sum((volumes[m] for m in span), simple.ZERO))
            return median(totals)

        def fires(i: int) -> bool:
            if i + 1 < window:
                return False
            bar = session[i]
            if not low <= bar.close <= high or bar.high <= bar.low:
                return False
            if (bar.close - bar.low) / (bar.high - bar.low) < s.close_location_min:
                return False
            if bar.start - session[i + 1 - window].start != (window - 1) * M1:
                return False  # a missing minute today: the window is not the same minutes
            usual = normal(i)
            if usual is None or usual <= simple.ZERO:
                return False
            mine = sum((b.volume for b in session[i + 1 - window:i + 1]), simple.ZERO)
            return mine >= s.volume_shock_multiple * usual

        last_index = len(session) - 1
        if last_index + 1 >= window and normal(last_index) is None and not any(
                fires(i) for i in range(last_index)):
            checks.append(v1._flag("prior_window_complete", False,
                                   "a prior session lacks a minute of the volume window"))
            return ignore()
        check, first = simple.first_signal(session, fires, c.signal_valid_minutes)
        checks.append(check)
        if first is None:
            return ignore()
        bar = session[first]
        checks.append(simple.not_invalidated(session, first, bar.low))
        if not checks[-1].passed:
            return ignore()
        return simple.enter(self, c, context, checks, SETUP, bar.close, bar.low)


def create(values: Mapping[str, Any]) -> VolumeShockV1:
    """Build the strategy from parsed strategy.toml values (ValueError on any key problem)."""
    flat = simple.parse(values, _SIGNAL, NAME)
    signal = Signal(**{k: flat[k] for k in Signal.__dataclass_fields__})
    if signal.close_location_min > simple.ONE:
        raise ValueError("[signal] close_location_min must be at most 1")
    if signal.change_min_percent >= signal.change_max_percent:
        raise ValueError("[signal] change_min_percent must be below change_max_percent")
    return VolumeShockV1(simple.common(flat), signal)
