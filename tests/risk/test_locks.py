"""The loss locks (backlog T11) as pure checks and inside admission."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tickr.api.contract import RefusalCode, refusal_of
from tickr.api.reasons import refusal_params
from tickr.risk.admission import NOT_SIMULATED, SIMULATED_CHECKS, admit
from tickr.risk.causes import AdmissionCause as C
from tickr.risk.locks import check_streak, check_weekly

from .conftest import inputs, limits

D = Decimal
# The conftest decision reserves 100 x (10.20 - 9.80) = 40.
PLANNED = D(40)


def test_both_locks_ship_off():
    lim = limits()
    assert (lim.max_weekly_loss_usd, lim.stop_out_streak_limit) == (0, 0)
    # Off, nothing they would read matters: unknown money and a long streak are admitted.
    assert admit(inputs(realized_loss_week_usd=None, stop_out_streak=None), lim).ok
    assert admit(inputs(realized_loss_week_usd=D(10**9), stop_out_streak=99), lim).ok


def test_weekly_ceiling_counts_realized_reserved_and_the_new_trade():
    assert check_weekly(D(900), D(60), D(40), D(1000)).ok  # exactly at the ceiling
    over = check_weekly(D("900.01"), D(60), D(40), D(1000))
    assert over.cause is C.WEEKLY_CEILING
    assert over.detail == ("week realized 900.01 + reserved 60 + new 40 = 1000.01 vs weekly "
                           "ceiling 1000")
    assert check_weekly(D(0), D(0), D(0), D(0)).ok  # 0 = off


@pytest.mark.parametrize(("week", "reserved", "cause"), [
    (None, D(0), C.WEEKLY_MONEY_UNKNOWN),
    (D(0), None, C.WEEKLY_MONEY_UNKNOWN),
    (D(-1), D(0), C.WEEKLY_MONEY_INVALID),
    (D("NaN"), D(0), C.WEEKLY_MONEY_INVALID),
    (D(0), D("Infinity"), C.WEEKLY_MONEY_INVALID),
])
def test_weekly_money_unknown_or_invalid_refuses_never_counts_as_zero(week, reserved, cause):
    assert check_weekly(week, reserved, D(1), D(1000)).cause is cause


def test_streak_lock_refuses_at_its_limit():
    assert check_streak(2, 3).ok
    at = check_streak(3, 3)
    assert at.cause is C.STOP_OUT_STREAK
    assert at.detail == "3 stop-outs in a row today, limit 3"
    assert check_streak(99, 0).ok  # 0 = off


@pytest.mark.parametrize("streak", [None, -1, True, 1.0, "2"])
def test_streak_that_is_not_a_count_refuses(streak):
    assert check_streak(streak, 3).cause is C.COUNT_INVALID


def test_admission_refuses_with_one_cause_per_lock():
    lim = replace(limits(), max_weekly_loss_usd=D(1000), stop_out_streak_limit=3)
    assert admit(inputs(realized_loss_week_usd=D(960)), lim).ok
    weekly = admit(inputs(realized_loss_week_usd=D("960.01")), lim)
    assert (weekly.ok, weekly.cause) == (False, C.WEEKLY_CEILING)
    assert admit(inputs(realized_loss_week_usd=None), lim).cause is C.WEEKLY_MONEY_UNKNOWN
    assert admit(inputs(stop_out_streak=2), lim).ok
    assert admit(inputs(stop_out_streak=3), lim).cause is C.STOP_OUT_STREAK
    # The week's room counts the open reservations as the daily rule does.
    assert admit(inputs(realized_loss_week_usd=D(900), open_reservations_usd=D("60.01")),
                 lim).cause is C.WEEKLY_CEILING


def test_the_daily_ceiling_still_comes_first_and_the_streak_before_the_quote():
    lim = replace(limits(), max_weekly_loss_usd=D(1000), stop_out_streak_limit=3)
    both = inputs(realized_loss_today_usd=lim.max_daily_loss_usd,
                  realized_loss_week_usd=D(1000))
    assert admit(both, lim).cause is C.DAILY_CEILING
    assert admit(inputs(stop_out_streak=3, quote=None), lim).cause is C.STOP_OUT_STREAK


def test_simulations_name_the_locks_as_not_simulated():
    assert "loss_locks" in NOT_SIMULATED
    assert not {"_check_weekly", "_check_stop_out_streak"} & {c.__name__ for c in SIMULATED_CHECKS}


@pytest.mark.parametrize(("cause", "changes", "params"), [
    (C.WEEKLY_CEILING, {"realized_loss_week_usd": D("960.01")},
     {"realized": "960.01", "reserved": "0", "new": "40.00", "total": "1000.01",
      "ceiling": "1000"}),
    (C.WEEKLY_MONEY_UNKNOWN, {"realized_loss_week_usd": None},
     {"missing": "realized loss this week"}),
    (C.STOP_OUT_STREAK, {"stop_out_streak": 3}, {"count": "3", "limit": "3"}),
])
def test_the_api_reads_each_lock_refusal_into_its_code_and_values(cause, changes, params):
    lim = replace(limits(), max_weekly_loss_usd=D(1000), stop_out_streak_limit=3)
    result = admit(inputs(**changes), lim)
    assert result.cause is cause
    refusal = refusal_of(f"{result.cause}: {result.detail}")
    assert refusal.code is RefusalCode(cause)
    assert refusal_params(str(cause), result.detail, None, None) == params
