"""Map a broker status plus quantities to our internal OrderState (spec-v1 section 9).

Anything not listed, or quantities that contradict the status, maps to UNKNOWN: the caller
refuses, keeps polling, sends nothing and blocks entries.
"""

from __future__ import annotations

from decimal import Decimal

from tickr.types import OrderState

_STATUS = {
    "PENDING": OrderState.ACCEPTED,
    "SUBMITTED": OrderState.ACCEPTED,
    "PARTIAL_FILLED": OrderState.PARTIALLY_FILLED,
    "PARTIAL FILLED": OrderState.PARTIALLY_FILLED,  # SDK enum spelling, with a space
    "FILLED": OrderState.FILLED,
    "CANCELLED": OrderState.CANCELLED,
    "FAILED": OrderState.REJECTED,
    "EXPIRED": OrderState.EXPIRED,
}


def map_status(raw: str, quantity: Decimal, filled: Decimal) -> OrderState:
    state = _STATUS.get(raw)
    if state is None or filled < 0 or filled > quantity or quantity <= 0:
        return OrderState.UNKNOWN
    consistent = {
        OrderState.ACCEPTED: filled == 0,
        OrderState.PARTIALLY_FILLED: 0 < filled < quantity,
        OrderState.FILLED: filled == quantity,
        OrderState.CANCELLED: True,
        OrderState.REJECTED: filled == 0,
        OrderState.EXPIRED: True,
    }[state]
    return state if consistent else OrderState.UNKNOWN
