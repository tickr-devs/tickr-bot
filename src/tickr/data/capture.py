"""Capture writer: every screener row, M1 bar and L1 quote read, appended as JSONL.

Layout: `<data_dir>/<capture_dir>/<environment>/<NY date>/{screener,bars,quotes}.jsonl`, where
the New York date is that of the record's receipt time. One JSON object per line; Decimal values
are strings (never floats); times are ISO-8601 UTC. Every record carries `provider` and
`receipt_time`.

- bars.jsonl: exactly the replay bar keys (`symbol, start, open, high, low, close, volume`, see
  `tickr.replay.bars`) plus `provider` and `receipt_time`. Discovery re-reads the same bars
  every pass, so a bar identical to one already written today by this writer is skipped; a bar
  whose values changed is written again (a provider revision is evidence, not noise).
  `export_replay_bars` turns a captured file into a file the replay tool loads.
- quotes.jsonl: Quote fields plus `provider` and `source` ("snapshot" or "stream").
- screener.jsonl: kind, rank, typed identity fields and the complete provider row `fields`,
  as discovery read them (the api splits this file into discovery passes).
- screener-recorded.jsonl: the same records plus `session` (PRE or POST: the change the
  gainers and losers rank by) for the screener reads outside discovery: the recorder's reads
  in the pre-market (the pre-open preload uses them) and the post-market (runtime.screeners).
  Together the two files hold every screener list the bot received.
- profiles.jsonl: instrument profile rows as read (`instrument_id`, `symbol`, the complete
  provider row `fields`: shortable, easy_to_borrow, the margin requirements ...), for the
  screeners' instruments (runtime.profiles): when first listed that day and again, at most every
  `[market_data] profile_refresh_seconds`, for the losers and the big gainers.
- events.jsonl: what the screener files cannot show by themselves: `screener-skipped` (a kind
  not read: `kind`, `cause` such as "budget" or the error type, `source`), `profiles-skipped`
  (profiles due and not read: the refused request's `symbols`, `cause`, `waiting`), `bot-start` and
  `bot-stop` (with the run's end `cause`), so a gap in the lists is either marked or is a time
  the bot was not running.
- discovery.jsonl: one line per discovery pass (`tickr.data.discovery.pass_record`): every
  instrument's outcome (candidate, refused with cause and measured value, or not reached) and
  the active strategy's fast movers, with `provider` and `receipt_time` (the pass's end). Read
  by the api server for the funnel stages, the symbols screen and the fast-movers list.
- notices.jsonl: stream status notices: `type` ("1001"), the integer counters `drop`, `rtt`,
  `sent` as received, `provider` and `receipt_time`. Read by `tickr.research latency`.

Bulk market data never enters the trading database (spec section 3). A write failure raises
`CaptureError`; the caller decides what that means for trading.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

from tickr.archive import GZ_SUFFIX, open_text
from tickr.calendar import ny_date_of
from tickr.config import Config
from tickr.data.base import (
    InstrumentProfile,
    ScreenerKind,
    ScreenerRow,
    ScreenerSession,
    StreamNotice,
)
from tickr.replay.bars import BAR_KEYS, bar_from_record
from tickr.replay.errors import BarFileError
from tickr.types import Bar, Quote

SCREENER_FILE = "screener.jsonl"
BARS_FILE = "bars.jsonl"
QUOTES_FILE = "quotes.jsonl"
NOTICES_FILE = "notices.jsonl"
DISCOVERY_FILE = "discovery.jsonl"
SCREENER_RECORDED_FILE = "screener-recorded.jsonl"
EVENTS_FILE = "events.jsonl"
SCREENER_SKIPPED = "screener-skipped"
PROFILES_FILE = "profiles.jsonl"
PROFILES_SKIPPED = "profiles-skipped"
BOT_START = "bot-start"
BOT_STOP = "bot-stop"
NOTICE_STATUS_TYPE = "1001"
STREAM_SOURCE = "stream"
CAPTURE_KEYS = frozenset({"provider", "receipt_time"})
NEWLINE = b"\n"


class CaptureError(Exception):
    """A capture record could not be written or read."""


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CaptureError(f"naive datetime {value!r}")
    return value.astimezone(UTC).isoformat()


def _day(value: datetime) -> date:
    """New York date of an aware receipt time (naive times are refused)."""
    _time(value)
    return ny_date_of(value)


def bar_record(bar: Bar) -> dict[str, str]:
    """A bar in the replay JSONL format."""
    return {
        "symbol": bar.symbol,
        "start": _time(bar.start),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }


def quote_record(quote: Quote, provider: str, source: str) -> dict[str, str]:
    return {
        "symbol": quote.symbol,
        "bid": str(quote.bid),
        "ask": str(quote.ask),
        "bid_size": str(quote.bid_size),
        "ask_size": str(quote.ask_size),
        "provider_time": _time(quote.provider_time),
        "receipt_time": _time(quote.receipt_time),
        "provider": provider,
        "source": source,
    }


def screener_record(row: ScreenerRow, provider: str, receipt_time: datetime) -> dict[str, Any]:
    return {
        "kind": row.kind.value,
        "rank": row.rank,
        "instrument_id": row.instrument_id,
        "symbol": row.symbol,
        "exchange_code": row.exchange_code,
        "currency": row.currency,
        "security_type": row.security_type,
        "fields": dict(row.fields),
        "provider": provider,
        "receipt_time": _time(receipt_time),
    }


def _digest(record: dict[str, str]) -> bytes:
    """A bar record's identity for the written-today check: BLAKE2b, 16 bytes, of its fields
    in key order (a collision, about 2^-64 per pair, would only leave one bar version out of the
    capture file)."""
    text = "\x1f".join(f"{k}\x1e{record[k]}" for k in sorted(record))
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


class CaptureWriter:
    """Appends capture records. Thread safe (stream thread and bot loop both write).

    Also a `QuoteListener`: stream quotes are recorded with source "stream".
    """

    def __init__(self, directory: Path, provider: str) -> None:
        self._directory = directory
        self._provider = provider
        self._lock = threading.Lock()
        self._handles: dict[tuple[date, str], IO[str]] = {}
        # Per day, a 16-byte digest of every bar record written (about 70 bytes each in the
        # set, against about 430 for the record's own strings: discovery writes about 2,800 new
        # bar versions a minute, 2026-11-17, so the whole tuples grew the bot ~1.2 MB/min).
        self._bars_written: dict[date, set[bytes]] = {}

    @classmethod
    def from_config(cls, config: Config, provider: str) -> CaptureWriter:
        directory = (config.root / config.run.data_dir / config.market_data.capture_dir
                     / config.environment_name)
        return cls(directory, provider)

    def path(self, day: date, name: str) -> Path:
        return self._directory / day.isoformat() / name

    def _append(self, day: date, name: str, records: Sequence[dict[str, Any]]) -> None:
        if not records:
            return
        lines = "".join(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n"
                        for r in records)
        try:
            handle = self._handles.get((day, name))
            if handle is None:
                self._close_other_days(day)
                path = self.path(day, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(path, "a", encoding="utf-8")  # kept open across appends
                self._handles[(day, name)] = handle
            handle.write(lines)
            handle.flush()
        except OSError as exc:
            raise CaptureError(f"cannot append to {self.path(day, name).name}: "
                               f"{type(exc).__name__}") from exc

    def _close_other_days(self, day: date) -> None:
        for key in [k for k in self._handles if k[0] != day]:
            self._handles.pop(key).close()
        for old in [d for d in self._bars_written if d != day]:
            del self._bars_written[old]

    # -- discovery CaptureSink
    def screener(self, rows: Sequence[ScreenerRow], provider: str, receipt_time: datetime,
                 *, file: str = SCREENER_FILE, session: ScreenerSession | None = None) -> None:
        """Discovery's records (screener.jsonl, the exact keys api.flow reads), or with
        `session` the recorder's, which also state the session the lists rank by."""
        day = _day(receipt_time)
        records = [screener_record(r, provider, receipt_time) for r in rows]
        if session is not None:
            records = [{**r, "session": session.value} for r in records]
        with self._lock:
            self._append(day, file, records)

    def profiles(self, profiles: Sequence[InstrumentProfile], provider: str,
                 receipt_time: datetime) -> None:
        records = [{"instrument_id": p.instrument_id, "symbol": p.symbol,
                    "fields": dict(p.fields), "provider": provider,
                    "receipt_time": _time(receipt_time)} for p in profiles]
        with self._lock:
            self._append(_day(receipt_time), PROFILES_FILE, records)

    def screener_skipped(self, kinds: Sequence[ScreenerKind], cause: str, source: str,
                         receipt_time: datetime) -> None:
        """One `screener-skipped` event per kind not read."""
        for kind in kinds:
            self.event(SCREENER_SKIPPED, receipt_time, kind=kind.value, cause=cause,
                       source=source)

    def event(self, name: str, at: datetime, **fields: str) -> None:
        """One line of events.jsonl."""
        line = {"event": name, **fields, "provider": self._provider, "receipt_time": _time(at)}
        with self._lock:
            self._append(_day(at), EVENTS_FILE, [line])

    def bars(self, bars: Sequence[Bar], provider: str, receipt_time: datetime) -> None:
        day = _day(receipt_time)
        stamp = _time(receipt_time)
        with self._lock:
            seen = self._bars_written.setdefault(day, set())
            records = []
            for bar in bars:
                record = bar_record(bar)
                key = _digest(record)
                if key in seen:
                    continue
                seen.add(key)
                records.append({**record, "provider": provider, "receipt_time": stamp})
            self._append(day, BARS_FILE, records)

    def quotes(self, quotes: Sequence[Quote], provider: str, source: str) -> None:
        by_day: dict[date, list[dict[str, str]]] = {}
        for quote in quotes:
            by_day.setdefault(_day(quote.receipt_time), []).append(
                quote_record(quote, provider, source))
        with self._lock:
            for day, records in sorted(by_day.items()):
                self._append(day, QUOTES_FILE, records)

    def discovery(self, record: dict[str, Any], receipt_time: datetime) -> None:
        """Append one discovery pass (`discovery.pass_record`) to the day of its end."""
        day = _day(receipt_time)
        line = {**record, "provider": self._provider, "receipt_time": _time(receipt_time)}
        with self._lock:
            self._append(day, DISCOVERY_FILE, [line])

    # -- QuoteListener (stream)
    def on_quote(self, quote: Quote) -> None:
        self.quotes([quote], self._provider, STREAM_SOURCE)

    def on_notice(self, notice: StreamNotice) -> None:
        record = {"type": NOTICE_STATUS_TYPE, "drop": notice.drop, "rtt": notice.rtt,
                  "sent": notice.sent, "provider": self._provider,
                  "receipt_time": _time(notice.receipt_time)}
        day = _day(notice.receipt_time)
        with self._lock:
            self._append(day, NOTICES_FILE, [record])

    def on_transport(self, up: bool, at: datetime, detail: str) -> None:
        """Transport events are journaled by the bot loop, not captured here."""

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.close()
            self._handles.clear()


@dataclass(frozen=True)
class ExportResult:
    bars: int
    duplicates: int
    revised: int


@dataclass(frozen=True)
class CapturedBars:
    """The bars of a captured bars.jsonl, one per (symbol, start): the last version received."""

    bars: dict[tuple[str, datetime], Bar]
    duplicates: int
    revised: int


def read_captured_bars(captured: Path, *, open_tail: bool = False) -> CapturedBars:
    """Read a captured bars.jsonl, keeping one bar per (symbol, start).

    Identical repeats (for example after a restart) are counted in `duplicates`. When the
    provider revised a bar, the last received version is kept and counted in `revised`.
    Malformed lines raise `CaptureError` naming the line. With `open_tail` (a file the bot may
    be appending to right now), a last line without its newline is not read yet; anywhere else
    it is read and refused like any other malformed line.
    """
    latest: dict[tuple[str, datetime], Bar] = {}
    duplicates = revised = 0
    with open_text(captured) as handle:  # plain or archived (.gz)
        lines = handle.readlines()
    if open_tail and lines and not lines[-1].endswith("\n"):
        lines.pop()
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        bar = _captured_bar(captured, number, text)
        key = (bar.symbol, bar.start)
        previous = latest.get(key)
        if previous is not None:
            if previous == bar:
                duplicates += 1
            else:
                revised += 1
        latest[key] = bar
    return CapturedBars(latest, duplicates, revised)


def _captured_bar(captured: Path, number: int, text: str) -> Bar:
    """The bar of line `number` of a captured bars.jsonl; CaptureError naming the line."""
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaptureError(f"{captured.name}:{number}: invalid JSON: {exc.msg}") from exc
    if not isinstance(record, dict) or set(record) != BAR_KEYS | CAPTURE_KEYS:
        raise CaptureError(f"{captured.name}:{number}: not a captured bar record")
    bar_only = {k: record[k] for k in BAR_KEYS}
    try:
        return bar_from_record(captured.name, number, bar_only)
    except BarFileError as exc:
        raise CaptureError(f"{captured.name}:{number}: {type(exc).__name__}") from exc


class _BarsTail:
    """One captured bars.jsonl read on as the bot appends: only the lines added since the last
    read are parsed. Each symbol's bars (one per start, the last version received, as
    `read_captured_bars` keeps them) are kept in start order and sorted again only when a new
    line touches that symbol. Not thread safe: `CapturedBarsDays` holds the lock."""

    def __init__(self, captured: Path) -> None:
        self.captured = captured
        self._reset(None)

    def _reset(self, identity: tuple[int, int] | None) -> None:
        self._identity = identity  # st_dev, st_ino of what was read
        self._head = b""  # the file's first line as read; a different one means another file
        self._offset = 0  # bytes parsed so far: complete lines only
        self._lines = 0  # lines parsed so far, for the line number of a malformed one
        self._latest: dict[str, dict[datetime, Bar]] = {}
        self._by_symbol: dict[str, tuple[Bar, ...]] = {}

    def by_symbol(self, open_tail: bool) -> dict[str, tuple[Bar, ...]]:
        if self.captured.name.endswith(GZ_SUFFIX):  # archived: read once, never appended to
            if self._identity is None:
                with open_text(self.captured) as text:
                    self._fold(self._parse(text, open_tail=False))
                stat = self.captured.stat()
                self._identity = (stat.st_dev, stat.st_ino)
            return dict(self._by_symbol)
        with open(self.captured, "rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            head = handle.read(len(self._head)) if self._head else b""
            if identity != self._identity or stat.st_size < self._offset or head != self._head:
                self._reset(identity)  # replaced, shrunk or recreated: read anew
            handle.seek(self._offset)
            if not self._head:
                self._head = handle.readline()  # the first line, to know the file again
                handle.seek(self._offset)
                if not self._head.endswith(NEWLINE):
                    self._head = b""  # not a whole line yet
            self._fold(self._parse(handle, open_tail))
        return dict(self._by_symbol)

    def _parse(self, lines: Iterable[bytes] | Iterable[str], open_tail: bool,
               ) -> tuple[dict[str, dict[datetime, Bar]], int, int]:
        """The bars of `lines` read on from the last one parsed (the last version per symbol and
        start), the bytes they took and the number of the last line. With `open_tail` a line
        without its newline (the bot is still writing it) is not read yet."""
        found: dict[str, dict[datetime, Bar]] = {}
        consumed = 0
        number = self._lines
        for raw in lines:
            if isinstance(raw, bytes):
                if open_tail and not raw.endswith(NEWLINE):
                    break
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CaptureError(f"{self.captured.name}:{number + 1}: "
                                       f"{type(exc).__name__}") from exc
                consumed += len(raw)
            else:
                text = raw
            number += 1
            if text.strip():
                bar = _captured_bar(self.captured, number, text)
                found.setdefault(bar.symbol, {})[bar.start] = bar
        return found, consumed, number

    def _fold(self, parsed: tuple[dict[str, dict[datetime, Bar]], int, int]) -> None:
        """Keeps a fully parsed chunk: a malformed line raised before and left the tail as it
        was. Each symbol a new line touched is put in start order again."""
        found, consumed, number = parsed
        for symbol, starts in found.items():
            kept = self._latest.setdefault(symbol, {})
            kept.update(starts)
            self._by_symbol[symbol] = tuple(kept[start] for start in sorted(kept))
        self._offset += consumed
        self._lines = number


class CapturedBarsDays:
    """The bars of captured bars.jsonl files by symbol, shared by every reader in the API (a
    day's trades and bars, the observers' and the skipped signals' simulations, a trade's
    what-if). Today's file is read on as the bot appends (`_BarsTail`): re-reading all of it
    whenever it grew cost the API tens of seconds of CPU per request on a busy day. Holds the
    file read with `open_tail` (today's) and the last other one. Thread safe: each of the two has
    its own lock, so today's reads never wait for a past day's file being parsed."""

    def __init__(self) -> None:
        self._locks = {"_open": threading.Lock(), "_closed": threading.Lock()}
        self._open: _BarsTail | None = None  # today's file, still being appended to
        self._closed: _BarsTail | None = None  # the last other day's file read

    def by_symbol(self, captured: Path, *, open_tail: bool) -> dict[str, tuple[Bar, ...]]:
        """Each symbol's bars of `captured` (plain or archived), oldest first, one per start:
        what `read_captured_bars` keeps. Raises CaptureError (a malformed line: the file is
        read anew next time) or OSError."""
        slot = "_open" if open_tail else "_closed"
        with self._locks[slot]:
            tail: _BarsTail | None = getattr(self, slot)
            if tail is None or tail.captured != captured:
                tail = _BarsTail(captured)
                setattr(self, slot, tail)
            try:
                return tail.by_symbol(open_tail)
            except CaptureError:
                setattr(self, slot, None)
                raise


def export_replay_bars(captured: Path, out: Path) -> ExportResult:
    """Write the bars of a captured bars.jsonl in the replay format, one bar per (symbol, start)
    (see `read_captured_bars`). Output is ordered by symbol, then start, so the replay tool's
    strictly-increasing rule holds."""
    read = read_captured_bars(captured)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        for key in sorted(read.bars):
            handle.write(json.dumps(bar_record(read.bars[key]), separators=(",", ":"),
                                    sort_keys=True) + "\n")
    return ExportResult(len(read.bars), read.duplicates, read.revised)
