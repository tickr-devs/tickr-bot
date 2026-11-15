"""The account-v1 document: what one successful reconciliation read from the broker
(positions, then open orders) and whether it agreed with the bot's records (docs/api.md).

Built only from `Engine.broker_view`, which a successful `reconcile()` sets; nothing here calls
the broker. The adapter reads no balance, so equity and buying power are null (unknown).
Account ids never enter the document: the broker rows carry none we copy.
"""

from __future__ import annotations

from datetime import datetime

from aperture.api import contract as api
from aperture.engine.core import Engine, Fence
from aperture.runtime.dayend import records_agree

# Fences that say the broker and the bot's records disagree, or that an order's state is not
# known; any of them means "does not match".
MISMATCH_FENCES = frozenset({Fence.FOREIGN_POSITION, Fence.FOREIGN_ORDER,
                             Fence.QUANTITY_MISMATCH, Fence.RESURRECTED, Fence.UNKNOWN_ORDER,
                             Fence.ORDER_MISSING, Fence.STATUS_UNKNOWN, Fence.OVER_PROTECTED})
NOT_AGREED = ("the broker's positions or open orders differ from what the bot's open trades "
              "and live orders expect")


def account_document(engine: Engine, read_at: datetime) -> api.AccountV1 | None:
    """The document for the latest reconciliation, or None when it did not read the broker."""
    view = engine.broker_view
    if view is None:
        return None
    ours = {o.client_order_id for o in engine.orders()}
    causes = [f"{name}: {cause}" for name, cause, _ in engine.fence_rows()
              if name.split(":", 1)[0] in MISMATCH_FENCES]
    agree = records_agree(engine, view)
    if not agree and not causes:
        causes = [NOT_AGREED]
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
