"""What the price did after each recorded WATCH and ENTER decision. OBSERVED, never profit.

For every record whose verdict is WATCH or ENTER (ENTERs taken, refused or never evaluated
alike) and every configured horizon `h` minutes:

- reference = close of the decision bar (the price when the bar the strategy saw completed);
- horizon time = end of the decision bar + `h` minutes; the price there is the close of the
  last bar that started at or before `horizon time - 1 minute` (a minute without trades
  leaves the last price unchanged);
- move = price - reference; max up / max down = highest high / lowest low of the bars after
  the decision bar up to the horizon, minus the reference (signed; no trades = 0);
- each in percent of the reference and in ATR (Wilder, `atr_period`, computed over the
  symbol's bars up to and including the decision bar, so no later bar is used).

A horizon is measured only when it ends at or before the regular-session close of the
decision's date (from the session calendar, so half days count; post-market bars are never
used) and the captured bars of that date reach it (some bar starts at or after the horizon's
last minute); otherwise the row carries a cause and no numbers. A missing decision bar or an
unavailable ATR is also a cause, never a guess.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from tickr.calendar import CalendarRefused
from tickr.research.records import Admission, DecisionRecord
from tickr.research.settings import ResearchSettings
from tickr.strategy.base import Verdict
from tickr.strategy.indicators import atr_series
from tickr.types import Bar

LABEL = "observed"
MEASURED_VERDICTS = frozenset({Verdict.WATCH, Verdict.ENTER})
M1 = timedelta(minutes=1)
HUNDRED = Decimal(100)

CloseFor = Callable[[date], datetime]
"""Regular-session close (aware) of an exchange date; raises CalendarRefused for a day the
calendar refuses."""


@dataclass(frozen=True)
class Outcome:
    record: DecisionRecord
    horizon_minutes: int
    reference: Decimal | None
    atr: Decimal | None
    price: Decimal | None
    move: Decimal | None
    max_up: Decimal | None
    max_down: Decimal | None
    cause: str
    """Empty when every number is present; otherwise why some or all are missing."""

    def percent(self, value: Decimal | None) -> Decimal | None:
        if value is None or self.reference is None:
            return None
        return value / self.reference * HUNDRED

    def in_atr(self, value: Decimal | None) -> Decimal | None:
        if value is None or not self.atr:
            return None
        return value / self.atr


class _SymbolBars:
    def __init__(self, bars: Sequence[Bar], settings: ResearchSettings) -> None:
        self.bars = tuple(bars)
        self.starts = [b.start for b in self.bars]
        self.index = {b.start: i for i, b in enumerate(self.bars)}
        self.atr = atr_series(self.bars, settings.atr_period)
        self.zone = settings.session_timezone

    def day(self, i: int) -> date:
        return self.bars[i].start.astimezone(self.zone).date()


def _measure(
    record: DecisionRecord,
    horizon: int,
    series: _SymbolBars | None,
    settings: ResearchSettings,
    close: datetime | str,
) -> Outcome:
    def empty(cause: str, reference=None, atr=None) -> Outcome:
        return Outcome(record, horizon, reference, atr, None, None, None, None, cause)

    if series is None:
        return empty("no bars for the symbol")
    i = series.index.get(record.bar_start)
    if i is None:
        return empty("decision bar not in bars")
    reference = series.bars[i].close
    atr = series.atr[i]
    atr_cause = ""
    if atr is None:
        atr_cause = f"atr unavailable (fewer than {settings.atr_period} bars)"
    elif atr == 0:
        atr_cause = "atr is zero"
    last_minute: datetime = record.bar_start + horizon * M1
    if isinstance(close, str):
        return empty(close, reference, atr)
    if last_minute + M1 > close:
        return empty("horizon ends after the regular close", reference, atr)
    day = series.day(i)
    reach = bisect_right(series.starts, last_minute - M1)  # first bar starting at/after it
    covered = reach < len(series.bars) and series.day(reach) == day
    if not covered:
        return empty("bars of the session end before the horizon", reference, atr)
    k = bisect_right(series.starts, last_minute) - 1  # last bar starting at or before it
    window = series.bars[i + 1 : k + 1]
    price = series.bars[k].close
    high = max((b.high for b in window), default=reference)
    low = min((b.low for b in window), default=reference)
    return Outcome(
        record=record,
        horizon_minutes=horizon,
        reference=reference,
        atr=atr if atr else None,
        price=price,
        move=price - reference,
        max_up=high - reference,
        max_down=low - reference,
        cause=atr_cause,
    )


def measured(record: DecisionRecord) -> bool:
    return record.decision.verdict in MEASURED_VERDICTS


def outcomes(
    records: Sequence[DecisionRecord],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    settings: ResearchSettings,
    close_for: CloseFor,
) -> tuple[Outcome, ...]:
    """One Outcome per (WATCH/ENTER record, horizon), in record order."""
    cache: dict[str, _SymbolBars | None] = {}
    closes: dict[date, datetime | str] = {}
    out: list[Outcome] = []
    for record in records:
        if not measured(record):
            continue
        symbol = record.symbol
        if symbol not in cache:
            bars = bars_by_symbol.get(symbol)
            cache[symbol] = _SymbolBars(bars, settings) if bars else None
        day = record.bar_start.astimezone(settings.session_timezone).date()
        if day not in closes:
            try:
                closes[day] = close_for(day)
            except CalendarRefused as exc:
                closes[day] = f"calendar refuses {day.isoformat()}: {exc}"
        for horizon in settings.horizons_minutes:
            out.append(_measure(record, horizon, cache[symbol], settings, closes[day]))
    return tuple(out)


def group_key(record: DecisionRecord) -> str:
    """Summary group: WATCH, or ENTER split by what admission did."""
    verdict = record.decision.verdict.value
    if record.decision.verdict is Verdict.ENTER:
        return f"{verdict}:{record.admission.value}"
    return verdict


@dataclass(frozen=True)
class GroupStats:
    group: str
    horizon_minutes: int
    rows: int
    measured: int
    up: int
    down: int
    mean_move_percent: Decimal | None
    median_move_percent: Decimal | None
    mean_move_atr: Decimal | None
    median_move_atr: Decimal | None


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize(rows: Sequence[Outcome]) -> tuple[GroupStats, ...]:
    """Per (group, horizon): how many rows, how many measured, direction and typical move."""
    grouped: dict[tuple[str, int], list[Outcome]] = {}
    for row in rows:
        grouped.setdefault((group_key(row.record), row.horizon_minutes), []).append(row)
    order = {a.value: n for n, a in enumerate(Admission)}

    def sort_key(item: tuple[str, int]) -> tuple[str, int, int]:
        group, horizon = item
        return (group.split(":")[0], order.get(group.partition(":")[2], -1), horizon)

    stats = []
    for key in sorted(grouped, key=sort_key):
        group_rows = grouped[key]
        moves = [r for r in group_rows if r.move is not None]
        pct = [p for r in moves if (p := r.percent(r.move)) is not None]
        atr = [a for r in moves if (a := r.in_atr(r.move)) is not None]
        stats.append(
            GroupStats(
                group=key[0],
                horizon_minutes=key[1],
                rows=len(group_rows),
                measured=len(moves),
                up=sum(1 for r in moves if r.move is not None and r.move > 0),
                down=sum(1 for r in moves if r.move is not None and r.move < 0),
                mean_move_percent=_mean(pct),
                median_move_percent=_median(pct),
                mean_move_atr=_mean(atr),
                median_move_atr=_median(atr),
            )
        )
    return tuple(stats)
