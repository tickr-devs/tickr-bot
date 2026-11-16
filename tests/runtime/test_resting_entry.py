"""A resting limit entry (Decision.rest_bars): a pullback buy that waits below the ask for a
bounded number of bars, then is cancelled; its fills are protected like any entry's."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import pytest

from tickr.config import GateResult
from tickr.engine.core import EntryRefused, Fence, Leg
from tickr.research.records import load_records, record_from_mapping, record_to_json
from tickr.risk.admission import AdmissionLimits
from tickr.runtime import entries
from tickr.strategy.base import Verdict

from .test_entries import BAR, CFG, GATE_OPEN, NOW, Desk, _foreign_order, decision, guard, quote

pytestmark = pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
REST = 5
M1 = timedelta(minutes=1)


def resting(**overrides):
    # Limit 9.90 under an ask of 10.05: a pullback buy.
    values = dict(entry_low=D("9.80"), entry_high=D("9.90"), stop=D("9.60"), rest_bars=REST)
    values.update(overrides)
    return decision(**values)


def opened(desk: Desk) -> str:
    record = desk.decide(resting())
    assert record.admission == "taken", record.refusal_cause
    (trade,) = desk.engine.trades()
    return trade.trade_id


def test_a_resting_entry_is_admitted_with_the_ask_above_its_limit(tmp_path):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    trade = desk.engine.trade(trade_id)
    (entry,) = desk.engine.orders(trade_id)
    assert (entry.limit_price, trade.stop_price) == (D("9.90"), D("9.60"))
    assert trade.entry_rest_until == BAR + (REST + 1) * M1
    (row,) = desk.store.journal("trade-opened", trade_id)
    assert row.body["rest_until"] == (BAR + (REST + 1) * M1).isoformat()


def test_a_marketable_entry_with_the_ask_above_its_limit_is_still_refused(tmp_path):
    desk = Desk(tmp_path)
    record = desk.decide(resting(rest_bars=None))
    assert record.admission == "refused"
    assert record.refusal_cause.startswith("PRICE_OUTSIDE_WINDOW")


@pytest.mark.parametrize("rest", [0, -1, CFG.risk.max_entry_rest_bars + 1, True, 2.5])
def test_a_rest_outside_the_platform_limit_is_refused(tmp_path, rest):
    desk = Desk(tmp_path)
    record = desk.decide(resting(rest_bars=rest))
    assert record.admission == "refused"
    assert record.refusal_cause.startswith("DECISION_INVALID")
    assert desk.engine.trades() == []


def test_a_resting_entry_is_refused_when_the_ask_is_already_below_its_window(tmp_path):
    desk = Desk(tmp_path)
    record = desk.decide(resting(), q=at("9.79", NOW))
    assert record.refusal_cause.startswith("PRICE_OUTSIDE_WINDOW")


def at(ask: str, now):
    q = quote(ask=ask)
    return type(q)(q.symbol, D(ask) - D("0.01"), D(ask), q.bid_size, q.ask_size,
                   now - timedelta(seconds=1), now)


END = BAR + (REST + 1) * M1


@pytest.mark.parametrize(("ask", "now", "cause"), [
    ("10.40", NOW, None),                                   # far above the limit: rests
    ("9.85", NOW, None),                                    # inside the window
    ("9.79", NOW, "ask 9.79 below the window 9.80"),
    ("10.40", END - timedelta(seconds=1), None),            # last second of its rest
    ("10.40", END, "rest time over"),                       # rest over: cancelled
])
def test_a_resting_entry_works_until_its_rest_is_over(tmp_path, ask, now, cause):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    q = at(ask, now)
    causes = guard(desk, q, now)
    (entry,) = desk.engine.orders(trade_id)
    if cause is None:
        assert causes == [] and entry.cancel_state is None
    else:
        assert len(causes) == 1 and causes[0].startswith(cause)
        assert entry.cancel_state == "sent"


def test_a_partly_filled_resting_entry_is_protected_and_the_rest_cancelled(tmp_path):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    (entry,) = desk.engine.orders(trade_id)
    desk.broker.fill(entry.client_order_id, D(4), D("9.90"))
    desk.engine.step()
    desk.engine.step()
    stops = [o for o in desk.engine.orders(trade_id) if o.leg is Leg.STOP]
    assert sum(s.quantity for s in stops) == D(4)
    assert {s.stop_price for s in stops} == {D("9.60")}
    (cause,) = guard(desk, at("10.40", END), END)
    assert cause.startswith("rest time over")
    desk.engine.step()
    desk.engine.step()
    assert desk.engine.expected_quantity(desk.engine.orders(trade_id)) == D(4)
    assert all(s.cancel_state is None for s in desk.engine.orders(trade_id)
               if s.leg is Leg.STOP)


def test_the_engine_refuses_a_resting_entry_without_a_window_or_an_aware_end(tmp_path):
    desk = Desk(tmp_path)
    with pytest.raises(EntryRefused):
        desk.engine.open_trade("ABC", D(1), D("9.90"), D("9.60"), D("0.30"),
                               rest_until=NOW + M1)
    with pytest.raises(EntryRefused):
        desk.engine.open_trade("ABC", D(1), D("9.90"), D("9.60"), D("0.30"),
                               entry_low_price=D("9.80"), rest_until=(NOW + M1).replace(
                                   tzinfo=None))


def test_rest_bars_round_trips_in_records_and_is_absent_when_unset(tmp_path):
    desk = Desk(tmp_path)
    desk.decide(resting())
    desk.decide(decision(Verdict.WATCH))
    desk.desk.log.close()
    path = desk.dir / "2026-11-16" / "decisions.jsonl"
    lines = path.read_text().splitlines()
    assert '"rest_bars": 5' in lines[0] and "rest_bars" not in lines[1]
    first, second = load_records([path])
    assert (first.decision.rest_bars, second.decision.rest_bars) == (REST, None)
    assert record_from_mapping(json.loads(record_to_json(first))) == first
    bad = lines[1].replace('"role"', '"rest_bars": 2, "role"')
    with pytest.raises(ValueError, match="only for an ENTER"):
        record_from_mapping(json.loads(bad))


def test_the_limit_comes_from_config():
    assert AdmissionLimits.from_config(CFG).max_entry_rest_bars == CFG.risk.max_entry_rest_bars


# --- a working entry ends while new entries are blocked (Fable review of #527) -----------


def _pause(desk: Desk) -> None:
    with desk.store.transaction():
        desk.engine.entry_stop_row(True, "operator pause")


def _foreign(desk: Desk) -> None:
    """A manual order in the entry's own symbol (owner 2026-11-16: manual trades block only
    their own symbol)."""
    _foreign_order(desk, "ABC")


def _gate(desk: Desk) -> None:
    desk.engine.gate = lambda: GateResult(False, "owner closed it")


@pytest.mark.parametrize(("block", "cause"), [
    (_pause, "entries fenced: entry-stop"),
    (_foreign, "entries fenced: foreign-order"),
    (_gate, "trading gate closed: owner closed it"),
])
def test_a_resting_entry_is_cancelled_once_new_entries_are_blocked(tmp_path, block, cause):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    assert guard(desk, at("10.40", NOW), NOW) == []  # resting, nothing blocks
    block(desk)
    later = NOW + 3 * M1
    (journaled,) = guard(desk, at("10.40", later), later)
    assert journaled.startswith(cause)
    (entry,) = desk.engine.orders(trade_id)
    assert entry.cancel_state == "sent"


def test_a_resting_entry_keeps_working_beside_a_manual_trade_in_another_symbol(tmp_path):
    desk = Desk(tmp_path)
    opened(desk)
    desk.broker.add_foreign_position("XYZ", D(1), D(5))
    desk.engine.step()
    _foreign_order(desk, "XYZ")
    later = NOW + 3 * M1
    assert guard(desk, at("10.40", later), later) == []


def test_a_resting_entry_is_cancelled_when_another_trade_uses_up_the_daily_loss(tmp_path,
                                                                                monkeypatch):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    room = entries.daily_loss_room(desk.engine, CFG, NOW)
    # Another trade closed today at a loss larger than the room this one left.
    monkeypatch.setattr(entries, "realized_loss_today", lambda engine, now: room + D(1))
    (journaled,) = guard(desk, at("10.40", NOW), NOW)
    assert journaled.startswith("daily loss ceiling")
    assert desk.engine.orders(trade_id)[0].cancel_state == "sent"


def test_a_resting_entry_is_cancelled_while_todays_money_is_unknown(tmp_path, monkeypatch):
    desk = Desk(tmp_path)
    opened(desk)
    monkeypatch.setattr(entries, "realized_loss_today", lambda engine, now: None)
    (journaled,) = guard(desk, at("10.40", NOW), NOW)
    assert journaled.startswith("today's realized result unknown")


def test_a_fence_about_the_trade_itself_leaves_its_entry_to_the_engine(tmp_path):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    with desk.store.transaction():
        desk.engine._fence_row(Fence.UNPROTECTED, trade_id, "partial fill, stop on its way")
    assert guard(desk, at("10.40", NOW), NOW) == []


def test_a_fill_racing_the_block_cancel_is_protected(tmp_path):
    desk = Desk(tmp_path)
    trade_id = opened(desk)
    (entry,) = desk.engine.orders(trade_id)
    _pause(desk)
    desk.broker.fill(entry.client_order_id, D(3), D("9.90"))  # fills before the cancel lands
    guard(desk, at("10.40", NOW), NOW)
    desk.engine.step()
    desk.engine.step()
    stops = [o for o in desk.engine.orders(trade_id) if o.leg is Leg.STOP]
    assert sum(s.quantity for s in stops) == D(3)


def test_the_simulator_refuses_a_resting_entry():
    from tickr.replay.engine import _entry_problem

    assert _entry_problem(resting()) == "resting limit entries (rest_bars) are not modelled"
    assert _entry_problem(decision()) is None
