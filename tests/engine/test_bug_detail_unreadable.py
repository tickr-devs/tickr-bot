"""Fixed bug: a Detail read that kept failing with a transport-level BrokerError was silent.
`_refresh` returned without a fence, so an order whose Detail could not be read kept its last
known state forever.

Found by the chaos test with persistent unreadable Detail (`Chaos(full=True)`, seed 22 among
others). Minimal case: the stop was verified working, then its Detail stops being readable;
an exit is requested (strategy exit or session flatten); the cancel of the stop takes at the
broker. The engine still counts the stop as live and verified, so `_exit_step` finds nothing
uncovered and sends no close, `_watch_protection` sees a verified stop and sets no
`unprotected` fence, and the cancel was answered, so no `cancel-unknown` fence either. The
broker holds the shares with no stop and no close, with no fence and no alert.

Breaks hard rule 2 (no position without a broker-side stop), spec 9 ("Unknown cancel
outcome: keep the stop, send nothing, alert"; here nothing alerts) and spec 9 status rule
("refuse, keep polling, block entries" for what cannot be established).

The fix (engine.core): a Detail read failing for `[orders] detail_unreadable_seconds` sets the
`detail-unreadable:<order>` fence (alerted at once by runtime.watch); while it is set the stop
is not counted as verified (so `unprotected` is set too) and a live entry or close no longer
excuses a quantity mismatch; and the exit teardown does not cancel a stop whose latest Detail
read failed (spec 9: keep the stop, send nothing, alert).
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tickr.broker.base import BrokerError
from tickr.broker.fake import FakeBroker
from tickr.engine.core import Engine, Fence, TradeState
from tickr.store import Store
from tickr.types import OrderState, OrderType

from .test_chaos import Chaos
from .test_engine import CFG, OPEN, POLL, Leg, enter, has, legs, make, run, working_stops
from .test_reviewer_findings import TeardownWatch, assert_unreadable_stop_kept


@pytest.fixture
def store():
    s = Store.open(":memory:", busy_timeout_seconds=0)
    yield s
    s.close()


class StopDetailDies(TeardownWatch):
    """Detail of STOP_LOSS orders fails once `dead` is set; a cancel takes at once and, with
    `dies_on_cancel`, sets `dead` (the cancelled stop's Detail cannot be read)."""

    dead = False
    dies_on_cancel = False

    def read_detail(self, client_order_id):
        order = FakeBroker.order_detail(self, client_order_id)
        if self.dead and order.order_type is OrderType.STOP_LOSS:
            raise BrokerError("Detail body could not be read")
        return order

    def take_cancel(self, client_order_id):
        TeardownWatch.take_cancel(self, client_order_id)
        if self.dies_on_cancel:
            self.dead = True


def exit_with_unreadable_stop(store, *, dies_on_cancel=False):
    """Detail of the stop dies before the exit is requested or, with `dies_on_cancel`, at the
    moment its cancel takes (the engine read it fine just before deciding the cancel)."""
    broker = StopDetailDies()
    engine = make(store, broker)
    trade = enter(engine, "2")
    broker.fill(legs(engine, trade, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(engine, 4)
    assert legs(engine, trade, Leg.STOP)[0].broker_state is OrderState.ACCEPTED
    assert working_stops(broker) == 2 and not engine.fences()
    broker.dead = not dies_on_cancel
    broker.dies_on_cancel = dies_on_cancel
    engine.request_exit(trade, "session-flatten")
    run(engine, 30)  # 90 s
    return broker, engine


def test_bare_position_after_cancel_is_fenced(store):
    # The stop's Detail dies only once its cancel took: the engine could not have known before
    # sending the cancel. It cannot confirm the cancel, so it sends nothing and fences.
    broker, engine = exit_with_unreadable_stop(store, dies_on_cancel=True)
    assert {p.symbol: p.quantity for p in broker.positions()} == {"ABC": D(2)}
    assert working_stops(broker) == 0
    assert engine.fences(), "shares held with no stop and no close, and no fence"
    (stop,) = legs(engine, "t1", Leg.STOP)
    fences = engine.fences()
    assert fences[f"{Fence.DETAIL_UNREADABLE}:{stop.client_order_id}"].startswith(
        "stop cancel sent and its Detail unreadable")
    assert f"{Fence.UNPROTECTED}:t1" in fences
    assert not [r for r in broker.sent if r.order_type is OrderType.MARKET]  # never guesses


def test_unreadable_stop_is_kept_through_the_exit_and_closed_once_readable(store):
    """The stop's Detail dies before the exit: the stop is not cancelled (it keeps
    protecting), entries are fenced; once Detail reads again the teardown goes on as usual."""
    broker, engine = exit_with_unreadable_stop(store)
    (stop,) = legs(engine, "t1", Leg.STOP)
    assert stop.cancel_state is None and working_stops(broker) == 2
    fences = engine.fences()
    assert fences[f"{Fence.DETAIL_UNREADABLE}:{stop.client_order_id}"].startswith(
        "Detail unreadable for")
    assert f"{Fence.UNPROTECTED}:t1" in fences  # an unreadable stop is not verified
    broker.dead = False
    run(engine, 6)
    (close,) = legs(engine, "t1", Leg.EXIT)
    assert legs(engine, "t1", Leg.STOP)[0].broker_state is OrderState.CANCELLED
    broker.fill(close.client_order_id, D(2), D(10))
    run(engine, 3)
    assert engine.trade("t1").state is TradeState.CLOSED
    assert engine.fences() == {}


def test_a_short_detail_outage_sets_no_fence(store):
    broker = StopDetailDies()
    engine = make(store, broker)
    trade = enter(engine, "2")
    broker.fill(legs(engine, trade, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(engine, 4)
    broker.dead = True
    run(engine, int(float(CFG.orders.detail_unreadable_seconds) / POLL) - 1)
    assert engine.fences() == {}
    broker.dead = False
    run(engine, 1)
    broker.dead = True
    run(engine, int(float(CFG.orders.detail_unreadable_seconds) / POLL) - 1)
    assert engine.fences() == {}  # the wait starts again after an answered read
    run(engine, 2)
    assert has(engine, Fence.DETAIL_UNREADABLE) and has(engine, Fence.UNPROTECTED)


def test_held_shares_keep_the_stop_they_cannot_read(store):
    broker, engine = exit_with_unreadable_stop(store)
    assert_unreadable_stop_kept(broker, engine, D(2))


def test_chaos_with_unreadable_detail():
    for seed in range(60):
        Chaos(seed, full=True).run()


def test_the_unreadable_wait_survives_a_restart(store):
    """The first failed read of a streak is journaled, so a restart does not start the wait
    again (a bot restarting every few seconds would otherwise never fence)."""
    broker = StopDetailDies()
    engine = make(store, broker)
    trade = enter(engine, "2")
    broker.fill(legs(engine, trade, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(engine, 4)
    broker.dead = True
    steps = int(float(CFG.orders.detail_unreadable_seconds) / POLL)
    run(engine, steps - 1)
    assert engine.fences() == {}
    again = Engine(store, broker, CFG, "restart1", lambda: OPEN, clock=engine.clock)
    run(again, 2)
    assert has(again, Fence.DETAIL_UNREADABLE)


def test_a_detail_outage_on_a_verified_stop_alerts_but_never_closes(store):
    """Reviewer finding (PR #372): a working, verified stop whose Detail fails for 75 s keeps
    the trade as it is. The outage is fenced and alerted (`detail-unreadable`, `unprotected`),
    but no exit is requested and no `unprotected-timeout` is set: the stop is never cancelled,
    no MARKET order is sent, and every fence clears once Detail answers again."""
    broker = StopDetailDies()
    engine = make(store, broker)
    trade = enter(engine, "2")
    broker.fill(legs(engine, trade, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(engine, 4)
    assert working_stops(broker) == 2 and not engine.fences()
    broker.dead = True
    run(engine, 25)  # 75 s, past detail_unreadable_seconds + max_unprotected_seconds
    assert has(engine, Fence.DETAIL_UNREADABLE) and has(engine, Fence.UNPROTECTED)
    assert not has(engine, Fence.UNPROTECTED_TIMEOUT)
    assert engine.trade(trade).exit_requested is None
    broker.dead = False
    run(engine, 5)
    (stop,) = legs(engine, trade, Leg.STOP)
    assert stop.cancel_state is None and stop.broker_state is OrderState.ACCEPTED
    assert working_stops(broker) == 2
    assert not [r for r in broker.sent if r.order_type is OrderType.MARKET]
    assert engine.trade(trade).exit_requested is None
    assert engine.trade(trade).state is TradeState.OPEN
    assert engine.fences() == {}
