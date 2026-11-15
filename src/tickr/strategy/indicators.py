"""Pure indicator functions over completed M1 bars.

Every series function returns one value per input position, and the value at position i
depends only on inputs 0..i (no look-ahead; tested). A value that cannot be computed yet is
None, never a guess. All arithmetic is Decimal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from tickr.types import Bar

NEW_YORK = ZoneInfo("America/New_York")
ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
THREE = Decimal(3)


def ny_date(bar: Bar) -> date:
    """The New York calendar date of the bar's start."""
    return bar.start.astimezone(NEW_YORK).date()


def ny_time(bar: Bar) -> time:
    """The New York wall-clock time of the bar's start."""
    return bar.start.astimezone(NEW_YORK).time()


def current_session(bars: Sequence[Bar]) -> Sequence[Bar]:
    """The trailing bars that share the New York date of the last bar (oldest first)."""
    if not bars:
        return bars
    day = ny_date(bars[-1])
    first = len(bars)
    while first > 0 and ny_date(bars[first - 1]) == day:
        first -= 1
    return bars[first:]


def ema_series(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Exponential moving average seeded with the first value, then
    `ema = prev + (value - prev) * 2 / (period + 1)`. Defined from the first value; callers
    require enough history for the seed to have decayed."""
    out: list[Decimal | None] = []
    alpha = TWO / (period + 1)
    prev: Decimal | None = None
    for value in values:
        prev = value if prev is None else prev + (value - prev) * alpha
        out.append(prev)
    return out


def true_range_series(bars: Sequence[Bar]) -> list[Decimal]:
    """True range; the first bar has no previous close, so its range is high - low."""
    out: list[Decimal] = []
    for i, bar in enumerate(bars):
        if i == 0:
            out.append(bar.high - bar.low)
            continue
        prev_close = bars[i - 1].close
        out.append(max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)))
    return out


def atr_series(bars: Sequence[Bar], period: int) -> list[Decimal | None]:
    """Wilder average true range: the first value is the simple average of the first
    `period` true ranges, then `atr = (prev * (period - 1) + tr) / period`."""
    out: list[Decimal | None] = []
    prev: Decimal | None = None
    ranges = true_range_series(bars)
    for i, tr in enumerate(ranges):
        if i + 1 < period:
            out.append(None)
            continue
        if prev is None:
            prev = sum(ranges[: i + 1], ZERO) / period
        else:
            prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


def vwap_series(session_bars: Sequence[Bar]) -> list[Decimal | None]:
    """Session VWAP of the typical price (high + low + close) / 3, cumulative from the
    first bar given. None while cumulative volume is zero."""
    out: list[Decimal | None] = []
    price_volume = ZERO
    volume = ZERO
    for bar in session_bars:
        price_volume += (bar.high + bar.low + bar.close) / THREE * bar.volume
        volume += bar.volume
        out.append(price_volume / volume if volume > ZERO else None)
    return out


def dollar_volume(session_bars: Sequence[Bar]) -> Decimal:
    """Sum of close x volume over the given completed bars, USD."""
    return sum((bar.close * bar.volume for bar in session_bars), ZERO)


def cumulative_volume_until(session_bars: Sequence[Bar], until: time) -> Decimal:
    """Volume of the bars whose New York start time is at or before `until`."""
    return sum((bar.volume for bar in session_bars if ny_time(bar) <= until), ZERO)


def relative_volume(
    session_bars: Sequence[Bar], prior_sessions: Sequence[Sequence[Bar]], sessions: int
) -> Decimal | None:
    """Current cumulative volume divided by the average cumulative volume of the most
    recent `sessions` prior sessions up to the same New York minute.

    None when there are fewer prior sessions than required, when the current session is
    empty, or when the prior average is zero (the ratio is undefined)."""
    if not session_bars or len(prior_sessions) < sessions:
        return None
    until = ny_time(session_bars[-1])
    used = prior_sessions[-sessions:]
    average = sum((cumulative_volume_until(s, until) for s in used), ZERO) / sessions
    if average <= ZERO:
        return None
    return cumulative_volume_until(session_bars, until) / average
