"""The account-v1 document: what one successful reconciliation read from the broker
(positions, then open orders) and whether it agreed with the bot's records, plus the latest
balance read (docs/api.md).

Positions and orders come only from `Engine.broker_view`, which a successful `reconcile()`
sets. Balances, the account's kind and today's fills come from `AccountReads`, which reads them
on its own thread: display only, no limit or decision reads them, and the loop never waits on
them. Account ids never enter the
document: the broker rows carry none we copy.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from tickr.api import contract as api
from tickr.broker.base import (
    AccountBalance,
    AccountSnapshot,
    BrokerError,
    BrokerFill,
    CashTrade,
)
from tickr.calendar import ny_date_of
from tickr.engine.core import Engine, Fence, OrderRow, order_from_row
from tickr.runtime.dayend import records_agree
from tickr.types import Side

# Journal row when a balance read fails: one per stretch of failures (the next success ends it).
BALANCE_READ_FAILED = "balance-read-failed"
# The same for the read of today's fills (order history).
FILLS_READ_FAILED = "fills-read-failed"
# The same for the read of today's trade cash activities (exact amounts).
CASH_READ_FAILED = "cash-read-failed"
# The same for an unexpected error in the display reads' own thread (it keeps running).
ACCOUNT_READS_FAILED = "account-reads-failed"
THREAD_NAME = "tickr-account-reads"


class DisplayReads(Protocol):
    """The broker reads behind the account file's balances and fills: display only."""

    def account(self) -> AccountSnapshot: ...

    def balance(self) -> AccountBalance: ...

    def fills_today(self, day: date) -> list[BrokerFill]: ...

    def cash_trades(self, day: date) -> list[CashTrade]: ...


@dataclass(frozen=True)
class Balances:
    """The latest successful display reads: balance, the account's kind, today's fills."""

    balance: AccountBalance | None = None
    read_at: datetime | None = None
    label: str | None = None
    fills: tuple[BrokerFill, ...] = ()
    fills_read_at: datetime | None = None
    cash: tuple[CashTrade, ...] = ()


class AccountReads:
    """The display reads (balance, the account's kind once, today's fills), made on their own
    daemon thread every `[orders] balance_read_seconds`, so a slow or paging broker route never
    delays the trading loop: the loop only takes the latest snapshot and journals the failures
    collected since its last look. Nothing decides on these numbers. The thread touches no
    engine or store state; a failed read keeps the last good one (the document carries its
    time) and is reported once per stretch of failures."""

    def __init__(self, broker: DisplayReads, interval_seconds: float,
                 now: Callable[[], datetime]) -> None:
        self._broker = broker
        self._interval = interval_seconds
        self._now = now
        self._lock = threading.Lock()
        self._snapshot = Balances()
        self._label_read = False  # the accounts list was read once (the label may be None)
        self._failing: dict[str, bool] = {}
        self._failures: list[tuple[str, dict[str, str]]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
            self._thread.start()

    def stop(self, timeout_seconds: float) -> None:
        """Ask the thread to end and wait at most `timeout_seconds` (a read in flight ends
        by its own HTTP timeout; the thread is a daemon either way)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.read_once()
            except Exception as exc:  # noqa: BLE001 - the display thread must outlive a bug
                self._report(ACCOUNT_READS_FAILED, exc)
            self._stop.wait(self._interval)

    def read_once(self) -> None:
        """One round of reads (called by the thread; tests call it directly)."""
        now = self._now()
        if not self._label_read:
            try:
                label = self._broker.account().label
            except BrokerError:
                pass  # display only: tried again next round
            else:
                self._label_read = True
                self._update(label=label)
        try:
            balance = self._broker.balance()
        except BrokerError as exc:
            self._report(BALANCE_READ_FAILED, exc)
        else:
            self._recovered(BALANCE_READ_FAILED)
            self._update(balance=balance, read_at=now)
        try:
            fills = self._broker.fills_today(ny_date_of(now))
        except BrokerError as exc:
            self._report(FILLS_READ_FAILED, exc)
        else:
            self._recovered(FILLS_READ_FAILED)
            self._update(fills=tuple(fills), fills_read_at=now)
        try:
            cash = self._broker.cash_trades(ny_date_of(now))
        except BrokerError as exc:
            self._report(CASH_READ_FAILED, exc)
        else:
            self._recovered(CASH_READ_FAILED)
            self._update(cash=tuple(cash))

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def _report(self, kind: str, exc: Exception) -> None:
        with self._lock:
            if not self._failing.get(kind):
                self._failures.append((kind, {"error": type(exc).__name__, "detail": str(exc)}))
            self._failing[kind] = True

    def _recovered(self, kind: str) -> None:
        with self._lock:
            self._failing[kind] = False

    def snapshot(self) -> Balances:
        with self._lock:
            return self._snapshot

    def journal_failures(self, engine: Engine) -> None:
        """On the loop thread: journal the failures reported since the last call."""
        with self._lock:
            failures, self._failures = self._failures, []
        if failures:
            with engine.store.transaction():
                for kind, body in failures:
                    engine.store.append(kind, body, run_id=engine.run_id)


# Fences that say the broker and the bot's records disagree, or that an order's state is not
# known; any of them means "does not match".
MISMATCH_FENCES = frozenset({Fence.FOREIGN_POSITION, Fence.FOREIGN_ORDER,
                             Fence.QUANTITY_MISMATCH, Fence.RESURRECTED, Fence.UNKNOWN_ORDER,
                             Fence.ORDER_MISSING, Fence.STATUS_UNKNOWN, Fence.OVER_PROTECTED})
# Journal row after a successful reconciliation: `{matches_records, mismatch}` as in account-v1.
# Written when the answer differs from the last such row, else at most once per
# `[orders] reconcile_proof_seconds`; the durable record that the broker and the bot's records
# agreed (or not) through the day, read by api.cleanday. Nothing in the bot reads it.
RECONCILE_CHECKED = "reconcile-checked"
NOT_AGREED = ("the broker's positions or open orders differ from what the bot's open trades "
              "and live orders expect")


def _fence_causes(engine: Engine) -> list[str]:
    return [f"{name}: {cause}" for name, cause, _ in engine.fence_rows()
            if name.split(":", 1)[0] in MISMATCH_FENCES]


def mismatch_causes(engine: Engine) -> list[str] | None:
    """Why the latest reconciliation does not match the records ([] when it matches), or None
    when it did not read the broker."""
    view = engine.broker_view
    if view is None:
        return None
    causes = _fence_causes(engine)
    if not records_agree(engine, view) and not causes:
        causes = [NOT_AGREED]
    return causes


def record_reconcile(engine: Engine, disagreed_before: bool) -> bool:
    """After a successful reconciliation: journal `reconcile-checked` when its answer
    (matches or not) differs from the last such row, or that row is at least
    `[orders] reconcile_proof_seconds` old by journal time (the store clock). Each change is
    recorded at once; an unchanged answer at most once per interval.

    A disagreement is recorded only once confirmed: a mismatch fence is set, or the records
    disagreed on this and the previous successful reconciliation (`disagreed_before`). One
    pass alone can disagree for a moment: reconcile reads open orders, then its step places
    the stop for a fill it just saw, so the stop is live in the records but not in that read.
    Returns whether the records disagreed this pass (False when it did not read the broker);
    the caller passes it back on the next successful reconciliation."""
    view = engine.broker_view
    if view is None:
        return False
    causes = _fence_causes(engine)
    disagree = not records_agree(engine, view)
    if disagree and not causes:
        if not disagreed_before:
            return True  # not confirmed yet: nothing recorded for this pass
        causes = [NOT_AGREED]
    matches = not causes
    store = engine.store
    last = store.query("SELECT at_utc, body FROM journal WHERE kind = ? ORDER BY seq DESC "
                       "LIMIT 1", (RECONCILE_CHECKED,))
    if last and json.loads(last[0]["body"]).get("matches_records") is matches:
        since = (datetime.fromisoformat(store.stamp())
                 - datetime.fromisoformat(last[0]["at_utc"])).total_seconds()
        if since < engine.config.orders.reconcile_proof_seconds:
            return disagree
    with store.transaction():
        store.append(RECONCILE_CHECKED, {"matches_records": matches,
                                         "mismatch": "; ".join(causes) or None},
                     run_id=engine.run_id)
    return disagree


# One US cent (unit factor): the order rows state prices to the cent (probe 2026-11-16).
CENT = Decimal("0.01")


def exact_amount(fill: BrokerFill, cash: tuple[CashTrade, ...],
                 fills: tuple[BrokerFill, ...]) -> Decimal | None:
    """The exact signed cash amount of `fill` (fees included; a buy is negative): the one
    TRADE cash activity of its symbol and side at its fill time (`biz_time` equals
    `filled_time_at`, probe 2026-11-16), accepted only when the price it implies, to the
    cent, is the order row's `filled_price`. None otherwise: before the activity is listed,
    or when an order filled in several executions lists one cash row per execution (unproven)
    and the row at the fill time covers only part of it. The order row's rounded price is
    then all there is. Stricter still: the day's cash rows of the fill's symbol and side
    must be as many as the day's filled orders of that symbol and side (`fills`), so a
    missing part too small to move the price by a cent is caught as well."""
    if fill.filled_at is None or fill.filled_price is None:
        return None
    rows = [c for c in cash if c.symbol == fill.symbol and c.side == fill.side]
    orders = [f for f in fills if f.symbol == fill.symbol and f.side == fill.side]
    if len(rows) != len(orders):
        return None
    found = [c for c in rows if c.at == fill.filled_at]
    if len(found) != 1:
        return None
    price = exact_price(fill, found[0].net_amount)
    if price is None or price.quantize(CENT, rounding=ROUND_HALF_UP) != fill.filled_price:
        return None
    return found[0].net_amount


def exact_price(fill: BrokerFill, amount: Decimal | None) -> Decimal | None:
    """The fill's average price from a cash amount and the fill's fees: |amount| less the
    fees on a buy, plus them on a sell, over the filled quantity. None when either is
    unknown."""
    if amount is None or fill.fees is None or fill.filled_quantity <= 0:
        return None
    gross = -amount - fill.fees if amount < 0 else amount + fill.fees
    return gross / fill.filled_quantity


def exact_fill(order: OrderRow, fill: BrokerFill, snapshot: Balances,
               ) -> tuple[Decimal, dict[str, Any]] | None:
    """The exact average fill price of our finished `order` and the evidence for it, or None
    while it cannot be proven. `fill` is the order history's row with the order's client order
    id. Proven only when: the order is finished at the broker (no more fills can come); the
    history row shows the bot's filled quantity; `exact_amount` accepts a cash activity for it
    against the snapshot's cash rows and fills (one row at the fill time, as many rows of the
    symbol and side as filled orders, implied price to the cent = the history row's price),
    with the sign of the order's side; its fees are known; and the derived price, to the
    cent, is also the order's own recorded average fill price. So a cash row that is only one
    of several executions (unproven whether Webull writes one row per execution) is never
    taken for the whole fill."""
    if (order.broker_state is None or not order.broker_state.terminal
            or order.filled_quantity <= 0 or order.average_fill_price is None
            or fill.filled_quantity != order.filled_quantity
            or fill.side != str(order.side)):
        return None
    amount = exact_amount(fill, snapshot.cash, snapshot.fills)
    if amount is None or (amount < 0) != (order.side is Side.BUY) or amount == 0:
        return None
    price = exact_price(fill, amount)
    if (price is None or price <= 0
            or price.quantize(CENT, rounding=ROUND_HALF_UP) != order.average_fill_price):
        return None
    return price, {"net_amount": amount, "fees": fill.fees,
                   "filled_quantity": fill.filled_quantity,
                   "average_fill_price": order.average_fill_price, "cash_at": fill.filled_at}


def record_exact_fills(engine: Engine, snapshot: Balances) -> int:
    """On the loop thread (the display thread never writes): record the exact fill price of
    each of our orders in the display reads' latest fills that has none yet and can be proven
    (`exact_fill`). An order whose cash activity is late is tried again on a later snapshot;
    one never proven keeps its rounded price. Returns how many were recorded."""
    by_id: dict[str, list[BrokerFill]] = {}
    for fill in snapshot.fills:
        if fill.client_order_id is not None:
            by_id.setdefault(fill.client_order_id, []).append(fill)
    if not by_id:
        return 0
    marks = ", ".join("?" for _ in by_id)
    rows = engine.store.query(f"SELECT * FROM orders WHERE client_order_id IN ({marks}) "
                              "AND exact_fill_price IS NULL", tuple(by_id))
    recorded = 0
    for order in map(order_from_row, rows):
        fills = by_id[order.client_order_id]
        if len(fills) != 1:
            continue  # the history lists the order twice: not one fill to prove
        found = exact_fill(order, fills[0], snapshot)
        if found is not None and engine.record_exact_fill_price(order.client_order_id, *found):
            recorded += 1
    return recorded


def account_document(engine: Engine, read_at: datetime,
                     balances: Balances | None) -> api.AccountV1 | None:
    """The document for the latest reconciliation, or None when it did not read the broker.
    `balances` None: no display reads run (their fields stay null and the fills empty)."""
    balances = balances if balances is not None else Balances()
    view = engine.broker_view
    causes = mismatch_causes(engine)
    if view is None or causes is None:
        return None
    ours = {o.client_order_id for o in engine.orders()}
    b = balances.balance
    return api.AccountV1(
        read_at=read_at,
        equity_usd=None if b is None else b.net_liquidation,
        buying_power_usd=None if b is None else b.day_buying_power,
        positions=tuple(api.AccountPosition(p.symbol, p.quantity, p.average_cost,
                                            p.last_price, p.market_value,
                                            p.unrealized_profit_loss, p.day_profit_loss)
                        for p in view.positions if p.quantity != 0),
        orders=tuple(api.AccountOrder(
            client_order_id=o.client_order_id or None, symbol=o.symbol, side=str(o.side),
            type=str(o.order_type), status=str(o.state), quantity=o.quantity,
            limit_price=o.limit_price, stop_price=o.stop_price, ours=o.client_order_id in ours)
            for o in view.open_orders),
        matches_records=not causes,
        mismatch="; ".join(causes) or None,
        balance_read_at=balances.read_at,
        cash_usd=None if b is None else b.cash,
        positions_value_usd=None if b is None else b.market_value,
        unrealized_pl_usd=None if b is None else b.unrealized_profit_loss,
        day_pl_usd=None if b is None else b.day_profit_loss,
        overnight_buying_power_usd=None if b is None else b.overnight_buying_power,
        maintenance_margin_usd=None if b is None else b.maintenance_margin,
        margin_calls=None if b is None else b.margin_calls,
        day_trades_left=None if b is None else b.day_trades_left,
        account_label=balances.label,
        fills_read_at=balances.fills_read_at,
        fills=tuple(_fill(f, balances, f.client_order_id in ours) for f in balances.fills),
    )


def _fill(f: BrokerFill, balances: Balances, ours: bool) -> api.AccountFill:
    amount = exact_amount(f, balances.cash, balances.fills)
    return api.AccountFill(
        client_order_id=f.client_order_id, symbol=f.symbol, side=f.side, type=f.order_type,
        quantity=f.filled_quantity, price=f.filled_price, filled_at=f.filled_at,
        fees_usd=f.fees, ours=ours, net_amount_usd=amount, exact_price=exact_price(f, amount))
