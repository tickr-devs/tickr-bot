"""Trade engine: entry -> protective stop -> exit -> closed, driven by broker reads.

Every send and cancel is written to the store BEFORE the broker call and its outcome after
it (commit-before-effect). An order whose send outcome is unknown is never resent: Detail,
open orders and positions resolve it. State advances only from broker reads; `step()` is
one pass and is safe to call repeatedly or after a crash.

Quantities: `expected` is what broker Detail says we bought and have not sold. Protection
and closing orders are sized on `expected` only, and a closing order is sent only when the
broker position agrees with it. A position larger than `expected` is fenced, never sold or
protected by guesswork.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

from tickr.broker.base import Broker, BrokerError, OrderNotFound, RateLimited, Refused
from tickr.config import Config, GateResult
from tickr.risk.ticks import tick
from tickr.risk.timers import WindowKind
from tickr.store import Store
from tickr.types import (
    OrderDetail,
    OrderRequest,
    OrderState,
    OrderType,
    Position,
    Side,
    TimeInForce,
    TradingSession,
)

ZERO = Decimal(0)
# Trading windows the engine can protect a trade in: only the regular session has a broker
# stop (F7). The pre-market and post-market join when the bot-watched stop and the LIMIT exits
# exist (design v2.1, PRs 3 and 4).
PROTECTED_WINDOWS = frozenset({WindowKind.REGULAR})


class Leg(StrEnum):
    ENTRY = "entry"
    STOP = "stop"
    EXIT = "exit"


class SendState(StrEnum):
    INTENT = "intent"  # written, not yet attempted
    ATTEMPTED = "attempted"  # about to call the broker
    ACKNOWLEDGED = "acknowledged"
    REFUSED = "refused"  # broker or local budget refused: the order was NOT created
    UNKNOWN = "unknown"  # may or may not exist: never resend, resolve by broker reads
    ABSENT = "absent"  # resolved: not in Detail and not in open orders


# The exit cause when a kept stop filled while a partial sale's stops were being released.
STOP_FILLED_DURING_REDUCE = "stop-filled-during-scale-out"


class ReduceState(StrEnum):
    """One partial sale of a trade (`request_reduce`)."""

    REQUESTED = "requested"  # stops for the shares are being released; nothing sold yet
    SENT = "sent"  # the MARKET SELL for the shares was written (and attempted)
    DONE = "done"  # over: filled, or ended unfilled, refused or not possible


class TradeState(StrEnum):
    OPENING = "opening"  # entry working, nothing filled yet
    OPEN = "open"  # some quantity filled; protection maintained
    EXITING = "exiting"  # teardown: cancel entry, then stops, then close what is held
    CLOSED = "closed"


TRADE_OPENED = "trade-opened"  # journal row written with a trade; api/history reads it


class Fence(StrEnum):
    """Reasons that block NEW entries. Never block exits, cancels or protection."""

    STARTUP_RECONCILE = "startup-reconcile"
    BROKER_READ = "broker-read-failed"
    DETAIL_BUDGET = "detail-budget"
    UNKNOWN_ORDER = "unknown-order"
    ORDER_MISSING = "order-missing"
    STATUS_UNKNOWN = "status-unknown"
    UNPROTECTED = "unprotected"
    OVER_PROTECTED = "over-protected"
    UNPROTECTED_TIMEOUT = "unprotected-timeout"
    STOP_REJECTED = "stop-rejected"
    EXIT_REFUSED = "exit-refused"
    CANCEL_UNKNOWN = "cancel-unknown"
    CANCEL_FAILED = "cancel-failed"
    CANCEL_NOT_SENT = "cancel-not-sent"  # cancels kept refused by our request budget
    RESURRECTED = "resurrected"
    FOREIGN_POSITION = "foreign-position"
    FOREIGN_ORDER = "foreign-order"
    QUANTITY_MISMATCH = "quantity-mismatch"
    SHUTTING_DOWN = "shutting-down"
    ENTRY_STOP = "entry-stop"  # operator command; only the entry-stop-off command clears it
    REPLACE_UNRESOLVED = "replace-unresolved"  # a stop replace whose result is not confirmed
    STOP_HELD = "stop-held"  # no stop and no close sent: an untracked SELL may rest at Webull
    EXIT_NOT_FILLED = "exit-not-filled"  # a closing order still not filled after max_exit_seconds
    # A close was due outside the regular session (engine.manual.close_refusal): no MARKET
    # order was sent, the stop stays; only the operator clears it, so it never sells at the
    # next open with nobody deciding.
    MARKET_CLOSED = "market-closed"
    # An order's Detail read has kept failing for `[orders] detail_unreadable_seconds`: its
    # state is unknown. While set, the order proves nothing: a stop is not verified protection
    # and an entry or close does not excuse a quantity mismatch. Clears on the next good read.
    DETAIL_UNREADABLE = "detail-unreadable"
    # The watchdog started this run early for a resting entry (runtime/watchdog.py): the run
    # protects, cancels and flattens, but takes no new entry until the operator clears it.
    WATCHDOG_STARTED = "watchdog-started"


# Fences that need the operator to look before entries resume; all others clear themselves
# when their condition is gone.
OPERATOR_FENCES = frozenset({Fence.STOP_REJECTED, Fence.EXIT_REFUSED, Fence.CANCEL_FAILED,
                             Fence.RESURRECTED, Fence.UNPROTECTED_TIMEOUT,
                             Fence.REPLACE_UNRESOLVED, Fence.EXIT_NOT_FILLED,
                             Fence.MARKET_CLOSED, Fence.WATCHDOG_STARTED})

_LEG_LETTER = {Leg.ENTRY: "e", Leg.STOP: "s", Leg.EXIT: "x"}
# Key of the broker-read fence a failed open-orders read sets; only a reconcile clears it.
OPEN_ORDERS_READ = "open-orders"
# A Detail read that changed an order's state or filled quantity.
ORDER_OBSERVED = "order-observed"
# A Detail read showing a stop still working with nothing filled, journaled at most once per
# `[orders] stop_held_proof_seconds` when no change was seen: a display-only record of how long
# the shares were provably held (api/history.py, MAE/MFE); nothing in the engine reads it.
ORDER_HELD = "order-held"
# The first failed Detail read of an order after an answered one ({"at": engine clock, "error"}),
# and the first answered read after such failures: how long Detail has been unreadable
# (`detail-unreadable` fence) survives a restart.
DETAIL_FAILED = "detail-read-failed"
DETAIL_ANSWERED = "detail-read-answered"
# A filled order's exact average fill price, recorded once from the account's cash activity of
# the fill (runtime/account.py, `record_exact_fills`); the body is the evidence it came from.
EXACT_FILL_PRICE = "exact-fill-price"
# A finished order's fees as the broker charged them, recorded once from the order history
# (runtime/account.py, `record_exact_fills`); the body is the evidence it came from.
ORDER_FEES = "order-fees"
# A finished order's settled cash amount (fees included) from the account's cash activity,
# recorded once; the body carries the order rows' provisional amount and the difference.
ORDER_CASH = "order-cash"
# Recorded fees that the cash activity proved wrong, replaced by the proven ones (before,
# after); runtime/watch.py alerts on each such row.
ORDER_FEES_CORRECTED = "order-fees-corrected"
_LIVE_SEND = (SendState.INTENT, SendState.ATTEMPTED, SendState.ACKNOWLEDGED, SendState.UNKNOWN)
_WORKING = (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED)
# Cancel states after which a later Detail read decides whether the cancel took.
_CANCEL_AWAITING = ("sent", "unknown", "refused")
# A cancel may be in effect: decided, or sent and not yet shown by Detail not to have taken.
_CANCEL_IN_FLIGHT = ("requested", *_CANCEL_AWAITING)


class EntryRefused(Exception):
    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True)
class OrderRow:
    client_order_id: str
    trade_id: str
    leg: Leg
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    send_state: SendState
    broker_state: OrderState | None
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    cancel_state: str | None
    cancel_attempts: int
    cancel_at: float | None
    absent_reads: int
    last_detail_at: float | None
    # The exact average fill price from the fill's cash activity (None: not known yet or never).
    exact_fill_price: Decimal | None = None
    # The broker's fees on the order (None: not read yet); the daily loss counts them.
    fees_usd: Decimal | None = None
    # The settled cash amount from the fill's cash activity, signed, fees included (None:
    # not proven yet).
    net_cash_usd: Decimal | None = None

    @property
    def fill_price(self) -> Decimal | None:
        """The average price the order filled at, as results count it: the exact price when
        recorded, else the broker's order row (rounded to cents for sub-dollar prices)."""
        if self.exact_fill_price is not None:
            return self.exact_fill_price
        return self.average_fill_price

    @property
    def live(self) -> bool:
        """Could still create exposure or fill."""
        if self.send_state not in _LIVE_SEND:
            return False
        return self.broker_state is None or not self.broker_state.terminal

    @property
    def verified(self) -> bool:
        return self.send_state is SendState.ACKNOWLEDGED and self.broker_state in _WORKING

    @property
    def open_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def cancelling(self) -> bool:
        return self.cancel_state in _CANCEL_IN_FLIGHT


@dataclass(frozen=True)
class TradeRow:
    trade_id: str
    symbol: str
    state: TradeState
    entry_quantity: Decimal
    entry_limit_price: Decimal
    stop_price: Decimal
    stop_percent_below_fill: Decimal | None
    entry_low_price: Decimal | None
    entry_rest_until: datetime | None
    planned_loss_usd: Decimal
    exit_requested: str | None
    stop_failures: int
    last_stop_failure_at: float | None
    scale_out_quantity: Decimal | None = None
    reduce_quantity: Decimal | None = None
    reduce_cause: str | None = None
    reduce_state: ReduceState | None = None
    trading_window: WindowKind = WindowKind.REGULAR
    reserved_usd: Decimal | None = None

    @property
    def reservation(self) -> Decimal:
        """What the trade holds against the daily loss ceiling: its planned loss, or for an
        extended-hours trade its whole position value (owner decision B2, 2026-11-17)."""
        return self.planned_loss_usd if self.reserved_usd is None else self.reserved_usd

    @property
    def reducing(self) -> bool:
        return self.reduce_state in (ReduceState.REQUESTED, ReduceState.SENT)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(value)


def order_from_row(row: Any) -> OrderRow:
    return OrderRow(
        client_order_id=row["client_order_id"], trade_id=row["trade_id"], leg=Leg(row["leg"]),
        symbol=row["symbol"], side=Side(row["side"]), order_type=OrderType(row["order_type"]),
        quantity=Decimal(row["quantity"]), limit_price=_dec(row["limit_price"]),
        stop_price=_dec(row["stop_price"]), send_state=SendState(row["send_state"]),
        broker_state=None if row["broker_state"] is None else OrderState(row["broker_state"]),
        filled_quantity=Decimal(row["filled_quantity"]),
        average_fill_price=_dec(row["average_fill_price"]), cancel_state=row["cancel_state"],
        cancel_attempts=row["cancel_attempts"], cancel_at=row["cancel_at"],
        absent_reads=row["absent_reads"], last_detail_at=row["last_detail_at"],
        exact_fill_price=_dec(row["exact_fill_price"]),
        fees_usd=_dec(row["fees_usd"]),
        net_cash_usd=_dec(row["net_cash_usd"]),
    )


def _trade(row: Any) -> TradeRow:
    return TradeRow(
        trade_id=row["trade_id"], symbol=row["symbol"], state=TradeState(row["state"]),
        entry_quantity=Decimal(row["entry_quantity"]),
        entry_limit_price=Decimal(row["entry_limit_price"]),
        stop_price=Decimal(row["stop_price"]),
        stop_percent_below_fill=_dec(row["stop_percent_below_fill"]),
        entry_low_price=_dec(row["entry_low_price"]),
        entry_rest_until=(None if row["entry_rest_until"] is None
                          else datetime.fromisoformat(row["entry_rest_until"])),
        planned_loss_usd=Decimal(row["planned_loss_usd"]),
        exit_requested=row["exit_requested"], stop_failures=row["stop_failures"],
        last_stop_failure_at=row["last_stop_failure_at"],
        scale_out_quantity=_dec(row["scale_out_quantity"]),
        reduce_quantity=_dec(row["reduce_quantity"]), reduce_cause=row["reduce_cause"],
        reduce_state=None if row["reduce_state"] is None else ReduceState(row["reduce_state"]),
        trading_window=WindowKind(row["trading_window"]),
        reserved_usd=_dec(row["reserved_usd"]),
    )


def _detail_priority(row: OrderRow, poll_seconds: float) -> float:
    """Oldest read attempt first, so no order can starve the others; the class only gives a
    bounded head start of up to three poll intervals: unknown sends, then unverified stops,
    then entries and exits, then verified stops and rechecks of orders given up on. (A stop
    whose cancel waits for Detail goes before all of these: `Engine._observe`.)"""
    if row.send_state is SendState.ABSENT:
        boost = 0  # a recheck is a fallback: an unexplained position is usually stale Detail
    elif row.send_state is SendState.UNKNOWN:
        boost = 3
    elif row.leg is Leg.STOP and not row.verified:
        boost = 2
    elif row.leg is not Leg.STOP:
        boost = 1
    else:
        boost = 0
    return (row.last_detail_at or 0.0) - boost * poll_seconds


# The most stops one fill observation adds: its own, and a split at the trade's scale-out
# boundary (`_stop_pieces`). `[orders] max_stops_per_trade` keeps room for one more.
STOPS_PER_FILL = 2


def _stop_pieces(trade: TradeRow, covered: Decimal, quantity: Decimal) -> list[Decimal]:
    """The stops to place for `quantity` more shares above `covered`: one, or two when the
    trade's planned scale-out boundary falls inside, so that the stops placed first cover
    exactly the scale-out shares (`_stops_to_release` then finds them)."""
    split = trade.scale_out_quantity
    if split is None or trade.reduce_state is not None or not covered < split < covered + quantity:
        return [quantity]
    return [split - covered, covered + quantity - split]


def _stops_to_release(live: list[OrderRow], amount: Decimal) -> list[OrderRow] | None:
    """Stops (placement order) to cancel so that exactly `amount` fewer shares stay covered:
    stops already being cancelled count first; then the earliest stops if their quantities
    add up to exactly the rest, else the latest ones. None when no such set exists: a larger
    release would leave shares unstopped until a later pass (hard rule 2), so none is made."""
    cancelling = [s for s in live if s.cancelling]
    need = amount - sum((s.open_quantity for s in cancelling), ZERO)
    if need == 0:
        return cancelling
    if need < 0:
        return None
    keep = [s for s in live if not s.cancelling]
    for ordered in (keep, keep[::-1]):
        total = ZERO
        for index, stop in enumerate(ordered):
            total += stop.open_quantity
            if total == need:
                return cancelling + ordered[:index + 1]
            if total > need:
                break
    return None


@dataclass(frozen=True)
class BrokerView:
    """What one successful reconcile pass read: open orders, then positions. For reports (day
    end) and for the symbols of foreign orders (`Engine.foreign_symbols`); never used to size
    or send anything."""

    positions: tuple[Position, ...]
    open_orders: tuple[OrderDetail, ...]


class Engine:
    def __init__(self, store: Store, broker: Broker, config: Config, run_id: str,
                 gate: Callable[[], GateResult],
                 clock: Callable[[], float] = time.time,
                 close_refusal: Callable[[], str | None] = lambda: None) -> None:
        self.store = store
        self.broker = broker
        self.config = config
        self.run_id = run_id
        self.gate = gate
        self.clock = clock
        # Why a closing MARKET order may not be sent now, or None (engine.manual.close_refusal
        # with the session calendar; the default never refuses: tests and tools).
        self.close_refusal = close_refusal
        self._positions: list[Position] | None = None  # this pass's positions read
        # The latest reconcile's reads; None after a failed read (or when a caller drops it).
        self.broker_view: BrokerView | None = None
        # Per trade held after an unresolved replace (`_replace_blocks`): when the last
        # open-orders read started (engine clock), the trade's live stops then, and how many
        # reads in a row since then showed no untracked SELL. Memory only: a restart holds anew.
        self._held_reads: dict[str, tuple[float, tuple[str, ...], int]] = {}
        # First not-sent cancel of each order since the last one that left (process memory:
        # after a restart the wait starts again, which only delays the alert).
        self._not_sent_since: dict[str, float] = {}
        # First failed Detail read of each order since its last answered one (a cache of the
        # journaled `detail-read-failed` row, see `_failing_since`).
        self._detail_failing_since: dict[str, float] = {}
        # Orders whose latest Detail read in this process was answered. The exit cancels a stop
        # only when it is in here, so after a restart a stop is read once before its cancel.
        self._detail_answered_ids: set[str] = set()
        # Per symbol with an open trade: (held, Detail quantity) as the latest positions read
        # and our records disagreed, and when that disagreement was first seen (engine clock).
        # The symbol's live orders not read since then go to the head of the Detail queue
        # (`_observe`), once each per new disagreement. Memory only.
        self._moved: dict[str, tuple[Decimal, Decimal, float]] = {}
        # Whether this step's open-orders read for new stops answered (`_verify_listed`);
        # while it does not, new stops go to Detail first.
        self._list_answered = False

    # --- fences ----------------------------------------------------------------------------

    def foreign_symbols(self) -> frozenset[str] | None:
        """Symbols of the positions and open orders the bot does not own: positions from their
        fences, orders from every open order of the latest complete reconcile that is not in
        our records (a manual order may carry no client order id, so several share one fence).
        None while a foreign order is fenced but no complete reconcile read is at hand: the
        caller must assume any symbol."""
        names = [name.partition(":") for name in self.fences()]
        symbols = {key for fence, _, key in names if fence == Fence.FOREIGN_POSITION}
        if any(fence == Fence.FOREIGN_ORDER for fence, _, _ in names):
            if self.broker_view is None:
                return None
            ours = {o.client_order_id for o in self.orders()}
            foreign = [o for o in self.broker_view.open_orders if o.client_order_id not in ours]
            if not foreign or any(not o.symbol for o in foreign):
                return None
            symbols |= {o.symbol for o in foreign}
        return frozenset(symbols)

    def entry_fences(self, symbol: str | None) -> dict[str, str]:
        """The fences that refuse a new entry in `symbol` (hard rule 1, narrowed by the owner
        2026-11-16: manual trades are ignored). Every fence blocks, except a foreign position
        or order in another known symbol; while a foreign order's symbol is unknown, foreign
        fences block every symbol. `symbol` None asks what blocks an entry in any symbol
        without foreign exposure (the status line). One rule for admission, `open_trade`, the
        working-entry guard and the status."""
        fences = self.fences()
        foreign = self.foreign_symbols()
        if foreign is None or (symbol is not None and symbol in foreign):
            return fences
        return {n: c for n, c in fences.items()
                if n.partition(":")[0] not in (Fence.FOREIGN_POSITION, Fence.FOREIGN_ORDER)}

    def fences(self) -> dict[str, str]:
        return {r["name"]: r["cause"] for r in self.store.query("SELECT name, cause FROM fences")}

    def fence_ages(self) -> dict[str, float]:
        """Seconds each fence has been set, by the engine's clock."""
        now = self.clock()
        return {r["name"]: now - r["set_at_epoch"]
                for r in self.store.query("SELECT name, set_at_epoch FROM fences")}

    def _fence_age(self, name: str) -> float | None:
        rows = self.store.query("SELECT set_at_epoch FROM fences WHERE name = ?", (name,))
        return None if not rows else self.clock() - rows[0][0]

    def _fence_row(self, fence: Fence, key: str, cause: str) -> None:
        """Set a fence inside an already open transaction."""
        name = f"{fence}:{key}" if key else str(fence)
        now = self.clock()
        if self.store.execute(
                "INSERT OR IGNORE INTO fences(name, cause, set_at, set_at_epoch) "
                "VALUES (?, ?, ?, ?)",
                (name, cause, datetime.fromtimestamp(now, UTC).isoformat(), now)):
            self.store.append("fence-set", {"cause": cause}, key=name, run_id=self.run_id)

    def _set_fence(self, fence: Fence, key: str, cause: str) -> None:
        with self.store.transaction():
            self._fence_row(fence, key, cause)

    def _clear_fence(self, fence: Fence, key: str = "") -> None:
        name = f"{fence}:{key}" if key else str(fence)
        with self.store.transaction():
            if self.store.execute("DELETE FROM fences WHERE name = ?", (name,)):
                self.store.append("fence-cleared", {}, key=name, run_id=self.run_id)

    def _has_fence(self, fence: Fence, key: str = "") -> bool:
        name = f"{fence}:{key}" if key else str(fence)
        return bool(self.store.query("SELECT 1 FROM fences WHERE name = ?", (name,)))

    def clear_operator_fence(self, name: str) -> bool:
        """Operator command: clear a fence that does not clear itself."""
        if name.split(":", 1)[0] not in OPERATOR_FENCES:
            return False
        with self.store.transaction():
            removed = self.store.execute("DELETE FROM fences WHERE name = ?", (name,))
            if removed:
                self.store.append("fence-cleared", {"by": "operator"}, key=name,
                                  run_id=self.run_id)
        return bool(removed)

    def watchdog_started_fence(self, cause: str) -> None:
        """Block new entries after an early watchdog start; only the operator clears it."""
        self._set_fence(Fence.WATCHDOG_STARTED, "", cause)

    def entry_stop_row(self, on: bool, cause: str) -> None:
        """Operator entry-stop, inside the caller's transaction (the command is journaled in
        the same one). Survives restarts; touches no other fence."""
        if on:
            self._fence_row(Fence.ENTRY_STOP, "", cause)
        elif self.store.execute("DELETE FROM fences WHERE name = ?", (str(Fence.ENTRY_STOP),)):
            self.store.append("fence-cleared", {"by": "operator"}, key=str(Fence.ENTRY_STOP),
                              run_id=self.run_id)

    def cancel_entries(self) -> int:
        """Operator cancel-own-orders: cancel working entry orders only, never stops or exits.
        Returns how many entries were asked to cancel."""
        asked = 0
        for trade in self.trades():
            for order in self.orders(trade.trade_id):
                if order.leg is Leg.ENTRY and order.live:
                    self._cancel(order)
                    asked += 1
        return asked

    def cancel_entry(self, trade_id: str, cause: str) -> bool:
        """Cancel this trade's working entry (never its stops). The cause is journaled once
        per entry order; later calls only repeat the cancel decision, which `_cancel` allows
        again only after Detail showed the order still working. A fill before the cancel
        lands becomes a protected position as usual. True if an entry was working."""
        live = [o for o in self.orders(trade_id) if o.leg is Leg.ENTRY and o.live]
        for order in live:
            if not self.store.journal("entry-cancel", order.client_order_id):
                with self.store.transaction():
                    self.store.append("entry-cancel", {"cause": cause},
                                      key=order.client_order_id, run_id=self.run_id)
            self._cancel(order)
        return bool(live)

    def fence_rows(self) -> list[tuple[str, str, str]]:
        """(name, cause, set_at UTC ISO) of every fence, oldest first."""
        return [(r["name"], r["cause"], r["set_at"]) for r in
                self.store.query("SELECT name, cause, set_at FROM fences ORDER BY set_at_epoch")]

    # --- reads -----------------------------------------------------------------------------

    def trades(self, *, open_only: bool = True) -> list[TradeRow]:
        sql = "SELECT * FROM trades"
        if open_only:
            sql += f" WHERE state != '{TradeState.CLOSED}'"
        return [_trade(r) for r in self.store.query(sql + " ORDER BY opened_at, trade_id")]

    def trade(self, trade_id: str) -> TradeRow:
        return _trade(self.store.query("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))[0])

    def orders(self, trade_id: str | None = None) -> list[OrderRow]:
        if trade_id is None:
            rows = self.store.query("SELECT * FROM orders ORDER BY rowid")
        else:
            rows = self.store.query("SELECT * FROM orders WHERE trade_id = ? ORDER BY rowid",
                                    (trade_id,))
        return [order_from_row(r) for r in rows]

    def order(self, client_order_id: str) -> OrderRow:
        return order_from_row(self.store.query("SELECT * FROM orders WHERE client_order_id = ?",
                                       (client_order_id,))[0])

    @staticmethod
    def expected_quantity(orders: list[OrderRow]) -> Decimal:
        """What Detail says we bought and have not sold."""
        bought = sum((o.filled_quantity for o in orders if o.leg is Leg.ENTRY), ZERO)
        sold = sum((o.filled_quantity for o in orders if o.leg is not Leg.ENTRY), ZERO)
        return bought - sold

    # --- entry and exit requests -------------------------------------------------------------

    def open_trade(self, symbol: str, quantity: Decimal, limit_price: Decimal,
                   stop_price: Decimal, planned_loss_usd: Decimal,
                   stop_percent_below_fill: Decimal | None = None,
                   entry_low_price: Decimal | None = None,
                   strategy: tuple[str, str] | None = None,
                   rest_until: datetime | None = None,
                   scale_out_quantity: Decimal | None = None,
                   window: WindowKind = WindowKind.REGULAR,
                   reserved_usd: Decimal | None = None) -> str:
        """Record and send a LIMIT BUY entry. Refuses (EntryRefused) instead of guessing.

        With `stop_percent_below_fill`, the stop is re-derived from the first observed average
        fill price (rounded down to the cent) before the first stop is placed; `stop_price`
        is the bound computed from the limit and is never loosened. `entry_low_price` is the
        bottom of the strategy's price window, kept so a resting entry can be cancelled once
        the quote leaves the window (`cancel_entry`). `strategy` (name, version) of the decision
        that opened it is journaled for display; None for a manual trade. `rest_until` (aware)
        makes it a resting entry that may wait below the ask until then
        (`runtime.entries.entry_cancel_cause`); it needs `entry_low_price`.
        `scale_out_quantity` (whole, below `quantity`) is the part the strategy plans to sell
        first: stops are placed so that some of them cover exactly that part, and a later
        `request_reduce` of it leaves the rest protected throughout. `window` is the trading
        window admission found; a trade is opened only in a window the engine can protect
        (`PROTECTED_WINDOWS`). `reserved_usd`: what it reserves against the daily ceiling when
        that is not its planned loss."""
        if window not in PROTECTED_WINDOWS:
            # Second barrier behind config.load (Fable on #574): admission may admit an
            # extended entry, but nothing here protects one yet (no broker stop exists there).
            raise EntryRefused(f"no protection path for a {window} trade yet")
        gate = self.gate()
        if not gate.open:
            raise EntryRefused(f"trading gate closed: {gate.cause}")
        fences = self.entry_fences(symbol)
        if fences:
            raise EntryRefused("entries fenced: " + ", ".join(sorted(fences)))
        trades = self.trades()
        if len(trades) >= self.config.risk.max_open_positions:
            raise EntryRefused("open position limit reached")
        if any(t.symbol == symbol for t in trades):
            # One trade per symbol: trades in one symbol would share the broker position that
            # each trade's stop, exit and reconciliation are sized against.
            raise EntryRefused(f"a trade in {symbol} is already open")
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise EntryRefused("quantity must be a positive whole number")
        if not ZERO < stop_price < limit_price:
            raise EntryRefused("stop must be above zero and below the entry limit")
        if entry_low_price is not None and not stop_price < entry_low_price <= limit_price:
            raise EntryRefused("window bottom must be above the stop and at most the limit")
        if rest_until is not None and (rest_until.tzinfo is None or entry_low_price is None):
            raise EntryRefused("a resting entry needs an aware end time and a window bottom")
        if scale_out_quantity is not None and not (
                ZERO < scale_out_quantity < quantity
                and scale_out_quantity == scale_out_quantity.to_integral_value()):
            raise EntryRefused("scale-out must be a whole number of shares below the quantity")

        with self.store.transaction():
            trade_id = f"t{self.store.next_counter('trade')}"
            self.store.execute(
                "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
                "entry_limit_price, stop_price, stop_percent_below_fill, planned_loss_usd, "
                "opened_at, entry_low_price, entry_rest_until, scale_out_quantity, "
                "trading_window, reserved_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_id, self.run_id, symbol, TradeState.OPENING, str(quantity),
                 str(limit_price), str(stop_price),
                 None if stop_percent_below_fill is None else str(stop_percent_below_fill),
                 str(planned_loss_usd), self.store.stamp(),
                 None if entry_low_price is None else str(entry_low_price),
                 None if rest_until is None else rest_until.astimezone(UTC).isoformat(),
                 None if scale_out_quantity is None else str(scale_out_quantity),
                 window, None if reserved_usd is None else str(reserved_usd)))
            self.store.append(TRADE_OPENED, {
                "symbol": symbol, "quantity": quantity, "limit_price": limit_price,
                "stop_price": stop_price, "planned_loss_usd": planned_loss_usd,
                "entry_low_price": entry_low_price,
                **({} if rest_until is None else {"rest_until": rest_until}),
                **({} if scale_out_quantity is None
                   else {"scale_out_quantity": scale_out_quantity}),
                **({} if strategy is None else {"strategy": strategy[0],
                                                "strategy_version": strategy[1]}),
                "code_version": self.store.run_code_version(self.run_id)},
                key=trade_id, run_id=self.run_id)
            cid = self._write_intent(trade_id, Leg.ENTRY, symbol, Side.BUY, OrderType.LIMIT,
                                     quantity, limit_price=limit_price)
        self._send(cid)
        return trade_id

    def request_exit(self, trade_id: str, cause: str) -> bool:
        """Ask for an orderly exit. Refused while a closing order of this trade was refused
        (the operator must look first), so a caller can never turn it into a send loop."""
        with self.store.transaction():
            return self.exit_request_row(trade_id, cause)

    def exit_request_row(self, trade_id: str, cause: str) -> bool:
        """`request_exit` inside the caller's transaction (so an operator command and its
        effect are one durable write)."""
        if self.store.query("SELECT 1 FROM fences WHERE name = ?",
                            (f"{Fence.EXIT_REFUSED}:{trade_id}",)):
            return False
        changed = self.store.execute(
            "UPDATE trades SET exit_requested = ? WHERE trade_id = ? AND state != ? "
            "AND exit_requested IS NULL", (cause, trade_id, TradeState.CLOSED))
        if changed:
            self.store.append("exit-requested", {"cause": cause}, key=trade_id,
                              run_id=self.run_id)
        return bool(changed)

    def request_reduce(self, trade_id: str, quantity: Decimal, cause: str) -> bool:
        """Ask to sell `quantity` shares of an open trade at market and keep the rest under
        its stop (a scale-out). One per trade. Refused (False, nothing recorded; the cause is
        `reduce_refusal`) unless the sale is the trade's planned scale-out and its live stops
        cover what Detail says is held, some of them exactly the shares to sell
        (`_stops_to_release`): the other shares then stay stopped throughout. The sale goes
        out once those stops have ended (`_reduce_step`)."""
        with self.store.transaction():
            if self.reduce_refusal(trade_id, quantity) is not None:
                return False
            expected = self.expected_quantity(self.orders(trade_id))
            self.store.execute(
                "UPDATE trades SET reduce_quantity = ?, reduce_cause = ?, reduce_state = ? "
                "WHERE trade_id = ?", (str(quantity), cause, ReduceState.REQUESTED, trade_id))
            self.store.append("reduce-requested", {"quantity": quantity, "cause": cause,
                                                   "held": expected},
                              key=trade_id, run_id=self.run_id)
        return True

    def reduce_refusal(self, trade_id: str, quantity: Decimal) -> str | None:
        """Why `request_reduce(trade_id, quantity)` is refused now, or None if it is taken."""
        trade = self.trade(trade_id)
        orders = self.orders(trade_id)
        expected = self.expected_quantity(orders)
        live = [o for o in orders if o.leg is Leg.STOP and o.live]
        if trade.state is not TradeState.OPEN or trade.exit_requested is not None:
            return f"trade is {trade.state}" + (", exit asked" if trade.exit_requested else "")
        if trade.reduce_state is not None:
            return "a partial sale was already asked for this trade"
        if self._has_fence(Fence.EXIT_REFUSED, trade_id):
            return "an earlier close was refused"
        if any(o.live for o in orders if o.leg is Leg.ENTRY):
            return "the entry is still working"
        if trade.scale_out_quantity is None or quantity != trade.scale_out_quantity:
            return f"{quantity} is not the planned scale-out ({trade.scale_out_quantity})"
        if not ZERO < quantity < expected:
            return f"holds {expected}, selling {quantity} would leave nothing"
        covered = sum((s.open_quantity for s in live), ZERO)
        if any(s.cancelling for s in live) or covered != expected:
            return f"live stops cover {covered} of {expected}"
        if _stops_to_release(live, quantity) is None:
            return (f"no live stops cover exactly {quantity}: "
                    f"{' + '.join(str(s.open_quantity) for s in live)}")
        return None

    def _set_reduce(self, trade: TradeRow, state: ReduceState, body: dict[str, Any]) -> None:
        """Inside or outside a transaction: move the trade's partial sale to `state`."""
        self.store.execute("UPDATE trades SET reduce_state = ? WHERE trade_id = ?",
                           (state, trade.trade_id))
        self.store.append(f"reduce-{state}", body, key=trade.trade_id, run_id=self.run_id)

    def _reduce_sales(self, trade: TradeRow, exits: list[OrderRow]) -> list[OrderRow]:
        cids = {r.body["client_order_id"]
                for r in self.store.journal(f"reduce-{ReduceState.SENT}", trade.trade_id)}
        return [e for e in exits if e.client_order_id in cids]

    def _reduce_pending(self, trade: TradeRow, exits: list[OrderRow]) -> Decimal:
        """Shares of an ongoing partial sale not sold yet: they are released from their stop
        on purpose, so protection is not owed for them."""
        if not trade.reducing or trade.reduce_quantity is None:
            return ZERO
        sold = sum((s.filled_quantity for s in self._reduce_sales(trade, exits)), ZERO)
        return max(trade.reduce_quantity - sold, ZERO)

    # --- sending ---------------------------------------------------------------------------

    def _client_order_id(self, trade_id: str, leg: Leg) -> str:
        n = self.store.next_counter(f"orders:{trade_id}")
        cid = (f"{self.config.orders.client_order_id_prefix}{self.run_id[:8]}-"
               f"{trade_id}-{_LEG_LETTER[leg]}{n}")
        if len(cid) > self.config.orders.client_order_id_max_length:
            raise ValueError(f"client_order_id too long: {cid}")
        return cid

    def _write_intent(self, trade_id: str, leg: Leg, symbol: str, side: Side,
                      order_type: OrderType, quantity: Decimal, *,
                      limit_price: Decimal | None = None,
                      stop_price: Decimal | None = None) -> str:
        cid = self._client_order_id(trade_id, leg)
        self.store.execute(
            "INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, order_type, "
            "quantity, limit_price, stop_price, send_state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, trade_id, leg, symbol, side, order_type, str(quantity),
             None if limit_price is None else str(limit_price),
             None if stop_price is None else str(stop_price), SendState.INTENT, self.store.stamp()))
        self.store.append("order-intent", {
            "trade_id": trade_id, "leg": leg, "symbol": symbol, "side": side,
            "order_type": order_type, "quantity": quantity, "limit_price": limit_price,
            "stop_price": stop_price}, key=cid, run_id=self.run_id)
        return cid

    def _set_send_state(self, cid: str, state: SendState, body: dict[str, Any],
                        *, refusal: str | None = None, broker_order_id: str | None = None) -> None:
        self.store.execute(
            "UPDATE orders SET send_state = ?, refusal = COALESCE(?, refusal), "
            "broker_order_id = COALESCE(?, broker_order_id), updated_at = ? "
            "WHERE client_order_id = ?",
            (state, refusal, broker_order_id, self.store.stamp(), cid))
        self.store.append(f"order-{state}", body, key=cid, run_id=self.run_id)

    def _send(self, cid: str) -> None:
        row = self.order(cid)
        request = OrderRequest(
            client_order_id=cid, symbol=row.symbol, side=row.side, order_type=row.order_type,
            quantity=row.quantity, time_in_force=TimeInForce.DAY,
            session=TradingSession.CORE, limit_price=row.limit_price,
            stop_price=row.stop_price)
        with self.store.transaction():
            self._set_send_state(cid, SendState.ATTEMPTED, {})
        try:
            ack = self.broker.place(request)
        except RateLimited:
            # Our request budget refused it: nothing was sent (a 429 on a trading request is
            # OutcomeUnknown in the adapter). Not a broker rejection, so it
            # does not count against stop retries; the next step decides again.
            with self.store.transaction():
                self._set_send_state(cid, SendState.REFUSED, {"code": "rate-limited"},
                                     refusal="rate-limited")
            return
        except Refused as exc:
            with self.store.transaction():
                self._set_send_state(cid, SendState.REFUSED, {"code": exc.code},
                                     refusal=exc.code)
                self._count_rejection(row)
            return
        except Exception as exc:  # OutcomeUnknown and anything unexpected: may exist
            with self.store.transaction():
                self._set_send_state(cid, SendState.UNKNOWN, {"error": type(exc).__name__})
                self._fence_row(Fence.UNKNOWN_ORDER, cid, "send outcome unknown")
            return
        with self.store.transaction():
            self._set_send_state(cid, SendState.ACKNOWLEDGED, {},
                                 broker_order_id=ack.broker_order_id)

    def _count_rejection(self, row: OrderRow, *, spaced: bool = True) -> None:
        """Inside a transaction: the broker refused or rejected a stop or a closing order.
        `spaced=False` counts a stop failure without restarting the retry spacing."""
        if row.leg is Leg.STOP:
            self.store.execute(
                "UPDATE trades SET stop_failures = stop_failures + 1, "
                "last_stop_failure_at = COALESCE(?, last_stop_failure_at) WHERE trade_id = ?",
                (self.clock() if spaced else None, row.trade_id))
        elif row.leg is Leg.EXIT:
            if self.trade(row.trade_id).reduce_state is ReduceState.SENT:
                self.store.execute("UPDATE trades SET reduce_state = ? WHERE trade_id = ?",
                                   (ReduceState.DONE, row.trade_id))
                self.store.append(f"reduce-{ReduceState.DONE}",
                                  {"refused": row.client_order_id}, key=row.trade_id,
                                  run_id=self.run_id)
            self.store.execute(
                "UPDATE trades SET state = ?, exit_requested = NULL WHERE trade_id = ?",
                (TradeState.OPEN, row.trade_id))
            self.store.append("exit-refused", {"client_order_id": row.client_order_id},
                              key=row.trade_id, run_id=self.run_id)
            trade = self.trade(row.trade_id)
            if trade.stop_failures > self.config.orders.stop_retry_attempts:
                text = ("closing order refused and the stop was refused after every retry: "
                        "NO broker stop; close the position in the Webull app")
            else:
                text = "closing order refused; the stop is placed again, operator must look"
            self._fence_row(Fence.EXIT_REFUSED, row.trade_id, text)

    def _cancel(self, row: OrderRow) -> None:
        """One cancel decision. A new decision is allowed only after a later Detail read showed
        the order still working (the previous cancel is then known not to have taken)."""
        if row.cancel_state in (*_CANCEL_AWAITING, "requested"):
            return
        cid = row.client_order_id
        if row.cancel_attempts > self.config.orders.cancel_retry_attempts:
            self._set_fence(Fence.CANCEL_FAILED, cid, "order still working after every cancel")
            return
        with self.store.transaction():
            self.store.execute(
                "UPDATE orders SET cancel_state = 'requested', cancel_attempts = "
                "cancel_attempts + 1, cancel_at = ?, updated_at = ? WHERE client_order_id = ?",
                (self.clock(), self.store.stamp(), cid))
            self.store.append("cancel-intent", {"attempt": row.cancel_attempts + 1}, key=cid,
                              run_id=self.run_id)
        try:
            self.broker.cancel(cid)
            outcome = "sent"
        except RateLimited:
            outcome = "not-sent"
        except (Refused, OrderNotFound):
            outcome = "refused"
        except Exception:  # noqa: BLE001 - any transport failure: outcome unknown
            outcome = "unknown"
        with self.store.transaction():
            self.store.execute("UPDATE orders SET cancel_state = ?, updated_at = ? "
                               "WHERE client_order_id = ?", (outcome, self.store.stamp(), cid))
            if outcome == "not-sent":
                # Refused by our request budget before reaching the broker: no cancel was
                # sent, so it does not use up one of the attempts that end in CANCEL_FAILED.
                self.store.execute("UPDATE orders SET cancel_attempts = cancel_attempts - 1 "
                                   "WHERE client_order_id = ?", (cid,))
            self.store.append(f"cancel-{outcome}", {}, key=cid, run_id=self.run_id)
            if outcome in ("unknown", "refused"):
                self._fence_row(Fence.CANCEL_UNKNOWN, cid, f"cancel {outcome}; waiting for Detail")
        self._note_not_sent(cid, outcome == "not-sent")

    def _note_not_sent(self, cid: str, not_sent: bool) -> None:
        """A cancel retried every pass but never sent must not stay silent: past
        `[orders] cancel_not_sent_alert_seconds` the operator is told; cleared once a cancel
        leaves or the order is finished."""
        if not not_sent:
            self._not_sent_since.pop(cid, None)
            self._clear_fence(Fence.CANCEL_NOT_SENT, cid)
            return
        since = self._not_sent_since.setdefault(cid, self.clock())
        waited = self.clock() - since
        if waited >= self.config.orders.cancel_not_sent_alert_seconds:
            self._set_fence(Fence.CANCEL_NOT_SENT, cid,
                            f"no cancel could be sent for {waited:.0f} s (request budget)")

    # --- observation -----------------------------------------------------------------------

    def record_exact_fill_price(self, client_order_id: str, price: Decimal,
                                evidence: dict[str, Any]) -> bool:
        """Record a finished order's exact average fill price with its evidence (one journal
        row), once: a price already recorded is never overwritten. Returns whether it was
        written. Raises ValueError for a price that is not positive."""
        if price <= 0:
            raise ValueError(f"exact fill price {price} is not positive")
        with self.store.transaction():
            written = self.store.execute(
                "UPDATE orders SET exact_fill_price = ? WHERE client_order_id = ? "
                "AND exact_fill_price IS NULL", (str(price), client_order_id))
            if written != 1:
                return False
            self.store.append(EXACT_FILL_PRICE, {"exact_fill_price": price, **evidence},
                              key=client_order_id, run_id=self.run_id)
        return True

    def record_order_cash(self, client_order_id: str, amount: Decimal, fees: Decimal,
                          evidence: dict[str, Any]) -> bool:
        """Record a finished order's settled cash amount, once, and make its recorded fees the
        ones the cash activity proved (`fees`): unrecorded fees are recorded, different ones
        are replaced with an `order-fees-corrected` row (the only change ever made to recorded
        fees). One transaction. Returns whether the amount was written. Raises ValueError for
        negative fees."""
        if fees < 0:
            raise ValueError(f"order fees {fees} are negative")
        with self.store.transaction():
            rows = self.store.query("SELECT fees_usd FROM orders WHERE client_order_id = ? "
                                    "AND net_cash_usd IS NULL", (client_order_id,))
            if len(rows) != 1:
                return False
            self.store.execute("UPDATE orders SET net_cash_usd = ? WHERE client_order_id = ?",
                               (str(amount), client_order_id))
            self.store.append(ORDER_CASH, {"net_cash_usd": amount, "fees_usd": fees,
                                           **evidence}, key=client_order_id, run_id=self.run_id)
            before = _dec(rows[0]["fees_usd"])
            if before != fees:
                self.store.execute("UPDATE orders SET fees_usd = ? WHERE client_order_id = ?",
                                   (str(fees), client_order_id))
                if before is None:
                    self.store.append(ORDER_FEES, {"fees_usd": fees,
                                                   "source": "cash-activities"},
                                      key=client_order_id, run_id=self.run_id)
                else:
                    self.store.append(ORDER_FEES_CORRECTED, {
                        "before": before, "after": fees, "difference": fees - before,
                        "source": "cash-activities"}, key=client_order_id, run_id=self.run_id)
        return True

    def record_order_fees(self, client_order_id: str, fees: Decimal,
                          evidence: dict[str, Any]) -> bool:
        """Record a finished order's fees as the broker charged them, with its evidence (one
        journal row), once: fees already recorded are never overwritten. Returns whether they
        were written. Raises ValueError for negative fees."""
        if fees < 0:
            raise ValueError(f"order fees {fees} are negative")
        with self.store.transaction():
            written = self.store.execute(
                "UPDATE orders SET fees_usd = ? WHERE client_order_id = ? "
                "AND fees_usd IS NULL", (str(fees), client_order_id))
            if written != 1:
                return False
            self.store.append(ORDER_FEES, {"fees_usd": fees, **evidence},
                              key=client_order_id, run_id=self.run_id)
        return True

    def _apply_detail(self, row: OrderRow, detail: OrderDetail, *, resurrected: bool) -> None:
        """Inside a transaction: record a Detail read."""
        cid = row.client_order_id
        if row.send_state is not SendState.ACKNOWLEDGED:
            self._set_send_state(cid, SendState.ACKNOWLEDGED,
                                 {"resolved_by": "resurrection" if resurrected else "detail"})
        changed = (detail.state != row.broker_state
                   or detail.filled_quantity != row.filled_quantity)
        self.store.execute("UPDATE orders SET last_detail_at = ?, absent_reads = 0 "
                           "WHERE client_order_id = ?", (self.clock(), cid))
        if changed:
            self.store.execute(
                "UPDATE orders SET broker_state = ?, filled_quantity = ?, "
                "average_fill_price = ?, updated_at = ? WHERE client_order_id = ?",
                (detail.state, str(detail.filled_quantity),
                 None if detail.average_fill_price is None
                 else str(detail.average_fill_price), self.store.stamp(), cid))
            self.store.append(ORDER_OBSERVED, {
                "state": detail.state, "raw_status": detail.raw_status,
                "filled_quantity": detail.filled_quantity,
                "average_fill_price": detail.average_fill_price}, key=cid, run_id=self.run_id)
            if detail.state is OrderState.REJECTED:
                self._count_rejection(row)
        elif self._held_proof_due(row, detail):
            self.store.append(ORDER_HELD, {
                "state": detail.state, "raw_status": detail.raw_status,
                "filled_quantity": detail.filled_quantity}, key=cid, run_id=self.run_id)
        if (detail.state.terminal and detail.filled_quantity > 0 and detail.fees is not None
                and detail.fees >= 0 and row.fees_usd is None):
            # The finished order's fees as Detail shows them, once (the daily loss counts
            # them); a Detail without readable fees leaves them to the order history.
            written = self.store.execute(
                "UPDATE orders SET fees_usd = ? WHERE client_order_id = ? AND fees_usd IS NULL",
                (str(detail.fees), cid))
            if written == 1:
                self.store.append(ORDER_FEES, {"fees_usd": detail.fees, "source": "detail",
                                               "filled_quantity": detail.filled_quantity},
                                  key=cid, run_id=self.run_id)
        # A cancel is known not to have taken when a read well after it still shows the
        # order working: a new cancel decision becomes possible.
        if (row.cancel_state in _CANCEL_AWAITING and not detail.state.terminal
                and row.cancel_at is not None
                and self.clock() - row.cancel_at
                >= float(self.config.orders.cancel_retry_spacing_seconds)):
            self.store.execute("UPDATE orders SET cancel_state = 'not-taken' "
                               "WHERE client_order_id = ?", (cid,))
            self.store.append("cancel-not-taken", {}, key=cid, run_id=self.run_id)

    def _held_proof_due(self, row: OrderRow, detail: OrderDetail) -> bool:
        """This unchanged read shows a stop working with nothing filled, and the order's last
        proof row (`order-observed` or `order-held`) is at least the configured interval old.
        Journal times only (the store clock), like the rows it compares."""
        if (row.leg is not Leg.STOP or detail.state not in _WORKING
                or detail.filled_quantity != 0):
            return False
        last = self.store.query(
            "SELECT at_utc FROM journal WHERE kind IN (?, ?) AND key = ? ORDER BY seq DESC "
            "LIMIT 1", (ORDER_OBSERVED, ORDER_HELD, row.client_order_id))
        if not last:
            return True
        since = (datetime.fromisoformat(self.store.stamp())
                 - datetime.fromisoformat(last[0]["at_utc"])).total_seconds()
        return since >= self.config.orders.stop_held_proof_seconds

    def _refresh(self, row: OrderRow, held: dict[str, Decimal] | None) -> None:
        """One Detail read; RateLimited propagates so the caller stops reading this step.
        The attempt time is recorded whatever the outcome, so scheduling rotates."""
        cid = row.client_order_id
        with self.store.transaction():
            self.store.execute("UPDATE orders SET last_detail_at = ? WHERE client_order_id = ?",
                               (self.clock(), cid))
        try:
            detail = self.broker.order_detail(cid)
        except OrderNotFound:
            self._not_found(row, held)
            return
        except RateLimited:
            # Not a failed read: our budget refused it, nothing was asked. The order keeps its
            # place in `_detail_answered_ids` (the budget's cooldown bounds this, and the
            # detail-budget fence blocks entries meanwhile).
            raise
        except BrokerError as exc:
            # Keep the last known state and try again next step, but never silently: a read
            # failing for too long fences (`_detail_failed`).
            self._detail_failed(row, type(exc).__name__)
            return
        self._detail_answered(cid)
        with self.store.transaction():
            self._apply_detail(row, detail, resurrected=False)
        for fence in (Fence.UNKNOWN_ORDER, Fence.ORDER_MISSING):
            self._clear_fence(fence, cid)
        if detail.state is OrderState.UNKNOWN:
            self._set_fence(Fence.STATUS_UNKNOWN, cid, f"unmapped status {detail.raw_status!r}")
        else:
            self._clear_fence(Fence.STATUS_UNKNOWN, cid)
        if detail.state.terminal or self.order(cid).cancel_state == "not-taken":
            self._clear_fence(Fence.CANCEL_UNKNOWN, cid)
        if detail.state.terminal:
            self._note_not_sent(cid, False)

    def _detail_failed(self, row: OrderRow, error: str) -> None:
        """A Detail read of this order failed (not the budget): past
        `[orders] detail_unreadable_seconds` of failures with no answered read in between,
        its state is unknown and entries are fenced; runtime.watch alerts at once. The first
        failure of a streak is journaled with its time, so a restart keeps counting."""
        cid = row.client_order_id
        self._detail_answered_ids.discard(cid)
        since = self._failing_since(cid)
        if since is None:
            since = self.clock()
            self._detail_failing_since[cid] = since
            with self.store.transaction():
                self.store.append(DETAIL_FAILED, {"at": since, "error": error}, key=cid,
                                  run_id=self.run_id)
        waited = self.clock() - since
        if waited < float(self.config.orders.detail_unreadable_seconds):
            return
        if row.leg is Leg.STOP and row.cancel_state in _CANCEL_AWAITING:
            cause = (f"stop cancel sent and its Detail unreadable for {int(waited)} s: the stop "
                     "may be gone and nothing is sold; check the position in the Webull app")
        else:
            cause = (f"Detail unreadable for {int(waited)} s: order state unknown; check it in "
                     "the Webull app")
        self._set_fence(Fence.DETAIL_UNREADABLE, cid, cause)

    def _detail_answered(self, cid: str) -> None:
        """Detail answered for this order (with the order or with "not found")."""
        if cid in self._detail_answered_ids:
            return  # answered before in this process, and no failure since
        if self._failing_since(cid) is not None:
            with self.store.transaction():
                self.store.append(DETAIL_ANSWERED, {}, key=cid, run_id=self.run_id)
        self._detail_failing_since.pop(cid, None)
        self._detail_answered_ids.add(cid)
        self._clear_fence(Fence.DETAIL_UNREADABLE, cid)

    def _failing_since(self, cid: str) -> float | None:
        """Engine-clock time of the first failed Detail read of the current streak, or None
        when the latest read was answered (memory first, then the journal after a restart)."""
        if cid in self._detail_failing_since:
            return self._detail_failing_since[cid]
        if cid in self._detail_answered_ids:
            return None
        rows = self.store.query(
            "SELECT kind, body FROM journal WHERE kind IN (?, ?) AND key = ? "
            "ORDER BY seq DESC LIMIT 1", (DETAIL_FAILED, DETAIL_ANSWERED, cid))
        if not rows or rows[0]["kind"] != DETAIL_FAILED:
            return None
        since = float(json.loads(rows[0]["body"])["at"])
        self._detail_failing_since[cid] = since
        return since

    def _detail_lost(self, row: OrderRow) -> bool:
        """Detail of this order has been unreadable for too long: it proves nothing now."""
        return self._has_fence(Fence.DETAIL_UNREADABLE, row.client_order_id)

    def _not_found(self, row: OrderRow, held: dict[str, Decimal] | None) -> None:
        cid = row.client_order_id
        self._detail_answered(cid)
        if row.send_state is SendState.ACKNOWLEDGED:
            # The broker acknowledged it and now says it does not exist: a conflict.
            self._set_fence(Fence.ORDER_MISSING, cid, "acknowledged order not found by Detail")
            return
        reads = row.absent_reads + 1
        with self.store.transaction():
            self.store.execute("UPDATE orders SET absent_reads = ?, last_detail_at = ? "
                               "WHERE client_order_id = ?", (reads, self.clock(), cid))
        if reads < self.config.orders.unknown_absent_reads or held is None:
            return  # too early, or the positions read failed: absence cannot be decided
        # Detail alone is not enough: also require absence from open orders, and no change
        # of the position that this order would explain: more shares than Detail accounts
        # for (an entry) or fewer (a sell). Giving up on a sell that filled would put a new
        # stop on shares no longer held.
        try:
            open_ids = {o.client_order_id for o in self.broker.open_orders()}
        except BrokerError:
            return
        if cid in open_ids:
            return  # it exists; Detail will catch up
        unexplained = held.get(row.symbol, ZERO) - self._expected_in(row.symbol)
        if (unexplained > 0) if row.side is Side.BUY else (unexplained < 0):
            return
        with self.store.transaction():
            self._set_send_state(cid, SendState.ABSENT, {"reads": reads})
            if row.leg in (Leg.STOP, Leg.EXIT):
                # Proved never created: a failed attempt like a refusal (spec 9), so a stop
                # counts against `stop_retry_attempts`. Counted once: only an UNKNOWN row gets
                # here, and an ABSENT row is rechecked, never resolved again. The spacing is
                # not restarted: proving absence took `unknown_absent_reads` reads already,
                # and the position is unprotected meanwhile.
                self._count_rejection(row, spaced=False)
        self._clear_fence(Fence.UNKNOWN_ORDER, cid)
        # A cancel answered "not found" for an order now proven absent (Detail, open orders,
        # no unexplained position) has nothing left to resolve; Detail never reads it again.
        self._clear_fence(Fence.CANCEL_UNKNOWN, cid)
        self._note_not_sent(cid, False)

    def _expected_in(self, symbol: str) -> Decimal:
        total = ZERO
        for trade in self.trades():
            if trade.symbol == symbol:
                total += self.expected_quantity(self.orders(trade.trade_id))
        return total

    def _resurrect(self, row: OrderRow, detail: OrderDetail | None, why: str) -> None:
        """An order we had given up on exists at the broker: track it again and fence."""
        cid = row.client_order_id
        with self.store.transaction():
            if detail is not None:
                self._apply_detail(row, detail, resurrected=True)
            else:
                self._set_send_state(cid, SendState.ACKNOWLEDGED, {"resolved_by": "open-orders"})
                self.store.execute("UPDATE orders SET broker_state = NULL "
                                   "WHERE client_order_id = ?", (cid,))
            self.store.execute(
                "UPDATE trades SET state = ?, closed_at = NULL, close_cause = NULL "
                "WHERE trade_id = ? AND state = ?",
                (TradeState.OPEN, row.trade_id, TradeState.CLOSED))
            self.store.append("order-resurrected", {"why": why}, key=cid, run_id=self.run_id)
            self._fence_row(Fence.RESURRECTED, cid, why)

    # --- the loop body ---------------------------------------------------------------------

    def step(self) -> None:
        """One pass: read positions, observe orders within budget, advance every trade."""
        self._positions = None
        try:
            positions = self.broker.positions()
        except BrokerError as exc:
            self.broker_view = None  # the latest read failed: what the broker holds is unknown
            self._set_fence(Fence.BROKER_READ, "", f"positions read failed: {type(exc).__name__}")
            self._step_blind()
            return
        self._positions = positions
        self._clear_fence(Fence.BROKER_READ)
        held = {p.symbol: p.quantity for p in positions if p.quantity != 0}

        self._observe(held)

        open_symbols = set()
        for trade in self.trades():
            open_symbols.add(trade.symbol)
            self._advance(trade, held.get(trade.symbol, ZERO))

        for symbol in held:
            if symbol not in open_symbols:
                self._set_fence(Fence.FOREIGN_POSITION, symbol,
                                "position not opened by this bot")
        for name in self.fences():
            fence, _, symbol = name.partition(":")
            if fence == Fence.FOREIGN_POSITION and (symbol not in held
                                                    or symbol in open_symbols):
                self._clear_fence(Fence.FOREIGN_POSITION, symbol)

    def _step_blind(self) -> None:
        """The positions read failed. Detail still says what filled and stops are sized on
        Detail alone, so fills are still observed and protected (hard rule 2). Nothing that
        needs the held quantity happens: no order is declared absent, no trade is closed and
        no closing order is sent."""
        self._observe(None)
        for trade in self.trades():
            self._advance(trade, None)

    def _observe(self, held: dict[str, Decimal] | None) -> None:
        """Detail reads within `detail_reads_per_step`, one queue for everything: orders still
        open, and orders we gave up on in a symbol whose position we cannot explain. `held`
        is None when the positions read failed. New stops are verified first, all of them by
        one open-orders read."""
        self._verify_listed()
        due = [o for o in self.orders()
               if o.send_state in (SendState.ACKNOWLEDGED, SendState.UNKNOWN)
               and (o.broker_state is None or not o.broker_state.terminal)]
        if held is not None:
            due += self._lost_fill_suspects(held)
            self._note_moved(held)
        poll = float(self.config.orders.detail_poll_seconds)
        # A symbol being closed moves because of our own closes: its other stops are not read
        # ahead of its cancel (they would only delay it).
        closing = {o.symbol for o in self.orders() if o.leg is Leg.EXIT and o.live}
        cancelling = {row.symbol for row in due
                      if self._awaits_cancel_read(row) and row.symbol not in closing}
        due.sort(key=lambda row: (self._read_first(row, cancelling),
                                  _detail_priority(row, poll)))
        for row in due[:self.config.orders.detail_reads_per_step]:
            try:
                if row.send_state is SendState.ABSENT:
                    self._recheck_absent(row)
                else:
                    self._refresh(row, held)
            except RateLimited:
                self._set_fence(Fence.DETAIL_BUDGET, "", "Detail reads refused by the budget")
                return
        self._clear_fence(Fence.DETAIL_BUDGET)

    def _verify_listed(self) -> None:
        """Verify every new stop with one open-orders read (owner decision 2026-11-17), so
        fills arriving together do not wait for a Detail read per stop: a stop the list shows
        working, as sent (symbol, side, type, quantity; prices are listed rounded, as in
        Detail, which does not compare them either) and with no more filled than Detail
        showed, counts as verified. Only acknowledged stops never seen
        working and not being cancelled; anything else in the list (a fill, a cancel, a
        different row) and any absence prove nothing and are left to Detail. A failed or
        budget-refused read leaves them to Detail too. The list is read every step while a new
        stop is unverified, so one listed a few polls late costs no Detail read."""
        self._list_answered = False
        new = {o.client_order_id: o for o in self.orders()
               if o.leg is Leg.STOP and o.send_state is SendState.ACKNOWLEDGED
               and o.broker_state is None and o.cancel_state is None}
        if not new:
            return
        try:
            rows = self.broker.open_orders()
        except BrokerError:
            return
        self._list_answered = True
        for listed in rows:
            row = new.get(listed.client_order_id)
            if (row is None or listed.state not in _WORKING or listed.symbol != row.symbol
                    or listed.side is not row.side or listed.order_type is not row.order_type
                    or listed.quantity != row.quantity
                    or listed.filled_quantity != row.filled_quantity):
                continue
            with self.store.transaction():
                self.store.execute(
                    "UPDATE orders SET broker_state = ?, updated_at = ? "
                    "WHERE client_order_id = ? AND broker_state IS NULL",
                    (listed.state, self.store.stamp(), row.client_order_id))
                # The same observation row a Detail read writes, marked with its source.
                self.store.append(ORDER_OBSERVED, {
                    "state": listed.state, "raw_status": listed.raw_status,
                    "filled_quantity": listed.filled_quantity,
                    "average_fill_price": listed.average_fill_price, "source": "open-orders"},
                    key=row.client_order_id, run_id=self.run_id)

    def _note_moved(self, held: dict[str, Decimal]) -> None:
        """Record each open trade's symbol whose broker position differs from what Detail
        says we hold (a fill or a stop-out Detail has not shown yet, usually), keeping the
        time a disagreement was first seen while it stays the same."""
        moved = {}
        for symbol in {t.symbol for t in self.trades()}:
            pair = (held.get(symbol, ZERO), self._expected_in(symbol))
            if pair[0] == pair[1]:
                continue
            last = self._moved.get(symbol)
            moved[symbol] = last if last is not None and last[:2] == pair else (
                *pair, self.clock())
        self._moved = moved

    def _read_first(self, row: OrderRow, cancelling: set[str]) -> int:
        """Where this order's Detail read goes, so the delay from a fill or a stop-out to its
        Detail does not grow with the number of open orders. 0, first: a live stop in a
        symbol whose broker position moved away from Detail, not read since that was first
        seen, when another stop of that symbol waits for its cancel's Detail (`cancelling`):
        a stop-out Detail has not shown yet changes what that cancel's confirmation leads to
        (a kept stop filling while a scale-out's stops are released). 1: a stop or entry
        whose cancel waits for Detail, not read since that cancel (a stop's position may have
        no stop until the read confirms it, and its close waits for it; an entry's read shows
        its last fills, which need their stops), and, while the open-orders list cannot be
        read, a stop never read (entries filling in the same poll each get their stops
        verified before the next entry is read, so the first fills are not left waiting
        behind the last). While the list answers it verifies new stops, however late it shows
        them, and their Detail stays in the common queue (a rejected stop is never listed),
        so the fills' reads are never spent on them. 2: the other live orders in a
        moved symbol, not read since the move. 3: the common queue. Once each per event:
        after that read the order is back in the common queue, so a lasting disagreement
        (shares bought by hand) never starves the others. A read stamped at the same instant
        as the event may have come before it (a step reads, then cancels): it does not count
        as the read after it."""
        if self._awaits_cancel_read(row):
            return 1
        if (row.leg is Leg.STOP and row.live and not row.verified and row.last_detail_at is None
                and not self._list_answered):
            return 1
        moved = self._moved.get(row.symbol)
        if moved is not None and row.live and (row.last_detail_at or 0.0) <= moved[2]:
            return 0 if row.leg is Leg.STOP and row.symbol in cancelling else 2
        return 3

    @staticmethod
    def _awaits_cancel_read(row: OrderRow) -> bool:
        return (row.leg is not Leg.EXIT and row.cancel_state in _CANCEL_AWAITING
                and row.cancel_at is not None and (row.last_detail_at or 0.0) <= row.cancel_at)

    def _lost_fill_suspects(self, held: dict[str, Decimal]) -> list[OrderRow]:
        """Orders we gave up on whose fill would explain the position: any in a symbol
        holding more than Detail accounts for, and an open trade's sells where it holds
        less."""
        suspects: list[OrderRow] = []
        for symbol, quantity in held.items():
            if quantity <= self._expected_in(symbol):
                continue
            suspects += [o for o in self.orders()
                         if o.symbol == symbol and o.send_state is SendState.ABSENT]
        for trade in self.trades():
            orders = self.orders(trade.trade_id)
            if held.get(trade.symbol, ZERO) < self._expected_in(trade.symbol):
                suspects += [o for o in orders
                             if o.side is Side.SELL and o.send_state is SendState.ABSENT]
        return suspects

    def _recheck_absent(self, row: OrderRow) -> None:
        """Ask Detail once more about an order we gave up on; a fill there means it existed
        after all. RateLimited propagates like any budgeted read."""
        with self.store.transaction():
            self.store.execute("UPDATE orders SET last_detail_at = ? WHERE client_order_id = ?",
                               (self.clock(), row.client_order_id))
        try:
            detail = self.broker.order_detail(row.client_order_id)
        except RateLimited:
            raise
        except BrokerError:
            return
        self._resurrect(row, detail, "order given up on was found with a fill")

    def _advance(self, trade: TradeRow, held: Decimal | None) -> None:
        """`held` is None when the positions read failed: the trade is not closed, the
        quantity fence is left as it is and no closing order is sent; stops are kept."""
        orders = self.orders(trade.trade_id)
        entry = next(o for o in orders if o.leg is Leg.ENTRY)
        stops = [o for o in orders if o.leg is Leg.STOP]
        exits = [o for o in orders if o.leg is Leg.EXIT]
        expected = self.expected_quantity(orders)
        live = [o for o in orders if o.live]

        if trade.state is TradeState.OPENING and entry.filled_quantity > 0:
            self._set_trade_state(trade, TradeState.OPEN)
            trade = self.trade(trade.trade_id)
        if trade.exit_requested and trade.state is not TradeState.EXITING:
            self._set_trade_state(trade, TradeState.EXITING)
            trade = self.trade(trade.trade_id)

        if not live and expected == 0 and held == 0:
            cause = trade.exit_requested or (
                "stopped-out" if any(o.filled_quantity > 0 for o in stops)
                else "entry-not-filled" if entry.filled_quantity == 0 else "closed")
            self._close(trade, cause)
            return

        # Only entry and exit orders change the quantity on purpose; a resting stop does not.
        # One whose Detail cannot be read explains nothing: the mismatch is fenced, not excused.
        in_flight = any(o.live and not self._detail_lost(o) for o in (entry, *exits))
        if held is None:
            pass
        elif held == expected or in_flight:
            self._clear_fence(Fence.QUANTITY_MISMATCH, trade.trade_id)
        else:
            self._set_fence(Fence.QUANTITY_MISMATCH, trade.trade_id,
                            f"broker holds {held}, Detail says {expected}")

        if trade.state is TradeState.EXITING and trade.reducing:
            # The teardown takes over: a partial sale still working counts as a close in
            # flight (`_exit_step` sells only what it does not), and the rest is closed.
            with self.store.transaction():
                self._set_reduce(trade, ReduceState.DONE, {"why": "exit requested"})
            trade = self.trade(trade.trade_id)
        if trade.state is TradeState.EXITING and not (
                self._has_fence(Fence.SHUTTING_DOWN) and not any(e.live for e in exits)):
            self._exit_step(trade, entry, stops, exits, held, expected)
        elif trade.state is TradeState.OPEN and trade.reducing:
            self._reduce_step(trade, stops, exits, held, expected)
        else:
            # Not exiting, or shutting down mid-teardown with no close working: stop tearing
            # down and keep protection, so a drain deadline never leaves a position unstopped.
            if trade.state is TradeState.EXITING and entry.live:
                self._cancel(entry)
            self._protect(trade, stops, expected)
            self._cap_stops(trade, entry)
        trade = self.trade(trade.trade_id)
        self._watch_protection(trade, exits, expected - self._reduce_pending(trade, exits))

    def _cap_stops(self, trade: TradeRow, entry: OrderRow) -> None:
        """An entry filling in pieces adds stops with each observation; the session flatten
        cancels them one at a time, so their number is bounded: once the trade holds
        `max_stops_per_trade` minus what one more observation adds, the entry's rest is
        cancelled (journaled), and the fills Detail shows by the read that confirms it still
        get their stop (hard rule 2). Only with several slots (owner 2026-11-17): with one, the
        entry keeps filling to its size and the one position's flatten has the whole window."""
        if (not entry.live or entry.cancelling
                or self.config.risk.max_open_positions <= 1):
            return
        stops = [o for o in self.orders(trade.trade_id) if o.leg is Leg.STOP and o.live]
        room = self.config.orders.max_stops_per_trade - STOPS_PER_FILL
        if len(stops) < room:
            return
        with self.store.transaction():
            self.store.append("entry-capped", {"stops": len(stops), "limit":
                                               self.config.orders.max_stops_per_trade},
                              key=trade.trade_id, run_id=self.run_id)
        self._cancel(entry)

    def _reduce_step(self, trade: TradeRow, stops: list[OrderRow], exits: list[OrderRow],
                     held: Decimal | None, expected: Decimal) -> None:
        """One partial sale: release stops covering exactly the shares to sell (the rest stay
        under their stops all along), then one MARKET SELL for them, never resent. Stops are
        released only after this process read their Detail (as for a close); the sale goes
        out only once Detail shows them ended and the broker position agrees with Detail.
        When no set of stops matches exactly any more, nothing is released and the reduce
        ends unsent."""
        assert trade.reduce_quantity is not None
        sales = self._reduce_sales(trade, exits)
        if trade.reduce_state is ReduceState.SENT:
            working = [s for s in sales if s.live]
            if working:
                self._watch_exit(trade, working)
                self._protect(trade, stops,
                              expected - sum((s.open_quantity for s in working), ZERO))
                return
            with self.store.transaction():
                self._set_reduce(trade, ReduceState.DONE, {
                    "sold": sum((s.filled_quantity for s in sales), ZERO)})
            self._protect(trade, stops, expected)
            return
        target = expected - trade.reduce_quantity
        if target <= 0:
            # A kept stop filled while the sale's stops were being released: the price traded
            # through the stop, so the released shares are not stopped again at it (Webull's
            # answer to a sell stop above the market is unproven). They are closed at market
            # by the exit (`_exit_step`); the fact is journaled first.
            with self.store.transaction():
                self._set_reduce(trade, ReduceState.DONE, {
                    "not_sent": f"a kept stop filled; holds {expected}, closing at market"})
                if expected > 0:
                    self.exit_request_row(trade.trade_id, STOP_FILLED_DURING_REDUCE)
            return
        refusal = self.close_refusal()
        if refusal is not None:
            with self.store.transaction():
                self._set_reduce(trade, ReduceState.DONE, {"not_sent": refusal})
            self._protect(trade, stops, expected)
            return
        if not self._may_close(trade, held, expected):
            self._protect(trade, stops, expected)  # wait until positions and Detail agree
            return
        if self._replace_blocks(trade, stops):
            return
        live_stops = [s for s in stops if s.live]
        covered = sum((s.open_quantity for s in live_stops), ZERO)
        if covered > target:
            release = _stops_to_release(live_stops, covered - target)
            if release is None:  # the stops changed (a partial stop fill, say): no exact set
                with self.store.transaction():
                    self._set_reduce(trade, ReduceState.DONE, {
                        "not_sent": f"no live stops cover exactly {covered - target}"})
                self._protect(trade, stops, expected)
                return
            for stop in release:
                if stop.client_order_id in self._detail_answered_ids:
                    self._cancel(stop)
            return  # the sale waits until Detail shows the released stops ended
        with self.store.transaction():
            cid = self._write_intent(trade.trade_id, Leg.EXIT, trade.symbol, Side.SELL,
                                     OrderType.MARKET, trade.reduce_quantity)
            self._set_reduce(trade, ReduceState.SENT, {"client_order_id": cid})
        self._send(cid)
        self._protect(trade, stops, target)

    def _protect(self, trade: TradeRow, stops: list[OrderRow], target: Decimal) -> None:
        """Keep live stops covering `target` (Detail quantity, never the positions read)."""
        covered = sum((s.open_quantity for s in stops if s.live), ZERO)
        if covered > target:
            self._set_fence(Fence.OVER_PROTECTED, trade.trade_id,
                            f"stops cover {covered}, Detail holds {target}")
            self._trim_stops(stops, target)
        else:
            self._clear_fence(Fence.OVER_PROTECTED, trade.trade_id)
        if covered >= target:
            self._clear_fence(Fence.STOP_HELD, trade.trade_id)
            return
        if self._replace_blocks(trade, stops):
            return
        trade = self._stop_from_fill(trade, stops)
        orders = self.config.orders
        if trade.stop_failures > orders.stop_retry_attempts:
            if not self._has_fence(Fence.STOP_REJECTED, trade.trade_id):
                self._set_fence(Fence.STOP_REJECTED, trade.trade_id,
                                "protective stop refused after retries; exiting")
                self.request_exit(trade.trade_id, "stop-rejected")
            return
        if (trade.last_stop_failure_at is not None and self.clock() - trade.last_stop_failure_at
                < float(orders.stop_retry_spacing_seconds)):
            return
        for quantity in _stop_pieces(trade, covered, target - covered):
            with self.store.transaction():
                cid = self._write_intent(trade.trade_id, Leg.STOP, trade.symbol, Side.SELL,
                                         OrderType.STOP_LOSS, quantity,
                                         stop_price=self._distinct_stop_price(
                                             trade.symbol, quantity, trade.stop_price))
            self._send(cid)
            if self.order(cid).send_state is not SendState.ACKNOWLEDGED:
                return  # the next pass decides again (refusal spacing, unknown outcome)

    def _distinct_stop_price(self, symbol: str, quantity: Decimal, price: Decimal) -> Decimal:
        """`price`, or one valid tick higher per clash, so that the new SELL stop is not
        identical to a live one of ours in `symbol`: Webull refuses an order identical to a
        working one (same quantity and stop price) as a duplicate, while a different quantity
        or price is accepted (docs/webull-facts.md, probe T D1). Equal pieces (an even
        scale-out split, equal partial fills) would otherwise be refused and their shares left
        unstopped. Higher is tighter for a long, so the planned loss only shrinks; no live stop
        is cancelled or changed. Journaled when it moves the price."""
        taken = {o.stop_price for o in self.orders()
                 if o.symbol == symbol and o.leg is Leg.STOP and o.side is Side.SELL
                 and o.live and o.quantity == quantity}
        distinct = price
        while distinct in taken:
            distinct += tick(distinct)
        if distinct != price:
            self.store.append("stop-price-distinct", {"stop": price, "placed": distinct,
                                                      "quantity": quantity},
                              key=symbol, run_id=self.run_id)
        return distinct

    def _replace_blocks(self, trade: TradeRow, stops: list[OrderRow]) -> bool:
        """A replace of this trade's stop is unresolved, so Webull may hold a new stop we do
        not track. Place no stop and sell nothing (either could sell the same share twice)
        until two reads of open orders in a row, each started after the replace fence and
        with the trade's live stops unchanged in between, show no working SELL in the symbol
        that is not ours. Webull may list the new order a read late, so one clean read is not
        enough. The read is made here, at the decision, at most once per
        `reconcile_interval_seconds` per trade; between reads, and after a failed read, the
        hold stays. It is shown as the `stop-held:<trade>` fence (one row per cause)."""
        tid = trade.trade_id
        fence_ages = [self._fence_age(f"{Fence.REPLACE_UNRESOLVED}:{s.client_order_id}")
                      for s in stops]
        ages = [age for age in fence_ages if age is not None]
        if not ages:
            self._held_reads.pop(tid, None)
            self._clear_fence(Fence.STOP_HELD, tid)
            return False
        started = self.clock()
        fenced_at = started - min(ages)
        live = tuple(sorted(s.client_order_id for s in stops if s.live))
        last = self._held_reads.get(tid)
        if last is not None and (last[0] < fenced_at or last[1] != live):
            last = None  # a read from before the replace, or before our stops changed
        if last is not None and started - last[0] < float(
                self.config.orders.reconcile_interval_seconds):
            self._hold(tid, None)
            return True
        try:
            rows = self.broker.open_orders()
        except BrokerError:
            self._held_reads.pop(tid, None)
            self._hold(tid, None)
            return True
        ours = {o.client_order_id for o in self.orders()}
        foreign = sorted(o.client_order_id for o in rows
                         if o.symbol == trade.symbol and o.side is Side.SELL
                         and not o.state.terminal and o.client_order_id not in ours)
        if foreign:
            self._held_reads[tid] = (started, live, 0)
            self._hold(tid, f"stop and close held: untracked SELL {', '.join(foreign)} rests "
                            f"in {trade.symbol} after an unresolved replace; resolve it in "
                            f"the Webull app")
            return True
        clean = 1 if last is None else last[2] + 1
        if clean < 2:
            self._held_reads[tid] = (started, live, clean)
            self._hold(tid, None)
            return True
        self._held_reads.pop(tid, None)
        self._clear_fence(Fence.STOP_HELD, tid)
        return False

    def _hold(self, trade_id: str, cause: str | None) -> None:
        """Set `stop-held:<trade>`. `cause` None keeps the cause already set, if any; a
        changed cause replaces it (one journal row each)."""
        name = f"{Fence.STOP_HELD}:{trade_id}"
        current = self.fences().get(name)
        if cause is None:
            if current is not None:
                return
            cause = ("stop and close held after an unresolved replace: open orders not yet "
                     "shown clean twice; check the Webull app")
        if current == cause:
            return
        if current is not None:
            self._clear_fence(Fence.STOP_HELD, trade_id)
        self._set_fence(Fence.STOP_HELD, trade_id, cause)

    def _trim_stops(self, stops: list[OrderRow], target: Decimal) -> None:
        """Cancel whole stops that are not needed, largest first, never going below `target`:
        a stop for shares we no longer hold could sell short. Stops already being cancelled
        do not count as kept. An excess no whole stop fits stays fenced (safer than less)."""
        keep = [s for s in stops if s.live and not s.cancelling]
        excess = sum((s.open_quantity for s in keep), ZERO) - target
        for stop in sorted(keep, key=lambda s: s.open_quantity, reverse=True):
            if ZERO < stop.open_quantity <= excess and stop.send_state is SendState.ACKNOWLEDGED:
                self._cancel(stop)
                excess -= stop.open_quantity

    def _stop_from_fill(self, trade: TradeRow, stops: list[OrderRow]) -> TradeRow:
        """Before the first stop: derive it from the observed average fill when the trade asks
        for it (the limit-based price was only the worst case for the planned-loss check)."""
        if trade.stop_percent_below_fill is None or stops:
            return trade
        entry = next(o for o in self.orders(trade.trade_id) if o.leg is Leg.ENTRY)
        if entry.average_fill_price is None:
            return trade
        factor = 1 - trade.stop_percent_below_fill / Decimal(100)
        derived = (entry.average_fill_price * factor).quantize(Decimal("0.01"),
                                                                rounding=ROUND_DOWN)
        if not ZERO < derived < entry.average_fill_price or derived == trade.stop_price:
            return trade
        with self.store.transaction():
            self.store.execute("UPDATE trades SET stop_price = ? WHERE trade_id = ?",
                               (str(derived), trade.trade_id))
            self.store.append("stop-from-fill", {"fill": entry.average_fill_price,
                                                 "stop": derived, "bound": trade.stop_price},
                              key=trade.trade_id, run_id=self.run_id)
        return self.trade(trade.trade_id)

    def _watch_protection(self, trade: TradeRow, exits: list[OrderRow],
                          expected: Decimal) -> None:
        """Fence while the held quantity lacks a verified stop; exit if that lasts too long."""
        stops = [o for o in self.orders(trade.trade_id) if o.leg is Leg.STOP]
        verified = sum((s.open_quantity for s in stops
                        if s.verified and not self._detail_lost(s)), ZERO)
        if expected <= 0 or verified >= expected:
            self._clear_fence(Fence.UNPROTECTED, trade.trade_id)
            return
        self._set_fence(Fence.UNPROTECTED, trade.trade_id,
                        f"verified stop covers {verified} of {expected}")
        # Stops last seen working, with no cancel of ours, whose Detail is only unreadable now:
        # they are not proof (the fences above alert), but closing for them would cancel a
        # likely working stop and end the day's entries over a read outage. No exit for them.
        unreadable = sum((s.open_quantity for s in stops if s.verified and not s.cancelling
                          and self._detail_lost(s)), ZERO)
        if verified + unreadable >= expected:
            return
        age = self._fence_age(f"{Fence.UNPROTECTED}:{trade.trade_id}") or 0.0
        exiting = trade.state is TradeState.EXITING or any(e.live for e in exits)
        if age >= float(self.config.orders.max_unprotected_seconds) and not exiting:
            # Asked every pass: refused while an earlier close waits for the operator, taken
            # as soon as they clear that fence.
            self.request_exit(trade.trade_id, "unprotected-too-long")
            if not self._has_fence(Fence.UNPROTECTED_TIMEOUT, trade.trade_id):
                # From the exit-refused fence, not from request_exit's answer: that is also
                # False when an exit was already requested earlier in this pass (stop-rejected),
                # and telling the operator to sell by hand then would double the sale.
                refused = self._has_fence(Fence.EXIT_REFUSED, trade.trade_id)
                # A stop whose latest Detail read failed is not cancelled by the exit
                # (`_exit_step`), so the close waits for its Detail: say so, not "exiting".
                unread = any(s.live and self._failing_since(s.client_order_id) is not None
                             for s in stops)
                if refused:
                    text = (f"no verified stop for {int(age)} s and the closing order was "
                            "refused before: close the position in the Webull app")
                elif unread:
                    text = (f"no verified stop for {int(age)} s and a stop's Detail cannot be "
                            "read: that stop is kept, nothing is sold; close the position in "
                            "the Webull app")
                else:
                    text = f"no verified stop for {int(age)} s; exiting"
                self._set_fence(Fence.UNPROTECTED_TIMEOUT, trade.trade_id, text)

    def _exit_step(self, trade: TradeRow, entry: OrderRow, stops: list[OrderRow],
                   exits: list[OrderRow], held: Decimal | None, expected: Decimal) -> None:
        """Teardown in a safe order: entry first (stops keep protecting late fills), then the
        stops, then one closing order for exactly the agreed quantity. Whenever the close
        cannot be sent, protection is restored instead of waiting unprotected."""
        if entry.live and not self._has_fence(Fence.CANCEL_FAILED, entry.client_order_id):
            self._cancel(entry)
            self._protect(trade, stops, expected)
            return
        # An entry still working after every cancel (CANCEL_FAILED, operator alerted) must not
        # keep the filled shares from being closed: they are closed now, and any later fill
        # of that entry is protected and closed by the following passes.
        live_stops = [s for s in stops if s.live]
        live_exits = [e for e in exits if e.live]
        selling = sum((e.open_quantity for e in live_exits), ZERO)
        if live_exits:
            self._watch_exit(trade, live_exits)
            if (not live_stops and expected <= selling) or self._has_fence(
                    Fence.EXIT_NOT_FILLED, trade.trade_id):
                return  # everything held is being sold, or a close waits for the operator
        elif self._has_fence(Fence.EXIT_NOT_FILLED, trade.trade_id):
            # A close that did not fill in time has ended (cancelled or expired): send no other
            # close until the operator has looked; keep what is held under a stop meanwhile.
            self._protect(trade, stops, max(expected, ZERO))
            return
        if not self._may_close(trade, held, expected, selling):
            # Positions and Detail disagree in a way a sale could get wrong: never sell on
            # that; keep the Detail quantity no close sells yet protected until they agree
            # (the mismatch fence is already set).
            self._protect(trade, stops, max(expected - selling, ZERO))
            return
        if not self._has_fence(Fence.MARKET_CLOSED, trade.trade_id):
            refusal = self.close_refusal()
            if refusal is not None:
                self._set_fence(Fence.MARKET_CLOSED, trade.trade_id, (
                    f"{trade.exit_requested} of {expected} {trade.symbol} not sent: {refusal}; "
                    "the stop stays. Close the position in the Webull app, or clear this fence in "
                    "the next regular session and the bot sends the close"))
        if self._has_fence(Fence.MARKET_CLOSED, trade.trade_id):
            self._protect(trade, stops, expected - selling)  # no MARKET order outside the session
            return
        if self._replace_blocks(trade, stops):
            return  # keep our stops; a sale could add to an untracked stop of the replace
        # Sell what no live stop covers and no working close sells yet: a piece whose stop's
        # cancel Detail has confirmed goes out at once, never after another piece's fill (a
        # stop leaves `live` only when Detail shows it ended). Live stops, working closes and
        # this sale never exceed the agreed quantity.
        uncovered = expected - sum((s.open_quantity for s in live_stops), ZERO) - selling
        if uncovered > 0:
            with self.store.transaction():
                cid = self._write_intent(trade.trade_id, Leg.EXIT, trade.symbol, Side.SELL,
                                         OrderType.MARKET, uncovered)
            self._send(cid)
        if self._cancel_in_flight():
            # One stop at a time, across every trade: the next stop is cancelled only once the
            # previous cancel is confirmed and its shares' close is sent, so shares wait
            # without a stop and a sale for one cancel read, whatever the slots, the stops per
            # trade and the closes' fill delays. A trade not yet started keeps its stops whole.
            if not live_exits and not any(s.cancelling for s in stops):
                self._protect(trade, stops, expected)
            return
        for stop in live_stops:
            if self._has_fence(Fence.CANCEL_FAILED, stop.client_order_id):
                # Still working after every cancel (the fence alerts the operator): it keeps
                # covering its shares, and the trade's other stops go on without it.
                continue
            if stop.client_order_id not in self._detail_answered_ids:
                # Spec 9: a cancel only Detail can confirm is sent only after this process's
                # latest Detail read of the stop was answered; while it fails the stop keeps
                # protecting and still counts as covering, so nothing is sold for its shares.
                # A read failing too long sets the detail-unreadable fence, which alerts.
                continue
            self._cancel(stop)
            return

    def _cancel_in_flight(self) -> bool:
        """Whether a stop's cancel waits for Detail (its shares may have no stop until the read
        confirms it and their close is sent). A stop whose Detail reads keep failing (the
        detail-unreadable fence alerts) does not hold the others back."""
        return any(o.leg is Leg.STOP and o.live and o.cancelling and not self._detail_lost(o)
                   for o in self.orders())

    def _may_close(self, trade: TradeRow, held: Decimal | None, expected: Decimal,
                   selling: Decimal = ZERO) -> bool:
        """Whether a close of this trade's own Detail quantity may be sent (owner decision
        2026-11-15). Agreement allows it, and so does the broker holding less by no more than
        `selling`, what this trade's working closes still sell (a close may have filled before
        Detail shows it; only the rest is sold). The broker holding MORE than all open trades in the
        symbol account for (shares bought by hand, say) allows it too: only `expected` is
        sold, which is never more than held, and the extra shares are never touched; the
        quantity-mismatch fence stays. Not while an entry in the symbol is still working: the
        extra may be its fill that Detail does not show yet. The broker holding fewer refuses:
        never sell shares that may not be there."""
        if held is None or expected <= 0:
            return False
        if held == expected or expected - selling <= held < expected:
            return True
        if held < self._expected_in(trade.symbol):
            return False
        return not any(o.live for o in self.orders()
                       if o.symbol == trade.symbol and o.leg is Leg.ENTRY)

    def _watch_exit(self, trade: TradeRow, live_exits: list[OrderRow]) -> None:
        """A closing order not filled `max_exit_seconds` after its send attempt (store clock,
        the journaled attempt) gets an operator fence, which alerts. Nothing is resent,
        cancelled or added: a halt or no buyers need the operator's eyes, not another order."""
        if self._has_fence(Fence.EXIT_NOT_FILLED, trade.trade_id):
            return
        now = datetime.fromisoformat(self.store.stamp())
        limit = float(self.config.orders.max_exit_seconds)
        for row in live_exits:
            attempts = self.store.journal(f"order-{SendState.ATTEMPTED}", row.client_order_id)
            if not attempts:
                continue  # never attempted: not at the broker
            age = (now - datetime.fromisoformat(attempts[0].at_utc)).total_seconds()
            if age < limit:
                continue
            self._set_fence(Fence.EXIT_NOT_FILLED, trade.trade_id,
                            f"closing order {row.client_order_id} for {row.quantity} "
                            f"{trade.symbol} not filled after {int(age)} s "
                            f"(filled {row.filled_quantity}, state "
                            f"{row.broker_state or row.send_state}); nothing more is sent: "
                            "look in the Webull app (halt? no buyers?)")
            return

    def _set_trade_state(self, trade: TradeRow, state: TradeState) -> None:
        with self.store.transaction():
            self.store.execute("UPDATE trades SET state = ? WHERE trade_id = ?",
                               (state, trade.trade_id))
            self.store.append("trade-state", {"from": trade.state, "to": state},
                              key=trade.trade_id, run_id=self.run_id)

    def _close(self, trade: TradeRow, cause: str) -> None:
        with self.store.transaction():
            self.store.execute(
                "UPDATE trades SET state = ?, closed_at = ?, close_cause = ? WHERE trade_id = ?",
                (TradeState.CLOSED, self.store.stamp(), cause, trade.trade_id))
            self.store.append("trade-closed", {"cause": cause}, key=trade.trade_id,
                              run_id=self.run_id)
        for fence in (Fence.UNPROTECTED, Fence.QUANTITY_MISMATCH, Fence.OVER_PROTECTED,
                      Fence.STOP_HELD):
            self._clear_fence(fence, trade.trade_id)

    # --- stop replace (probe F5) --------------------------------------------------------------

    def replace_intent(self, stop: OrderRow, stop_price: Decimal, facts: dict[str, Any]) -> None:
        """Durable BEFORE the replace request: the intent, and an operator fence that only a
        confirmed result (`replace_confirmed`) or the operator clears, so a crash or an
        unknown outcome leaves entries blocked until someone has looked."""
        cid = stop.client_order_id
        with self.store.transaction():
            self.store.append("replace-intent", {"from": stop.stop_price, "to": stop_price,
                                                 "quantity": stop.quantity, **facts},
                              key=cid, run_id=self.run_id)
            self._fence_row(Fence.REPLACE_UNRESOLVED, cid,
                            f"stop replace to {stop_price} not confirmed; operator must look")
        with self.store.transaction():
            self.store.append("replace-attempted", {}, key=cid, run_id=self.run_id)

    def replace_outcome(self, cid: str, outcome: str, body: dict[str, Any],
                        *, unchanged: bool = False) -> None:
        """Record the answer to the replace request. Only an answer proving the order did not
        change (Refused, or the local budget) lifts the fence here."""
        with self.store.transaction():
            self.store.append(f"replace-{outcome}", body, key=cid, run_id=self.run_id)
            if unchanged:
                self._drop_replace_fence(cid, f"replace-{outcome}")

    def replace_confirmed(self, cid: str, stop_price: Decimal, how: dict[str, Any]) -> None:
        """Detail and open orders showed the same order working at the new price; the trade
        takes it too, so a re-placed stop never goes back to the looser price."""
        with self.store.transaction():
            self.store.execute("UPDATE orders SET stop_price = ?, updated_at = ? "
                               "WHERE client_order_id = ?",
                               (str(stop_price), self.store.stamp(), cid))
            self.store.execute("UPDATE trades SET stop_price = ? WHERE trade_id = "
                               "(SELECT trade_id FROM orders WHERE client_order_id = ?)",
                               (str(stop_price), cid))
            self.store.append("replace-confirmed", {"stop_price": stop_price, **how}, key=cid,
                              run_id=self.run_id)
            self._drop_replace_fence(cid, "replace-confirmed")

    def _drop_replace_fence(self, cid: str, by: str) -> None:
        """Inside a transaction."""
        name = f"{Fence.REPLACE_UNRESOLVED}:{cid}"
        if self.store.execute("DELETE FROM fences WHERE name = ?", (name,)):
            self.store.append("fence-cleared", {"by": by}, key=name, run_id=self.run_id)

    def adopt_replacement(self, old: OrderRow, old_detail: OrderDetail,
                          detail: OrderDetail, expected_stop: Decimal) -> None:
        """Webull answered the replace with a NEW order (`old_detail` shows the old one
        terminal): track it as this trade's stop so protection and teardown see it. Its stop
        price is the one shown, or `expected_stop` when the row shows none (journaled as not
        shown). The replace fence stays set."""
        stop_price = expected_stop if detail.stop_price is None else detail.stop_price
        # trades.stop_price is not moved here (unlike `replace_confirmed`): the new price may
        # never have been shown by Webull, so it stays an expectation on this order row only.
        with self.store.transaction():
            self._apply_detail(old, old_detail, resurrected=False)
            self.store.execute(
                "INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, order_type, "
                "quantity, limit_price, stop_price, send_state, broker_state, "
                "filled_quantity, average_fill_price, broker_order_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (detail.client_order_id, old.trade_id, Leg.STOP, detail.symbol, detail.side,
                 detail.order_type, str(detail.quantity),
                 str(stop_price), SendState.ACKNOWLEDGED, detail.state, str(detail.filled_quantity),
                 None if detail.average_fill_price is None else str(detail.average_fill_price),
                 detail.broker_order_id, self.store.stamp()))
            self.store.append("order-adopted", {"replaces": old.client_order_id,
                                                "stop_price": stop_price,
                                                "stop_price_shown": detail.stop_price is not None,
                                                "state": detail.state},
                              key=detail.client_order_id, run_id=self.run_id)

    # --- shutdown --------------------------------------------------------------------------

    def shutdown_pending(self) -> list[str]:
        """One shutdown pass (spec 11): cancel working entries, keep protecting fills, leave
        stops resting. Returns what still blocks a clean stop (empty = done)."""
        with self.store.transaction():
            self._fence_row(Fence.SHUTTING_DOWN, "", "shutdown in progress")
        for trade in self.trades():
            for order in self.orders(trade.trade_id):
                if order.leg is Leg.ENTRY and order.live:
                    self._cancel(order)
        self.step()
        pending = []
        for trade in self.trades():
            orders = self.orders(trade.trade_id)
            if any(o.live and o.leg is not Leg.STOP for o in orders):
                pending.append(f"{trade.trade_id}: entry or exit still working")
            if self._has_fence(Fence.UNPROTECTED, trade.trade_id):
                pending.append(f"{trade.trade_id}: position without a verified stop")
        return pending

    # --- startup ---------------------------------------------------------------------------

    def recover(self) -> None:
        """Call once at startup before anything else. Fences entries until reconciled."""
        self._set_fence(Fence.STARTUP_RECONCILE, "", "startup reconciliation pending")
        self._clear_fence(Fence.SHUTTING_DOWN)
        for row in self.orders():
            if row.send_state is SendState.INTENT:
                # The attempt was never written, so the broker was never called.
                with self.store.transaction():
                    self._set_send_state(row.client_order_id, SendState.ABSENT,
                                         {"recovered": "never attempted"})
            elif row.send_state is SendState.ATTEMPTED:
                with self.store.transaction():
                    self._set_send_state(row.client_order_id, SendState.UNKNOWN,
                                         {"recovered": "crashed during send"})
                    self._fence_row(Fence.UNKNOWN_ORDER, row.client_order_id,
                                    "crashed during send")
            if row.cancel_state == "requested":
                with self.store.transaction():
                    self.store.execute("UPDATE orders SET cancel_state = 'unknown' "
                                       "WHERE client_order_id = ?", (row.client_order_id,))
                    self.store.append("cancel-unknown", {"recovered": "crashed during cancel"},
                                      key=row.client_order_id, run_id=self.run_id)
                    self._fence_row(Fence.CANCEL_UNKNOWN, row.client_order_id,
                                    "crashed during cancel")
        self.reconcile()

    def reconcile(self) -> bool:
        """Compare broker open orders with our records, then step. Returns False if a read
        failed. The startup fence clears on the first success, whenever that is."""
        self.broker_view = None
        try:
            open_orders = self.broker.open_orders()
        except BrokerError as exc:
            # Its own keyed fence: a positions read that works (step) must not clear it, or
            # entries would go out between reconciles while foreign orders cannot be seen.
            self._set_fence(Fence.BROKER_READ, OPEN_ORDERS_READ,
                            f"open orders read failed: {type(exc).__name__}")
            self.step()  # fills are still observed and protection maintained
            return False
        self._clear_fence(Fence.BROKER_READ, OPEN_ORDERS_READ)
        rows = {o.client_order_id: o for o in self.orders()}
        broker_ids = set()
        for order in open_orders:
            cid = order.client_order_id
            broker_ids.add(cid)
            row = rows.get(cid)
            if row is None:
                self._set_fence(Fence.FOREIGN_ORDER, cid, "open order not in our records")
            elif row.send_state is SendState.UNKNOWN:
                with self.store.transaction():
                    self._apply_detail(row, order, resurrected=False)
                self._clear_fence(Fence.UNKNOWN_ORDER, cid)
            elif not row.live:
                # Ours, working at the broker, but we had given up on it.
                self._resurrect(row, order, "working at the broker but not live in records")
        for name in self.fences():
            fence, _, cid = name.partition(":")
            if fence == Fence.FOREIGN_ORDER and cid not in broker_ids:
                self._clear_fence(Fence.FOREIGN_ORDER, cid)
        self.step()
        if self._has_fence(Fence.BROKER_READ) or self._positions is None:
            return False
        self.broker_view = BrokerView(tuple(self._positions), tuple(open_orders))
        self._clear_fence(Fence.STARTUP_RECONCILE)
        return True
