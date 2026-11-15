"""Quote latency from capture files: receipt time minus provider time, by symbol and by hour,
plus the stream notice 1001 counters per hour. See docs/research.md ("latency").

Reads the capture format of `tickr.data.capture` as written; any malformed line is refused
with file, line and cause. A negative latency (receipt before provider time) is clock skew: it
stays in the statistics and is counted, never dropped.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tickr.archive import open_text
from tickr.research.errors import ResearchError

QUOTE_KEYS = frozenset({"symbol", "bid", "ask", "bid_size", "ask_size", "provider_time",
                        "receipt_time", "provider", "source"})
NOTICE_KEYS = frozenset({"type", "drop", "rtt", "sent", "provider", "receipt_time"})
NOTICE_COUNTERS = ("drop", "rtt", "sent")
NOTICE_STATUS_TYPE = "1001"
# The report's percentile column is named p90: it is the definition of the column, not a tunable.
PERCENTILE = 90
PERCENT = 100
MICROSECONDS_PER_MILLISECOND = 1000
HOUR_FORMAT = "%Y-%m-%d %H:00"


@dataclass(frozen=True)
class Stats:
    count: int
    median: Decimal
    p90: Decimal
    max: Decimal
    min: Decimal
    clock_skew: int  # values below zero


@dataclass(frozen=True)
class LatencyReport:
    by_symbol: dict[str, Stats]
    by_hour: dict[str, Stats]
    notices_by_hour: dict[str, dict[str, Decimal | int]]
    partial_last_lines: int  # files whose last line had no newline yet (being written)


def stats(values: Sequence[Decimal]) -> Stats:
    """Median (middle two averaged), nearest-rank p90, max, min and the count below zero."""
    ordered = sorted(values)
    rank = -(-len(ordered) * PERCENTILE // PERCENT)
    return Stats(len(ordered), statistics.median(ordered), ordered[rank - 1], ordered[-1],
                 ordered[0], sum(1 for v in ordered if v < 0))


def _read(path: Path, keys: frozenset[str], parse: Callable[[str, dict[str, Any]], None]) -> bool:
    """Parse every complete line of a capture file. True when a last unterminated line (the
    bot may be appending right now) was left unread."""
    with open_text(path) as handle:  # plain or archived (.gz)
        lines = handle.readlines()
    partial = bool(lines) and not lines[-1].endswith("\n")
    if partial:
        lines.pop()
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        where = f"{path}:{number}"
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ResearchError(f"{where}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict) or set(record) != keys:
            raise ResearchError(f"{where}: not a captured record with keys {sorted(keys)}")
        parse(where, record)
    return partial


def _time(where: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() is None:
        raise ResearchError(f"{where}: not an aware ISO-8601 time: {value!r}")
    return parsed


def latency_report(quote_files: Sequence[Path], notice_files: Sequence[Path], source: str,
                   zone: ZoneInfo) -> LatencyReport:
    """Quotes of `source` ("stream" or "snapshot"); hours are receipt hours in `zone`."""
    by_symbol: dict[str, list[Decimal]] = {}
    by_hour: dict[str, list[Decimal]] = {}
    counters: dict[str, dict[str, list[int]]] = {}

    def quote(where: str, record: dict[str, Any]) -> None:
        if record["source"] != source:
            return
        received = _time(where, record["receipt_time"])
        delta = received - _time(where, record["provider_time"])
        millis = Decimal(delta // timedelta(microseconds=1)) / MICROSECONDS_PER_MILLISECOND
        by_symbol.setdefault(str(record["symbol"]), []).append(millis)
        by_hour.setdefault(received.astimezone(zone).strftime(HOUR_FORMAT), []).append(millis)

    def notice(where: str, record: dict[str, Any]) -> None:
        if record["type"] != NOTICE_STATUS_TYPE:
            raise ResearchError(f"{where}: unknown notice type {record['type']!r}")
        hour = _time(where, record["receipt_time"]).astimezone(zone).strftime(HOUR_FORMAT)
        slot = counters.setdefault(hour, {name: [] for name in NOTICE_COUNTERS})
        for name in NOTICE_COUNTERS:
            value = record[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ResearchError(f"{where}: {name} is not a whole number >= 0: {value!r}")
            slot[name].append(value)

    partial = sum(_read(p, QUOTE_KEYS, quote) for p in quote_files)
    partial += sum(_read(p, NOTICE_KEYS, notice) for p in notice_files)
    notices = {
        hour: {"notices": len(c["rtt"]), "drop_sum": sum(c["drop"]), "drop_max": max(c["drop"]),
               "sent_sum": sum(c["sent"]), "sent_max": max(c["sent"]),
               "rtt_median": statistics.median(map(Decimal, c["rtt"])), "rtt_max": max(c["rtt"])}
        for hour, c in sorted(counters.items())
    }
    return LatencyReport({k: stats(v) for k, v in sorted(by_symbol.items())},
                         {k: stats(v) for k, v in sorted(by_hour.items())}, notices, partial)
