from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from baseline import load_baseline

from tickr import config
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.engine.core import Engine, Fence, Leg
from tickr.observers.log import DecisionLog
from tickr.research.records import load_records
from tickr.runtime.entries import (
    EntryDesk,
    guard_entries,
    realized_loss_today,
    round_to_tick,
)
from tickr.store import Store
from tickr.strategy.base import Check, Decision, Verdict
from tickr.types import OrderDetail, OrderState, OrderType, Quote, Side

ROOT = Path(__file__).resolve().parents[2]
CFG = load_baseline()
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
    (opened,) = desk.engine.store.journal("trade-opened", trade.trade_id)
    d = decision()
    assert (opened.body["strategy"], opened.body["strategy_version"]) == (d.strategy, d.version)


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
    (lambda d: d.broker.add_foreign_position("ABC", D(1), D(5)) or d.engine.step(),
     "FOREIGN_EXPOSURE"),
    (lambda d: _foreign_order(d, "XYZ") or setattr(d.engine, "broker_view", None),
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


def _foreign_order(desk: Desk, symbol: str) -> None:
    """A manual order the bot does not own, seen by a reconcile."""
    desk.broker.orders["manual-1"] = OrderDetail(
        client_order_id="manual-1", symbol=symbol, side=Side.BUY, order_type=OrderType.LIMIT,
        quantity=D(1), filled_quantity=D(0), average_fill_price=None, state=OrderState.ACCEPTED,
        raw_status="SUBMITTED", limit_price=D(5))
    assert desk.engine.reconcile()


@pytest.mark.parametrize("setup", [
    lambda d: d.broker.add_foreign_position("XYZ", D(1), D(5)) or d.engine.step(),
    lambda d: _foreign_order(d, "XYZ"),
])
def test_manual_trades_in_other_symbols_are_ignored(tmp_path, setup):
    """Owner 2026-11-16: a foreign position or order in another symbol neither blocks the
    entry nor counts toward the position limit; its fence (and alert) stays."""
    desk = Desk(tmp_path)
    setup(desk)
    assert any(n.startswith(("foreign-position", "foreign-order")) for n in desk.engine.fences())
    record = desk.decide(decision())
    assert record.admission == "taken" and record.refusal_cause is None


def test_a_manual_order_in_the_same_symbol_blocks_the_entry(tmp_path):
    desk = Desk(tmp_path)
    _foreign_order(desk, "ABC")
    record = desk.decide(decision())
    assert record.admission == "refused" and record.refusal_cause.startswith("FOREIGN_EXPOSURE")


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
    monkeypatch.setattr("tickr.runtime.entries.realized_loss_today",
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
    from types import SimpleNamespace

    def trades(*nets):
        return lambda store, closed_on: [SimpleNamespace(trade_id=str(i), result_net_usd=n)
                                         for i, n in enumerate(nets)]

    monkeypatch.setattr("tickr.runtime.entries.closed_trades", trades(D(-20), D(5), D(-5)))
    desk = Desk(tmp_path)
    assert realized_loss_today(desk.engine, NOW) == D(20)  # wins offset losses (owner 2026-11-14)
    monkeypatch.setattr("tickr.runtime.entries.closed_trades", trades(D(-20), D(50)))
    assert realized_loss_today(desk.engine, NOW) == D(0)
    monkeypatch.setattr("tickr.runtime.entries.closed_trades", trades(D(-20), None))
    assert realized_loss_today(desk.engine, NOW) is None  # a trade's fees not recorded yet


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
    last_entry = datetime(2026, 11, 16, 15, 40, tzinfo=NEW_YORK)
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
    from tickr.engine.core import EntryRefused

    desk = Desk(tmp_path)
    with pytest.raises(EntryRefused):
        desk.engine.open_trade("ABC", D(1), D("10.10"), D("9.75"), D("0.35"),
                               entry_low_price=D("10.11"))
    with pytest.raises(EntryRefused):
        desk.engine.open_trade("ABC", D(1), D("10.10"), D("9.75"), D("0.35"),
                               entry_low_price=D("9.75"))


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _round_trip(engine: Engine, broker: FakeBroker, clock: Clock, symbol: str,
                opened: datetime, closed: datetime, exit_price: str) -> str:
    """Open, fill and close one trade (10 shares bought at 10.05) through the fake broker."""
    clock.at = opened
    trade_id = engine.open_trade(symbol, D(10), D("10.10"), D("9.75"), D("3.50"))
    broker.fill(engine.orders(trade_id)[0].client_order_id, D(10), D("10.05"))
    for _ in range(3):
        engine.step()
    (stop,) = [o for o in engine.orders(trade_id) if o.leg is Leg.STOP and o.live]
    clock.at = closed
    broker.fill(stop.client_order_id, D(10), D(exit_price))
    for _ in range(3):
        engine.step()
    assert not engine.trades(), "the trade should be closed"
    return trade_id


def test_cooldown_is_built_from_the_store_across_a_restart(tmp_path):
    def at(day: int, hh: int, mm: int) -> datetime:
        return datetime(2026, 11, day, hh, mm, tzinfo=NEW_YORK)

    clock = Clock(at(13, 11, 0))
    broker = FakeBroker()
    path = tmp_path / "db.sqlite3"
    store = Store.open(path, busy_timeout_seconds=0, now=clock)
    engine = Engine(store, broker, CFG, "run00001", lambda: GateResult(True, "t"))
    engine.recover()
    # Yesterday (Friday) ABC lost: another New York day, no pause today.
    _round_trip(engine, broker, clock, "ABC", at(13, 11, 0), at(13, 11, 5), "9.75")
    # Today: ABC won at 10:10 (no pause), ABC lost at 10:41, XYZ won.
    _round_trip(engine, broker, clock, "ABC", at(16, 10, 0), at(16, 10, 10), "10.50")
    _round_trip(engine, broker, clock, "ABC", at(16, 10, 30), at(16, 10, 41), "9.75")
    _round_trip(engine, broker, clock, "XYZ", at(16, 10, 45), at(16, 10, 50), "10.50")
    store.close()

    # Restart: a new store connection, engine and desk read the same database.
    clock.at = at(16, 11, 0)
    store = Store.open(path, busy_timeout_seconds=0, now=clock)
    engine = Engine(store, broker, CFG, "run00002", lambda: GateResult(True, "t"))
    engine.recover()
    desk = EntryDesk(engine, CFG, calendar=CALENDAR, last_broker_read_at=clock,
                     log=DecisionLog(tmp_path / "decisions"))
    assert desk._inputs(decision(), quote(), clock.at).symbol_last_loss_at == at(16, 10, 41)
    assert desk._inputs(decision(symbol="XYZ"), quote(), clock.at).symbol_last_loss_at is None

    def decide(now: datetime):
        clock.at = now
        q = Quote("ABC", D("10.04"), D("10.05"), D(100), D(100), now, now)
        return desk.on_decision(decision(), bar_start=now - timedelta(minutes=1), quote=q,
                                now=now)

    # The 30-minute pause after the 10:41 loss ends at 11:11 exactly.
    assert decide(at(16, 11, 10)).refusal_cause.startswith("SYMBOL_COOLDOWN")
    assert decide(at(16, 11, 11)).admission == "taken"


# --- manual trades are ignored (owner 2026-11-16): one rule for admission and the guard ------


def test_an_entry_beside_a_foreign_position_keeps_working(tmp_path):
    """Fable review of #539: the guard used every fence, so the entry admitted beside a
    foreign XYZ position was cancelled on the next pass."""
    desk = Desk(tmp_path)
    desk.broker.add_foreign_position("XYZ", D(1), D(5))
    desk.engine.step()
    opened(desk)
    assert guard(desk, quote()) == []
    assert desk.engine.trades()[0].trade_id and desk.engine.fences()


def test_manual_orders_without_an_id_are_all_seen(tmp_path, monkeypatch):
    """Fable review of #539: manual orders without a client order id share one fence; the
    symbols come from every foreign open order of the reconcile, so ABC is refused."""
    desk = Desk(tmp_path)
    manual = [OrderDetail(client_order_id="", symbol=s, side=Side.BUY,
                          order_type=OrderType.LIMIT, quantity=D(1), filled_quantity=D(0),
                          average_fill_price=None, state=OrderState.ACCEPTED,
                          raw_status="SUBMITTED", limit_price=D(5)) for s in ("ABC", "XYZ")]
    monkeypatch.setattr(desk.broker, "open_orders", lambda: list(manual))
    assert desk.engine.reconcile()
    assert desk.engine.foreign_symbols() == {"ABC", "XYZ"}
    record = desk.decide(decision())
    assert record.admission == "refused" and record.refusal_cause.startswith("FOREIGN_EXPOSURE")
    other = desk.decide(decision(symbol="QQQ"), replace(quote(), symbol="QQQ"))
    assert other.admission == "taken", other.refusal_cause
