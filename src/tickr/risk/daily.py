"""Daily loss rule (spec section 7, check 6; owner decision 2026-11-14).

net realized loss today + open reservations + new planned loss <= max_daily_loss_usd, in USD
(the caller nets wins against losses before passing the realized loss).
Unknown money (None) is a refusal, never treated as zero. Negative amounts are refused: the
caller passes losses and reservations as non-negative magnitudes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tickr.risk.causes import AdmissionCause


@dataclass(frozen=True)
class DailyCheck:
    ok: bool
    cause: AdmissionCause | None
    detail: str
    total_usd: Decimal | None = None


def check_daily(
    realized_loss_usd: Decimal | None,
    open_reservations_usd: Decimal | None,
    new_planned_loss_usd: Decimal,
    max_daily_loss_usd: Decimal,
    min_daily_room_usd: Decimal,
) -> DailyCheck:
    if realized_loss_usd is None or open_reservations_usd is None:
        missing = [name for name, value in (("realized loss", realized_loss_usd),
                                            ("open reservations", open_reservations_usd))
                   if value is None]
        return DailyCheck(False, AdmissionCause.DAILY_MONEY_UNKNOWN,
                          f"unknown: {', '.join(missing)}")
    for name, value in (("realized loss", realized_loss_usd),
                        ("open reservations", open_reservations_usd),
                        ("new planned loss", new_planned_loss_usd)):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            return DailyCheck(False, AdmissionCause.DAILY_MONEY_INVALID,
                              f"{name} {value} must be a finite amount >= 0")
    room = max_daily_loss_usd - realized_loss_usd - open_reservations_usd
    if room < min_daily_room_usd:
        return DailyCheck(False, AdmissionCause.DAILY_CEILING,
                          f"room {room} = ceiling {max_daily_loss_usd} - realized "
                          f"{realized_loss_usd} - reserved {open_reservations_usd} is below "
                          f"min_daily_room_usd {min_daily_room_usd}")
    total = realized_loss_usd + open_reservations_usd + new_planned_loss_usd
    detail = (f"realized {realized_loss_usd} + reserved {open_reservations_usd} + "
              f"new {new_planned_loss_usd} = {total} vs ceiling {max_daily_loss_usd}")
    if total > max_daily_loss_usd:
        return DailyCheck(False, AdmissionCause.DAILY_CEILING, detail, total)
    return DailyCheck(True, None, detail, total)
