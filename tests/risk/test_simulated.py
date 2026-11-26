"""The simulated book (backlog T12): live admission checks fed from simulated trades."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tickr import config
from tickr.calendar import SessionCalendar
from tickr.risk.admission import AdmissionLimits, admit
from tickr.risk.causes import AdmissionCause
from tickr.risk.simulated import SimulatedBook, SimulationRules
from tickr.strategy.base import Decision, Verdict

CFG = config.load(Path(__file__).resolve().parents[2] / "config" / "bot.toml")
DAY = date(2026, 11, 10)
MARKET_DAY = SessionCalendar.load((CFG.root / CFG.session.calendar_file).parent).day(DAY)
T = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # 10:00 New York
MINUTE = timedelta(minutes=1)


def book(**changes: object) -> SimulatedBook:
    return SimulatedBook(SimulationRules(replace(AdmissionLimits.from_config(CFG), **changes)))


def enter(symbol: str = "AAA", quantity: str = "10") -> Decision:
    return Decision("sim", "1", symbol, Verdict.ENTER, (), entry_low=Decimal("10.05"),
                    entry_high=Decimal("10.15"), stop=Decimal("9.95"),
                    quantity=Decimal(quantity))


def test_admits_and_reserves_the_planned_loss():
    b = book()
    admission = b.admit(enter(), T, MARKET_DAY)
    assert admission.ok and admission.planned_loss_usd == Decimal("2.00")


def test_simulated_inputs_never_pass_live_admission():
    inputs = book().inputs(enter(), T, MARKET_DAY)
    refused = admit(inputs, AdmissionLimits.from_config(CFG))
    assert not refused.ok and refused.cause is AdmissionCause.GATE_CLOSED


def test_state_follows_times_not_recording_order():
    b = book(max_open_positions=1)
    key = b.entered("AAA", T, b.admit(enter(), T, MARKET_DAY))
    b.filled(key, T + MINUTE)
    b.ended(key, T + 5 * MINUTE, Decimal("-3"))  # a whole trade recorded at once
    during = b.admit(enter("BBB"), T + 2 * MINUTE, MARKET_DAY)
    assert during.cause is AdmissionCause.POSITION_LIMIT_REACHED
    assert b.admit(enter("BBB"), T + 5 * MINUTE, MARKET_DAY).ok
    again = b.admit(enter("AAA"), T + 6 * MINUTE, MARKET_DAY)
    assert again.cause is AdmissionCause.SYMBOL_COOLDOWN


def test_one_trade_per_symbol_and_working_entry_limit():
    b = book(max_working_entries=1)
    b.entered("AAA", T, b.admit(enter(), T, MARKET_DAY))
    assert b.admit(enter(), T + MINUTE, MARKET_DAY).cause is AdmissionCause.OWN_ORDER_WORKING
    other = b.admit(enter("BBB"), T + MINUTE, MARKET_DAY)
    assert other.cause is AdmissionCause.WORKING_ENTRY_LIMIT_REACHED


def test_wins_offset_losses_under_the_daily_ceiling():
    b = book(max_daily_loss_usd=Decimal(10), min_daily_room_usd=Decimal(1),
             symbol_cooldown_minutes_after_loss=None)
    for pnl, symbol in ((Decimal("-9"), "AAA"), (Decimal("4"), "BBB")):
        key = b.entered(symbol, T, b.admit(enter(symbol), T, MARKET_DAY))
        b.ended(key, T + MINUTE, pnl)
    assert b.admit(enter("CCC"), T + MINUTE, MARKET_DAY).ok  # net loss 5 + 2 new <= 10
    late = book(max_daily_loss_usd=Decimal(10), min_daily_room_usd=Decimal(1))
    key = late.entered("AAA", T, late.admit(enter(), T, MARKET_DAY))
    late.ended(key, T + MINUTE, Decimal("-9"))
    assert late.admit(enter("CCC"), T + MINUTE, MARKET_DAY).cause is AdmissionCause.DAILY_CEILING


def test_no_calendar_day_refuses():
    assert book().admit(enter(), T, None).cause is AdmissionCause.CALENDAR_UNKNOWN


def test_label_names_the_values():
    label = SimulationRules.from_config(CFG).label()
    assert f"risk.max_open_positions={CFG.risk.max_open_positions}" in label
    assert ("not simulated: gate, reconciliation, quote, request_budget, tradability, "
            "buying_power") in label
