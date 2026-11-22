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
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.api import contract as api
from tickr.api.after import AfterSettings, Closes, trade_after
from tickr.api.cleanday import clean_days
from tickr.api.performance import performance
from tickr.api.settled import DayBarsIndex
from tickr.archive import existing
from tickr.calendar import NEW_YORK, CalendarRefused, SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.data.capture import BARS_FILE, CapturedBarsDays, CaptureError
from tickr.engine.core import (
    ORDER_HELD,
    ORDER_OBSERVED,
    TRADE_OPENED,
    Leg,
    OrderRow,
    SendState,
    order_from_row,
)
from tickr.store import DATABASE_FILE, MIGRATIONS, Store
from tickr.types import Bar, OrderState
from tickr.version import recorded

ZERO = Decimal(0)
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9./-]*$")
_DAY_DIR = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _bar_start(bar: Bar) -> datetime:
    return bar.start


class HistoryUnavailable(Exception):
    """History cannot be read right now; the message is the single cause."""


def average_fill_price(orders: list[OrderRow]) -> Decimal | None:
    """Fill-weighted average of the orders' fill prices, each the exact one when recorded
    (`OrderRow.fill_price`)."""
    filled = [(o.filled_quantity, o.fill_price) for o in orders if o.filled_quantity > 0]
    priced = [(quantity, price) for quantity, price in filled if price is not None]
    quantity = sum((q for q, _ in priced), ZERO)
    if quantity == 0:
        return None
    return sum((q * price for q, price in priced), ZERO) / quantity


def recorded_fees(orders: list[OrderRow]) -> Decimal | None:
    """The broker's fees on the orders that filled, as recorded (`OrderRow.fees_usd`); None
    while any of them is not recorded yet (never counted as free)."""
    total = ZERO
    for order in orders:
        if order.filled_quantity > 0:
            if order.fees_usd is None:
                return None
            total += order.fees_usd
    return total


def trade_net(orders: list[OrderRow], gross: Decimal) -> Decimal | None:
    """A closed trade's result after the broker's fees, as the daily loss counts it: the sum
    of its filled orders' settled cash amounts once every one is proven by the account's cash
    activity (the broker's own figure); before that, `gross` (fill prices, exact where
    recorded) less the recorded fees; None while any filled order's fees are not recorded."""
    filled = [o for o in orders if o.filled_quantity > 0]
    if filled and all(o.net_cash_usd is not None for o in filled):
        return sum((o.net_cash_usd for o in filled if o.net_cash_usd is not None), ZERO)
    fees = recorded_fees(orders)
    return None if fees is None else gross - fees


def total_net(trades: Iterable[api.DayTrade]) -> Decimal | None:
    """The sum of the trades' results after fees; None while any of them is unknown."""
    total = ZERO
    for trade in trades:
        if trade.result_net_usd is None:
            return None
        total += trade.result_net_usd
    return total


# The journal row that confirms a stop: a Detail read (`order-observed`) of one of the trade's
# stop orders showing it working at the broker. The same row `protection-latency` records as
# `stop_verified_at` (runtime/protection.py); the engine calls such a stop "verified".
OBSERVED = ORDER_OBSERVED
# A later unchanged read of a stop still working with nothing filled (journaled at most once per
# `[orders] stop_held_proof_seconds`): extends the window MAE/MFE prove the shares were held.
HELD = ORDER_HELD
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
    # unfilled (`order-observed` or `order-held`, filled quantity 0 and no earlier read showing
    # a fill). None when a sell order that sold has no such row.
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
        raise MalformedJournal(f"journal row {row['seq']} ({row['kind']} {row['key']}): "
                               f"{type(exc).__name__}: {exc}") from exc


def fill_times(store: Store, orders: list[OrderRow]) -> FillTimes:
    """Raises MalformedJournal for a row it cannot read; callers show null instead."""
    legs = {o.client_order_id: o.leg for o in orders}
    rows = store.query(
        f"SELECT seq, at_utc, kind, key, body FROM journal WHERE kind IN (?, ?, ?) AND key IN "
        f"({', '.join('?' for _ in legs)}) ORDER BY seq", (OBSERVED, HELD, INTENT, *legs)
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
        if row["kind"] == HELD:
            # Proof only: never a fill, a confirmation or a change. After a read showing a
            # fill it proves nothing about the full quantity.
            if (legs[cid] is Leg.STOP and quantity == 0 and filled.get(cid, ZERO) == 0
                    and state in _WORKING):
                unfilled[cid] = at
            continue
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
        exits = [o.fill_price for o in orders if o.leg is not Leg.ENTRY
                 and o.filled_quantity > 0 and o.fill_price is not None]
        mae, mfe = excursions(bars, trade.entry_price, trade.quantity, exits, times)
    return replace(trade, stop_confirmed_after_seconds=confirmed, mae_usd=mae, mfe_usd=mfe)


# Entry send (docs/api.md, "Entry send"): the engine journals `order-<SendState>` for every
# change of an order's send state: `order-intent` before the send, `order-attempted` just
# before the HTTP call, then the outcome.
_SEND_KINDS = {f"order-{state}": state for state in SendState}
NO_ENTRY = api.EntrySend(None, None, None, None, None, None, None)
NOT_READ = "not read on this path"  # the daily-loss path never reads the journal


def _send_outcome(states: list[SendState]) -> SendState | None:
    """The state the send itself recorded: the row after the first attempt (acknowledged,
    refused or unknown); without an attempt, the row after the intent (absent when recovery
    found it never attempted); with no such row yet, that last state (intent or attempted).
    A later resolution by a broker read (unknown -> acknowledged) does not change it."""
    for anchor in (SendState.ATTEMPTED, SendState.INTENT):
        if anchor in states:
            at = states.index(anchor)
            return states[at + 1] if at + 1 < len(states) else anchor
    return states[-1] if states else None


def trade_strategy(store: Store, trade_id: str) -> api.StrategyId | None:
    """The strategy (name, version) whose decision opened the trade, from its `trade-opened`
    row; None for a manual trade or one opened before the bot journaled it. Raises
    MalformedJournal for a row it cannot read. Display only."""
    rows = store.query("SELECT seq, at_utc, kind, key, body FROM journal WHERE kind = ? AND "
                       "key = ? ORDER BY seq", (TRADE_OPENED, trade_id))
    if len(rows) != 1:
        if rows:
            raise MalformedJournal(f"trade {trade_id} has {len(rows)} {TRADE_OPENED} rows")
        return None
    _, body = _parsed(rows[0])
    name, version = body.get("strategy"), body.get("strategy_version")
    if name is None and version is None:
        return None
    if not isinstance(name, str) or not isinstance(version, str):
        raise MalformedJournal(f"trade {trade_id}: {TRADE_OPENED} strategy {name!r} "
                               f"version {version!r}")
    return api.StrategyId(name, version)


def entry_send(store: Store, trade_id: str) -> tuple[api.EntrySend, str | None]:
    """The trade's entry send as journaled, and why any field is null (None when none is).
    Raises MalformedJournal for a row it cannot read. Display only: never on the daily-loss
    or trading path."""
    entries = [order_from_row(o) for o in store.query(
        "SELECT * FROM orders WHERE trade_id = ? AND leg = ? ORDER BY rowid",
        (trade_id, str(Leg.ENTRY)))]
    if len(entries) != 1:
        return NO_ENTRY, f"trade {trade_id} has {len(entries)} entry orders, expected 1"
    cid = entries[0].client_order_id
    kinds = (*_SEND_KINDS, OBSERVED)
    marks = ", ".join("?" for _ in kinds)
    rows = store.query("SELECT seq, at_utc, kind, key, body FROM journal WHERE kind IN "
                       f"({marks}) AND key = ? ORDER BY seq", (*kinds, cid))
    intent = sent = filled = None
    states: list[SendState] = []
    for row in rows:
        at, body = _parsed(row)
        if row["kind"] == OBSERVED:
            quantity, _ = _observed(row, body)
            if filled is None and quantity > 0:
                filled = at
            continue
        state = _SEND_KINDS[row["kind"]]
        states.append(state)
        if state is SendState.INTENT and intent is None:
            intent = at
        elif state is SendState.ATTEMPTED and sent is None:
            sent = at
    outcome = _send_outcome(states)
    causes = [text for missing, text in (
        (intent is None, "no order-intent row"),
        (sent is None, "never attempted: no order-attempted row"),
        (filled is None, "no order-observed row shows the entry filled")) if missing]
    entry = api.EntrySend(cid, intent, sent,
                          None if outcome is None else api.SendOutcome(outcome),
                          api.SendOutcome(states[-1]) if states else None,
                          states.count(SendState.ATTEMPTED), filled)
    return entry, "; ".join(causes) or None


def closed_trades(store: Store, environment: str | None = None,
                  closed_on: date | None = None) -> list[api.DayTrade]:
    """Closed trades that had fills, oldest close first, with their gross result.

    `environment` limits them to runs of that environment; None takes every run (the bot's own
    database view). `closed_on` keeps only trades closed that New York date (checked before
    their orders are read, so the bot's per-step call stays cheap). Quantity is what was
    sold; prices are fill-weighted averages."""
    # The code version is the opening run's own record (`runs.code_version`), never re-derived.
    if environment is None:
        rows = store.query("SELECT t.*, r.code_version AS run_code_version FROM trades t "
                           "LEFT JOIN runs r ON r.run_id = t.run_id "
                           "WHERE t.closed_at IS NOT NULL ORDER BY t.closed_at, t.trade_id")
    else:
        rows = store.query("SELECT t.*, r.code_version AS run_code_version FROM trades t "
                           "JOIN runs r ON r.run_id = t.run_id "
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
            result_net_usd=trade_net(orders, (exit_price - entry_price) * quantity),
            # Display metrics are never read here (see `trade_metrics`): this path feeds the
            # daily-loss check and stays as it was.
            stop_confirmed_after_seconds=None, mae_usd=None, mfe_usd=None,
            entry=NO_ENTRY, entry_cause=NOT_READ, prices_after=(), prices_after_cause=NOT_READ,
            overshoot_usd=None, overshoot_r=None, overshoot_cause=NOT_READ, strategy=None,
            code_version=(None if row["run_code_version"] is None
                          else recorded(row["run_code_version"]))))
    return result


@dataclass(frozen=True)
class HistoryPaths:
    environment: str
    database: Path
    capture: Path  # <capture_dir>/<environment>
    calendar: Path  # the session calendar's folder (clean-day verdicts)
    reconcile_max_gap_seconds: Decimal  # [orders]: clean-day verdicts

    @classmethod
    def from_config(cls, config: Config) -> HistoryPaths:
        data = config.root / config.run.data_dir
        return cls(config.environment_name, data / DATABASE_FILE,
                   data / config.market_data.capture_dir / config.environment_name,
                   (config.root / config.session.calendar_file).parent,
                   config.orders.reconcile_max_gap_seconds)


class History:
    """Answers GET /days, /days/<date>, /bars and /performance. Thread safe; caches the last
    bars file read. Without `after`, day-v1 trades carry no prices after and no overshoot."""

    def __init__(self, paths: HistoryPaths, after: AfterSettings | None = None,
                 bars: CapturedBarsDays | None = None,
                 bars_index: DayBarsIndex | None = None) -> None:
        self.paths = paths
        self.captured_bars = bars or CapturedBarsDays()  # the server's shared reader
        # A past day's bars by symbol on disk (api/settled.py); None = always the bars file.
        self.bars_index = bars_index
        self.after = after
        self.closes = None if after is None else Closes(after.calendar)
        self._lock = threading.Lock()
        self._session_calendar: SessionCalendar | None = None

    # ------------------------------------------------------------ sources

    def _open(self) -> Store | None:
        """The database read-only, None while none exists; the caller closes it."""
        if not self.paths.database.exists():
            return None
        try:
            store = Store.open_readonly(self.paths.database)
        except sqlite3.Error as exc:
            raise HistoryUnavailable(f"database unreadable: {exc}") from exc
        try:
            version = store.schema_version
        except sqlite3.Error as exc:
            store.close()
            raise HistoryUnavailable(f"database unreadable: {exc}") from exc
        if version != len(MIGRATIONS):
            store.close()
            raise HistoryUnavailable(
                f"database schema v{version}, this API reads v{len(MIGRATIONS)}")
        return store

    def entries(self, day: date, today: date) -> tuple[api.FlowStopV1, ...]:
        """Trades of this environment opened on `day` whose entry filled, oldest first, each
        with its stop as the journal proves it (`fill_times`, the same rows as
        `stop_confirmed_after_seconds`): `verified` once seen working; else `pending` while
        the trade is open today, `never_verified` once it is closed or the day is past."""
        store = self._open()
        if store is None:
            return ()
        # `opened_at` is written by Store.stamp() as UTC ISO text, so text order is time order.
        start, end = (datetime.combine(d, time(), NEW_YORK).astimezone(UTC).isoformat()
                      for d in (day, day + timedelta(days=1)))
        try:
            found = []
            for row in store.query(
                    "SELECT t.trade_id, t.symbol, t.opened_at, t.closed_at FROM trades t "
                    "JOIN runs r ON r.run_id = t.run_id WHERE r.environment = ? "
                    "AND t.opened_at >= ? AND t.opened_at < ? ORDER BY t.opened_at, t.trade_id",
                    (self.paths.environment, start, end)):
                if ny_date_of(datetime.fromisoformat(row["opened_at"])) != day:
                    continue  # a row not in Store.stamp() form; never seen, kept as a guard
                orders = [order_from_row(o) for o in store.query(
                    "SELECT * FROM orders WHERE trade_id = ? ORDER BY rowid", (row["trade_id"],))]
                if not any(o.leg is Leg.ENTRY and o.filled_quantity > 0 for o in orders):
                    continue
                closed = (None if row["closed_at"] is None
                          else datetime.fromisoformat(row["closed_at"]))
                try:
                    times = fill_times(store, orders)
                except MalformedJournal:
                    found.append(api.FlowStopV1(row["trade_id"], row["symbol"],
                                                api.FlowStopState.UNKNOWN, closed_at=closed))
                    continue
                open_today = day == today and row["closed_at"] is None
                state = (api.FlowStopState.VERIFIED if times.stop_confirmed is not None
                         else api.FlowStopState.PENDING if open_today
                         else api.FlowStopState.NEVER_VERIFIED)
                found.append(api.FlowStopV1(row["trade_id"], row["symbol"], state,
                                            times.first_fill, times.stop_confirmed, closed))
            return tuple(found)
        except sqlite3.Error as exc:
            raise HistoryUnavailable(f"database unreadable: {exc}") from exc
        finally:
            store.close()

    def _read_db(self, metrics_of: tuple[date, date] | None = None,
                 ) -> tuple[list[api.DayTrade], set[date]]:
        """Closed trades and New York dates of bot runs; none while no database exists.
        `metrics_of` (day, today): the trades closed that day get their display metrics."""
        store = self._open()
        if store is None:
            return [], set()
        try:
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
        # Display only: an unreadable journal row shows null, each part on its own.
        try:
            entry, cause = entry_send(store, trade.trade_id)
        except MalformedJournal as exc:
            entry, cause = NO_ENTRY, f"journal unreadable: {exc}"
        try:
            strategy = trade_strategy(store, trade.trade_id)
        except MalformedJournal:
            strategy = None
        trade = replace(trade, entry=entry, entry_cause=cause, strategy=strategy)
        bars = self._bars_of(trade.symbol, day, today)
        if self.after is not None and self.closes is not None:
            trade = trade_after(trade, bars, self.closes.close(day), self.after)
        try:
            return trade_metrics(store, trade, bars)
        except MalformedJournal:
            return trade

    def _capture_days(self) -> set[date]:
        if not self.paths.capture.is_dir():
            return set()
        return {date.fromisoformat(p.name) for p in self.paths.capture.iterdir()
                if p.is_dir() and _DAY_DIR.match(p.name)}

    def _bars_file(self, day: date) -> Path:
        return self.paths.capture / day.isoformat() / BARS_FILE

    def _bars_by_symbol(self, day: date, today: date) -> dict[str, tuple[Bar, ...]]:
        path = existing(self._bars_file(day))  # plain or archived (.gz)
        if path is None:
            return {}
        try:
            # Today's file may be half way through a line the bot is writing.
            return self.captured_bars.by_symbol(path, open_tail=day == today)
        except (CaptureError, OSError) as exc:
            raise HistoryUnavailable(f"capture file unreadable: {exc}") from exc

    def _bars_on(self, day: date, today: date) -> dict[str, tuple[Bar, ...]]:
        """Each symbol's captured bars that start on New York date `day`. A day's capture file
        holds the bars received that day, and the first reads of a session reach back into the
        sessions before it (a read returns the last `[market_data] bars_per_symbol` minutes):
        those belong to their own date, never to this day's chart, trades or symbols. The
        simulations read the whole file (their history); this view does not."""
        first = datetime.combine(day, time(), NEW_YORK)
        after = datetime.combine(day + timedelta(days=1), time(), NEW_YORK)
        on_day: dict[str, tuple[Bar, ...]] = {}
        for symbol, bars in self._bars_by_symbol(day, today).items():
            # Each symbol's bars are in start order (CapturedBarsDays): cut, never scan.
            low = bisect_left(bars, first, key=_bar_start)
            high = bisect_left(bars, after, lo=low, key=_bar_start)
            if high > low:
                on_day[symbol] = bars[low:high]
        return on_day

    def _bars_v1_on(self, day: date, today: date) -> dict[str, list[api.BarV1]]:
        return {symbol: [api.BarV1(b.start, b.open, b.high, b.low, b.close, b.volume)
                         for b in bars] for symbol, bars in self._bars_on(day, today).items()}

    def _indexed(self, day: date, today: date) -> Path | None:
        """The bars file of past `day` when its bars are read through the index; None for
        today (still written), without an index, or without a bars file."""
        if self.bars_index is None or day >= today:
            return None
        return existing(self._bars_file(day))

    def _symbols_on(self, day: date, today: date) -> tuple[str, ...]:
        """The symbols with captured bars that start on `day`, sorted."""
        path = self._indexed(day, today)
        if path is None or self.bars_index is None:
            return tuple(sorted(self._bars_on(day, today)))
        try:
            return self.bars_index.symbols(day, path, lambda: self._bars_v1_on(day, today))
        except (OSError, ValueError) as exc:
            raise HistoryUnavailable(f"bars index unreadable: {exc}") from exc

    def _bars_of(self, symbol: str, day: date, today: date) -> list[api.BarV1]:
        path = self._indexed(day, today)
        if path is None or self.bars_index is None:
            return [api.BarV1(bar.start, bar.open, bar.high, bar.low, bar.close, bar.volume)
                    for bar in self._bars_on(day, today).get(symbol, ())]
        try:
            return self.bars_index.bars(day, path, lambda: self._bars_v1_on(day, today), symbol)
        except (OSError, ValueError) as exc:
            raise HistoryUnavailable(f"bars index unreadable: {exc}") from exc

    # ------------------------------------------------------------ answers

    @staticmethod
    def _of_day(trades: Iterable[api.DayTrade], day: date) -> list[api.DayTrade]:
        return [t for t in trades if ny_date_of(t.closed_at) == day]

    def _calendar(self) -> SessionCalendar | None:
        """The session calendar, loaded once; None when it cannot be read (no day is clean
        then: the verdict says why)."""
        with self._lock:
            if self._session_calendar is None:
                try:
                    self._session_calendar = SessionCalendar.load(self.paths.calendar)
                except (CalendarRefused, OSError):
                    return None
            return self._session_calendar

    def _clean(self, dates: set[date], today: date) -> dict[date, api.CleanDay]:
        calendar = self._calendar()
        store = self._open()
        try:
            return clean_days(store, self.paths.environment, dates, today, calendar,
                              self.paths.reconcile_max_gap_seconds)
        except sqlite3.Error as exc:
            raise HistoryUnavailable(f"database unreadable: {exc}") from exc
        finally:
            if store is not None:
                store.close()

    def latest_past_day(self, today: date) -> date | None:
        """The newest New York date before `today` with a capture folder, or None."""
        return max((d for d in self._capture_days() if d < today), default=None)

    def days(self, today: date) -> api.DaysV1:
        trades, runs = self._read_db()
        dates = runs | self._capture_days() | {ny_date_of(t.closed_at) for t in trades}
        clean = self._clean(dates, today)
        summaries = []
        for day in sorted(dates, reverse=True):
            of_day = self._of_day(trades, day)
            summaries.append(api.DaySummary(
                day=day, trades=len(of_day),
                wins=sum(1 for t in of_day if t.result_gross_usd > 0),
                losses=sum(1 for t in of_day if t.result_gross_usd < 0),
                result_gross_usd=sum((t.result_gross_usd for t in of_day), ZERO),
                result_net_usd=total_net(of_day),
                has_bars=existing(self._bars_file(day)) is not None, clean=clean[day]))
        return api.DaysV1(environment=self.paths.environment, days=tuple(summaries))

    def day(self, day: date, today: date) -> api.DayV1:
        trades, _ = self._read_db((day, today))
        of_day = self._of_day(trades, day)
        symbols = self._symbols_on(day, today)
        return api.DayV1(environment=self.paths.environment, day=day, trades=tuple(of_day),
                         result_gross_usd=sum((t.result_gross_usd for t in of_day), ZERO),
                         bar_symbols=symbols, result_net_usd=total_net(of_day))

    def bars(self, symbol: str, day: date, today: date) -> api.BarsV1:
        bars = self._bars_of(symbol, day, today)
        return api.BarsV1(environment=self.paths.environment, symbol=symbol, day=day,
                          bars=tuple(bars))

    def performance(self, from_day: date, to_day: date, places: int) -> api.PerformanceV1:
        trades, _ = self._read_db()
        return performance(self.paths.environment, from_day, to_day, trades, places)
