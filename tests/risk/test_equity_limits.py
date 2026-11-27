"""Percent-of-equity limits (backlog X2): off = unchanged, on = the smaller of USD and percent
of the broker's equity, an unusable equity refuses with one cause, a percent never loosens."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tickr.api.contract import RefusalCode, refusal_of
from tickr.risk.admission import Equity, admit
from tickr.risk.causes import AdmissionCause as C
from tickr.risk.equity import effective_limit

from .conftest import NOW, D, decision, inputs

FRESH = NOW - timedelta(seconds=1)
EQUITY = Equity(D(10000), FRESH)
# Baseline entry: 100 shares x (10.20 - 9.80) = 40 planned loss, 1020 notional; the baseline
# limits are 50 planned loss, 2500 notional, 300 daily ceiling.


def on(lim, **percents):
    return replace(lim, **percents)


def test_off_is_exactly_the_usd_behaviour(lim):
    base = admit(inputs(), lim)
    for equity in (Equity(), EQUITY, Equity(D(1), FRESH), Equity(D("NaN"), FRESH)):
        assert admit(inputs(equity=equity), lim) == base
    assert not lim.percent_limits_on


def test_effective_limit_is_the_smaller_of_usd_and_percent():
    assert effective_limit(D(50), D(0), None) == D(50)
    assert effective_limit(D(50), D("0.3"), D(10000)) == D(30)  # percent tighter
    assert effective_limit(D(50), D(5), D(10000)) == D(50)  # percent larger: USD stays


@pytest.mark.parametrize("equity", [None, D(0), D(-5), D("NaN"), D("Infinity")])
def test_effective_limit_refuses_an_unusable_equity(equity):
    with pytest.raises(ValueError):
        effective_limit(D(50), D(1), equity)


def test_planned_loss_percent_tightens(lim):
    tight = on(lim, planned_loss_percent=D("0.3"))  # 30 < 40
    result = admit(inputs(equity=EQUITY), tight)
    assert result.cause is C.PLANNED_LOSS_OVER_LIMIT and "limit 30" in result.detail
    assert admit(inputs(equity=EQUITY), on(lim, planned_loss_percent=D("0.5"))).ok  # 50 = USD


def test_position_cap_percent_tightens(lim):
    result = admit(inputs(equity=EQUITY), on(lim, position_notional_cap_percent=D(5)))  # 500
    assert result.cause is C.NOTIONAL_OVER_CAP and "cap 500" in result.detail
    assert admit(inputs(equity=EQUITY), on(lim, position_notional_cap_percent=D(11))).ok


def test_daily_loss_percent_tightens(lim):
    result = admit(inputs(equity=EQUITY), on(lim, max_daily_loss_percent=D("0.2")))  # 20 < 40
    assert result.cause is C.DAILY_CEILING and "ceiling 20" in result.detail
    assert admit(inputs(equity=EQUITY), on(lim, max_daily_loss_percent=D(1))).ok  # 100


def test_a_percent_never_loosens_a_usd_limit(lim):
    huge = Equity(D(10) ** 12, FRESH)
    loose = on(lim, planned_loss_percent=D(100), position_notional_cap_percent=D(100),
               max_daily_loss_percent=D(100))
    assert admit(inputs(equity=huge), loose) == admit(inputs(), lim)
    over_planned = inputs(equity=huge, decision=decision(quantity=D(150)))  # loss 60 > 50
    assert admit(over_planned, loose).cause is C.PLANNED_LOSS_OVER_LIMIT
    over_cap = inputs(equity=huge, decision=decision(quantity=D(250)))  # 2550 > 2500
    assert admit(over_cap, loose).cause is C.NOTIONAL_OVER_CAP
    assert admit(inputs(equity=huge, realized_loss_today_usd=D(280)),
                 loose).cause is C.DAILY_CEILING  # 280 + 40 > 300


@pytest.mark.parametrize("equity, detail", [
    (Equity(), "no equity read"),
    (Equity(D(10000), None), "no equity read"),
    (Equity(None, FRESH), "no equity read"),
    (Equity(D(10000), NOW - timedelta(seconds=16)), "equity read 16"),
    (Equity(D(0), FRESH), "equity figure invalid"),
    (Equity(D(-1), FRESH), "equity figure invalid"),
    (Equity(D("NaN"), FRESH), "equity figure invalid"),
])
def test_unknown_stale_or_invalid_equity_refuses_when_a_percent_is_on(lim, equity, detail):
    for name in ("planned_loss_percent", "position_notional_cap_percent",
                 "max_daily_loss_percent"):
        result = admit(inputs(equity=equity), on(lim, **{name: D(50)}))
        assert result.cause is C.EQUITY_UNKNOWN and detail in result.detail


def test_an_equity_read_in_the_future_is_a_clock_refusal(lim):
    future = Equity(D(10000), NOW + timedelta(seconds=1))
    result = admit(inputs(equity=future), on(lim, planned_loss_percent=D(50)))
    assert result.cause is C.CLOCK_INVALID


@pytest.mark.parametrize("equity", [Equity(), Equity(D(10000), NOW - timedelta(seconds=16)),
                                    Equity(D(0), FRESH)])
def test_the_api_reads_the_equity_refusal(lim, equity):
    result = admit(inputs(equity=equity), on(lim, planned_loss_percent=D(50)))
    refusal = refusal_of(f"{result.cause}: {result.detail}")
    assert refusal.code is RefusalCode.EQUITY_UNKNOWN and refusal.message
