"""What the price did after a closed trade's exit or a skipped signal, a trade's overshoot, and
what a skipped signal would have given. Display only and read-only (docs/api.md, "Prices after,
overshoot and what if"): computed from the captured M1 bars, the session calendar and, for the
what-if, the replay settings. Nothing here reads the broker or the risk path, and nothing here
feeds them.

- A point `minutes` after an anchor (a trade's `closed_at`, a skipped signal's decision bar
  start) is the close of the captured M1 bar covering `anchor + minutes`: the bar whose start
  is that moment floored to the minute. For a signal that is the bar ending `minutes` after
  the decision bar ends, the price research `outcomes` measures. Null with a cause when the
  moment is at or after the regular close, the symbol has no bar that day, the bars end
  before it, or no bar was recorded for that minute (never the price of another minute).
- Overshoot (a long exit): the highest high of the bars after the exit minute, up to the bar
  covering `closed_at + longest offset` (or the last bar before the regular close, if
  sooner), minus the exit price. The exit minute's bar is left out (it may hold prices from
  before the exit). Signed: below zero, the price never came back to the exit.
- What-if: the signal replayed alone by `research.whatif.simulate_one` (the replay's
  conservative fill model and `config/replay.toml`, as GET /observers), for the recorded
  quantity. Today's simulated trade still open ends at the last captured bar's close.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from aperture.api import contract as api
from aperture.api.observer_results import (
    NoInputs,
    SimulationPaths,
    day_bars,
    folder_stamp,
    inputs_key,
    replay_inputs,
)
from aperture.calendar import CalendarRefused, SessionCalendar
from aperture.replay.clock import ClockFor
from aperture.replay.errors import ReplayError
from aperture.replay.settings import ReplaySettings
from aperture.research.errors import ResearchError
from aperture.research.records import DecisionRecord
from aperture.research.whatif import simulate_one
from aperture.types import Bar

M1 = timedelta(minutes=1)
Close = datetime | str  # the day's regular close, or why it is unknown


class _Priced(Protocol):
    @property
    def start(self) -> datetime: ...
    @property
    def high(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...


@dataclass(frozen=True)
class AfterSettings:
    minutes: tuple[int, ...]  # [api] price_after_minutes
    r_places: int  # [api] after_r_decimal_places
    calendar: Path  # folder of the session calendar files


def _minute(at: datetime) -> datetime:
    return at.replace(second=0, microsecond=0)


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places))


class Closes:
    """Regular close per New York date from the session calendar, reloaded once its files
    change. Thread safe."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._lock = threading.Lock()
        self._key: object = None
        self._calendar: SessionCalendar | None = None

    def close(self, day: date) -> Close:
        key = folder_stamp(self.folder)
        with self._lock:
            calendar = self._calendar if key is not None and key == self._key else None
        if calendar is None:
            try:
                calendar = SessionCalendar.load(self.folder)
            except (CalendarRefused, OSError) as exc:
                return f"session calendar unreadable: {exc}"
            with self._lock:
                self._key, self._calendar = key, calendar
        try:
            return calendar.day(day).regular_close
        except CalendarRefused as exc:
            return f"calendar refuses {day.isoformat()}: {exc}"


def prices_after(symbol: str, bars: Sequence[_Priced], anchor: datetime,
                 reference: Decimal | None, reference_cause: str | None,
                 minutes: Sequence[int], close: Close) -> tuple[api.PriceAfterV1, ...]:
    """One point per offset in `minutes` after `anchor` (module docstring). `reference`: the
    price `change_usd` is measured from; None with `reference_cause` when unknown."""
    by_start = {b.start: b for b in bars}
    last = bars[-1].start if bars else None
    points = []
    for offset in minutes:
        at = anchor + offset * M1
        price = None
        if isinstance(close, str):
            cause: str | None = close
        elif at >= close:
            cause = f"the session ended at {api.format_time(close)}"
        elif last is None:
            cause = f"no bar recorded for {symbol} that day"
        elif (bar := by_start.get(_minute(at))) is not None:
            price, cause = bar.close, reference_cause
        elif last < _minute(at):
            cause = f"the bars end at {api.format_time(last)}"
        else:
            cause = f"no bar recorded for the minute {api.format_time(_minute(at))}"
        change = None if price is None or reference is None else price - reference
        points.append(api.PriceAfterV1(offset, at, price, change, cause))
    return tuple(points)


def overshoot(trade: api.DayTrade, bars: Sequence[_Priced], longest: int, close: Close,
              r_places: int) -> tuple[Decimal | None, Decimal | None, str | None]:
    """(USD per share, R, cause) of `trade`'s overshoot (module docstring). R is the overshoot
    times the quantity sold over `planned_loss_usd`."""
    if isinstance(close, str):
        return None, None, close
    if not bars:
        return None, None, f"no bar recorded for {trade.symbol} that day"
    first = _minute(trade.closed_at)
    end = min(_minute(trade.closed_at + longest * M1), _minute(close - M1))
    if end <= first:
        return None, None, f"no minute after the exit before the close {api.format_time(close)}"
    if bars[-1].start < end:
        return None, None, (f"the bars end at {api.format_time(bars[-1].start)}, before "
                            f"{api.format_time(end)}")
    highs = [b.high for b in bars if first < b.start <= end]
    if not highs:
        return None, None, (f"no bar recorded after the exit minute up to "
                            f"{api.format_time(end)}")
    usd = max(highs) - trade.exit_price
    if trade.planned_loss_usd <= 0:
        return usd, None, f"planned loss {trade.planned_loss_usd} is not positive: no R"
    return usd, _quantize(usd * trade.quantity / trade.planned_loss_usd, r_places), None


def trade_after(trade: api.DayTrade, bars: Sequence[_Priced], close: Close,
                settings: AfterSettings) -> api.DayTrade:
    """`trade` with its prices after the exit and its overshoot. Display only: an unexpected
    error shows null with its cause, never breaks the document."""
    try:
        points = prices_after(trade.symbol, bars, trade.closed_at, trade.exit_price, None,
                              settings.minutes, close)
        usd, r, cause = overshoot(trade, bars, settings.minutes[-1], close, settings.r_places)
    except Exception as exc:  # noqa: BLE001 - display only, see docstring
        failed = f"{type(exc).__name__}: {exc}"
        return replace(trade, prices_after=(), prices_after_cause=failed, overshoot_usd=None,
                       overshoot_r=None, overshoot_cause=failed)
    return replace(trade, prices_after=points, prices_after_cause=None, overshoot_usd=usd,
                   overshoot_r=r, overshoot_cause=cause)


def what_if(record: DecisionRecord, bars: Sequence[Bar], settings: ReplaySettings,
            clock_for: ClockFor) -> api.WhatIfV1:
    """`record` entered alone in the replay simulation, rounded like GET /observers."""
    try:
        found = simulate_one(record, bars, settings, clock_for)
    except (ReplayError, ResearchError) as exc:
        return api.WhatIfV1(None, None, None, f"simulation refused: {exc}")
    if found.trade is None or found.result_usd is None:
        return api.WhatIfV1(None, None, None, f"{found.outcome.value}: {found.cause}")
    places = settings.output_decimal_places
    return api.WhatIfV1(_quantize(found.result_usd, places),
                        _quantize(found.trade.r_multiple, places), found.trade.exit_reason, None)


def _signal_after(signal: api.SkippedSignalV1, record: DecisionRecord,
                  bars: Mapping[str, tuple[Bar, ...]] | str, close: Close,
                  replay: tuple[ReplaySettings, ClockFor] | str,
                  minutes: Sequence[int]) -> api.SkippedSignalV1:
    """`signal` with its prices after and what-if; `bars` / `replay` are a cause when they
    could not be read. Display only, as `trade_after`."""
    try:
        if isinstance(bars, str):
            points, points_cause = (), bars
        else:
            series = bars.get(record.symbol, ())
            decision = next((b for b in series if b.start == record.bar_start), None)
            points, points_cause = prices_after(
                record.symbol, series, record.bar_start,
                None if decision is None else decision.close,
                "the decision bar was not captured: no reference price" if decision is None
                else None, minutes, close), None
        if isinstance(replay, str) or isinstance(bars, str):
            result = api.WhatIfV1(None, None, None, replay if isinstance(replay, str) else bars)
        else:
            result = what_if(record, bars.get(record.symbol, ()), *replay)
    except Exception as exc:  # noqa: BLE001 - display only, see docstring
        failed = f"{type(exc).__name__}: {exc}"
        return replace(signal, prices_after=(), prices_after_cause=failed,
                       what_if=api.WhatIfV1(None, None, None, failed))
    return replace(signal, prices_after=points, prices_after_cause=points_cause,
                   what_if=result)


class SkippedAfter:
    """GET /skipped with each row's prices after and what-if. Thread safe; remembers the last
    day it answered until one of its input files changes (the key GET /observers uses), so a
    client polling the same day simulates again only after a new bar or record. An answer a
    transient failure decided (an OSError, an unreadable calendar) is not kept."""

    def __init__(self, paths: SimulationPaths, minutes: tuple[int, ...]) -> None:
        self.paths = paths
        self.minutes = minutes
        self.closes = Closes(paths.calendar)
        self._lock = threading.Lock()
        self._key: tuple[object, ...] | None = None
        self._value: api.SkippedV1 | None = None

    def document(self, day: date, today: date, records: tuple[DecisionRecord, ...],
                 decision_file: Path) -> api.SkippedV1:
        key = inputs_key(self.paths, day, today, decision_file)
        with self._lock:
            if key == self._key and self._value is not None:
                return self._value
        base = api.skipped_document(day, records)
        transient = False
        bars: Mapping[str, tuple[Bar, ...]] | str = {}
        replay: tuple[ReplaySettings, ClockFor] | str = ""
        close: Close = ""
        if base.signals:
            try:
                bars = day_bars(self.paths, day, today)
            except NoInputs as exc:
                bars, transient = str(exc), exc.transient
            try:
                replay = replay_inputs(self.paths)
            except NoInputs as exc:
                replay, transient = str(exc), transient or exc.transient
            close = self.closes.close(day)
            transient = transient or isinstance(close, str)
        value = replace(base, signals=tuple(
            _signal_after(s, r, bars, close, replay, self.minutes)
            for s, r in zip(base.signals, api.skipped_records(records), strict=True)))
        if not transient:
            with self._lock:
                self._key, self._value = key, value
        return value
