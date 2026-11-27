"""Loss locks beyond the per-symbol pause (backlog T11): the weekly loss ceiling and the
stop-out streak lock. Pure: the caller counts the money and the streak from stored trades.

Weekly: this New York week's net realized loss (Monday to today, wins offset losses, after the
broker's fees, as the daily rule counts a day) + open reservations + the new planned loss <=
`max_weekly_loss_usd`. Streak: fewer than `stop_out_streak_limit` stop-outs in a row today.
A limit of 0 turns its lock off. Both refuse new entries only; exits and protection never pass
through admission. Unknown money (None) refuses, never treated as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tickr.risk.causes import AdmissionCause

OFF = 0
"""A limit of 0 turns its lock off (config comment of each key)."""


@dataclass(frozen=True)
class LockCheck:
    ok: bool
    cause: AdmissionCause | None
    detail: str


def check_weekly(realized_loss_week_usd: Decimal | None, open_reservations_usd: Decimal | None,
                 new_planned_loss_usd: Decimal, max_weekly_loss_usd: Decimal) -> LockCheck:
    if max_weekly_loss_usd == OFF:
        return LockCheck(True, None, "weekly loss ceiling off")
    if realized_loss_week_usd is None or open_reservations_usd is None:
        missing = [name for name, value in (("realized loss this week", realized_loss_week_usd),
                                            ("open reservations", open_reservations_usd))
                   if value is None]
        return LockCheck(False, AdmissionCause.WEEKLY_MONEY_UNKNOWN,
                         f"unknown: {', '.join(missing)}")
    for name, value in (("realized loss this week", realized_loss_week_usd),
                        ("open reservations", open_reservations_usd),
                        ("new planned loss", new_planned_loss_usd)):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            return LockCheck(False, AdmissionCause.WEEKLY_MONEY_INVALID,
                             f"{name} {value} must be a finite amount >= 0")
    total = realized_loss_week_usd + open_reservations_usd + new_planned_loss_usd
    detail = (f"week realized {realized_loss_week_usd} + reserved {open_reservations_usd} + "
              f"new {new_planned_loss_usd} = {total} vs weekly ceiling {max_weekly_loss_usd}")
    if total > max_weekly_loss_usd:
        return LockCheck(False, AdmissionCause.WEEKLY_CEILING, detail)
    return LockCheck(True, None, detail)


def check_streak(stop_out_streak: int | None, stop_out_streak_limit: int) -> LockCheck:
    if stop_out_streak_limit == OFF:
        return LockCheck(True, None, "stop-out streak lock off")
    streak = stop_out_streak
    if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
        return LockCheck(False, AdmissionCause.COUNT_INVALID,
                         f"stop_out_streak {streak!r} is not a count")
    detail = f"{streak} stop-outs in a row today, limit {stop_out_streak_limit}"
    if streak >= stop_out_streak_limit:
        return LockCheck(False, AdmissionCause.STOP_OUT_STREAK, detail)
    return LockCheck(True, None, detail)
