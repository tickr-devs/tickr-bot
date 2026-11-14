"""Prior-session M1 history per symbol: paged reads, refusals with one cause, a per-day cache.

`HistoryReader.prior_sessions(symbol, before, sessions)` returns the regular-session M1 bars of
the `sessions` open trading days strictly before the New York date `before`, oldest first. Today's
bars are not included: they come from `MarketData.bars` as before.

How it reads: pages backwards with `MarketData.bars_before(symbol, end, per_request)`, starting at
the regular close of the last wanted day and moving `end` to the oldest bar received, until a bar
at or before the first wanted day's open arrives, the provider returns nothing older, or the page
limit is reached. Every page takes request budget; `RateLimited` and `DelayedData` propagate and
nothing is cached.

Refusals (`HistoryUnavailable`, one `HistoryCause`), never a silent partial answer:
- CALENDAR: the calendar cannot say which days were sessions (out of coverage, invalid);
- LOOKBACK: fewer than `sessions` open days within `max_lookback_days` before `before`;
- NO_PROGRESS: a page returned a bar at or after its end (end time not applied);
- INCOMPLETE: a page came back empty before a bar at or before the first wanted session's open
  arrived (no older history, or an adapter that drops bars at or after the end it ignored);
- PAGE_LIMIT: `max_pages` pages did not reach the first wanted session's open;
- EMPTY_SESSION: a wanted session has no bar at all (not listed yet, halted all day);
- OUTSIDE_SESSION: a bar inside the window starts outside every wanted regular session;
- CONFLICT: two different bars for the same symbol and minute.

A minute with no bar inside a session is not a refusal: thin symbols do not trade every minute.

Cache: in memory and on disk at `<directory>/<before>/<symbol>-<sessions>.jsonl`, in the replay
bar format, written whole through a temporary file. A cache file that does not parse, or a failed
write, raises `HistoryUnavailable(CACHE)`; a bad file is never silently re-fetched over. A fetched
history stays in memory even when its write failed, so the budget is not spent twice.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from aperture.calendar import (
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
    ny_date_of,
)
from aperture.config import Config
from aperture.data.base import MarketData, MarketDataError
from aperture.data.capture import bar_record
from aperture.replay.bars import parse_bar
from aperture.replay.errors import BarFileError
from aperture.types import Bar

# Symbols are used in cache file names; anything else is refused rather than escaped.
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]*$")
NOT_A_SESSION = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})


class HistoryCause(StrEnum):
    CALENDAR = "CALENDAR"
    LOOKBACK = "LOOKBACK"
    NO_PROGRESS = "NO_PROGRESS"
    INCOMPLETE = "INCOMPLETE"
    PAGE_LIMIT = "PAGE_LIMIT"
    EMPTY_SESSION = "EMPTY_SESSION"
    OUTSIDE_SESSION = "OUTSIDE_SESSION"
    CONFLICT = "CONFLICT"
    CACHE = "CACHE"
    INVALID_REQUEST = "INVALID_REQUEST"


class HistoryUnavailable(MarketDataError):
    """Prior-session history could not be established; `cause` says why."""

    def __init__(self, symbol: str, cause: HistoryCause, detail: str) -> None:
        super().__init__(f"{symbol}: {cause}: {detail}")
        self.symbol = symbol
        self.cause = cause
        self.detail = detail


@dataclass(frozen=True)
class SessionHistory:
    symbol: str
    sessions: tuple[date, ...]  # New York dates, oldest first
    bars: tuple[Bar, ...]  # regular-session bars of those dates, oldest first
    pages: int  # provider requests made (0 when served from cache)

    def bars_of(self, day: date) -> tuple[Bar, ...]:
        """Bars of one of the sessions (by New York date of the session)."""
        if day not in self.sessions:
            raise KeyError(day)
        return tuple(b for b in self.bars if ny_date_of(b.start) == day)


@dataclass(frozen=True)
class HistorySettings:
    bars_per_request: int
    max_pages: int
    max_lookback_days: int

    @classmethod
    def from_config(cls, config: Config) -> HistorySettings:
        md = config.market_data
        return cls(md.history_bars_per_request, md.history_max_pages_per_symbol,
                   md.history_max_lookback_days)


class HistoryReader:
    """Thread safe. One lock: a read holds it across its pages, so reads of different symbols
    wait for each other (fine for a per-day lookup; not for a hot path).

    A refusal is remembered in memory for the (symbol, day, sessions) and raised again without
    requests; budget refusals (RateLimited) and DelayedData are not refusals and are not kept.
    A failed disk write keeps the history in memory but is not remembered as a refusal."""

    def __init__(self, source: MarketData, calendar: SessionCalendar,
                 settings: HistorySettings, directory: Path | None) -> None:
        self._source = source
        self._calendar = calendar
        self._settings = settings
        self._directory = directory
        self._lock = threading.Lock()
        self._memory: dict[tuple[str, date, int], SessionHistory | HistoryUnavailable] = {}

    @classmethod
    def from_config(cls, config: Config, source: MarketData,
                    calendar: SessionCalendar) -> HistoryReader:
        directory = (config.root / config.run.data_dir / config.market_data.history_dir
                     / config.environment_name)
        return cls(source, calendar, HistorySettings.from_config(config), directory)

    def prior_sessions(self, symbol: str, before: date, sessions: int) -> SessionHistory:
        if not SYMBOL_PATTERN.match(symbol):
            raise HistoryUnavailable(symbol, HistoryCause.INVALID_REQUEST,
                                     f"symbol {symbol!r} not allowed")
        if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1:
            raise HistoryUnavailable(symbol, HistoryCause.INVALID_REQUEST,
                                     f"sessions must be a positive int, got {sessions!r}")
        key = (symbol, before, sessions)
        with self._lock:
            # A new New York day drops older entries: the cache is per day asked on.
            for old in [k for k in self._memory if k[1] != before]:
                del self._memory[old]
            cached = self._memory.get(key)
            if isinstance(cached, HistoryUnavailable):
                raise cached
            if cached is not None:
                return cached
            try:
                days = self._sessions_before(symbol, before, sessions)
                history = self._load(symbol, before, sessions, days)
                if history is None:
                    history = self._fetch(symbol, days)
                    self._memory[key] = history  # kept even if the disk write below fails
                    self._store(before, sessions, history)
            except HistoryUnavailable as refused:
                if refused.cause is not HistoryCause.CACHE:
                    self._memory[key] = refused  # same answer all day, no budget spent again
                raise
            self._memory[key] = history
            return history

    # -- calendar
    def _sessions_before(self, symbol: str, before: date, sessions: int) -> list[MarketDay]:
        days: list[MarketDay] = []
        for back in range(1, self._settings.max_lookback_days + 1):
            try:
                days.append(self._calendar.day(before - timedelta(days=back)))
            except CalendarRefused as refused:
                if refused.cause in NOT_A_SESSION:
                    continue
                raise HistoryUnavailable(symbol, HistoryCause.CALENDAR, str(refused)) from None
            if len(days) == sessions:
                return list(reversed(days))
        raise HistoryUnavailable(
            symbol, HistoryCause.LOOKBACK,
            f"{len(days)} of {sessions} sessions within {self._settings.max_lookback_days} days "
            f"before {before.isoformat()}")

    # -- provider
    def _fetch(self, symbol: str, days: list[MarketDay]) -> SessionHistory:
        first_open = days[0].regular_open
        end = days[-1].regular_close
        received: dict[datetime, Bar] = {}
        pages = 0
        while True:
            if pages == self._settings.max_pages:
                raise HistoryUnavailable(
                    symbol, HistoryCause.PAGE_LIMIT,
                    f"{pages} pages did not reach {first_open.isoformat()}")
            page = self._source.bars_before(symbol, end, self._settings.bars_per_request)
            pages += 1
            if not page:
                # Nothing older before the first wanted open arrived: either the provider has no
                # more history or it ignored the end time. Either way the window is incomplete.
                oldest = min(received) if received else None
                raise HistoryUnavailable(
                    symbol, HistoryCause.INCOMPLETE,
                    f"nothing before {end.isoformat()}; oldest bar received "
                    f"{oldest.isoformat() if oldest else 'none'}, first wanted open "
                    f"{first_open.isoformat()}")
            for bar in page:
                if bar.symbol != symbol or bar.start >= end:
                    raise HistoryUnavailable(symbol, HistoryCause.NO_PROGRESS,
                                             f"page bar {bar.symbol} {bar.start.isoformat()} "
                                             f"not before {end.isoformat()}")
                known = received.get(bar.start)
                if known is not None and known != bar:
                    raise HistoryUnavailable(symbol, HistoryCause.CONFLICT,
                                             f"two different bars at {bar.start.isoformat()}")
                received[bar.start] = bar
            oldest = min(b.start for b in page)
            if oldest <= first_open:
                break
            end = oldest
        return SessionHistory(symbol, tuple(d.ny_date for d in days),
                              self._window(symbol, days, received.values()), pages)

    @staticmethod
    def _window(symbol: str, days: list[MarketDay], bars: Iterable[Bar]) -> tuple[Bar, ...]:
        first_open, last_close = days[0].regular_open, days[-1].regular_close
        kept: list[Bar] = []
        per_day = {d.ny_date: 0 for d in days}
        for bar in sorted(bars, key=lambda b: b.start):
            if bar.start < first_open or bar.start >= last_close:
                continue  # older or newer than the window: not asked for
            day = next((d for d in days if d.in_regular_session(bar.start)), None)
            if day is None:
                raise HistoryUnavailable(symbol, HistoryCause.OUTSIDE_SESSION,
                                         f"bar at {bar.start.isoformat()} is in no session")
            per_day[day.ny_date] += 1
            kept.append(bar)
        empty = [d.isoformat() for d, n in per_day.items() if n == 0]
        if empty:
            raise HistoryUnavailable(symbol, HistoryCause.EMPTY_SESSION,
                                     f"no bars on {', '.join(empty)}")
        return tuple(kept)

    # -- disk cache
    def _path(self, before: date, symbol: str, sessions: int) -> Path | None:
        if self._directory is None:
            return None
        return self._directory / before.isoformat() / f"{symbol}-{sessions}.jsonl"

    def _load(self, symbol: str, before: date, sessions: int,
              days: list[MarketDay]) -> SessionHistory | None:
        path = self._path(before, symbol, sessions)
        if path is None or not path.exists():
            return None
        bars: list[Bar] = []
        try:
            with open(path, encoding="utf-8") as handle:
                for number, text in enumerate(handle, start=1):
                    bars.append(parse_bar(str(path), number, text))
        except (OSError, BarFileError) as exc:
            raise HistoryUnavailable(symbol, HistoryCause.CACHE, str(exc)) from exc
        if any(b.symbol != symbol for b in bars):
            raise HistoryUnavailable(symbol, HistoryCause.CACHE, f"{path}: another symbol")
        return SessionHistory(symbol, tuple(d.ny_date for d in days),
                              self._window(symbol, days, bars), 0)

    def _store(self, before: date, sessions: int, history: SessionHistory) -> None:
        path = self._path(before, history.symbol, sessions)
        if path is None:
            return
        text = "".join(json.dumps(bar_record(b), separators=(",", ":"), sort_keys=True) + "\n"
                       for b in history.bars)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".partial")
            partial.write_text(text, encoding="utf-8")
            os.replace(partial, path)
        except OSError as exc:
            raise HistoryUnavailable(history.symbol, HistoryCause.CACHE,
                                     f"cannot write {path}: {exc}") from exc
