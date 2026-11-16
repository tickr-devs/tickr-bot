"""A partial sale (scale-out): the engine releases stops for exactly the shares to sell,
sells them once at market, and keeps the rest under its stop throughout."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tickr.broker.fake import FakeBroker
from tickr.engine.core import Fence, Leg, ReduceState, TradeState, _stops_to_release
from tickr.store import Store
from tickr.types import OrderType, Side

from .test_engine import CFG, assert_single_sends, has, legs, make, run, working_stops


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    yield s
    s.close()


def sells_working(broker) -> D:
    """Shares that resting stops and working sells could sell together."""
    return sum((o.quantity - o.filled_quantity for o in broker.open_orders()
                if o.side is Side.SELL), D(0))


def held(broker) -> D:
    return sum((p.quantity for p in broker.positions()), D(0))


def scaled(store, qty="10", split="5"):
    b = FakeBroker()
    e = make(store, b)
    t = e.open_trade("ABC", D(qty), D("10.00"), D("9.80"), D("2"),
                     scale_out_quantity=None if split is None else D(split))
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(qty), D(10))
    run(e, 3)
    assert working_stops(b) == D(qty) and not has(e, Fence.UNPROTECTED)
    return b, e, t


def drive(e, b, steps=6):
    """Step the engine, checking each pass that nothing could sell more than is held."""
    for _ in range(steps):
        run(e)
        assert sells_working(b) <= held(b)


def test_stops_are_split_at_the_scale_out_boundary(store):
    b, e, t = scaled(store)
    assert [s.quantity for s in legs(e, t, Leg.STOP)] == [D(5), D(5)]


def test_partial_entry_fills_still_leave_a_boundary_at_the_scale_out(store):
    b = FakeBroker()
    e = make(store, b)
    t = e.open_trade("ABC", D(10), D("10.00"), D("9.80"), D("2"), scale_out_quantity=D(5))
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.fill(cid, D(3), D(10))
    run(e, 3)
    b.fill(cid, D(7), D(10))
    run(e, 3)
    assert [s.quantity for s in legs(e, t, Leg.STOP)] == [D(3), D(2), D(5)]


def test_a_reduce_sells_the_planned_part_and_the_rest_stays_stopped(store):
    b, e, t = scaled(store)
    first, second = legs(e, t, Leg.STOP)
    assert e.request_reduce(t, D(5), "strategy-scale-out")
    drive(e, b, 3)
    assert e.order(first.client_order_id).broker_state.terminal
    assert e.order(second.client_order_id).cancel_state is None
    (sale,) = legs(e, t, Leg.EXIT)
    assert (sale.quantity, sale.order_type) == (D(5), OrderType.MARKET)
    assert e.trade(t).reduce_state is ReduceState.SENT
    assert working_stops(b) == D(5)  # the other 5 never lost their stop
    b.fill(sale.client_order_id, D(5), D("10.20"))
    drive(e, b, 3)
    trade = e.trade(t)
    assert trade.reduce_state is ReduceState.DONE and trade.state is TradeState.OPEN
    assert e.expected_quantity(e.orders(t)) == D(5) == working_stops(b) == held(b)
    assert not has(e, Fence.UNPROTECTED) and not has(e, Fence.QUANTITY_MISMATCH)
    assert_single_sends(b)
    assert [r.body for r in store.journal("reduce-done", t)] == [{"sold": "5"}]


def test_a_reduce_without_a_planned_split_is_refused_with_its_cause(store):
    b, e, t = scaled(store, split=None)  # one stop of 10
    assert not e.request_reduce(t, D(4), "strategy-scale-out")
    assert "not the planned scale-out" in e.reduce_refusal(t, D(4))
    drive(e, b, 3)
    assert legs(e, t, Leg.EXIT) == [] and working_stops(b) == D(10) == held(b)


def test_a_reduce_other_than_the_planned_split_is_refused(store):
    b, e, t = scaled(store, split="5")  # stops 5 + 5
    assert not e.request_reduce(t, D(3), "strategy-scale-out")
    drive(e, b, 3)
    assert legs(e, t, Leg.EXIT) == [] and working_stops(b) == D(10) == held(b)
    assert store.journal("reduce-requested", t) == []


def test_a_second_reduce_and_bad_quantities_are_refused(store):
    b, e, t = scaled(store)
    assert not e.request_reduce(t, D(10), "x")  # nothing would be left
    assert not e.request_reduce(t, D("2.5"), "x")
    assert not e.request_reduce(t, D(0), "x")
    assert e.request_reduce(t, D(5), "x")
    assert not e.request_reduce(t, D(1), "x")
    assert len(store.journal("reduce-requested", t)) == 1


def test_a_reduce_is_refused_while_the_entry_works(store):
    b = FakeBroker()
    e = make(store, b)
    t = e.open_trade("ABC", D(10), D("10.00"), D("9.80"), D("2"))
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(4), D(10))
    run(e, 3)
    assert not e.request_reduce(t, D(2), "x")


def test_an_exit_during_a_reduce_waits_for_the_sale_then_closes_the_rest(store):
    b, e, t = scaled(store)
    e.request_reduce(t, D(5), "x")
    drive(e, b, 3)
    (sale,) = legs(e, t, Leg.EXIT)
    e.request_exit(t, "session-flatten")
    drive(e, b, 3)
    assert e.trade(t).reduce_state is ReduceState.DONE
    assert len(legs(e, t, Leg.EXIT)) == 1  # nothing more while the sale works
    b.fill(sale.client_order_id, D(5), D("10.20"))
    drive(e, b, 4)
    close = [x for x in legs(e, t, Leg.EXIT) if x.client_order_id != sale.client_order_id]
    assert [c.quantity for c in close] == [D(5)]
    b.fill(close[0].client_order_id, D(5), D("10.10"))
    drive(e, b, 3)
    assert e.trade(t).state is TradeState.CLOSED and held(b) == 0
    assert_single_sends(b)


def test_a_refused_sale_ends_the_reduce_and_restores_the_stop(store):
    b, e, t = scaled(store)
    e.request_reduce(t, D(5), "x")
    run(e)  # releases the first stop
    b.script("refuse")
    drive(e, b, 4)
    trade = e.trade(t)
    assert trade.reduce_state is ReduceState.DONE and trade.state is TradeState.OPEN
    assert has(e, Fence.EXIT_REFUSED)
    assert working_stops(b) == D(10) == held(b)


def test_a_reduce_is_refused_once_a_stop_has_sold_the_rest(store):
    b, e, t = scaled(store)
    first, second = legs(e, t, Leg.STOP)
    b.fill(second.client_order_id, D(5), D("9.80"))  # half stopped out before the request
    run(e, 3)
    assert not e.request_reduce(t, D(5), "x")
    assert "would leave nothing" in e.reduce_refusal(t, D(5))


def test_a_kept_stop_filling_during_the_release_closes_the_released_shares_at_market(store):
    b, e, t = scaled(store)
    first, second = legs(e, t, Leg.STOP)
    assert e.request_reduce(t, D(5), "x")
    run(e)  # the first stop's release is sent
    assert e.order(first.client_order_id).cancelling
    b.fill(second.client_order_id, D(5), D("9.80"))  # the kept stop fires meanwhile
    drive(e, b, 4)
    trade = e.trade(t)
    assert trade.reduce_state is ReduceState.DONE
    assert [r.body["cause"] for r in store.journal("exit-requested", t)] == [
        "stop-filled-during-scale-out"]
    # never stopped again at a price the market traded through: one MARKET close of the 5
    stop_cids = {s.client_order_id for s in legs(e, t, Leg.STOP)}
    assert stop_cids == {first.client_order_id, second.client_order_id}
    (close,) = legs(e, t, Leg.EXIT)
    assert (close.quantity, close.order_type) == (D(5), OrderType.MARKET)
    b.fill(close.client_order_id, D(5), D("9.70"))
    drive(e, b, 3)
    assert e.trade(t).state is TradeState.CLOSED and held(b) == 0
    assert_single_sends(b)


def test_stops_that_change_before_the_release_end_the_reduce_unsent(store):
    b, e, t = scaled(store)
    first, second = legs(e, t, Leg.STOP)
    assert e.request_reduce(t, D(5), "x")
    b.fill(first.client_order_id, D(2), D("9.80"))  # stops now 3 + 5, holds 8: release 5
    b.fill(second.client_order_id, D(1), D("9.80"))  # stops 3 + 4, holds 7: no exact 5
    drive(e, b, 4)
    assert e.trade(t).reduce_state is ReduceState.DONE
    assert legs(e, t, Leg.EXIT) == []
    assert working_stops(b) == D(7) == held(b) and not has(e, Fence.UNPROTECTED)
    assert "no live stops cover exactly" in store.journal("reduce-done", t)[0].body["not_sent"]


def test_release_picks_stops_that_add_up_exactly():
    from tickr.engine.core import OrderRow, SendState
    from tickr.types import OrderState

    def stop(n, qty):
        return OrderRow(f"s{n}", "t1", Leg.STOP, "ABC", Side.SELL, OrderType.STOP_LOSS, D(qty),
                        None, D("9"), SendState.ACKNOWLEDGED, OrderState.ACCEPTED, D(0), None,
                        None, 0, None, 0, None)

    stops = [stop(1, 3), stop(2, 2), stop(3, 5)]
    assert [s.client_order_id for s in _stops_to_release(stops, D(5))] == ["s1", "s2"]
    assert [s.client_order_id for s in _stops_to_release(stops, D(7))] == ["s3", "s2"]
    assert _stops_to_release(stops, D(4)) is None  # no exact set: nothing is released
    assert CFG.orders.stop_retry_attempts >= 0
