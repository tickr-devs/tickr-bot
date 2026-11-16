"""The account-v1 document: what one successful reconciliation read from the broker
(positions, then open orders) and whether it agreed with the bot's records, plus the latest
balance read (docs/api.md).

Positions and orders come only from `Engine.broker_view`, which a successful `reconcile()`
sets. `Balances` reads the account's balance (and, once, its kind) after a successful
reconciliation, at most once per `[orders] balance_read_seconds`: display only, no limit or
decision reads it, and a failed read never stops the loop. Account ids never enter the
document: the broker rows carry none we copy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from tickr.api import contract as api
from tickr.broker.base import AccountBalance, BrokerError
from tickr.engine.core import Engine, Fence
from tickr.runtime.dayend import records_agree
from tickr.runtime.protection import busy

# Journal row when a balance read fails: one per stretch of failures (the next success ends it).
BALANCE_READ_FAILED = "balance-read-failed"


@dataclass
class Balances:
    """The latest successful balance read and the account's kind, refreshed by `refresh`."""

    balance: AccountBalance | None = None
    read_at: datetime | None = None
    label: str | None = None
    _label_read: bool = False  # the accounts list was read once (label may still be None)
    _attempt_at: datetime | None = None
    _failing: bool = False

    def refresh(self, engine: Engine, now: datetime) -> None:
        """Read the balance when the last attempt is at least `[orders] balance_read_seconds`
        old, and the account's kind until one accounts read succeeds. Skipped while a trade
        needs the loop (`protection.busy`: an entry working, an exit in progress, a fill not
        yet covered by a verified stop), so a slow balance route never delays protection.
        A failed balance read keeps the last good one (the document carries its time) and is
        journaled once per stretch; a failed accounts read only leaves the kind unknown."""
        interval = timedelta(seconds=float(engine.config.orders.balance_read_seconds))
        if self._attempt_at is not None and now - self._attempt_at < interval:
            return
        if busy(engine) is not None:
            return
        self._attempt_at = now
        if not self._label_read:
            try:
                self.label = engine.broker.account().label
                self._label_read = True
            except BrokerError:
                pass  # display only: tried again at the next balance read
        try:
            balance = engine.broker.balance()
        except BrokerError as exc:
            if not self._failing:
                with engine.store.transaction():
                    engine.store.append(BALANCE_READ_FAILED,
                                        {"error": type(exc).__name__, "detail": str(exc)},
                                        run_id=engine.run_id)
            self._failing = True
            return
        self._failing = False
        self.balance, self.read_at = balance, now


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


def account_document(engine: Engine, read_at: datetime,
                     balances: Balances) -> api.AccountV1 | None:
    """The document for the latest reconciliation, or None when it did not read the broker."""
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
        positions=tuple(api.AccountPosition(p.symbol, p.quantity, p.average_cost)
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
    )
