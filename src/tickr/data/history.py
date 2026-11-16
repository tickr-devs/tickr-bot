"""Prior-session M1 history, many symbols per request: paged reads, refusals with one cause per
symbol, a per-day cache.

`HistoryReader.prior_sessions(symbols, before, sessions)` establishes, for each symbol, the
regular-session M1 bars of the `sessions` open trading days strictly before the New York date
`before`, oldest first. Today's bars are not included: they come from `MarketData.bars`.

How it reads: the symbols not yet known go in batches of `[market_data]
bars_symbols_per_request` (Webull: at most 20). One page is ONE request
`MarketData.bars_before(batch, end, per_request)`; `count` applies per symbol (probe
2026-11-16), so a page brings up to `per_request` bars of every symbol in it. Each symbol keeps
its own next end (the oldest bar it received; first the regular close of the last wanted day).
A page asks every unfinished symbol of the batch at the LATEST of their next ends: the symbols
whose own end that is get their next page, the others (thinner symbols that reached further
back) are carried along and receive bars they partly have, which is harmless (identical bars
merge, different ones are a CONFLICT). A symbol is finished when a bar at or before the first
wanted day's open arrives, and drops out of later pages. The latest end falls with every page,
so the paging ends; `max_pages` counts a symbol's own pages only, as a single-symbol read would.
Every page takes request budget (`budget_reserve` requests of the route are left free, as for
discovery). When a page refuses the batch for an unknown symbol (`SymbolsRefused`), the batch
is split until that symbol is alone (`read_without_refused`, like discovery) and it alone is
refused (SYMBOL_REFUSED).

The answer is a `HistoryRead`: the histories found, the refusals (per symbol: one symbol's
refusal never fails another), and the error that stopped the rest, if any: `RateLimited`,
`DelayedData` or a failed read stop the call; symbols finished before it are kept (memory and
disk), the others are simply not in the answer and are asked again next time.

Refusals (`HistoryUnavailable`, one `HistoryCause`), never a silent partial answer:
- CALENDAR: the calendar cannot say which days were sessions (out of coverage, invalid);
- LOOKBACK: fewer than `sessions` open days within `max_lookback_days` before `before`;
- NO_PROGRESS: a page returned a bar of the symbol at or after its end (end time not applied);
- INCOMPLETE: a page brought no bar of the symbol before a bar at or before the first wanted
  session's open arrived (no older history, or an adapter that drops bars at or after the end it
  ignored);
- PAGE_LIMIT: `max_pages` own pages did not reach the first wanted session's open;
- EMPTY_SESSION: a wanted session has no bar at all (not listed yet, halted all day);
- OUTSIDE_SESSION: a bar inside the window starts outside every wanted regular session;
- CONFLICT: two different bars for the same symbol and minute;
- SYMBOL_REFUSED: the bars route refused the symbol as unknown;
- CACHE, INVALID_REQUEST: see below and the argument checks.

Webull behaviour (probe M4, docs/webull-facts.md): end_time is exclusive and count pages back
without duplicates from end_time = oldest bar start; three months back were served. The
NO_PROGRESS and INCOMPLETE refusals stay as guards, not as expected outcomes.

A minute with no bar inside a session is not a refusal: thin symbols do not trade every minute.

Cache: in memory and on disk at `<directory>/<before>/<symbol>-<sessions>.jsonl`, in the replay
bar format, written whole through a temporary file. A cache file that does not parse, or a failed
write, is refused as CACHE; a bad file is never silently re-fetched over. A fetched history stays
in memory even when its write failed, so the budget is not spent twice.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from tickr.broker.base import BrokerError
from tickr.calendar import (
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
    ny_date_of,
)
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError, SymbolsRefused, read_without_refused
from tickr.data.capture import bar_record
from tickr.replay.bars import parse_bar
from tickr.replay.errors import BarFileError
from tickr.types import Bar

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
    SYMBOL_REFUSED = "SYMBOL_REFUSED"
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
        """Bars of one of the sessions (by New York date of the session). Grouped once per
        history: the bot loop asks for every session of every symbol each minute."""
        if day not in self.sessions:
            raise KeyError(day)
        return self._by_day().get(day, ())

    def _by_day(self) -> dict[date, tuple[Bar, ...]]:
        cached: dict[date, tuple[Bar, ...]] | None = self.__dict__.get("_grouped")
        if cached is None:
            grouped: dict[date, list[Bar]] = {}
            for bar in self.bars:
                grouped.setdefault(ny_date_of(bar.start), []).append(bar)
            cached = {d: tuple(b) for d, b in grouped.items()}
            object.__setattr__(self, "_grouped", cached)  # frozen: a derived cache only
        return cached


@dataclass(frozen=True)
class HistorySettings:
    bars_per_request: int  # per symbol
    max_pages: int  # own pages per symbol
    max_lookback_days: int
    symbols_per_request: int

    @classmethod
    def from_config(cls, config: Config) -> HistorySettings:
        md = config.market_data
        return cls(md.history_bars_per_request, md.history_max_pages_per_symbol,
                   md.history_max_lookback_days, md.bars_symbols_per_request)


@dataclass(frozen=True)
class HistoryRead:
    """The answer of one `HistoryReader.prior_sessions` call. A symbol asked is in `found`, in
    `refused`, or in neither: then `error` stopped the call before it was established."""

    found: dict[str, SessionHistory]
    refused: dict[str, HistoryUnavailable]
    error: MarketDataError | BrokerError | None  # RateLimited, DelayedData, a failed read
    requests: int  # requests the provider answered in this call (budget refusals not counted)


@dataclass
class _Pending:
    """One symbol being paged: its own next end, the bars so far, its own and all pages."""

    symbol: str
    end: datetime
    received: dict[datetime, Bar] = field(default_factory=dict)
    own_pages: int = 0
    pages: int = 0


class HistoryReader:
    """Thread safe. One lock: a call holds it across its pages, so calls wait for each other
    (the bot loop is the only caller: pre-open preload and the strategy's evaluation).

    A refusal is remembered in memory for the (symbol, day, sessions) and answered again without
    requests; budget refusals (RateLimited) and DelayedData are not refusals and are not kept.
    A failed disk write keeps the history in memory but is not remembered as a refusal.
    INVALID_REQUEST is not remembered either (no request was made)."""

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

    def prior_sessions(self, symbols: Sequence[str], before: date, sessions: int, *,
                       budget_reserve: int = 0) -> HistoryRead:
        """Prior sessions of every symbol in `symbols` (in that order of priority: batches are
        formed and read in it). See the module docstring."""
        found: dict[str, SessionHistory] = {}
        refused: dict[str, HistoryUnavailable] = {}
        counter = [0]
        error: MarketDataError | BrokerError | None = None
        with self._lock:
            # A new New York day drops older entries: the cache is per day asked on.
            for old in [k for k in self._memory if k[1] != before]:
                del self._memory[old]
            missing: list[str] = []
            days: list[MarketDay] | None = None
            for symbol in dict.fromkeys(symbols):
                if not SYMBOL_PATTERN.match(symbol):
                    refused[symbol] = HistoryUnavailable(symbol, HistoryCause.INVALID_REQUEST,
                                                         f"symbol {symbol!r} not allowed")
                    continue
                if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1:
                    refused[symbol] = HistoryUnavailable(
                        symbol, HistoryCause.INVALID_REQUEST,
                        f"sessions must be a positive int, got {sessions!r}")
                    continue
                key = (symbol, before, sessions)
                cached = self._memory.get(key)
                if isinstance(cached, HistoryUnavailable):
                    refused[symbol] = cached
                    continue
                if cached is not None:
                    found[symbol] = cached
                    continue
                try:
                    days = self._sessions_before(symbol, before, sessions)
                    history = self._load(symbol, before, sessions, days)
                except HistoryUnavailable as exc:
                    self._refuse(refused, before, sessions, exc)
                    continue
                if history is None:
                    missing.append(symbol)
                else:
                    self._memory[key] = history
                    found[symbol] = history
            if missing:
                assert days is not None  # the same days for every symbol of one call
                size = self._settings.symbols_per_request
                for start in range(0, len(missing), size):
                    try:
                        self._fetch(missing[start:start + size], days, before, sessions,
                                    budget_reserve, found, refused, counter)
                    except (MarketDataError, BrokerError) as exc:  # RateLimited, DelayedData
                        error = exc
                        break
        return HistoryRead(found, refused, error, counter[0])

    def _refuse(self, refused: dict[str, HistoryUnavailable], before: date, sessions: int,
                exc: HistoryUnavailable) -> None:
        if exc.cause is not HistoryCause.CACHE:
            self._memory[(exc.symbol, before, sessions)] = exc  # same answer all day
        refused[exc.symbol] = exc

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
    def _fetch(self, batch: list[str], days: list[MarketDay], before: date, sessions: int,
               budget_reserve: int, found: dict[str, SessionHistory],
               refused: dict[str, HistoryUnavailable], counter: list[int]) -> None:
        """Page `batch` back until each symbol reached the first wanted open or was refused.
        A symbol is settled (memory, disk, `found` or `refused`) the moment it finishes, so a
        budget stop on a later page keeps it."""
        first_open = days[0].regular_open
        pending = {s: _Pending(s, days[-1].regular_close) for s in batch}

        def settle(item: _Pending) -> None:
            del pending[item.symbol]
            try:
                history = SessionHistory(item.symbol, tuple(d.ny_date for d in days),
                                         self._window(item.symbol, days,
                                                      item.received.values()), item.pages)
            except HistoryUnavailable as exc:
                self._refuse(refused, before, sessions, exc)
                return
            self._memory[(item.symbol, before, sessions)] = history  # kept if the write fails
            try:
                self._store(before, sessions, history)
            except HistoryUnavailable as exc:
                refused[item.symbol] = exc  # CACHE: not remembered, memory serves next time
                return
            found[item.symbol] = history

        def fail(item: _Pending, cause: HistoryCause, detail: str) -> None:
            del pending[item.symbol]
            self._refuse(refused, before, sessions, HistoryUnavailable(item.symbol, cause, detail))

        while pending:
            end = max(p.end for p in pending.values())
            limited = [p for p in pending.values()
                       if p.end == end and p.own_pages == self._settings.max_pages]
            for item in limited:
                fail(item, HistoryCause.PAGE_LIMIT,
                     f"{item.own_pages} pages did not reach {first_open.isoformat()}")
            if limited:
                continue
            asked = list(pending)

            def read(symbols: Sequence[str], end: datetime = end) -> list[Bar]:
                try:
                    page = self._source.bars_before(symbols, end,
                                                    self._settings.bars_per_request,
                                                    budget_reserve=budget_reserve)
                except SymbolsRefused:
                    counter[0] += 1  # answered (refused whole), unlike a budget refusal
                    raise
                counter[0] += 1
                return page

            def unknown(symbol: str) -> None:  # at once: survives a budget stop of the split
                fail(pending[symbol], HistoryCause.SYMBOL_REFUSED,
                     "the bars route refused the symbol as unknown")

            page = read_without_refused(read, asked, unknown)
            stray = sorted({b.symbol for b in page} - set(asked))
            if stray:
                raise MarketDataError(f"history page: bars of {', '.join(stray)} not requested")
            for symbol in [s for s in asked if s in pending]:
                self._accept(pending[symbol], [b for b in page if b.symbol == symbol], end,
                             first_open, settle, fail)

    @staticmethod
    def _accept(item: _Pending, page: list[Bar], end: datetime, first_open: datetime,
                settle: Callable[[_Pending], None],
                fail: Callable[[_Pending, HistoryCause, str], None]) -> None:
        """One page's bars of one symbol, asked at `end`."""
        own = item.end == end  # its own next page, not carried along at a later end
        item.pages += 1
        item.own_pages += own
        if not page:
            # Nothing older before the first wanted open arrived: either the provider has no
            # more history or it ignored the end time. Either way the window is incomplete.
            oldest = min(item.received) if item.received else None
            fail(item, HistoryCause.INCOMPLETE,
                 f"nothing before {end.isoformat()}; oldest bar received "
                 f"{oldest.isoformat() if oldest else 'none'}, first wanted open "
                 f"{first_open.isoformat()}")
            return
        for bar in page:
            if bar.start >= end:
                fail(item, HistoryCause.NO_PROGRESS,
                     f"page bar {bar.symbol} {bar.start.isoformat()} not before {end.isoformat()}")
                return
            known = item.received.get(bar.start)
            if known is not None and known != bar:
                fail(item, HistoryCause.CONFLICT, f"two different bars at {bar.start.isoformat()}")
                return
            item.received[bar.start] = bar
        oldest = min(b.start for b in page)
        if oldest <= first_open:
            settle(item)
        elif oldest < item.end:
            item.end = oldest

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
