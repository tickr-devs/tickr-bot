from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tickr.api.contract import RefusalCode, refusal_of
from tickr.calendar import NEW_YORK, CalendarCause, CalendarRefused
from tickr.config import GateResult
from tickr.risk.admission import CHECKS, admit
from tickr.risk.causes import AdmissionCause as C
from tickr.strategy.base import Verdict

from .conftest import CALENDAR, NOW, D, decision, inputs, quote

RECON = inputs().reconciliation
TODAY = NOW.date()


def test_baseline_is_admitted(lim):
    result = admit(inputs(), lim)
    assert result.ok and result.cause is None
    # 100 shares x (10.20 - 9.80)
    assert result.planned_loss_usd == D("40.00")


def test_limits_come_from_config(lim):
    assert lim.planned_loss_usd == D(50)
    assert lim.position_notional_cap_usd == D(2500)
    assert lim.max_daily_loss_usd == D(300)
    assert lim.max_open_positions == 1
    assert lim.max_quote_age_seconds == D(5)
    assert lim.max_clock_skew_seconds == D(1)
    assert lim.active_strategy == "price-action-v3"
    assert lim.symbol_cooldown_minutes_after_loss == 30


def test_cooldown_boundaries(lim):
    #: one second before its end refused, exactly at its end admitted.
    end = NOW - timedelta(minutes=30)
    assert admit(inputs(symbol_last_loss_at=end + timedelta(seconds=1)), lim).cause \
        is C.SYMBOL_COOLDOWN
    assert admit(inputs(symbol_last_loss_at=end), lim).ok
    assert admit(inputs(symbol_last_loss_at=end.astimezone(UTC)), lim).ok


def test_cooldown_turned_off(lim):
    just_lost = inputs(symbol_last_loss_at=NOW)
    assert admit(just_lost, lim).cause is C.SYMBOL_COOLDOWN
    assert admit(just_lost, replace(lim, symbol_cooldown_minutes_after_loss=None)).ok


def test_there_are_seven_checks():
    assert len(CHECKS) == 7


def _at(hh: int, mm: int, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEW_YORK)


def _moved(now: datetime, **changes):
    """Inputs at another instant with broker read and quote kept fresh."""
    recon = replace(RECON, last_broker_read_at=now - timedelta(seconds=1))
    q = quote(provider_time=now - timedelta(seconds=1), receipt_time=now)
    base = dict(now=now, reconciliation=recon, quote=q)
    base.update(changes)
    return inputs(**base)


REFUSALS = [
    # 1
    (C.GATE_CLOSED, lambda: inputs(gate=GateResult(False, "disabled"))),
    # 2
    (C.CLOCK_INVALID, lambda: inputs(now=NOW.replace(tzinfo=None))),
    (C.RECONCILIATION_NOT_DONE, lambda: inputs(reconciliation=replace(RECON, startup_done=False))),
    (C.BROKER_READ_MISSING,
     lambda: inputs(reconciliation=replace(RECON, last_broker_read_at=None))),
    (C.BROKER_READ_STALE, lambda: inputs(reconciliation=replace(
        RECON, last_broker_read_at=NOW - timedelta(seconds=31)))),
    (C.CLOCK_INVALID, lambda: inputs(reconciliation=replace(
        RECON, last_broker_read_at=NOW + timedelta(seconds=1)))),
    (C.CLOCK_INVALID, lambda: inputs(reconciliation=replace(
        RECON, last_broker_read_at=datetime(2026, 11, 16, 15, 0)))),
    (C.UNRESOLVED_EXPOSURE,
     lambda: inputs(reconciliation=replace(RECON, unresolved_exposure=True))),
    (C.FOREIGN_EXPOSURE, lambda: inputs(reconciliation=replace(RECON, foreign_exposure=True))),
    (C.ENTRY_STOP_SET, lambda: inputs(entry_stop=True)),
    # 3
    (C.CALENDAR_UNKNOWN, lambda: inputs(
        session=CalendarRefused(CalendarCause.OUT_OF_COVERAGE, "not covered"))),
    (C.CALENDAR_UNKNOWN, lambda: inputs(session=None)),
    (C.CALENDAR_DAY_MISMATCH, lambda: inputs(session=CALENDAR.day(date(2026, 11, 17)))),
    (C.HALF_DAY, lambda: _moved(_at(10, 0, date(2026, 11, 27)),
                                session=CALENDAR.day(date(2026, 11, 27)))),
    (C.OUTSIDE_REGULAR_SESSION, lambda: _moved(_at(9, 29))),
    (C.OUTSIDE_REGULAR_SESSION, lambda: _moved(_at(16, 0))),
    (C.AFTER_LAST_ENTRY, lambda: _moved(_at(15, 45))),
    # 4
    (C.COUNT_INVALID, lambda: inputs(own_working_orders=-1)),
    (C.COUNT_INVALID, lambda: inputs(open_positions=True)),
    (C.OWN_POSITION_OPEN, lambda: inputs(own_open_positions=1, open_positions=1)),
    (C.OWN_ORDER_WORKING, lambda: inputs(own_working_orders=1)),
    (C.POSITION_LIMIT_REACHED, lambda: inputs(open_positions=1)),
    (C.SYMBOL_COOLDOWN, lambda: inputs(symbol_last_loss_at=NOW - timedelta(minutes=29))),
    (C.SYMBOL_COOLDOWN, lambda: inputs(symbol_last_loss_at=NOW + timedelta(seconds=1))),
    (C.CLOCK_INVALID, lambda: inputs(symbol_last_loss_at=datetime(2026, 11, 16, 9, 45))),
    # 5
    (C.DECISION_NOT_ENTER, lambda: inputs(decision=decision(verdict=Verdict.WATCH))),
    (C.DECISION_NOT_ACTIVE, lambda: inputs(decision=decision(strategy="observer-x"))),
    (C.DECISION_INCOMPLETE, lambda: inputs(decision=decision(stop=None))),
    (C.ORDER_PRICES_MISSING, lambda: inputs(limit_price=None)),
    (C.ORDER_PRICES_MISSING, lambda: inputs(stop_price=None)),
    (C.ORDER_PRICES_INVALID, lambda: inputs(limit_price=D("10.21"))),
    (C.ORDER_PRICES_INVALID, lambda: inputs(limit_price=D("9.99"))),
    (C.ORDER_PRICES_INVALID, lambda: inputs(stop_price=D("10.20"))),
    (C.ORDER_PRICES_INVALID, lambda: inputs(stop_price=D("0"))),
    (C.ORDER_PRICES_INVALID, lambda: inputs(stop_price=D("NaN"))),
    (C.DECISION_INCOMPLETE, lambda: inputs(decision=decision(quantity=None))),
    (C.DECISION_INVALID, lambda: inputs(decision=decision(stop=D("10.00")))),
    (C.DECISION_INVALID, lambda: inputs(decision=decision(entry_low=D("10.30")))),
    (C.DECISION_INVALID, lambda: inputs(decision=decision(entry_high=D("NaN")))),
    (C.DECISION_INVALID, lambda: inputs(decision=decision(quantity=100.0))),
    (C.SETUP_INVALIDATED, lambda: inputs(setup_valid=False)),
    (C.QUOTE_MISSING, lambda: inputs(quote=None)),
    (C.QUOTE_SYMBOL_MISMATCH, lambda: inputs(quote=quote(symbol="WXYZ"))),
    (C.QUOTE_INVALID, lambda: inputs(quote=quote(bid=D("10.11")))),
    (C.QUOTE_INVALID, lambda: inputs(quote=quote(bid=D("0")))),
    (C.QUOTE_INVALID, lambda: inputs(quote=quote(ask=D("Infinity")))),
    (C.QUOTE_TIME_INVALID, lambda: inputs(quote=quote(
        provider_time=NOW + timedelta(seconds=1.001)))),
    (C.QUOTE_TIME_INVALID, lambda: inputs(quote=quote(receipt_time=NOW + timedelta(seconds=1)))),
    (C.QUOTE_TIME_INVALID, lambda: inputs(quote=quote(receipt_time=datetime(2026, 11, 16)))),
    (C.QUOTE_STALE, lambda: inputs(quote=quote(provider_time=NOW - timedelta(seconds=5.001)))),
    (C.PRICE_OUTSIDE_WINDOW, lambda: inputs(quote=quote(ask=D("10.21")))),
    (C.PRICE_OUTSIDE_WINDOW, lambda: inputs(quote=quote(bid=D("9.98"), ask=D("9.99")))),
    # Ask inside the strategy window but above the rounded-down limit that will be sent.
    (C.PRICE_OUTSIDE_WINDOW, lambda: inputs(limit_price=D("10.15"),
                                            quote=quote(ask=D("10.17")))),
    # 6
    (C.QUANTITY_NOT_WHOLE, lambda: inputs(decision=decision(quantity=D("10.5")))),
    (C.QUANTITY_BELOW_ONE, lambda: inputs(decision=decision(quantity=D("0")))),
    (C.NOTIONAL_OVER_CAP, lambda: inputs(decision=decision(quantity=D("246")))),
    (C.PLANNED_LOSS_OVER_LIMIT, lambda: inputs(decision=decision(quantity=D("126")))),
    (C.DAILY_MONEY_UNKNOWN, lambda: inputs(realized_loss_today_usd=None)),
    (C.DAILY_MONEY_UNKNOWN, lambda: inputs(open_reservations_usd=None)),
    (C.DAILY_MONEY_INVALID, lambda: inputs(realized_loss_today_usd=D("-5"))),
    (C.DAILY_CEILING, lambda: inputs(realized_loss_today_usd=D("200"),
                                     open_reservations_usd=D("60.01"))),
    # 7
    (C.ENTRY_BUDGET_UNAVAILABLE, lambda: inputs(entry_budget_available=False)),
    (C.STOP_BUDGET_UNAVAILABLE, lambda: inputs(stop_budget_available=False)),
]


@pytest.mark.parametrize(("cause", "make"), REFUSALS,
                         ids=[f"{c}-{n}" for n, (c, _) in enumerate(REFUSALS)])
def test_each_refusal_has_its_cause(lim, cause, make):
    result = admit(make(), lim)
    assert not result.ok
    assert result.cause is cause, result.detail
    assert result.detail
    assert result.planned_loss_usd is None


def test_every_cause_is_exercised():
    # SESSION_TIMES_INVALID needs other limits; see test_invalid_session_offsets_refuse.
    assert {c for c, _ in REFUSALS} | {C.SESSION_TIMES_INVALID} == set(C)


def _recorded(result) -> str:
    """The cause as runtime/entries.py records it in the decision file."""
    return f"{result.cause}: {result.detail}"


ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}")
TIMED = {C.SYMBOL_COOLDOWN: (True, True), C.AFTER_LAST_ENTRY: (True, False)}


@pytest.mark.parametrize(("cause", "make"), REFUSALS,
                         ids=[f"{c}-{n}" for n, (c, _) in enumerate(REFUSALS)])
def test_the_api_reads_every_recorded_refusal(lim, cause, make):
    """GET /skipped and /decisions read each refusal admission writes into its exact code, a
    message without raw timestamps, and its times as fields (never UNKNOWN)."""
    result = admit(make(), lim)
    refusal = refusal_of(_recorded(result))
    assert refusal.code is RefusalCode(cause), refusal
    assert refusal.message and not ISO_TIME.search(refusal.message), refusal.message
    has_since, has_until = TIMED.get(cause, (False, False))
    assert (refusal.since is not None, refusal.until is not None) == (has_since, has_until)
    if cause is C.SYMBOL_COOLDOWN:
        assert refusal.until - refusal.since == timedelta(minutes=30)
        assert refusal.message == "ABCD closed at a loss; no new entry in it during the cooldown"


def test_the_api_reads_session_times_invalid(lim):
    bad = replace(lim, last_entry_minutes_before_close=5, flatten_minutes_before_close=15)
    refusal = refusal_of(_recorded(admit(inputs(), bad)))
    assert refusal.code is RefusalCode.SESSION_TIMES_INVALID and refusal.message


def test_cooldown_times_are_read_exactly(lim):
    lost = NOW - timedelta(minutes=29, seconds=1, microseconds=250000)
    refusal = refusal_of(_recorded(admit(inputs(symbol_last_loss_at=lost), lim)))
    assert (refusal.since, refusal.until) == (lost, lost + timedelta(minutes=30))


def test_after_last_entry_time_is_read(lim):
    refusal = refusal_of(_recorded(admit(_moved(_at(15, 50)), lim)))
    assert refusal.since == _at(15, 45)
    assert refusal.message == "the session's last entry time had passed"


def test_outside_session_message_keeps_the_session_hours(lim):
    refusal = refusal_of(_recorded(admit(_moved(_at(9, 29)), lim)))
    assert refusal.message == "outside the regular session 09:30:00-16:00:00 New York"


def test_boundaries_admit(lim):
    # Quote exactly at max age, ask exactly at the window top, broker read exactly at max age.
    edge = inputs(quote=quote(provider_time=NOW - timedelta(seconds=5), ask=D("10.20")),
                  reconciliation=replace(RECON, last_broker_read_at=NOW - timedelta(seconds=30)))
    assert admit(edge, lim).ok
    # Ask at the window bottom; last minute before last entry; first minute of the session.
    assert admit(inputs(quote=quote(bid=D("9.99"), ask=D("10.00"))), lim).ok
    assert admit(_moved(_at(15, 44)), lim).ok
    assert admit(_moved(_at(9, 30)), lim).ok


def test_daily_ceiling_exactly_reached_is_admitted(lim):
    # 200 + 60 + 40 == 300
    result = admit(inputs(realized_loss_today_usd=D("200"), open_reservations_usd=D("60")), lim)
    assert result.ok


def test_size_limits_exactly_at_cap_are_admitted(lim):
    # floor(2500 / 10.20) = 245 shares; risk 0.40 x 245 = 98 > 50, so widen nothing: use a
    # tighter stop so the notional cap binds.
    d = decision(stop=D("10.00"), entry_low=D("10.10"), quantity=D("245"))
    assert admit(inputs(decision=d, stop_price=D("10.00")), lim).ok
    # floor(50 / 0.40) = 125 shares: the planned-loss limit binds.
    assert admit(inputs(decision=decision(quantity=D("125"))), lim).ok


def test_half_day_admitted_when_enabled(lim):
    day = date(2026, 11, 27)
    result = admit(_moved(_at(10, 0, day), session=CALENDAR.day(day)),
                   replace(lim, half_days_enabled=True))
    assert result.ok
    # Half-day close is 13:00 New York, so last entry is 12:45.
    refused = admit(_moved(_at(12, 45, day), session=CALENDAR.day(day)),
                    replace(lim, half_days_enabled=True))
    assert refused.cause is C.AFTER_LAST_ENTRY


def test_invalid_session_offsets_refuse(lim):
    bad = replace(lim, last_entry_minutes_before_close=5, flatten_minutes_before_close=15)
    assert admit(inputs(), bad).cause is C.SESSION_TIMES_INVALID


def test_now_in_utc_is_equivalent(lim):
    assert admit(_moved(NOW.astimezone(UTC)), lim).ok


def test_first_failure_wins(lim):
    # Everything broken at once: the gate (check 1) is the recorded cause.
    broken = inputs(
        gate=GateResult(False, "disabled"), entry_stop=True, own_working_orders=1,
        quote=None, realized_loss_today_usd=None, entry_budget_available=False,
    )
    assert admit(broken, lim).cause is C.GATE_CLOSED
    # Fix checks one by one: the cause walks down the list in spec order.
    broken = replace(broken, gate=GateResult(True, "ok"))
    assert admit(broken, lim).cause is C.ENTRY_STOP_SET
    broken = replace(broken, entry_stop=False)
    assert admit(broken, lim).cause is C.OWN_ORDER_WORKING
    broken = replace(broken, own_working_orders=0)
    assert admit(broken, lim).cause is C.QUOTE_MISSING
    broken = replace(broken, quote=quote())
    assert admit(broken, lim).cause is C.DAILY_MONEY_UNKNOWN
    broken = replace(broken, realized_loss_today_usd=Decimal(0))
    assert admit(broken, lim).cause is C.ENTRY_BUDGET_UNAVAILABLE
    broken = replace(broken, entry_budget_available=True)
    assert admit(broken, lim).ok


def test_dst_change_day(lim):
    # 2026-11-02 is the first Monday after the DST change (2026-11-01).
    day = date(2026, 11, 2)
    assert admit(_moved(_at(15, 44, day), session=CALENDAR.day(day)), lim).ok
    assert admit(_moved(_at(15, 45, day), session=CALENDAR.day(day)), lim).cause \
        is C.AFTER_LAST_ENTRY


def test_size_runs_on_final_rounded_prices(lim):
    # Raw decision risk 10.20 - 9.80 = 0.40 x 125 = 50.00, exactly the limit. If the stop that
    # will be sent is rounded one tick down, the real risk is 0.41 x 125 = 51.25: refused.
    at_limit = decision(quantity=D("125"))
    assert admit(inputs(decision=at_limit), lim).ok
    rounded = admit(inputs(decision=at_limit, stop_price=D("9.79")), lim)
    assert rounded.cause is C.PLANNED_LOSS_OVER_LIMIT
    # A limit rounded down and a stop rounded up only shrink the risk; the reservation follows
    # the prices that will be sent.
    tighter = admit(inputs(decision=at_limit, limit_price=D("10.19"), stop_price=D("9.81"),
                           quote=quote(ask=D("10.19"))), lim)
    assert tighter.ok and tighter.planned_loss_usd == D("47.50")


def test_daily_reservation_uses_final_prices(lim):
    # 200 + 60 + 100 x (10.20 - 9.80) = 300 admitted; one tick lower stop = 301 refused.
    money = dict(realized_loss_today_usd=D("200"), open_reservations_usd=D("60"))
    assert admit(inputs(**money), lim).ok
    assert admit(inputs(stop_price=D("9.79"), **money), lim).cause is C.DAILY_CEILING


def test_clock_skew_tolerance(lim):
    # Provider clock up to max_clock_skew_seconds ahead of ours counts as a fresh quote.
    ahead = quote(provider_time=NOW + timedelta(seconds=1))
    assert admit(inputs(quote=ahead), lim).ok
    small = quote(provider_time=NOW + timedelta(milliseconds=50))
    assert admit(inputs(quote=small), lim).ok
    beyond = quote(provider_time=NOW + timedelta(seconds=1, milliseconds=1))
    assert admit(inputs(quote=beyond), lim).cause is C.QUOTE_TIME_INVALID
    # Zero tolerance: any lead refuses.
    strict = replace(lim, max_clock_skew_seconds=Decimal(0))
    assert admit(inputs(quote=small), strict).cause is C.QUOTE_TIME_INVALID
