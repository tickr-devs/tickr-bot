"""Read-only history for the local API: closed trades from the bot database, captured M1 bars.

The API never writes here. The database is opened read-only (it runs in WAL mode, so a reader
never blocks the bot's writer) and capture files are only read. Everything is per environment:
trades of runs in the configured environment, capture files under its folder.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from aperture.api import contract as api
from aperture.api.performance import performance
from aperture.archive import existing
from aperture.calendar import ny_date_of
from aperture.config import Config
from aperture.data.capture import BARS_FILE, CaptureError, read_captured_bars
from aperture.engine.core import Leg, OrderRow, order_from_row
from aperture.store import DATABASE_FILE, MIGRATIONS, Store
from aperture.types import OrderState

ZERO = Decimal(0)
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9./-]*$")
_DAY_DIR = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class HistoryUnavailable(Exception):
    """History cannot be read right now; the message is the single cause."""


def average_fill_price(orders: list[OrderRow]) -> Decimal | None:
    filled = [o for o in orders if o.filled_quantity > 0 and o.average_fill_price is not None]
    quantity = sum((o.filled_quantity for o in filled), ZERO)
    if quantity == 0:
        return None
    return sum((o.filled_quantity * o.average_fill_price for o in filled), ZERO) / quantity


# The journal row that confirms a stop: a Detail read (`order-observed`) of one of the trade's
# stop orders showing it working at the broker. The same row `protection-latency` records as
# `stop_verified_at` (runtime/protection.py); the engine calls such a stop "verified".
OBSERVED = "order-observed"
INTENT = "order-intent"  # written before an order is sent
_WORKING = (str(OrderState.ACCEPTED), str(OrderState.PARTIALLY_FILLED))


class MalformedJournal(ValueError):
    """A journal row lacks what the metrics read; the message is the cause."""


@dataclass(frozen=True)
class FillTimes:
    """Journal times of one trade. Webull Detail has no fill time and `order-observed` is
    written only when a read shows a change, so every time here is when the journal proves
    something, which may be later than the event itself."""

    first_fill: datetime | None  # first entry read showing filled quantity > 0
    first_fill_quantity: Decimal  # the filled quantity that read showed (0 without one)
    stop_confirmed: datetime | None  # first stop read showing it working, after first_fill
    last_exit_fill: datetime | None  # last sell read (stop or exit) showing more filled
    # Latest moment the journal proves no share had been sold yet: the earliest, over the sell
    # orders that sold shares, of a close's send intent or a stop's last read showing it
    # unfilled. None when a sell order that sold has no such row.
    held_until: datetime | None


def _parsed(row: Any) -> tuple[datetime, dict[str, Any]]:
    try:
        return datetime.fromisoformat(row["at_utc"]), json.loads(row["body"])
    except (ValueError, TypeError) as exc:
        raise MalformedJournal(f"journal row {row['seq']} ({row['kind']} {row['key']}): "
                               f"{type(exc).__name__}: {exc}") from exc


def _observed(row: Any, body: dict[str, Any]) -> tuple[Decimal, str]:
    try:
        return Decimal(str(body["filled_quantity"])), str(body["state"])
    except (KeyError, ArithmeticError) as exc:
        raise MalformedJournal(f"journal row {row['seq']} ({OBSERVED} {row['key']}): "
                               f"{type(exc).__name__}: {exc}") from exc


def fill_times(store: Store, orders: list[OrderRow]) -> FillTimes:
    """Raises MalformedJournal for a row it cannot read; callers show null instead."""
    legs = {o.client_order_id: o.leg for o in orders}
    rows = store.query(
        f"SELECT seq, at_utc, kind, key, body FROM journal WHERE kind IN (?, ?) AND key IN "
        f"({', '.join('?' for _ in legs)}) ORDER BY seq", (OBSERVED, INTENT, *legs)
    ) if legs else []
    first = stop = last = None
    first_quantity = ZERO
    filled: dict[str, Decimal] = {}
    intent: dict[str, datetime] = {}
    unfilled: dict[str, datetime] = {}  # a stop's last read still showing nothing filled
    for row in rows:
        cid = row["key"]
        at, body = _parsed(row)
        if row["kind"] == INTENT:
            intent.setdefault(cid, at)
            continue
        quantity, state = _observed(row, body)
        more = quantity > filled.get(cid, ZERO)
        filled[cid] = quantity
        if legs[cid] is Leg.ENTRY:
            if first is None and quantity > 0:
                first, first_quantity = at, quantity
            continue
        if legs[cid] is Leg.STOP and first is not None and stop is None and state in _WORKING:
            stop = at
        if quantity == 0 and legs[cid] is Leg.STOP:
            unfilled[cid] = at
        if more:
            last = at
    proofs = [unfilled.get(o.client_order_id) if o.leg is Leg.STOP
              else intent.get(o.client_order_id)
              for o in orders if o.leg is not Leg.ENTRY and o.filled_quantity > 0]
    held = None if not proofs or None in proofs else min(p for p in proofs if p is not None)
    return FillTimes(first, first_quantity, stop, last, held)


def _minute(at: datetime) -> datetime:
    return at.replace(second=0, microsecond=0)


def excursions(bars: Sequence[api.BarV1], entry: Decimal, quantity: Decimal,
               exits: Sequence[Decimal], times: FillTimes,
               ) -> tuple[Decimal | None, Decimal | None]:
    """(MAE, MFE) in USD as a lower bound (docs/api.md, "Per-trade metrics"): prices certainly
    traded while shares were held, times a quantity certainly held. Prices: the entry fill
    price, the exit fill prices, and the M1 bars that start after the minute of the first
    entry fill read and end by the minute of `held_until` (none when that is unknown).
    Quantity: the smaller of the first entry fill read's quantity and `quantity` (what was
    sold). The bars of the first fill read's minute and of the last exit fill read's minute
    must be captured, as proof the capture covered the trade: (None, None) without them."""
    start, end = times.first_fill, times.last_exit_fill
    if start is None or end is None or not exits or times.first_fill_quantity <= 0:
        return None, None
    first, last = _minute(start), _minute(end)
    if not {first, last} <= {b.start for b in bars}:
        return None, None
    bound = first if times.held_until is None else _minute(times.held_until)
    inner = [b for b in bars if first < b.start < bound]
    held = min(times.first_fill_quantity, quantity)
    low = min([entry, *exits, *(b.low for b in inner)])
    high = max([entry, *exits, *(b.high for b in inner)])
    return (low - entry) * held, (high - entry) * held


def trade_metrics(store: Store, trade: api.DayTrade,
                  bars: Sequence[api.BarV1] | None) -> api.DayTrade:
    """`trade` with its display metrics (docs/api.md, "Per-trade metrics"); `bars` None
    leaves MAE and MFE null. Raises MalformedJournal. Display only: never on the daily-loss
    or trading path."""
    orders = [order_from_row(o) for o in store.query(
        "SELECT * FROM orders WHERE trade_id = ? ORDER BY rowid", (trade.trade_id,))]
    times = fill_times(store, orders)
    confirmed = (None if times.first_fill is None or times.stop_confirmed is None
                 else api.seconds_between(times.first_fill, times.stop_confirmed))
    mae = mfe = None
    if bars is not None:
        exits = [o.average_fill_price for o in orders if o.leg is not Leg.ENTRY
                 and o.filled_quantity > 0 and o.average_fill_price is not None]
        mae, mfe = excursions(bars, trade.entry_price, trade.quantity, exits, times)
    return replace(trade, stop_confirmed_after_seconds=confirmed, mae_usd=mae, mfe_usd=mfe)


def closed_trades(store: Store, environment: str | None = None,
                  closed_on: date | None = None) -> list[api.DayTrade]:
    """Closed trades that had fills, oldest close first, with their gross result.

    `environment` limits them to runs of that environment; None takes every run (the bot's own
    database view). `closed_on` keeps only trades closed that New York date (checked before
    their orders are read, so the bot's per-step call stays cheap). Quantity is what was
    sold; prices are fill-weighted averages."""
    if environment is None:
        rows = store.query("SELECT * FROM trades WHERE closed_at IS NOT NULL "
                           "ORDER BY closed_at, trade_id")
    else:
        rows = store.query("SELECT t.* FROM trades t JOIN runs r ON r.run_id = t.run_id "
                           "WHERE r.environment = ? AND t.closed_at IS NOT NULL "
                           "ORDER BY t.closed_at, t.trade_id", (environment,))
    result = []
    for row in rows:
        if closed_on is not None and ny_date_of(
                datetime.fromisoformat(row["closed_at"])) != closed_on:
            continue
        orders = [order_from_row(o) for o in store.query(
            "SELECT * FROM orders WHERE trade_id = ? ORDER BY rowid", (row["trade_id"],))]
        entries = [o for o in orders if o.leg is Leg.ENTRY]
        sells = [o for o in orders if o.leg is not Leg.ENTRY]
        entry_price, exit_price = average_fill_price(entries), average_fill_price(sells)
        quantity = sum((o.filled_quantity for o in sells), ZERO)
        if entry_price is None or exit_price is None or quantity == 0:
            continue  # never filled: not a trade
        stops = [o.stop_price for o in orders if o.leg is Leg.STOP and o.stop_price is not None]
        result.append(api.DayTrade(
            trade_id=row["trade_id"], symbol=row["symbol"], quantity=quantity,
            entry_price=entry_price, exit_price=exit_price,
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_at=datetime.fromisoformat(row["closed_at"]),
            exit_reason=row["close_cause"], stop_price=stops[0] if stops else None,
            planned_loss_usd=Decimal(row["planned_loss_usd"]),
            result_gross_usd=(exit_price - entry_price) * quantity,
            # Display metrics are never read here (see `trade_metrics`): this path feeds the
            # daily-loss check and stays as it was.
            stop_confirmed_after_seconds=None, mae_usd=None, mfe_usd=None))
    return result


@dataclass(frozen=True)
class HistoryPaths:
    environment: str
    database: Path
    capture: Path  # <capture_dir>/<environment>

    @classmethod
    def from_config(cls, config: Config) -> HistoryPaths:
        data = config.root / config.run.data_dir
        return cls(config.environment_name, data / DATABASE_FILE,
                   data / config.market_data.capture_dir / config.environment_name)


class History:
    """Answers GET /days, /days/<date>, /bars and /performance. Thread safe; caches the last
    bars file read."""

    def __init__(self, paths: HistoryPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()
        self._bars_key: tuple[str, int, int, bool] | None = None
        self._bars: dict[str, list[api.BarV1]] = {}

    # ------------------------------------------------------------ sources

    def _read_db(self, metrics_of: tuple[date, date] | None = None,
                 ) -> tuple[list[api.DayTrade], set[date]]:
        """Closed trades and New York dates of bot runs; none while no database exists.
        `metrics_of` (day, today): the trades closed that day get their display metrics."""
        if not self.paths.database.exists():
            return [], set()
        try:
            store = Store.open_readonly(self.paths.database)
        except sqlite3.Error as exc:
            raise HistoryUnavailable(f"database unreadable: {exc}") from exc
        try:
            version = store.schema_version
            if version != len(MIGRATIONS):
                raise HistoryUnavailable(
                    f"database schema v{version}, this API reads v{len(MIGRATIONS)}")
            trades = closed_trades(store, self.paths.environment)
            if metrics_of is not None:
                trades = [self._with_metrics(store, t, *metrics_of) for t in trades]
            runs = {ny_date_of(datetime.fromisoformat(r["started_at"])) for r in store.query(
                "SELECT started_at FROM runs WHERE environment = ?", (self.paths.environment,))}
            return trades, runs
        except sqlite3.Error as exc:
            raise HistoryUnavailable(f"database unreadable: {exc}") from exc
        finally:
            store.close()

    def _with_metrics(self, store: Store, trade: api.DayTrade, day: date,
                      today: date) -> api.DayTrade:
        if ny_date_of(trade.closed_at) != day:
            return trade
        try:
            return trade_metrics(store, trade,
                                 self._bars_by_symbol(day, today).get(trade.symbol, []))
        except MalformedJournal:
            return trade  # display only: an unreadable journal row shows null metrics

    def _capture_days(self) -> set[date]:
        if not self.paths.capture.is_dir():
            return set()
        return {date.fromisoformat(p.name) for p in self.paths.capture.iterdir()
                if p.is_dir() and _DAY_DIR.match(p.name)}

    def _bars_file(self, day: date) -> Path:
        return self.paths.capture / day.isoformat() / BARS_FILE

    def _bars_by_symbol(self, day: date, today: date) -> dict[str, list[api.BarV1]]:
        path = existing(self._bars_file(day))  # plain or archived (.gz)
        if path is None:
            return {}
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size, day == today)
        with self._lock:
            if self._bars_key == key:
                return self._bars
        try:
            # Today's file may be half way through a line the bot is writing.
            read = read_captured_bars(path, open_tail=day == today)
        except (CaptureError, OSError) as exc:
            raise HistoryUnavailable(f"capture file unreadable: {exc}") from exc
        by_symbol: dict[str, list[api.BarV1]] = {}
        for (symbol, _), bar in sorted(read.bars.items()):
            by_symbol.setdefault(symbol, []).append(api.BarV1(
                bar.start, bar.open, bar.high, bar.low, bar.close, bar.volume))
        with self._lock:
            self._bars_key, self._bars = key, by_symbol
        return by_symbol

    # ------------------------------------------------------------ answers

    @staticmethod
    def _of_day(trades: Iterable[api.DayTrade], day: date) -> list[api.DayTrade]:
        return [t for t in trades if ny_date_of(t.closed_at) == day]

    def days(self) -> api.DaysV1:
        trades, runs = self._read_db()
        dates = runs | self._capture_days() | {ny_date_of(t.closed_at) for t in trades}
        summaries = []
        for day in sorted(dates, reverse=True):
            of_day = self._of_day(trades, day)
            summaries.append(api.DaySummary(
                day=day, trades=len(of_day),
                wins=sum(1 for t in of_day if t.result_gross_usd > 0),
                losses=sum(1 for t in of_day if t.result_gross_usd < 0),
                result_gross_usd=sum((t.result_gross_usd for t in of_day), ZERO),
                has_bars=existing(self._bars_file(day)) is not None))
        return api.DaysV1(environment=self.paths.environment, days=tuple(summaries))

    def day(self, day: date, today: date) -> api.DayV1:
        trades, _ = self._read_db((day, today))
        of_day = self._of_day(trades, day)
        symbols = tuple(sorted(self._bars_by_symbol(day, today)))
        return api.DayV1(environment=self.paths.environment, day=day, trades=tuple(of_day),
                         result_gross_usd=sum((t.result_gross_usd for t in of_day), ZERO),
                         bar_symbols=symbols)

    def bars(self, symbol: str, day: date, today: date) -> api.BarsV1:
        bars = self._bars_by_symbol(day, today).get(symbol, [])
        return api.BarsV1(environment=self.paths.environment, symbol=symbol, day=day,
                          bars=tuple(bars))

    def performance(self, from_day: date, to_day: date, places: int) -> api.PerformanceV1:
        trades, _ = self._read_db()
        return performance(self.paths.environment, from_day, to_day, trades, places)
