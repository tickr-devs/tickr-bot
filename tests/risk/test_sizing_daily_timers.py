from __future__ import annotations

from datetime import date, datetime

import pytest

from tickr.calendar import NEW_YORK, CalendarCause, CalendarRefused
from tickr.risk.causes import AdmissionCause as C
from tickr.risk.daily import check_daily
from tickr.risk.sizing import check_size, max_quantity
from tickr.risk.timers import deadlines, deadlines_for

from .conftest import CALENDAR, CONFIG, D


@pytest.mark.parametrize(("risk", "entry_high", "expected"), [
    (D("0.40"), D("10.20"), D("125")),    # planned loss binds: floor(50 / 0.40)
    (D("0.10"), D("10.20"), D("245")),    # notional binds: floor(2500 / 10.20)
    (D("0.33"), D("1"), D("151")),        # floor(151.51...)
    (D("60"), D("100"), D("0")),          # one share would lose more than planned
])
def test_max_quantity(risk, entry_high, expected):
    assert max_quantity(D(50), risk, D(2500), entry_high) == expected


@pytest.mark.parametrize(("risk", "entry_high"), [(D(0), D(10)), (D(1), D(0)), (D(-1), D(10))])
def test_max_quantity_rejects_non_positive(risk, entry_high):
    with pytest.raises(ValueError):
        max_quantity(D(50), risk, D(2500), entry_high)


def test_check_size_reports_values():
    ok = check_size(D(100), D("10.20"), D("9.80"), D(50), D(2500))
    assert ok.ok and ok.max_quantity == D(125)
    assert ok.planned_loss_usd == D("40.00") and ok.notional_usd == D("1020.00")


def test_check_size_refusals():
    assert check_size(D(1), D("10"), D("10"), D(50), D(2500)).cause is C.DECISION_INVALID
    assert check_size(D("1.5"), D("10"), D("9"), D(50), D(2500)).cause is C.QUANTITY_NOT_WHOLE
    assert check_size(D(0), D("10"), D("9"), D(50), D(2500)).cause is C.QUANTITY_BELOW_ONE
    assert check_size(D(-1), D("10"), D("9"), D(50), D(2500)).cause is C.QUANTITY_BELOW_ONE
    assert check_size(D(251), D("10"), D("9.9"), D(50), D(2500)).cause is C.NOTIONAL_OVER_CAP
    assert check_size(D(51), D("10"), D("9"), D(50), D(2500)).cause is C.PLANNED_LOSS_OVER_LIMIT


def test_daily_rule():
    ok = check_daily(D(100), D(50), D(50), D(300), D(0))
    assert ok.ok and ok.total_usd == D(200)
    assert check_daily(D(250), D(0), D(50), D(300), D(0)).ok
    over = check_daily(D(250), D("0.01"), D(50), D(300), D(0))
    assert over.cause is C.DAILY_CEILING and over.total_usd == D("300.01")


def test_daily_room_below_the_minimum_is_the_ceiling():
    # 300 - 280 - 11 = 9 USD of room, below the 10 USD minimum: refused even for a 1 USD trade.
    short = check_daily(D(280), D(11), D(1), D(300), D(10))
    assert short.cause is C.DAILY_CEILING and "min_daily_room_usd" in short.detail
    assert check_daily(D(280), D(10), D(10), D(300), D(10)).ok


def test_daily_unknown_money_is_never_zero():
    assert check_daily(None, D(0), D(1), D(300), D(0)).cause is C.DAILY_MONEY_UNKNOWN
    assert check_daily(D(0), None, D(1), D(300), D(0)).cause is C.DAILY_MONEY_UNKNOWN
    both = check_daily(None, None, D(1), D(300), D(0))
    assert "realized loss" in both.detail and "open reservations" in both.detail


@pytest.mark.parametrize("bad", [D(-1), D("NaN"), D("Infinity"), 0.0])
def test_daily_invalid_money(bad):
    assert check_daily(bad, D(0), D(1), D(300), D(0)).cause is C.DAILY_MONEY_INVALID
    assert check_daily(D(0), D(0), bad, D(300), D(0)).cause is C.DAILY_MONEY_INVALID


def test_deadlines_normal_day():
    times = deadlines_for(CALENDAR, date(2026, 11, 16), CONFIG.session)
    assert times.last_entry == datetime(2026, 11, 16, 15, 35, tzinfo=NEW_YORK)
    assert times.flatten == datetime(2026, 11, 16, 15, 45, tzinfo=NEW_YORK)


def test_deadlines_half_day():
    times = deadlines(CALENDAR.day(date(2026, 11, 27)), 15, 5)
    assert times.last_entry == datetime(2026, 11, 27, 12, 45, tzinfo=NEW_YORK)
    assert times.flatten == datetime(2026, 11, 27, 12, 55, tzinfo=NEW_YORK)


def test_deadlines_across_dst():
    summer = deadlines_for(CALENDAR, date(2026, 10, 30), CONFIG.session)
    winter = deadlines_for(CALENDAR, date(2026, 11, 2), CONFIG.session)
    assert summer.flatten.utcoffset() != winter.flatten.utcoffset()
    assert summer.flatten.timetz().replace(tzinfo=None) == winter.flatten.time()


@pytest.mark.parametrize(("day", "cause"), [
    (date(2026, 11, 26), CalendarCause.HOLIDAY),
    (date(2026, 11, 14), CalendarCause.WEEKEND),
    (date(2028, 1, 4), CalendarCause.OUT_OF_COVERAGE),
])
def test_deadlines_refuse_unknown_days(day, cause):
    with pytest.raises(CalendarRefused) as exc:
        deadlines_for(CALENDAR, day, CONFIG.session)
    assert exc.value.cause is cause
