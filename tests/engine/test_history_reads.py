"""Engine reads scale with today's work, not with history (backlog T17): the loop reads live
orders and the orders a broker row names, never the whole `orders` table, and still reaches
an old order of ours when a late fill or an open order names it."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tickr.broker.fake import FakeBroker
from tickr.engine.core import Fence, Leg, SendState, TradeState
from tickr.store import Store

from .test_engine import CFG, enter, has, legs, make, run, working_stops


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    yield s
    s.close()


def closed_history(e, b, trades: int) -> None:
    """`trades` whole trades: bought, protected, stopped out."""
    for _ in range(trades):
        t = enter(e)
        b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(2), D(10))
        run(e, 3)
        b.fill(legs(e, t, Leg.STOP)[0].client_order_id, D(2), D("9.80"))
        run(e, 3)
        assert e.trade(t).state is TradeState.CLOSED


def test_live_orders_are_exactly_the_live_ones(store):
    b = FakeBroker()
    e = make(store, b)
    closed_history(e, b, 3)
    t = enter(e)
    assert e.live_orders() == [o for o in e.orders() if o.live]
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(e, 3)
    assert [o.leg for o in e.live_orders()] == [Leg.STOP]
    assert e.live_orders() == [o for o in e.orders() if o.live]
    assert set(e.orders_by_id([o.client_order_id for o in e.orders()] + ["not-ours"])) == {
        o.client_order_id for o in e.orders()}


def test_a_step_reads_no_closed_order(store):
    b = FakeBroker()
    e = make(store, b)
    closed_history(e, b, 5)
    finished = {o.client_order_id for o in e.orders()}
    read: list[str] = []
    real = store.query

    def watched(sql, params=()):
        rows = real(sql, params)
        read.extend(r["client_order_id"] for r in rows if "client_order_id" in r.keys())
        return rows

    store.query = watched
    run(e, 3)
    e.reconcile()
    assert not finished & set(read)


def test_a_late_fill_of_an_old_order_we_gave_up_on_is_still_found(store):
    b = FakeBroker()
    b.script("unknown")
    e = make(store, b)
    t = enter(e)
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    hidden = b.orders.pop(cid)
    run(e, CFG.orders.unknown_absent_reads + 1)
    assert legs(e, t, Leg.ENTRY)[0].send_state is SendState.ABSENT
    closed_history(e, b, 5)  # newer trades in the same and other symbols since
    b.orders[cid] = hidden
    b.fill(cid, D(2), D(10))
    run(e, 4)
    assert e.trade(t).state is TradeState.OPEN
    assert working_stops(b) == D(2)
    assert has(e, Fence.RESURRECTED)
