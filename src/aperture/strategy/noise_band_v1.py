"""Strategy noise-band-v1: buy when a stock moves beyond its usual move for the time of day
(an observer candidate from docs/strategy-ledger.md L2, never the active strategy unless the
owner selects it).

Values come from `strategies/noise-band-v1/strategy.toml`. On the last completed M1 bar,
`minutes` after the regular open:

1. The session's first bar is the 09:30 bar (its open is the session open), the previous
   session's last close is at least `min_previous_close_usd`, and `minutes` is within
   [`earliest_minutes_after_open`, `latest_minutes_after_open`].
2. Band = mean over the `prior_sessions` prior sessions of |close of their bar at the same New
   York minute / their 09:30 open - 1|. Every prior session must have both bars.
3. The close is above session open x (1 + `band_multiple` x band).
4. This is the first bar of the session where 1-3 hold. An earlier bar whose band is unknown
   (a prior session lacks that minute) counts as not fired.

That is one signal bar per symbol and session: if the quote does not allow an entry during
that minute, the session is spent. Then ENTER with the stop at the higher of the band level
and the session VWAP (VWAP only while it is below the close), or lower, at the minimum stop
distance below the entry window, and the
exit left to the platform's session flatten. A half day among the prior sessions has no bar
after its early close, so later minutes get no band and no signal until it leaves the window.
The history test (L2) is positive only on days that later became gainers, which is
look-ahead; this module exists to test it live, where nothing can look ahead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Any

from aperture.strategy import indicators as ind
from aperture.strategy import price_action_v1 as v1
from aperture.strategy import simple
from aperture.strategy.base import Decision, SymbolContext, Verdict
from aperture.types import Bar

NAME = "noise-band-v1"
SETUP = "noise_band"
REGULAR_OPEN = time(9, 30)
MINUTES_PER_HOUR = 60
REGULAR_SESSION_MINUTES = 390  # 09:30 to 16:00, minute bars 0..389

_SIGNAL: simple.Schema = {
    "signal": {
        "band_multiple": v1._positive,
        "prior_sessions": v1._positive_int,
        "earliest_minutes_after_open": v1._positive_int,
        "latest_minutes_after_open": v1._positive_int,
    },
}


@dataclass(frozen=True)
class Signal:
    band_multiple: Decimal
    prior_sessions: int
    earliest_minutes_after_open: int
    latest_minutes_after_open: int


def _minutes_after_open(bar: Bar) -> int:
    t = ind.ny_time(bar)
    return (t.hour - REGULAR_OPEN.hour) * MINUTES_PER_HOUR + t.minute - REGULAR_OPEN.minute


def _closes_by_minute(session: Sequence[Bar]) -> tuple[Decimal | None, dict[int, Decimal]]:
    """A prior session's 09:30 open (None without the 09:30 bar) and its closes by minute
    after the open."""
    closes = {_minutes_after_open(b): b.close for b in session}
    has_open = bool(session) and _minutes_after_open(session[0]) == 0 and session[0].open > 0
    return (session[0].open if has_open else None), closes


class NoiseBandV1:
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
        checks.append(v1._flag("session_open_bar", _minutes_after_open(session[0]) == 0,
                               "the session's first bar is the 09:30 bar"))
        if not checks[-1].passed:
            return ignore()
        session_open = session[0].open
        prior_closes = [_closes_by_minute(p) for p in prior]
        minutes = [_minutes_after_open(b) for b in session]

        def band(minute: int) -> Decimal | None:
            """Mean |close / open - 1| at `minute` over the prior sessions; None if any of
            them lacks the 09:30 bar or that minute."""
            moves = []
            for opened, closes in prior_closes:
                if opened is None or minute not in closes:
                    return None
                moves.append(abs(closes[minute] / opened - simple.ONE))
            return sum(moves, simple.ZERO) / len(moves)

        def in_hours(minute: int) -> bool:
            return s.earliest_minutes_after_open <= minute <= s.latest_minutes_after_open

        def fires(i: int) -> bool:
            if not in_hours(minutes[i]):
                return False
            b = band(minutes[i])
            return b is not None and session[i].close > session_open * (
                simple.ONE + s.band_multiple * b)

        if in_hours(minutes[-1]) and band(minutes[-1]) is None:
            checks.append(v1._flag("prior_band_complete", False,
                                   "a prior session lacks its 09:30 bar or this minute"))
            return ignore()
        checks.append(simple.first_signal(session, fires))
        if not checks[-1].passed:
            return ignore()
        last = session[-1]
        last_band = band(minutes[-1])
        assert last_band is not None  # the signal fired on this bar
        level = session_open * (simple.ONE + s.band_multiple * last_band)
        vwap = ind.vwap_series(session)[-1]
        # VWAP raises the stop only while it is below the close; above it, it is no support.
        stop_level = level if vwap is None or vwap >= last.close else max(level, vwap)
        return simple.enter(self, c, context, checks, SETUP, last.close, stop_level)


def create(values: Mapping[str, Any]) -> NoiseBandV1:
    """Build the strategy from parsed strategy.toml values (ValueError on any key problem)."""
    flat = simple.parse(values, _SIGNAL, NAME)
    signal = Signal(**{k: flat[k] for k in Signal.__dataclass_fields__})
    if signal.latest_minutes_after_open >= REGULAR_SESSION_MINUTES:
        raise ValueError("[signal] latest_minutes_after_open must be inside the regular session")
    if signal.earliest_minutes_after_open > signal.latest_minutes_after_open:
        raise ValueError("[signal] earliest_minutes_after_open must not exceed "
                         "latest_minutes_after_open")
    return NoiseBandV1(simple.common(flat), signal)
