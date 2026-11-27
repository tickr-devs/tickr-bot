"""The loss locks (backlog T11) on the bot's own records: the weekly loss ceiling counts this New
York week's net realized loss after fees, the stop-out streak counts today's stop-outs in a row;
both are rebuilt from stored trades (never from memory), refuse new entries only, and never
block an exit or a stop."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from baseline import load_baseline
from funded import FUNDS

from tickr import config
from tickr.api import contract as api
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, CalendarCause, CalendarRefused, SessionCalendar
from tickr.config import GateResult
from tickr.engine.core import Engine, Leg
from tickr.observers.log import DecisionLog
from tickr.runtime import service
from tickr.runtime.entries import (
    EntryDesk,
    entry_block_cause,
    realized_loss_week,
    stop_out_streak,
    week_start,
)
from tickr.store import Store
from tickr.strategy.base import Check, Decision, Verdict
from tickr.types import Quote

ROOT = Path(__file__).resolve().parents[2]
BASE = load_baseline()
CALENDAR = SessionCalendar.load((ROOT / BASE.session.calendar_file).parent)
pytestmark = pytest.mark.skipif(not config.trading_gate(BASE).open,
                                reason="the configured trading gate is closed")
# A trade below: 10 shares bought at 10.05 with its stop at 9.75; a stop fill at 9.75 loses 3.
LOSS = D(3)


def locked(weekly: str = "0", streak: int = 0):
    # Two slots, so a held trade and a new one can be open together.
    return replace(BASE, risk=replace(BASE.risk, max_weekly_loss_usd=D(weekly),
                                      stop_out_streak_limit=streak, max_open_positions=2))


def ny(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEW_YORK)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Rig:
    """An engine and an entry desk on one database; `restart` opens new ones on the same file."""

    def __init__(self, tmp_path: Path, at: datetime, cfg) -> None:
        self.path, self.cfg = tmp_path / "db.sqlite3", cfg
        self.clock = Clock(at)
        self.broker = FakeBroker(funds=FUNDS)
        self.decisions = tmp_path / "decisions"
        self._open("run00001")

    def _open(self, run_id: str) -> None:
        self.store = Store.open(self.path, busy_timeout_seconds=0, now=self.clock)
        self.engine = Engine(self.store, self.broker, self.cfg, run_id,
                             lambda: GateResult(True, "t"))
        self.engine.recover()
        self.desk = EntryDesk(self.engine, self.cfg, calendar=CALENDAR,
                              last_broker_read_at=self.clock, log=DecisionLog(self.decisions))

    def restart(self) -> None:
        self.store.close()
        self._open("run00002")

    def steps(self, count: int = 3) -> None:
        for _ in range(count):
            self.engine.step()

    def opened(self, symbol: str, at: datetime) -> str:
        """A filled entry of 10 shares at 10.05 with its broker stop at 9.75 working."""
        self.clock.at = at
        trade_id = self.engine.open_trade(symbol, D(10), D("10.10"), D("9.75"), D("3.50"))
        self.broker.fill(self.engine.orders(trade_id)[0].client_order_id, D(10), D("10.05"))
        self.steps()
        return trade_id

    def stop(self, trade_id: str) -> str:
        (stop,) = [o for o in self.engine.orders(trade_id) if o.leg is Leg.STOP and o.live]
        return stop.client_order_id

    def trip(self, symbol: str, opened: datetime, closed: datetime, price: str = "9.75",
             by_stop: bool = True) -> str:
        """Open, fill and close one trade: by its stop filling at `price`, or by a market exit
        (not a stop fill) at `price`."""
        trade_id = self.opened(symbol, opened)
        self.clock.at = closed
        if by_stop:
            self.broker.fill(self.stop(trade_id), D(10), D(price))
            self.steps()
        else:
            self.engine.request_exit(trade_id, "test")
            for _ in range(8):
                self.engine.step()
                for order in self.engine.orders(trade_id):
                    if order.order_type == "MARKET" and order.live:
                        self.broker.fill(order.client_order_id, D(10), D(price))
        assert trade_id not in {t.trade_id for t in self.engine.trades()}, "not closed"
        return trade_id

    def decide(self, verdict: Verdict = Verdict.ENTER, symbol: str = "ABC"):
        now = self.clock.at
        values = dict(strategy=self.cfg.strategy.active, version="1", symbol=symbol,
                      verdict=verdict, checks=(Check("rule", D(1), ">=", D(1), True),),
                      setup="breakout", trigger=D("10.00"), invalidation=D("9.50"),
                      entry_low=D("10.00"), entry_high=D("10.106"), stop=D("9.759"),
                      quantity=D(10))
        if verdict is not Verdict.ENTER:
            values.update(entry_low=None, entry_high=None, stop=None, quantity=None)
        q = Quote(symbol, D("10.04"), D("10.05"), D(100), D(100), now, now)
        return self.desk.on_decision(Decision(**values), bar_start=now - timedelta(minutes=1),
                                     quote=q, now=now)


MON, TUE, WED, FRI = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 20)
LAST_FRI = date(2026, 11, 13)


# --- the week ---------------------------------------------------------------------------------


def test_the_week_starts_on_monday_in_new_york_time(tmp_path):
    rig = Rig(tmp_path, ny(LAST_FRI, 10), locked())
    # Last Friday 19:30 New York is already Saturday 00:30 UTC: still last week.
    rig.trip("AAA", ny(LAST_FRI, 15), ny(LAST_FRI, 19, 30))
    assert ny(LAST_FRI, 19, 30).astimezone(NEW_YORK).date() == LAST_FRI
    assert realized_loss_week(rig.engine, ny(LAST_FRI, 23, 59)) == LOSS
    # Sunday 23:59 New York (Monday 04:59 UTC) is the last minute of that week.
    assert realized_loss_week(rig.engine, ny(MON, 0) - timedelta(minutes=1)) == LOSS
    assert realized_loss_week(rig.engine, ny(MON, 0)) == 0
    rig.trip("BBB", ny(MON, 10), ny(MON, 10, 5))
    rig.trip("CCC", ny(WED, 10), ny(WED, 10, 5))
    assert realized_loss_week(rig.engine, ny(FRI, 15)) == 2 * LOSS
    assert week_start(FRI) == MON and week_start(MON) == MON


def test_a_week_that_starts_with_a_holiday_still_starts_on_its_monday(tmp_path):
    """Martin Luther King Jr. Day 2026: the calendar closes Monday January 19, so the week's
    trading starts Tuesday; Friday the 16th's loss stays in the week before."""
    holiday, friday, tuesday = date(2026, 1, 19), date(2026, 1, 16), date(2026, 1, 20)
    with pytest.raises(CalendarRefused) as refused:
        CALENDAR.day(holiday)
    assert refused.value.cause is CalendarCause.HOLIDAY
    rig = Rig(tmp_path, ny(friday, 10), locked("100"))
    rig.trip("AAA", ny(friday, 10), ny(friday, 10, 5))
    rig.trip("BBB", ny(tuesday, 10), ny(tuesday, 10, 5))
    assert week_start(tuesday) == holiday
    assert realized_loss_week(rig.engine, ny(tuesday, 11)) == LOSS
    assert realized_loss_week(rig.engine, ny(date(2026, 1, 22), 11)) == LOSS


def test_the_week_nets_wins_against_losses_after_fees_and_unknown_fees_refuse(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked("100"))
    rig.trip("AAA", ny(MON, 10), ny(MON, 10, 5))  # -3
    rig.trip("BBB", ny(TUE, 10), ny(TUE, 10, 5), "10.25")  # +2
    assert realized_loss_week(rig.engine, ny(TUE, 11)) == D(1)
    # A flat trade by price loses its fees: they count.
    rig.broker.fill_fees = D("0.50")
    rig.trip("CCC", ny(TUE, 11), ny(TUE, 11, 5), "10.05", by_stop=False)
    assert realized_loss_week(rig.engine, ny(TUE, 12)) == D(2)  # 1 + 2 fills x 0.50
    rig.broker.fill_fees = None  # the fees of a fill cannot be read
    rig.trip("DDD", ny(WED, 10), ny(WED, 10, 5), "10.25")
    rig.clock.at = ny(WED, 11)
    assert realized_loss_week(rig.engine, rig.clock.at) is None
    record = rig.decide()
    assert record.admission == "refused"
    # Today's money is unknown too, so the daily rule refuses first.
    assert record.refusal_cause.startswith("DAILY_MONEY_UNKNOWN")
    rig.clock.at = ny(date(2026, 11, 19), 10)
    record = rig.decide()
    assert record.refusal_cause.startswith("WEEKLY_MONEY_UNKNOWN"), record.refusal_cause


def test_the_weekly_ceiling_refuses_once_the_week_has_no_room(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked(str(2 * LOSS + D("3.50"))))
    rig.trip("AAA", ny(MON, 10), ny(MON, 10, 5))
    rig.trip("BBB", ny(TUE, 10), ny(TUE, 10, 5))
    rig.clock.at = ny(WED, 10)
    # 6 lost + this trade's 3.50 planned loss (10 x (10.10 - 9.75)) is the ceiling exactly; a
    # cent less refuses.
    rig.desk.limits = replace(rig.desk.limits, max_weekly_loss_usd=D("9.49"))
    record = rig.decide()
    assert record.refusal_cause == ("WEEKLY_CEILING: week realized 6.00 + reserved 0 + new 3.50 "
                                    "= 9.50 vs weekly ceiling 9.49")
    rig.desk.limits = replace(rig.desk.limits, max_weekly_loss_usd=D("9.50"))
    assert rig.decide().admission == "taken"
    # Now the open trade's 3.50 is reserved against the week too.
    rig.clock.at = ny(WED, 10, 1)
    assert rig.decide(symbol="XYZ").refusal_cause.startswith("WEEKLY_CEILING")


# --- the streak -------------------------------------------------------------------------------


def test_streak_counts_stop_outs_in_a_row_and_a_winning_or_flat_exit_resets_it(tmp_path):
    rig = Rig(tmp_path, ny(MON, 9, 30), locked())
    rig.trip("OLD", ny(LAST_FRI, 10), ny(LAST_FRI, 10, 5))  # another day: not counted

    def streak_after(*args, **kwargs) -> int:
        rig.trip(*args, **kwargs)
        return stop_out_streak(rig.engine, rig.clock.at)

    assert stop_out_streak(rig.engine, ny(MON, 9, 30)) == 0
    assert streak_after("A1", ny(MON, 10), ny(MON, 10, 1)) == 1
    assert streak_after("A2", ny(MON, 10, 2), ny(MON, 10, 3)) == 2
    # A losing exit that was not a stop fill leaves the streak as it is.
    assert streak_after("A3", ny(MON, 10, 4), ny(MON, 10, 5), "9.90", by_stop=False) == 2
    assert streak_after("A4", ny(MON, 10, 6), ny(MON, 10, 7)) == 3
    # A flat exit (result 0 after fees) resets it.
    assert streak_after("A5", ny(MON, 10, 8), ny(MON, 10, 9), "10.05", by_stop=False) == 0
    assert streak_after("A6", ny(MON, 10, 10), ny(MON, 10, 11)) == 1
    # A stop that filled at a gain (a trailed stop) is a winning exit: it resets.
    assert streak_after("A7", ny(MON, 10, 12), ny(MON, 10, 13), "10.30") == 0
    # A stop filled flat by price but losing its fees is a stop-out.
    rig.broker.fill_fees = D("0.01")
    assert streak_after("A8", ny(MON, 10, 14), ny(MON, 10, 15), "10.05") == 1
    # A new day starts from 0.
    assert stop_out_streak(rig.engine, ny(TUE, 9, 30)) == 0


def test_streak_lock_refuses_new_entries_for_the_rest_of_the_day(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked(streak=2))
    rig.trip("AAA", ny(MON, 10), ny(MON, 10, 1))
    rig.clock.at = ny(MON, 10, 2)
    assert rig.desk._inputs(rig_decision(rig), None, rig.clock.at).stop_out_streak == 1
    rig.trip("BBB", ny(MON, 10, 2), ny(MON, 10, 3))
    for at in (ny(MON, 10, 4), ny(MON, 15, 30)):
        rig.clock.at = at
        record = rig.decide()
        assert record.admission == "refused"
        assert record.refusal_cause == ("STOP_OUT_STREAK: 2 stop-outs in a row today, limit 2")
    rig.clock.at = ny(TUE, 10)
    assert rig.decide().admission == "taken"


def rig_decision(rig: Rig) -> Decision:
    return Decision(strategy=rig.cfg.strategy.active, version="1", symbol="ABC",
                    verdict=Verdict.ENTER, checks=(), trigger=D("10.00"),
                    entry_low=D("10.00"), entry_high=D("10.106"), stop=D("9.759"),
                    quantity=D(10))


# --- restart ----------------------------------------------------------------------------------


def test_both_locks_are_rebuilt_from_stored_trades_after_a_restart(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked("8", streak=2))
    rig.trip("AAA", ny(MON, 10), ny(MON, 10, 5))
    rig.trip("BBB", ny(TUE, 10), ny(TUE, 10, 1))
    rig.trip("CCC", ny(TUE, 10, 2), ny(TUE, 10, 3))
    rig.clock.at = ny(TUE, 10, 4)
    before = rig.desk._inputs(rig_decision(rig), None, rig.clock.at)
    rig.restart()
    after = rig.desk._inputs(rig_decision(rig), None, rig.clock.at)
    assert (after.realized_loss_week_usd, after.stop_out_streak) == (3 * LOSS, 2)
    assert (before.realized_loss_week_usd, before.stop_out_streak) == (3 * LOSS, 2)
    assert rig.decide().refusal_cause.startswith("STOP_OUT_STREAK")
    # Next day the streak is gone; the week's 9 lost is still over the ceiling of 8.
    rig.restart()
    rig.clock.at = ny(WED, 10)
    assert rig.decide().refusal_cause.startswith("WEEKLY_CEILING")


# --- exits and protection are never blocked ---------------------------------------------------


def test_exits_and_stops_go_on_while_both_locks_refuse_entries(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked("3", streak=1))
    held = rig.opened("HLD", ny(MON, 9, 50))
    rig.trip("AAA", ny(MON, 10), ny(MON, 10, 1))
    rig.clock.at = ny(MON, 10, 2)
    assert rig.decide().refusal_cause.startswith("STOP_OUT_STREAK")
    # The held trade keeps its stop through steps, and its strategy EXIT is taken.
    rig.steps()
    assert rig.engine.protects(next(o for o in rig.engine.orders(held)
                                    if o.client_order_id == rig.stop(held)))
    record = rig.decide(Verdict.EXIT, symbol="HLD")
    assert record.admission == "not_applicable"
    (trade,) = rig.engine.trades()
    assert trade.trade_id == held and trade.exit_requested is not None
    sent = len(rig.broker.sent)
    rig.steps()
    assert len(rig.broker.sent) > sent  # the exit's cancel and sell went out


def test_a_working_entry_is_cancelled_once_a_lock_refuses_new_entries(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked(streak=1))
    rig.clock.at = ny(MON, 10)
    working = rig.engine.open_trade("WRK", D(10), D("10.10"), D("9.75"), D("3.50"))
    (trade,) = rig.engine.trades()
    assert entry_block_cause(rig.engine, rig.cfg, trade, rig.clock.at) is None
    rig.trip("AAA", ny(MON, 10, 1), ny(MON, 10, 2))
    (trade,) = [t for t in rig.engine.trades() if t.trade_id == working]
    assert entry_block_cause(rig.engine, rig.cfg, trade, rig.clock.at) == (
        "stop-out streak: 1 in a row today, limit 1")


# --- status -----------------------------------------------------------------------------------


def _service(rig: Rig) -> service.Service:
    paths = service.ServicePaths(rig.path.parent / "status.json", rig.path.parent / "inbox",
                                 rig.path.parent / "processed",
                                 run_facts=rig.path.parent / "run-facts.json")
    svc = service.Service(rig.engine, now=rig.clock, calendar=CALENDAR,
                          strategy=service.strategy_id(rig.cfg), paths=paths)
    svc.evaluator = object()  # type: ignore[assignment]  # entries wired
    svc.state = api.BotState.RUNNING
    return svc


def test_run_facts_show_both_counts_and_status_says_which_lock_blocks(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked("6", streak=2))
    svc = _service(rig)
    rig.clock.at = ny(MON, 11)
    facts = svc.run_facts()
    assert (facts.realized_loss_week_usd, facts.stop_out_streak, svc.status().entry_block) == (
        0, 0, None)
    rig.trip("AAA", ny(MON, 11), ny(MON, 11, 1))
    rig.trip("BBB", ny(MON, 11, 2), ny(MON, 11, 3))
    rig.clock.at = ny(MON, 11, 4)
    facts, status = svc.run_facts(), svc.status()
    assert (facts.realized_loss_week_usd, facts.stop_out_streak) == (2 * LOSS, 2)
    # The week's 6 lost leaves no room under 6: the weekly lock is named first.
    assert status.entry_block is not None and status.entry_block.cause == "weekly_ceiling"
    assert status.entry_block.params == {"limit_usd": "6"}
    rig.cfg = locked("100", streak=2)
    rig.restart()
    block = _service(rig).status().entry_block
    assert block is not None and block.cause == "stop_out_streak"
    assert block.params == {"count": "2", "limit": "2"}


def test_the_counts_reach_the_run_facts_file_when_they_change_not_status_json(tmp_path):
    rig = Rig(tmp_path, ny(MON, 10), locked("100", streak=5))
    svc = _service(rig)
    rig.clock.at = ny(MON, 11)
    svc.write_status()
    assert not svc.paths.run_facts.exists()  # the start writes it first (Service.run)
    svc.write_run_facts()
    first = api.read_run_facts(svc.paths.run_facts)
    assert (first.realized_loss_week_usd, first.stop_out_streak) == (0, 0)
    rig.clock.at = ny(MON, 11, 1)
    svc.write_status()  # nothing changed: the file is not rewritten
    assert api.read_run_facts(svc.paths.run_facts) == first
    rig.trip("AAA", ny(MON, 11, 1), ny(MON, 11, 2))
    rig.clock.at = ny(MON, 11, 3)
    svc.write_status()
    facts = api.read_run_facts(svc.paths.run_facts)
    assert (facts.realized_loss_week_usd, facts.stop_out_streak) == (LOSS, 1)
    raw = json.loads(svc.paths.status.read_text("utf-8"))
    assert "realized_loss_week_usd" not in raw and "stop_out_streak" not in raw
    served = api.served_status(api.read_status(svc.paths.status), facts)
    assert (served.realized_loss_week_usd, served.stop_out_streak) == (LOSS, 1)
