"""Price increments of the orders the bot sends, shared by the order path and the API's
what-if prices so both show the same number."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

# Minimum price increments of US equities (SEC Regulation NMS Rule 612): one cent at or above
# one dollar, 0.0001 below it. Protocol constants, not tunable numbers.
CENT = Decimal("0.01")
SUB_DOLLAR_TICK = Decimal("0.0001")
ONE_DOLLAR = Decimal(1)


def round_to_tick(price: Decimal) -> Decimal:
    """Down to the valid increment: a limit never above the window, a stop never above the
    strategy's stop (the planned loss is recomputed from the rounded prices)."""
    tick = CENT if price >= ONE_DOLLAR else SUB_DOLLAR_TICK
    return price.quantize(tick, rounding=ROUND_DOWN)
