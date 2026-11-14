"""Capture writer: every screener row, M1 bar and L1 quote read, appended as JSONL.

Layout: `<data_dir>/<capture_dir>/<environment>/<NY date>/{screener,bars,quotes}.jsonl`, where
the New York date is that of the record's receipt time. One JSON object per line; Decimal values
are strings (never floats); times are ISO-8601 UTC. Every record carries `provider` and
`receipt_time`.

- bars.jsonl: exactly the replay bar keys (`symbol, start, open, high, low, close, volume`, see
  `aperture.replay.bars`) plus `provider` and `receipt_time`. Discovery re-reads the same bars
  every pass, so a bar identical to one already written today by this writer is skipped; a bar
  whose values changed is written again (a provider revision is evidence, not noise).
  `export_replay_bars` turns a captured file into a file the replay tool loads.
- quotes.jsonl: Quote fields plus `provider` and `source` ("snapshot" or "stream").
- screener.jsonl: kind, rank, typed identity fields and the complete provider row `fields`.
- notices.jsonl: stream status notices: `type` ("1001"), the integer counters `drop`, `rtt`,
  `sent` as received, `provider` and `receipt_time`. Read by `aperture.research latency`.

Bulk market data never enters the trading database (spec section 3). A write failure raises
`CaptureError`; the caller decides what that means for trading.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

from aperture.archive import open_text
from aperture.calendar import ny_date_of
from aperture.config import Config
from aperture.data.base import ScreenerRow, StreamNotice
from aperture.replay.bars import BAR_KEYS, parse_bar
from aperture.replay.errors import BarFileError
from aperture.types import Bar, Quote

SCREENER_FILE = "screener.jsonl"
BARS_FILE = "bars.jsonl"
QUOTES_FILE = "quotes.jsonl"
NOTICES_FILE = "notices.jsonl"
NOTICE_STATUS_TYPE = "1001"
STREAM_SOURCE = "stream"
CAPTURE_KEYS = frozenset({"provider", "receipt_time"})


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


class CaptureWriter:
    """Appends capture records. Thread safe (stream thread and bot loop both write).

    Also a `QuoteListener`: stream quotes are recorded with source "stream".
    """

    def __init__(self, directory: Path, provider: str) -> None:
        self._directory = directory
        self._provider = provider
        self._lock = threading.Lock()
        self._handles: dict[tuple[date, str], IO[str]] = {}
        self._bars_written: dict[date, set[tuple[str, ...]]] = {}

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
            raise CaptureError(f"cannot append to {self.path(day, name)}: {exc}") from exc

    def _close_other_days(self, day: date) -> None:
        for key in [k for k in self._handles if k[0] != day]:
            self._handles.pop(key).close()
        for old in [d for d in self._bars_written if d != day]:
            del self._bars_written[old]

    # -- discovery CaptureSink
    def screener(self, rows: Sequence[ScreenerRow], provider: str, receipt_time: datetime) -> None:
        day = _day(receipt_time)
        records = [screener_record(r, provider, receipt_time) for r in rows]
        with self._lock:
            self._append(day, SCREENER_FILE, records)

    def bars(self, bars: Sequence[Bar], provider: str, receipt_time: datetime) -> None:
        day = _day(receipt_time)
        stamp = _time(receipt_time)
        with self._lock:
            seen = self._bars_written.setdefault(day, set())
            records = []
            for bar in bars:
                record = bar_record(bar)
                key = tuple(record[k] for k in sorted(record))
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
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"{captured}:{number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict) or set(record) != BAR_KEYS | CAPTURE_KEYS:
            raise CaptureError(f"{captured}:{number}: not a captured bar record")
        bar_only = {k: record[k] for k in BAR_KEYS}
        try:
            bar = parse_bar(str(captured), number, json.dumps(bar_only))
        except BarFileError as exc:
            raise CaptureError(str(exc)) from exc
        key = (bar.symbol, bar.start)
        previous = latest.get(key)
        if previous is not None:
            if previous == bar:
                duplicates += 1
            else:
                revised += 1
        latest[key] = bar
    return CapturedBars(latest, duplicates, revised)


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
