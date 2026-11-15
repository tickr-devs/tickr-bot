"""The account-v1 document: what one successful reconciliation read from the broker
(positions, then open orders) and whether it agreed with the bot's records (docs/api.md).

Built only from `Engine.broker_view`, which a successful `reconcile()` sets; nothing here calls
the broker. The adapter reads no balance, so equity and buying power are null (unknown).
Account ids never enter the document: the broker rows carry none we copy.
"""

from __future__ import annotations

import json
from datetime import datetime

from tickr.api import contract as api
from tickr.engine.core import Engine, Fence
from tickr.runtime.dayend import records_agree

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


def account_document(engine: Engine, read_at: datetime) -> api.AccountV1 | None:
    """The document for the latest reconciliation, or None when it did not read the broker."""
    view = engine.broker_view
    causes = mismatch_causes(engine)
    if view is None or causes is None:
        return None
    ours = {o.client_order_id for o in engine.orders()}
    return api.AccountV1(
        read_at=read_at,
        equity_usd=None,
        buying_power_usd=None,
        positions=tuple(api.AccountPosition(p.symbol, p.quantity, p.average_cost)
                        for p in view.positions if p.quantity != 0),
        orders=tuple(api.AccountOrder(
            client_order_id=o.client_order_id or None, symbol=o.symbol, side=str(o.side),
            type=str(o.order_type), status=str(o.state), quantity=o.quantity,
            limit_price=None, stop_price=o.stop_price, ours=o.client_order_id in ours)
            for o in view.open_orders),
        matches_records=not causes,
        mismatch="; ".join(causes) or None,
    )
