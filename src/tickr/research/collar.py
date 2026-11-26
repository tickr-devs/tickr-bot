"""How far an entry price falls from the price collar's references in captured data (backlog
T10), so the owner can pick `[risk] entry_collar_bar_ratio` and `entry_collar_quote_ratio` from
measurements. See docs/research.md ("collar").

Two kinds of entry price are measured, each against both references, with the collar's own
measure (`risk.admission.collar_distance`: |price - reference| / reference):

- `quote`: every captured stream quote's ask (a marketable entry at that moment), against the
  close of the symbol's last bar completed by the quote's provider time, and against the ask of
  the stream quote before it (as the bot's stream inbox keeps it: a quote older than the newest
  one is ignored).
- `decision` (with `--decisions`): every recorded ENTER's final limit (its `entry_high` rounded
  to the tick, as admission sees it), against the last bar completed by the decision time and
  the stream quote before the newest one received by then.

A captured bar's last received version is used (`data.capture.read_captured_bars`). Malformed
lines are refused with file, line and cause; a quote line the bot may still be writing is left.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr.data.capture import CaptureError, read_captured_bars
from tickr.research.errors import ResearchError
from tickr.research.latency import QUOTE_KEYS, _read, _time
from tickr.research.records import DecisionRecord
from tickr.risk.admission import collar_distance
from tickr.risk.ticks import round_to_tick
from tickr.strategy.base import Verdict

STREAM = "stream"
M1 = timedelta(minutes=1)
# The report's percentile columns (nearest rank): definitions of the columns, not tunables.
PERCENTILES = (Decimal(50), Decimal(90), Decimal(99), Decimal("99.9"))
PERCENTILE_NAMES = ("p50", "p90", "p99", "p99_9")
PERCENT = 100
OVERALL = "all"
REFERENCES = ("bar", "quote")


@dataclass(frozen=True)
class Distribution:
    count: int
    percentiles: tuple[Decimal, ...]
    """Nearest-rank values at `PERCENTILES`, in that order."""
    max: Decimal


@dataclass(frozen=True)
class CollarReport:
    rows: dict[tuple[str, str, str], Distribution]
    """(entry kind, reference, symbol or `all`) -> distances."""
    missing: dict[tuple[str, str], int]
    """(entry kind, reference) -> entry prices with no usable reference (no completed bar, no
    earlier quote): the bot would refuse those as PRICE_COLLAR_UNKNOWN while the collar is on."""
    partial_last_lines: int


def distribution(values: Sequence[Decimal]) -> Distribution:
    ordered = sorted(values)
    ranks = [max(math.ceil(len(ordered) * p / PERCENT), 1) for p in PERCENTILES]
    return Distribution(len(ordered), tuple(ordered[r - 1] for r in ranks), ordered[-1])


@dataclass(frozen=True)
class _Quote:
    at: datetime
    ask: Decimal


def _quotes(files: Sequence[Path]) -> tuple[dict[str, list[_Quote]], int]:
    """Stream quotes per symbol as the inbox would hold them in turn: in file order, each one
    not older than the newest so far."""
    by_symbol: dict[str, list[_Quote]] = {}

    def quote(where: str, record: dict[str, Any]) -> None:
        if record["source"] != STREAM:
            return
        try:
            ask = Decimal(record["ask"])
        except (InvalidOperation, TypeError) as exc:
            raise ResearchError(f"{where}: ask is not a number: {record['ask']!r}") from exc
        if not ask.is_finite() or ask <= 0:
            raise ResearchError(f"{where}: ask is not a price: {record['ask']!r}")
        series = by_symbol.setdefault(str(record["symbol"]), [])
        at = _time(where, record["provider_time"])
        if not series or at >= series[-1].at:
            series.append(_Quote(at, ask))

    partial = sum(_read(p, QUOTE_KEYS, quote) for p in files)
    return by_symbol, partial


def _closes(files: Sequence[Path]) -> dict[str, tuple[list[datetime], list[Decimal]]]:
    """Per symbol: bar end times (start + 1 minute) ascending, and their closes."""
    bars: dict[str, list[tuple[datetime, Decimal]]] = {}
    for path in files:
        try:
            read = read_captured_bars(path)
        except CaptureError as exc:
            raise ResearchError(str(exc)) from exc
        for (symbol, start), bar in read.bars.items():
            bars.setdefault(symbol, []).append((start + M1, bar.close))
    out = {}
    for symbol, rows in bars.items():
        rows.sort()
        out[symbol] = ([end for end, _ in rows], [close for _, close in rows])
    return out


def _last_close(closes: tuple[list[datetime], list[Decimal]] | None,
                at: datetime) -> Decimal | None:
    if closes is None:
        return None
    index = bisect.bisect_right(closes[0], at)
    return closes[1][index - 1] if index else None


def collar_report(quote_files: Sequence[Path], bar_files: Sequence[Path],
                  records: Sequence[DecisionRecord] = ()) -> CollarReport:
    quotes, partial = _quotes(quote_files)
    closes = _closes(bar_files)
    values: dict[tuple[str, str, str], list[Decimal]] = {}
    missing: dict[tuple[str, str], int] = {}

    def add(kind: str, symbol: str, price: Decimal, close: Decimal | None,
            previous: Decimal | None) -> None:
        for reference, value in zip(REFERENCES, (close, previous), strict=True):
            if value is None or value <= 0:
                missing[(kind, reference)] = missing.get((kind, reference), 0) + 1
                continue
            distance = collar_distance(price, value)
            for group in (symbol, OVERALL):
                values.setdefault((kind, reference, group), []).append(distance)

    for symbol, series in sorted(quotes.items()):
        for n, q in enumerate(series):
            add("quote", symbol, q.ask, _last_close(closes.get(symbol), q.at),
                series[n - 1].ask if n else None)
    times = {symbol: [q.at for q in series] for symbol, series in quotes.items()}
    for record in records:
        d = record.decision
        if d.verdict is not Verdict.ENTER or d.entry_high is None:
            continue
        series = quotes.get(d.symbol, [])
        newest = bisect.bisect_right(times.get(d.symbol, []), record.time)
        previous = series[newest - 2].ask if newest >= 2 else None
        add("decision", d.symbol, round_to_tick(d.entry_high),
            _last_close(closes.get(d.symbol), record.time), previous)
    rows = {key: distribution(v) for key, v in sorted(values.items())}
    return CollarReport(rows, dict(sorted(missing.items())), partial)
