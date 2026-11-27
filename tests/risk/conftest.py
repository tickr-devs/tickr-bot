from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from baseline import load_baseline

from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.risk.admission import AdmissionInputs, AdmissionLimits, BuyingPower, Reconciliation
from tickr.strategy.base import Decision, Verdict
from tickr.types import Quote

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_baseline()
CALENDAR = SessionCalendar.load(ROOT / "config" / "calendar")

# A normal Monday, 10:00 New York.
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=NEW_YORK)

D = Decimal


def limits() -> AdmissionLimits:
    return AdmissionLimits.from_config(CONFIG)


def decision(**changes) -> Decision:
    base = Decision(
        strategy=CONFIG.strategy.active, version="1", symbol="ABCD", verdict=Verdict.ENTER,
        checks=(), trigger=D("10.00"), entry_low=D("10.00"), entry_high=D("10.20"),
        stop=D("9.80"), quantity=D("100"),
    )
    return replace(base, **changes)


def quote(**changes) -> Quote:
    base = Quote(symbol="ABCD", bid=D("10.08"), ask=D("10.10"), bid_size=D("100"),
                 ask_size=D("100"), provider_time=NOW - timedelta(seconds=1),
                 receipt_time=NOW - timedelta(milliseconds=500))
    return replace(base, **changes)


def inputs(**changes) -> AdmissionInputs:
    base = AdmissionInputs(
        gate=GateResult(True, "trading enabled by configuration"),
        reconciliation=Reconciliation(startup_done=True,
                                      last_broker_read_at=NOW - timedelta(seconds=2),
                                      foreign_exposure=False, unresolved_exposure=False),
        entry_stop=False,
        session=CALENDAR.day(NOW.date()),
        now=NOW,
        own_open_positions=0,
        own_working_orders=0,
        open_positions=0,
        working_entries=0,
        symbol_last_loss_at=None,
        decision=decision(),
        limit_price=D("10.20"),
        stop_price=D("9.80"),
        setup_valid=True,
        quote=quote(),
        last_bar=None,
        previous_quote=None,
        realized_loss_today_usd=D("0"),
        open_reservations_usd=D("0"),
        buying_power=BuyingPower(D("4000000"), NOW - timedelta(seconds=2), D("0")),
        entry_budget_available=True,
        stop_budget_available=True,
        realized_loss_week_usd=D("0"),
        stop_out_streak=0,
    )
    return replace(base, **changes)


@pytest.fixture
def lim() -> AdmissionLimits:
    return limits()
