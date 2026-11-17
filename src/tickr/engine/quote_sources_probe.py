"""Probe Q1, read only: which Webull read is fresh outside the regular session.

F7 measured snapshot quotes minutes old in the post-market (docs/webull-facts.md), but the bot's
snapshot read never sends `extend_hour_required`, so that age may be the read, not the market.
`tickr probe-quote-sources SYMBOL...` reads each symbol four ways (the snapshot as the bot reads
it, the snapshot with `extend_hour_required`, the top of the book and the newest trades), keeps
every answer as received in `sources.jsonl` of an evidence folder, and prints the age of every
time field each answer holds. It sends no orders and decides nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tickr.data.base import SourceRead

SOURCES_FILE = "sources.jsonl"
TIME_MARK = "time"  # a field whose name contains this holds a time (quote_time, time, ...)
MS_PER_SECOND = 1000
UTC_SUFFIX = "+0000"  # bars and ticks spell UTC this way; fromisoformat wants +00:00
UTC_OFFSET = "+00:00"
AGE_STEP = Decimal("0.1")  # printed ages, seconds


@runtime_checkable
class QuoteSources(Protocol):
    def quote_sources(self, symbol: str) -> list[SourceRead]: ...


def _as_time(value: Any) -> datetime | None:
    """An epoch-ms number (or its string) or an ISO time with an offset; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / MS_PER_SECOND, UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace(UTC_SUFFIX, UTC_OFFSET))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def time_fields(body: Any, path: str = "") -> Iterator[tuple[str, datetime]]:
    """Every (path, time) in `body` whose key names a time, depth first, in body order."""
    if isinstance(body, dict):
        for key, value in body.items():
            where = f"{path}.{key}" if path else str(key)
            if TIME_MARK in str(key):
                when = _as_time(value)
                if when is not None:
                    yield where, when
                    continue
            yield from time_fields(value, where)
    elif isinstance(body, list):
        for index, value in enumerate(body):
            yield from time_fields(value, f"{path}[{index}]")


def describe(read: SourceRead) -> list[str]:
    """Lines for one read: its answer, then each time field with its age in seconds."""
    head = f"  {read.name}: HTTP {read.status}"
    if read.error_code:
        head += f" {read.error_code}"
    lines = [head]
    for where, when in time_fields(read.body):
        age = Decimal(str((read.received - when).total_seconds())).quantize(AGE_STEP)
        lines.append(f"    {where} {when.isoformat()} age {age} s")
    return lines


def run(source: QuoteSources, symbols: Sequence[str], folder: Path,
        out: Callable[[str], None]) -> int:
    """Read and record every symbol; 0 when every read was answered, 3 otherwise."""
    unanswered = 0
    with (folder / SOURCES_FILE).open("a", encoding="utf-8") as file:
        for symbol in symbols:
            out(symbol)
            for read in source.quote_sources(symbol):
                file.write(json.dumps({"symbol": symbol, "read": read.name,
                                       "status": read.status, "error_code": read.error_code,
                                       "received": read.received.isoformat(),
                                       "body": read.body}, default=str, sort_keys=True) + "\n")
                for line in describe(read):
                    out(line)
                if read.status is None:
                    unanswered += 1
    return 0 if unanswered == 0 else 3
