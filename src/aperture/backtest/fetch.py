"""Read-only history fetch for the backtest: instrument list, daily bars, M1 bars.

Runs on the owner's Mac against the configured environment's market data (docs/backtest.md).
Every step writes files and can be rerun: finished files are skipped, so an interrupted fetch
continues where it stopped. Requests take the bot's request budget; when the budget refuses,
the step waits for the time the budget names and asks again (reads only, never orders).

Refusals stop the step with one cause (`BacktestError`); a symbol whose bars are incomplete is
recorded with its cause in the status file and later skipped by the run, never filled in.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from datetime import time as wall_time
from pathlib import Path
from typing import Any, Protocol

from aperture.backtest.errors import BacktestError
from aperture.backtest.sessions import ONE_DAY, sessions_before, sessions_between
from aperture.backtest.settings import BacktestSettings
from aperture.broker.base import RateLimited
from aperture.broker.budget import BudgetExhausted
from aperture.broker.webull import SymbolsRefused
from aperture.calendar import NEW_YORK, MarketDay, SessionCalendar, ny_date_of
from aperture.config import Config
from aperture.data.capture import bar_record
from aperture.types import Bar

UNIVERSE_FILE = "universe.jsonl"
UNIVERSE_REFUSED_FILE = "universe_refused.jsonl"
DAILY_DIR = "daily"
MINUTES_DIR = "minutes"
MINUTES_STATUS_FILE = "minutes_status.jsonl"


class InstrumentLike(Protocol):
    instrument_id: str
    symbol: str
    exchange_code: str
    currency: str
    security_type: str | None
    status: str | None


class PageLike(Protocol):
    @property
    def rows(self) -> Sequence[InstrumentLike]: ...

    @property
    def next_key(self) -> str | None: ...


class DailyLike(Protocol):
    symbol: str
    time: datetime
    open: Any
    high: Any
    low: Any
    close: Any
    volume: Any


class HistorySource(Protocol):
    """What the fetch needs; `broker.webull.WebullHistory` implements it."""

    @property
    def requests(self) -> int: ...

    def instruments(self, sub_category: str, pagination_key: str | None) -> PageLike: ...

    def daily_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[Any]: ...

    def minute_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[Bar]: ...


Sleep = Callable[[float], None]
Log = Callable[[str], None]


def patiently[T](call: Callable[[], T], sleep: Sleep, log: Log) -> T:
    """Call; while the request budget refuses, wait the time it names and call again. After a
    provider 429 the budget pauses the route, so the next attempt waits for that pause."""
    while True:
        try:
            return call()
        except BudgetExhausted as refused:
            sleep(refused.retry_after_seconds)
        except RateLimited as limited:
            log(f"provider rate limit: {limited}; waiting for the budget pause")


def _write_lines(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Whole file through a temporary name, so a file that exists is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with open(partial, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    os.replace(partial, path)


def read_lines(path: Path) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(text) for text in handle if text.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestError(f"cannot read {path}: {exc}") from exc


# ------------------------------------------------------------------------------- universe


def universe_cause(row: InstrumentLike, cfg: Config, settings: BacktestSettings) -> str | None:
    md = cfg.market_data
    if row.security_type is None:
        return "SECURITY_TYPE_UNKNOWN"
    if row.security_type not in md.allowed_security_types:
        return "SECURITY_TYPE_NOT_ALLOWED"
    if row.exchange_code not in md.allowed_exchanges:
        return "EXCHANGE_NOT_ALLOWED"
    if row.currency not in md.allowed_currencies:
        return "CURRENCY_NOT_ALLOWED"
    if row.status not in settings.instrument_statuses:
        return "STATUS_NOT_ALLOWED"
    return None


def fetch_universe(source: HistorySource, cfg: Config, settings: BacktestSettings, out: Path,
                   sleep: Sleep = time.sleep, log: Log = print) -> list[str]:
    """All instruments of the allowed security types, filtered like discovery's universe
    (exchange, currency, type from the instrument profile). A symbol listed under two instrument
    ids is refused (`DUPLICATE_SYMBOL`), never resolved by picking one. Returns kept symbols."""
    path = out / UNIVERSE_FILE
    if path.exists():
        log(f"{path} exists: skipped")
        return [r["symbol"] for r in read_lines(path)]
    rows: list[InstrumentLike] = []
    for sub_category in cfg.market_data.allowed_security_types:
        seen_keys: set[str] = set()
        key: str | None = None
        for page_number in range(1, settings.instrument_max_pages + 1):
            current = key
            page = patiently(lambda t=sub_category, k=current: source.instruments(t, k), sleep, log)
            rows.extend(page.rows)
            log(f"instruments {sub_category} page {page_number}: {len(page.rows)} rows")
            key = page.next_key
            if key is None:
                break
            if key in seen_keys:
                raise BacktestError(f"instrument pagination key repeated on page {page_number}")
            seen_keys.add(key)
        else:
            raise BacktestError(f"instrument list did not end within "
                                f"{settings.instrument_max_pages} pages")
    kept: dict[str, list[InstrumentLike]] = {}
    refused: list[dict[str, Any]] = []
    for row in rows:
        cause = universe_cause(row, cfg, settings)
        if cause is None:
            kept.setdefault(row.symbol, []).append(row)
        else:
            refused.append({**_instrument_record(row), "cause": cause})
    final: list[dict[str, Any]] = []
    for _symbol, listed in sorted(kept.items()):
        ids = {r.instrument_id for r in listed}
        if len(ids) > 1:
            refused.extend({**_instrument_record(r), "cause": "DUPLICATE_SYMBOL"} for r in listed)
        else:
            final.append(_instrument_record(listed[0]))
    _write_lines(out / UNIVERSE_REFUSED_FILE, refused)
    _write_lines(path, final)
    log(f"universe: {len(final)} symbols kept, {len(refused)} rows refused")
    return [r["symbol"] for r in final]


def _instrument_record(row: InstrumentLike) -> dict[str, Any]:
    return {"instrument_id": row.instrument_id, "symbol": row.symbol,
            "exchange_code": row.exchange_code, "currency": row.currency,
            "security_type": row.security_type, "status": row.status}


# ------------------------------------------------------------------------------- daily bars


def daily_window(calendar: SessionCalendar, first: date, last: date,
                 max_lookback_days: int) -> list[MarketDay]:
    """The sessions from `first` to `last` plus the one before `first` (its close is the
    previous close of the first day), oldest first."""
    days = sessions_between(calendar, first, last)
    if not days:
        raise BacktestError(f"no sessions between {first} and {last}")
    return sessions_before(calendar, days[0].ny_date, 1, max_lookback_days) + days


def daily_records(bars: Sequence[DailyLike], sessions: Sequence[MarketDay]) -> list[dict]:
    """Daily bars -> records keyed by New York session date. A label whose New York date is
    not one of the sessions asked for, or two bars of one symbol on one date, stop the step:
    the label convention would be wrong and every change computed from it too."""
    wanted = {d.ny_date for d in sessions}
    first = sessions[0].ny_date
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, date]] = set()
    for bar in bars:
        day = ny_date_of(bar.time)
        if day < first:
            continue  # older than asked: count reached further back
        if day not in wanted:
            raise BacktestError(f"daily bar {bar.symbol} labelled {bar.time.isoformat()} maps to "
                                f"{day.isoformat()}, not a session of the window")
        if (bar.symbol, day) in seen:
            raise BacktestError(f"two daily bars for {bar.symbol} on {day.isoformat()}")
        seen.add((bar.symbol, day))
        records.append({"symbol": bar.symbol, "date": day.isoformat(),
                        "open": str(bar.open), "high": str(bar.high), "low": str(bar.low),
                        "close": str(bar.close), "volume": str(bar.volume)})
    return records


def fetch_daily(source: HistorySource, calendar: SessionCalendar, cfg: Config,
                symbols: Sequence[str], first: date, last: date, out: Path,
                sleep: Sleep = time.sleep, log: Log = print) -> Path:
    """Daily bars of every symbol for the window, in chunks of `bars_symbols_per_request`,
    one file per chunk under `daily/` (a rerun skips written chunks)."""
    sessions = daily_window(calendar, first, last, cfg.market_data.history_max_lookback_days)
    end = datetime.combine(last + ONE_DAY, wall_time(0), NEW_YORK)
    size = cfg.market_data.bars_symbols_per_request
    ordered = sorted(set(symbols))
    directory = out / DAILY_DIR
    chunks = [ordered[i:i + size] for i in range(0, len(ordered), size)]
    for number, chunk in enumerate(chunks):
        path = directory / f"{number:05d}.jsonl"
        if path.exists():
            continue
        bars, refused = _daily_without_refused(source, chunk, end, len(sessions), sleep, log)
        _write_lines(directory / f"{number:05d}.refused", ({"symbol": s} for s in refused))
        _write_lines(path, daily_records(bars, sessions))
        if number % 25 == 0:
            log(f"daily: chunk {number + 1} of {len(chunks)}, {source.requests} requests")
    log(f"daily: {len(chunks)} chunks in {directory}")
    return directory


def _daily_without_refused(source: HistorySource, symbols: Sequence[str], end: datetime,
                           count: int, sleep: Sleep, log: Log) -> tuple[list[Any], list[str]]:
    """Daily bars of `symbols`; when the route refuses the request for an unknown symbol, the
    chunk is split in halves until each refused symbol is alone, and those symbols are returned
    as refused (recorded, never part of the pool). Reads only; each split is a new request."""
    try:
        return patiently(lambda: source.daily_bars(symbols, end, count), sleep, log), []
    except SymbolsRefused:
        if len(symbols) == 1:
            log(f"daily: {symbols[0]} refused as an unknown symbol")
            return [], list(symbols)
    middle = len(symbols) // 2
    left = _daily_without_refused(source, symbols[:middle], end, count, sleep, log)
    right = _daily_without_refused(source, symbols[middle:], end, count, sleep, log)
    return left[0] + right[0], left[1] + right[1]


def load_daily(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        rows.extend(read_lines(path))
    return rows


# ------------------------------------------------------------------------------- minute bars


@dataclass
class _Cursor:
    symbol: str
    sessions: tuple[MarketDay, ...]  # contiguous, oldest first
    end: datetime
    pages: int = 0
    bars: dict[datetime, Bar] = field(default_factory=dict)
    done: bool = False
    complete: bool = False
    cause: str = ""

    @property
    def first_open(self) -> datetime:
        return self.sessions[0].regular_open


@dataclass(frozen=True)
class RunStatus:
    symbol: str
    first_session: str
    last_session: str
    sessions: int
    bars: int
    pages: int
    complete: bool
    cause: str
    empty_sessions: tuple[str, ...]


def needed_runs(pool_days: Iterable[date], all_sessions: Sequence[MarketDay],
                history_sessions: int) -> list[tuple[MarketDay, ...]]:
    """Each pool day plus its `history_sessions` previous sessions, merged into contiguous runs
    of `all_sessions` (which must cover them), oldest first."""
    index = {d.ny_date: i for i, d in enumerate(all_sessions)}
    wanted: set[int] = set()
    for day in pool_days:
        if day not in index:
            raise BacktestError(f"pool day {day} is not a session of the window")
        position = index[day]
        if position < history_sessions:
            raise BacktestError(f"window does not hold {history_sessions} sessions before {day}")
        wanted.update(range(position - history_sessions, position + 1))
    runs: list[list[MarketDay]] = []
    for position in sorted(wanted):
        if runs and index[runs[-1][-1].ny_date] == position - 1:
            runs[-1].append(all_sessions[position])
        else:
            runs.append([all_sessions[position]])
    return [tuple(run) for run in runs]


def _accept(cursor: _Cursor, page: Sequence[Bar], max_pages: int) -> None:
    cursor.pages += 1
    if not page:
        cursor.done, cursor.cause = True, "INCOMPLETE: no older bars"
        return
    last_close = cursor.sessions[-1].regular_close
    for bar in page:
        if bar.start < cursor.first_open or bar.start >= last_close:
            continue
        if not any(d.in_regular_session(bar.start) for d in cursor.sessions):
            cursor.done, cursor.cause = True, f"OUTSIDE_SESSION: {bar.start.isoformat()}"
            return
        known = cursor.bars.get(bar.start)
        if known is not None and known != bar:
            cursor.done, cursor.cause = True, f"CONFLICT: {bar.start.isoformat()}"
            return
        cursor.bars[bar.start] = bar
    oldest = min(b.start for b in page)
    if oldest <= cursor.first_open:
        cursor.done, cursor.complete = True, True
    elif oldest >= cursor.end:
        cursor.done, cursor.cause = True, "NO_PROGRESS"
    elif cursor.pages >= max_pages:
        cursor.done, cursor.cause = True, f"PAGE_LIMIT: {cursor.pages} pages"
    else:
        cursor.end = oldest


def _status(cursor: _Cursor) -> RunStatus:
    counts = {d.ny_date: 0 for d in cursor.sessions}
    for start in cursor.bars:
        counts[ny_date_of(start)] = counts.get(ny_date_of(start), 0) + 1
    empty = tuple(d.isoformat() for d, n in counts.items() if n == 0)
    return RunStatus(cursor.symbol, cursor.sessions[0].ny_date.isoformat(),
                     cursor.sessions[-1].ny_date.isoformat(), len(cursor.sessions),
                     len(cursor.bars), cursor.pages, cursor.complete, cursor.cause, empty)


def fetch_minutes(source: HistorySource, cfg: Config, settings: BacktestSettings,
                  runs_by_symbol: dict[str, list[tuple[MarketDay, ...]]], out: Path,
                  sleep: Sleep = time.sleep, log: Log = print) -> None:
    """Regular-session M1 bars for every symbol's runs. Symbols are fetched in waves; within a
    wave every request carries up to `bars_symbols_per_request` symbols that page from the same
    end time. Per symbol: `minutes/<SYMBOL>.jsonl` (replay bar format) and one status line per
    run in `minutes_status.jsonl`. A rerun skips symbols whose file exists."""
    directory = out / MINUTES_DIR
    status_path = out / MINUTES_STATUS_FILE
    per_request = cfg.market_data.history_bars_per_request
    size = cfg.market_data.bars_symbols_per_request
    todo = [s for s in sorted(runs_by_symbol) if not (directory / f"{s}.jsonl").exists()]
    log(f"minutes: {len(todo)} of {len(runs_by_symbol)} symbols to fetch")
    for start in range(0, len(todo), settings.symbols_per_wave):
        wave = todo[start:start + settings.symbols_per_wave]
        cursors = [_Cursor(s, run, run[-1].regular_close)
                   for s in wave for run in runs_by_symbol[s]]
        while True:
            active = [c for c in cursors if not c.done]
            if not active:
                break
            by_end: dict[datetime, list[_Cursor]] = {}
            for cursor in active:
                by_end.setdefault(cursor.end, []).append(cursor)
            for end, group in sorted(by_end.items(), reverse=True):
                for chunk in _unique_chunks(group, size):
                    symbols = [c.symbol for c in chunk]
                    page = patiently(
                        lambda s=symbols, e=end: source.minute_bars(s, e, per_request), sleep, log)
                    for cursor in chunk:
                        own = [b for b in page if b.symbol == cursor.symbol]
                        _accept(cursor, own, settings.minute_max_pages_per_run)
        statuses: list[RunStatus] = []
        for symbol in wave:
            mine = [c for c in cursors if c.symbol == symbol]
            bars = sorted((b for c in mine for b in c.bars.values()), key=lambda b: b.start)
            statuses.extend(_status(c) for c in mine)
            _write_lines(directory / f"{symbol}.jsonl", (bar_record(b) for b in bars))
        with open(status_path, "a", encoding="utf-8") as handle:
            for status in statuses:
                handle.write(json.dumps(asdict(status), sort_keys=True) + "\n")
        log(f"minutes: {min(start + len(wave), len(todo))} of {len(todo)} symbols, "
            f"{source.requests} requests")


def _unique_chunks(group: list[_Cursor], size: int) -> list[list[_Cursor]]:
    """Chunks of at most `size` cursors with distinct symbols (one symbol per request slot)."""
    chunks: list[list[_Cursor]] = []
    for cursor in group:
        target = next((c for c in chunks
                       if len(c) < size and all(x.symbol != cursor.symbol for x in c)), None)
        if target is None:
            chunks.append([cursor])
        else:
            target.append(cursor)
    return chunks


def load_statuses(out: Path) -> list[dict[str, Any]]:
    path = out / MINUTES_STATUS_FILE
    return read_lines(path) if path.exists() else []
