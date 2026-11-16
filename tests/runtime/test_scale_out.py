"""The active strategy's scale-out: ENTER with `scale_out`, then EXIT with `quantity`."""

from __future__ import annotations

import json
from decimal import Decimal as D

import pytest

from tickr.engine.core import Leg, ReduceState
from tickr.research.records import record_from_mapping, record_to_json
from tickr.runtime.entries import REDUCE_REFUSED
from tickr.strategy.base import Verdict

from .test_entries import GATE_OPEN, Desk, decision

pytestmark = pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")


def test_a_scale_out_enter_then_a_partial_exit_sells_that_part(tmp_path):
    desk = Desk(tmp_path)
    record = desk.decide(decision(scale_out=D(5)))
    assert record.admission == "taken"
    (trade,) = desk.engine.trades()
    assert trade.scale_out_quantity == D(5)
    desk.broker.fill(desk.engine.orders(trade.trade_id)[0].client_order_id, D(10), D("10.05"))
    for _ in range(3):
        desk.engine.step()
    assert [s.quantity for s in desk.engine.orders(trade.trade_id) if s.leg is Leg.STOP] == [
        D(5), D(5)]
    desk.decide(decision(Verdict.EXIT, quantity=D(5)))
    assert desk.engine.trade(trade.trade_id).reduce_state is ReduceState.REQUESTED
    assert desk.engine.trade(trade.trade_id).exit_requested is None


@pytest.mark.parametrize("scale_out", [D(0), D(10), D("2.5"), D(11)])
def test_a_scale_out_outside_the_quantity_is_refused(tmp_path, scale_out):
    desk = Desk(tmp_path)
    record = desk.decide(decision(scale_out=scale_out))
    assert record.admission == "refused"
    assert record.refusal_cause.startswith("DECISION_INVALID")


def test_a_partial_exit_the_engine_cannot_take_is_journaled_once(tmp_path):
    desk = Desk(tmp_path)
    desk.decide(decision())
    (trade,) = desk.engine.trades()  # entry still working: no reduce possible
    desk.decide(decision(Verdict.EXIT, quantity=D(5)))
    desk.decide(decision(Verdict.EXIT, quantity=D(5)))
    (row,) = desk.store.journal(REDUCE_REFUSED, trade.trade_id)
    assert row.body["quantity"] == "5"
    assert desk.engine.trade(trade.trade_id).exit_requested is None


def test_scale_out_round_trips_in_records(tmp_path):
    desk = Desk(tmp_path)
    record = desk.decide(decision(scale_out=D(5)))
    line = record_to_json(record)
    assert json.loads(line)["scale_out"] == "5"
    assert record_from_mapping(json.loads(line)) == record
    assert "scale_out" not in record_to_json(desk.decide(decision(Verdict.WATCH)))


def test_the_first_fill_seen_is_what_the_position_view_carries(tmp_path):
    from datetime import datetime

    from tickr.runtime.evaluation import _first_fill_seen

    desk = Desk(tmp_path)
    desk.decide(decision())
    (trade,) = desk.engine.trades()
    (entry,) = desk.engine.orders(trade.trade_id)
    desk.engine.step()
    assert _first_fill_seen(desk.engine, entry.client_order_id) is None
    desk.broker.fill(entry.client_order_id, D(4), D("10.05"))
    desk.engine.step()
    seen = _first_fill_seen(desk.engine, entry.client_order_id)
    assert isinstance(seen, datetime) and seen.tzinfo is not None
    desk.broker.fill(entry.client_order_id, D(6), D("10.05"))
    desk.engine.step()
    assert _first_fill_seen(desk.engine, entry.client_order_id) == seen


def test_the_simulator_refuses_a_scale_out_entry():
    from tickr.replay.engine import _entry_problem

    assert _entry_problem(decision(scale_out=D(5))) == "scale-out (partial exits) is not modelled"
