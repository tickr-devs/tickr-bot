"""Daily pool of past gainers, chosen from daily bars only (no minute data, no look-ahead into
the run: the run decides per minute when a pool symbol became a gainer, see gate.py).

A (symbol, session) enters the pool when, against the previous session's close,
- the day's HIGH reached `gainer_change_percent` (a symbol on the gainers list at any minute
  had a price at least that far up, so this is a superset of the minute-level rule), and
- the day's dollar volume, typical price x volume, reached bot.toml
  `min_session_dollar_volume_usd` (discovery's floor applies to the session so far, which can
  never exceed the whole day).
Per session at most `max_pool_per_day` symbols, best daily-high change first. Ranking by the
day's high looks ahead, so the cap should not bind: `pool_cap.csv` records every session's
candidates and drops, and the report shows where it bound.

Daily bars are forward-adjusted; a ratio of two adjacent bars of the same series is unaffected.
A symbol without a bar on the previous session is skipped for that day (no previous close).
So `previous_close` and `day_close` are on the ADJUSTED daily scale, while M1 bars are raw: never
compare a pool price with an M1 price directly (a split shows as a +100% gap, a dividend biases
the gap up). `PoolEntry.previous_close_on_minute_scale` converts with the day's own M1 close.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from aperture.backtest.errors import BacktestError

POOL_FILE = "pool.csv"
POOL_COLUMNS = ("session_date", "rank", "symbol", "previous_close", "day_close",
                "day_high_change_percent", "day_dollar_volume_usd")
CAP_FILE = "pool_cap.csv"
CAP_COLUMNS = ("session_date", "candidates", "kept", "dropped")
PERCENT = Decimal(100)
THREE = Decimal(3)


@dataclass(frozen=True)
class PoolEntry:
    session_date: date
    rank: int
    symbol: str
    previous_close: Decimal
    day_close: Decimal
    day_high_change_percent: Decimal
    day_dollar_volume_usd: Decimal

    def previous_close_on_minute_scale(self, minute_day_close: Decimal) -> Decimal:
        """The previous close on the raw M1 scale of `session_date`: previous_close x (that day's
        last M1 close / day_close). The adjusted ratio previous/day is kept, so a split or a
        dividend between the two sessions does not appear as a price move."""
        if self.day_close <= 0 or minute_day_close <= 0:
            raise BacktestError(f"{self.symbol} {self.session_date}: close not positive")
        return self.previous_close * minute_day_close / self.day_close


@dataclass(frozen=True)
class CapRecord:
    """How many candidates one session had and how many the cap dropped. A binding cap ranks
    by the END-OF-DAY high, which is look-ahead: the report shows every session where it bound."""

    session_date: date
    candidates: int
    kept: int

    @property
    def dropped(self) -> int:
        return self.candidates - self.kept


def select_pool(rows: Iterable[dict[str, Any]], sessions: Sequence[date],
                change_percent: Decimal, dollar_volume_floor: Decimal,
                max_per_day: int) -> tuple[list[PoolEntry], list[CapRecord]]:
    """`sessions` are the window's session dates oldest first; the first one only supplies the
    previous close of the second."""
    by_symbol = _by_symbol(rows)
    pool: list[PoolEntry] = []
    caps: list[CapRecord] = []
    for previous, day in zip(sessions, sessions[1:], strict=False):
        candidates: list[tuple[Decimal, str, Decimal, Decimal, Decimal]] = []
        for symbol, days in by_symbol.items():
            today, before = days.get(day), days.get(previous)
            if today is None or before is None:
                continue
            previous_close = Decimal(before["close"])
            if previous_close <= 0:
                raise BacktestError(f"{symbol} {previous}: close {previous_close} not positive")
            high, close = Decimal(today["high"]), Decimal(today["close"])
            change = (high / previous_close - 1) * PERCENT
            dollar_volume = _dollar_volume(today)
            if change >= change_percent and dollar_volume >= dollar_volume_floor:
                candidates.append((change, symbol, previous_close, close, dollar_volume))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        kept = candidates[:max_per_day]
        caps.append(CapRecord(day, len(candidates), len(kept)))
        for rank, (change, symbol, previous_close, close, dollar_volume) in enumerate(
                kept, start=1):
            pool.append(PoolEntry(day, rank, symbol, previous_close, close, change,
                                  dollar_volume))
    return pool, caps


def select_liquid(rows: Iterable[dict[str, Any]], sessions: Sequence[date], top_count: int,
                  min_price: Decimal) -> tuple[list[PoolEntry], list[CapRecord]]:
    """A universe without look-ahead: for each session, the `top_count` symbols with the highest
    PREVIOUS-session dollar volume (typical price x volume) among those whose previous close is
    at least `min_price`. Only the previous session decides; the day itself is read for the
    informational columns, so a symbol with no bar on the day (halted, delisted) is left out and
    counted in pool_cap.csv as dropped."""
    by_symbol = _by_symbol(rows)
    pool: list[PoolEntry] = []
    caps: list[CapRecord] = []
    for previous, day in zip(sessions, sessions[1:], strict=False):
        ranked: list[tuple[Decimal, str]] = []
        for symbol, days in by_symbol.items():
            before = days.get(previous)
            if before is None or Decimal(before["close"]) < min_price:
                continue
            ranked.append((_dollar_volume(before), symbol))
        ranked.sort(key=lambda c: (-c[0], c[1]))
        chosen = ranked[:top_count]
        kept = 0
        for rank, (_, symbol) in enumerate(chosen, start=1):
            today = by_symbol[symbol].get(day)
            if today is None:
                continue
            previous_close = Decimal(by_symbol[symbol][previous]["close"])
            high, close = Decimal(today["high"]), Decimal(today["close"])
            kept += 1
            pool.append(PoolEntry(day, rank, symbol, previous_close, close,
                                  (high / previous_close - 1) * PERCENT, _dollar_volume(today)))
        caps.append(CapRecord(day, len(chosen), kept))
    return pool, caps


def _dollar_volume(row: dict[str, Any]) -> Decimal:
    high, low, close = (Decimal(row[k]) for k in ("high", "low", "close"))
    return (high + low + close) / THREE * Decimal(row["volume"])


def _by_symbol(rows: Iterable[dict[str, Any]]) -> dict[str, dict[date, dict[str, Any]]]:
    by_symbol: dict[str, dict[date, dict[str, Any]]] = {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        own = by_symbol.setdefault(row["symbol"], {})
        if day in own:
            raise BacktestError(f"two daily rows for {row['symbol']} on {day}")
        own[day] = row
    return by_symbol


def write_caps(caps: Sequence[CapRecord], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CAP_COLUMNS)
        for c in caps:
            writer.writerow([c.session_date.isoformat(), c.candidates, c.kept, c.dropped])


def read_caps(path: Path) -> list[CapRecord]:
    if not path.exists():
        raise BacktestError(f"{path} is missing: rerun select")
    with open(path, encoding="utf-8", newline="") as handle:
        return [CapRecord(date.fromisoformat(r["session_date"]), int(r["candidates"]),
                          int(r["kept"])) for r in csv.DictReader(handle)]


def write_pool(pool: Sequence[PoolEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(POOL_COLUMNS)
        for e in pool:
            writer.writerow([e.session_date.isoformat(), e.rank, e.symbol, e.previous_close,
                             e.day_close, e.day_high_change_percent, e.day_dollar_volume_usd])


def read_pool(path: Path) -> list[PoolEntry]:
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != POOL_COLUMNS:
                raise BacktestError(f"{path}: columns {reader.fieldnames} are not {POOL_COLUMNS}")
            return [PoolEntry(date.fromisoformat(r["session_date"]), int(r["rank"]), r["symbol"],
                              Decimal(r["previous_close"]), Decimal(r["day_close"]),
                              Decimal(r["day_high_change_percent"]),
                              Decimal(r["day_dollar_volume_usd"])) for r in reader]
    except OSError as exc:
        raise BacktestError(f"cannot read {path}: {exc}") from exc
