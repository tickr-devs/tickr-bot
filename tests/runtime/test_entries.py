from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import config
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.config import GateResult
from aperture.engine.core import Engine, Fence, Leg
from aperture.observers.log import DecisionLog
from aperture.research.records import load_records
from aperture.runtime.entries import (
    EntryDesk,
    guard_entries,
    realized_loss_today,
    round_to_tick,
)
from aperture.store import Store
from aperture.strategy.base import Check, Decision, Verdict
from aperture.types import Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
NOW = datetime(2026, 11, 16, 11, 0, 30, tzinfo=NEW_YORK)
BAR = datetime(2026, 11, 16, 10, 59, tzinfo=NEW_YORK)
GATE_OPEN = config.trading_gate(CFG).open
pytestmark = pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")


def decision(verdict=Verdict.ENTER, **overrides) -> Decision:
    values = dict(strategy=CFG.strategy.active, version="1", symbol="ABC", verdict=verdict,
                  checks=(Check("rule", D(1), ">=", D(1), True),), setup="breakout",
                  trigger=D("10.00"), invalidation=D("9.50"), entry_low=D("10.00"),
                  entry_high=D("10.106"), stop=D("9.759"), quantity=D(10))
    if verdict is not Verdict.ENTER:
        values.update(entry_low=None, entry_high=None, stop=None, quantity=None)
    values.update(overrides)
    return Decision(**values)


def quote(ask="10.05", age_seconds=1) -> Quote:
    return Quote("ABC", D("10.04"), D(ask), D(100), D(100),
                 NOW - timedelta(seconds=age_seconds), NOW)


class Desk:
    def __init__(self, tmp_path: Path, broker: FakeBroker | None = None) -> None:
        self.broker = broker or FakeBroker()
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
        self.engine = Engine(self.store, self.broker, CFG, "desk0001",
                             lambda: GateResult(True, "t"))
        self.engine.recover()
        self.read_at = NOW
        self.dir = tmp_path / "decisions"
        self.desk = EntryDesk(self.engine, CFG, calendar=CALENDAR,
                              last_broker_read_at=lambda: self.read_at, log=DecisionLog(self.dir))

    def decide(self, d: Decision, q: Quote | None = None, now: datetime = NOW):
        return self.desk.on_decision(d, bar_start=BAR, quote=q or quote(), now=now)


def test_round_to_tick():
    assert round_to_tick(D("10.106")) == D("10.10")
    assert round_to_tick(D("0.98765")) == D("0.9876")


def test_admitted_enter_opens_a_trade_at_the_top_of_the_window(tmp_path):
    desk = Desk(tmp_path)
    record = desk.decide(decision())
    assert record.admission == "taken" and record.refusal_cause is None
    (trade,) = desk.engine.trades()
    (entry,) = desk.engine.orders(trade.trade_id)
    assert (entry.leg, entry.limit_price, trade.stop_price) == (Leg.ENTRY, D("10.10"),
                                                                D("9.75"))
    assert trade.planned_loss_usd == D("3.50")  # 10 x (10.10 - 9.75)


def test_every_decision_is_recorded_in_the_research_format(tmp_path):
    desk = Desk(tmp_path)
    desk.decide(decision(Verdict.WATCH))
    desk.decide(decision(), q=quote(ask="10.50"))  # ask above the window: refused
    desk.decide(decision())
    desk.desk.log.close()
    path = desk.dir / "2026-11-16" / "decisions.jsonl"
    records = load_records([path])
    assert [r.admission for r in records] == ["not_applicable", "refused", "taken"]
    assert records[1].refusal_cause.startswith("PRICE_OUTSIDE_WINDOW")
    assert all(json.loads(line)["role"] == "active" for line in path.read_text().splitlines())


@pytest.mark.parametrize("setup, cause", [
    (lambda d: _entry_stop(d), "ENTRY_STOP_SET"),
    (lambda d: d.broker.add_foreign_position("XYZ", D(1), D(5)) or d.engine.step(),
     "FOREIGN_EXPOSURE"),
    (lambda d: setattr(d, "read_at", NOW - timedelta(hours=1)), "BROKER_READ_STALE"),
    (lambda d: setattr(d.broker, "place_budget", 1), "STOP_BUDGET_UNAVAILABLE"),
    (lambda d: _open_one(d), "OWN_POSITION_OPEN"),
])
def test_refusals_carry_one_cause_and_send_nothing(tmp_path, setup, cause):
    desk = Desk(tmp_path)
    setup(desk)
    sent = len(desk.broker.sent)
    record = desk.decide(decision())
    assert record.admission == "refused" and record.refusal_cause.startswith(cause)
    assert len(desk.broker.sent) == sent


def _entry_stop(desk: Desk) -> None:
    with desk.store.transaction():
        desk.engine.entry_stop_row(True, "test")


def _open_one(desk: Desk) -> None:
    desk.decide(decision())
    trade = desk.engine.trades()[0]
    desk.broker.fill(desk.engine.orders(trade.trade_id)[0].client_order_id, D(10), D("10.05"))
    for _ in range(3):
        desk.engine.step()


def test_stale_quote_and_after_last_entry_are_refused(tmp_path):
    desk = Desk(tmp_path)
    assert desk.decide(decision(), q=quote(age_seconds=60)).refusal_cause.startswith(
        "QUOTE_STALE")
    late = datetime(2026, 11, 16, 15, 59, tzinfo=NEW_YORK)
    desk.read_at = late
    record = desk.decide(decision(), q=Quote("ABC", D("10.04"), D("10.05"), D(1), D(1),
                                             late, late), now=late)
    assert record.refusal_cause.startswith("AFTER_LAST_ENTRY")


def test_no_calendar_refuses(tmp_path):
    desk = Desk(tmp_path)
    desk.desk.calendar = None
    assert desk.decide(decision()).refusal_cause.startswith("CALENDAR_UNKNOWN")


def test_daily_ceiling_counts_todays_realized_loss(tmp_path, monkeypatch):
    desk = Desk(tmp_path)
    monkeypatch.setattr("aperture.runtime.entries.realized_loss_today",
                        lambda engine, now: CFG.risk.max_daily_loss_usd)
    assert desk.decide(decision()).refusal_cause.startswith("DAILY_CEILING")


def test_strategy_exit_requests_the_normal_exit(tmp_path):
    desk = Desk(tmp_path)
    trade = desk.engine.open_trade("ABC", D(1), D(10), D(9), D(1))
    record = desk.decide(decision(Verdict.EXIT))
    assert record.admission == "not_applicable"
    assert desk.engine.trade(trade).exit_requested == "strategy-exit"


def test_unresolved_fence_blocks_entries(tmp_path):
    desk = Desk(tmp_path)
    with desk.store.transaction():
        desk.engine._fence_row(Fence.UNKNOWN_ORDER, "x", "test")
    assert desk.decide(decision()).refusal_cause.startswith("UNRESOLVED_EXPOSURE")


def test_realized_loss_is_the_net_loss_of_the_day(tmp_path, monkeypatch):
    from aperture.api import contract as api

    def trade(result):
        return api.Trade("ABC", D(1), D(10), D(10) + result, NOW, NOW, "x", result)

    monkeypatch.setattr("aperture.runtime.entries.trades_today",
                        lambda engine, now: (trade(D(-20)), trade(D(5)), trade(D(-5))))
    desk = Desk(tmp_path)
    assert realized_loss_today(desk.engine, NOW) == D(20)  # wins offset losses (owner 2026-11-14)
    monkeypatch.setattr("aperture.runtime.entries.trades_today",
                        lambda engine, now: (trade(D(-20)), trade(D(50))))
    assert realized_loss_today(desk.engine, NOW) == D(0)


def test_record_failure_after_send_is_journaled_and_never_raises(tmp_path):
    desk = Desk(tmp_path)

    def broken(record):
        raise OSError("disk full")

    desk.desk.log.append = broken
    record = desk.decide(decision())
    assert record.admission == "taken"
    assert len(desk.engine.trades()) == 1  # the entry went out; the loop goes on
    (row,) = desk.store.journal("decision-record-failed")
    assert row.body["error"] == "OSError" and row.body["admission"] == "taken"


# --- working entries leave with their window (spec 9) ------------------------------------


def opened(desk: Desk) -> str:
    desk.decide(decision())
    (trade,) = desk.engine.trades()
    assert trade.entry_low_price == D("10.00") and trade.entry_limit_price == D("10.10")
    return trade.trade_id


def guard(desk: Desk, q: Quote | None, now: datetime = NOW) -> list[str]:
    guard_entries(desk.engine, CFG, CALENDAR, lambda symbol: q, now)
    return [r.body["cause"] for r in desk.store.journal("entry-cancel")]


@pytest.mark.parametrize(("q", "cause"), [
    (quote(ask="10.05"), None),                         # inside 10.00..10.10: rests
    (quote(ask="10.10"), None),                         # at the limit: rests
    (quote(ask="10.11"), "ask 10.11 above the limit 10.10"),
    (quote(ask="9.99"), "ask 9.99 below the window 10.00"),
    (quote(age_seconds=float(CFG.risk.max_quote_age_seconds) + 1), "quote stale"),
    (quote(age_seconds=-(float(CFG.risk.max_clock_skew_seconds) + 1)), "quote from the future"),
    (None, "no quote"),
])
def test_a_working_entry_is_cancelled_when_its_quote_leaves_the_window(tmp_path, q, cause):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    causes = guard(desk, q)
    (entry,) = desk.engine.orders(trade_id)
    if cause is None:
        assert causes == [] and entry.cancel_state is None
        assert desk.broker.orders[entry.client_order_id].state.value == "ACCEPTED"
    else:
        assert len(causes) == 1 and causes[0].startswith(cause)
        assert entry.cancel_state == "sent"
        desk.engine.step()
        assert desk.engine.trades() == []  # closed: entry-not-filled


def test_working_entries_are_cancelled_at_the_last_entry_time(tmp_path):
    desk = Desk(tmp_path)
    opened(desk)
    last_entry = datetime(2026, 11, 16, 15, 45, tzinfo=NEW_YORK)
    fresh = Quote("ABC", D("10.04"), D("10.05"), D(100), D(100), last_entry, last_entry)
    assert guard(desk, fresh, last_entry - timedelta(seconds=1)) == []
    (cause,) = guard(desk, fresh, last_entry)
    assert cause.startswith("last entry time")


def test_the_cancel_cause_is_journaled_once_and_stops_are_never_touched(tmp_path):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    (entry,) = desk.engine.orders(trade_id)
    desk.broker.fill(entry.client_order_id, D(4), D("10.08"))  # partial fill: protect it
    desk.engine.step()
    desk.engine.step()
    away = quote(ask="10.20")
    guard(desk, away)
    guard(desk, away)
    assert len(desk.store.journal("entry-cancel")) == 1
    stops = [o for o in desk.engine.orders(trade_id) if o.leg is Leg.STOP]
    assert stops and all(s.cancel_state is None for s in stops)
    desk.engine.step()
    (trade,) = desk.engine.trades()  # the 4 shares stay a protected position
    assert desk.engine.expected_quantity(desk.engine.orders(trade_id)) == D(4)
    assert trade.trade_id == trade_id


def test_an_entry_already_exiting_is_left_to_the_teardown(tmp_path):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    desk.engine.request_exit(trade_id, "operator-flatten")
    assert guard(desk, None) == []


def test_the_window_bottom_must_sit_between_the_stop_and_the_limit(tmp_path):
    from aperture.engine.core import EntryRefused

    desk = Desk(tmp_path)
    with pytest.raises(EntryRefused):
        desk.engine.open_trade("ABC", D(1), D("10.10"), D("9.75"), D("0.35"),
                               entry_low_price=D("10.11"))
    with pytest.raises(EntryRefused):
        desk.engine.open_trade("ABC", D(1), D("10.10"), D("9.75"), D("0.35"),
                               entry_low_price=D("9.75"))
