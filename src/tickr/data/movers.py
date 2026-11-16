"""Fast movers: symbols whose latest completed M1 bar breaks sharply from the bars before it.

One detector, two readers (owner 2026-11-16): the apps' fast-movers list and the spike setup of
price-action-v3, which trades an upward fast mover. The thresholds are the active strategy's
(`[spike]` of its strategy.toml), so the list shows exactly what the strategy would act on. It
is measured on completed session bars the bot already reads, so it costs no market-data
request of its own.

For the latest completed bar of the regular session, against the `lookback_bars` bars before it:
- volume ratio: its volume / the mean volume of those bars;
- move in ATR: (its close - the previous close) / the mean true range of those bars (signed);
- dollar-volume ratio: its close x volume / the mean close x volume of those bars.
A symbol is a fast mover when the bar trades at least `min_bar_dollar_volume_usd` and at least
one signal reaches its threshold (the move by its size, either way). Each signal keeps its value
and whether it fired, so the apps show why.

A symbol with fewer than `lookback_bars + 2` session bars (the bar, the lookback, and one more
bar for the first true range) is not measured, and so is one whose lookback has no volume or no
range: it is never called a mover by a guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tickr.types import Bar


@dataclass(frozen=True)
class MoverSettings:
    lookback_bars: int
    min_volume_ratio: Decimal
    min_move_atr: Decimal
    min_dollar_volume_ratio: Decimal
    min_bar_dollar_volume_usd: Decimal


@dataclass(frozen=True)
class Signal:
    """One measured signal of the latest bar and whether it reached its threshold."""

    value: Decimal
    threshold: Decimal
    fired: bool


@dataclass(frozen=True)
class Mover:
    """The latest bar of one symbol that broke sharply from the bars before it."""

    symbol: str
    bar_start: datetime
    close: Decimal
    low: Decimal
    bar_dollar_volume: Decimal
    volume_ratio: Signal
    move_atr: Signal  # signed: above 0 the bar closed up
    dollar_volume_ratio: Signal

    @property
    def spike_up(self) -> bool:
        """The volume broke out and the bar closed up by at least the move threshold: what the
        spike setup trades (long only)."""
        return self.volume_ratio.fired and self.move_atr.value >= self.move_atr.threshold


def _true_range(bar: Bar, previous_close: Decimal) -> Decimal:
    return max(bar.high - bar.low, abs(bar.high - previous_close),
               abs(bar.low - previous_close))


def measure(symbol: str, session: Sequence[Bar], settings: MoverSettings) -> Mover | None:
    """The fast mover of `session` (completed regular-session bars, oldest first), or None when
    the latest bar is not one or cannot be measured (see the module docstring)."""
    n = settings.lookback_bars
    if len(session) < n + 2:
        return None
    last, previous = session[-1], session[-2]
    window = session[-n - 1:-1]
    before = session[-n - 2:-2]
    volume = sum((b.volume for b in window), Decimal(0)) / n
    dollars = sum((b.close * b.volume for b in window), Decimal(0)) / n
    atr = sum((_true_range(b, p.close) for b, p in zip(window, before, strict=True)),
              Decimal(0)) / n
    if volume <= 0 or dollars <= 0 or atr <= 0:
        return None
    bar_dollars = last.close * last.volume
    if bar_dollars < settings.min_bar_dollar_volume_usd:
        return None
    volume_ratio = last.volume / volume
    move = (last.close - previous.close) / atr
    dollar_ratio = bar_dollars / dollars
    signals = (
        Signal(volume_ratio, settings.min_volume_ratio, volume_ratio >= settings.min_volume_ratio),
        Signal(move, settings.min_move_atr, abs(move) >= settings.min_move_atr),
        Signal(dollar_ratio, settings.min_dollar_volume_ratio,
               dollar_ratio >= settings.min_dollar_volume_ratio),
    )
    if not any(s.fired for s in signals):
        return None
    return Mover(symbol, last.start, last.close, last.low, bar_dollars, *signals)
