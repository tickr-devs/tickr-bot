"""Platform-side size check (spec section 7, check 6).

The strategy computes the quantity (owner decision 2026-11-14). The platform never resizes;
it only verifies the quantity against its own `[risk]` limits and refuses on breach. The
platform's per-share risk is the worst fill (top of the price window) to the stop; a strategy
may size on a larger risk (costs, slippage) and so be stricter, never looser.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from tickr.risk.causes import AdmissionCause


@dataclass(frozen=True)
class SizeCheck:
    ok: bool
    cause: AdmissionCause | None
    detail: str
    max_quantity: Decimal | None = None
    planned_loss_usd: Decimal | None = None
    notional_usd: Decimal | None = None


def _floor(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


def max_quantity(
    planned_loss_usd: Decimal,
    risk_per_share: Decimal,
    notional_cap_usd: Decimal,
    entry_high: Decimal,
) -> Decimal:
    """min(floor(planned loss / risk per share), floor(notional cap / entry high)).

    Callers must pass positive `risk_per_share` and `entry_high`; anything else is a
    programming error and raises ValueError rather than producing a size.
    """
    if risk_per_share <= 0 or entry_high <= 0:
        raise ValueError(f"risk_per_share={risk_per_share} and entry_high={entry_high} must be > 0")
    by_loss = _floor(planned_loss_usd / risk_per_share)
    by_notional = _floor(notional_cap_usd / entry_high)
    return min(by_loss, by_notional)


def check_size(
    quantity: Decimal,
    entry_high: Decimal,
    stop: Decimal,
    planned_loss_limit_usd: Decimal,
    notional_cap_usd: Decimal,
) -> SizeCheck:
    """Verify the strategy's quantity; first failure is the cause."""
    risk_per_share = entry_high - stop
    if risk_per_share <= 0 or entry_high <= 0:
        return SizeCheck(False, AdmissionCause.DECISION_INVALID,
                         f"entry_high {entry_high} must be > stop {stop} and > 0")
    if quantity != _floor(quantity):
        return SizeCheck(False, AdmissionCause.QUANTITY_NOT_WHOLE,
                         f"quantity {quantity} is not a whole number of shares")
    cap = max_quantity(planned_loss_limit_usd, risk_per_share, notional_cap_usd, entry_high)
    notional = quantity * entry_high
    planned_loss = quantity * risk_per_share
    values = {"max_quantity": cap, "planned_loss_usd": planned_loss, "notional_usd": notional}
    if quantity < 1:
        return SizeCheck(False, AdmissionCause.QUANTITY_BELOW_ONE,
                         f"quantity {quantity} < 1 (platform maximum {cap})", **values)
    if notional > notional_cap_usd:
        return SizeCheck(False, AdmissionCause.NOTIONAL_OVER_CAP,
                         f"notional {notional} > cap {notional_cap_usd}", **values)
    if planned_loss > planned_loss_limit_usd:
        return SizeCheck(False, AdmissionCause.PLANNED_LOSS_OVER_LIMIT,
                         f"planned loss {planned_loss} > limit {planned_loss_limit_usd}", **values)
    return SizeCheck(True, None, f"quantity {quantity} <= platform maximum {cap}", **values)
