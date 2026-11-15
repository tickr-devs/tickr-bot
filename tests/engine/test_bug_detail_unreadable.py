"""BUG (stays RED until fixed; xfail strict): a Detail read that keeps failing with a
transport-level BrokerError is silent. `_refresh` returns without a fence, so an order whose
Detail cannot be read keeps its last known state forever.

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
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tickr.broker.base import BrokerError
from tickr.broker.fake import FakeBroker
from tickr.store import Store
from tickr.types import OrderState, OrderType

from .test_chaos import Chaos
from .test_engine import Leg, enter, legs, make, run, working_stops
from .test_reviewer_findings import TeardownWatch, assert_unreadable_stop_kept


@pytest.fixture
def store():
    s = Store.open(":memory:", busy_timeout_seconds=0)
    yield s
    s.close()


class StopDetailDies(TeardownWatch):
    """Detail of STOP_LOSS orders fails once `dead` is set; a cancel takes at once."""

    dead = False

    def read_detail(self, client_order_id):
        order = FakeBroker.order_detail(self, client_order_id)
        if self.dead and order.order_type is OrderType.STOP_LOSS:
            raise BrokerError("Detail body could not be read")
        return order


def exit_with_unreadable_stop(store):
    broker = StopDetailDies()
    engine = make(store, broker)
    trade = enter(engine, "2")
    broker.fill(legs(engine, trade, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(engine, 4)
    assert legs(engine, trade, Leg.STOP)[0].broker_state is OrderState.ACCEPTED
    assert working_stops(broker) == 2 and not engine.fences()
    broker.dead = True
    engine.request_exit(trade, "session-flatten")
    run(engine, 30)  # 90 s
    return broker, engine


@pytest.mark.xfail(strict=True, reason="bug: unreadable stop Detail after its cancel took "
                   "leaves the position bare with no fence and no alert")
def test_bare_position_after_cancel_is_fenced(store):
    broker, engine = exit_with_unreadable_stop(store)
    assert {p.symbol: p.quantity for p in broker.positions()} == {"ABC": D(2)}
    assert working_stops(broker) == 0
    assert engine.fences(), "shares held with no stop and no close, and no fence"


@pytest.mark.xfail(strict=True, reason="bug: the engine cancels the stop it cannot read and "
                   "then can neither confirm the cancel nor close, with no fence")
def test_held_shares_keep_the_stop_they_cannot_read(store):
    broker, engine = exit_with_unreadable_stop(store)
    assert_unreadable_stop_kept(broker, engine, D(2))


@pytest.mark.xfail(strict=True, reason="bug: chaos with persistent unreadable Detail finds "
                   "held shares with no stop, no close and no fence on the trade: seed 22 "
                   "(stop cancelled, see module docstring), seeds 26 and 57 (unreadable "
                   "entry Detail, test_reviewer_findings.py bug C)")
def test_chaos_with_unreadable_detail():
    for seed in range(60):
        Chaos(seed, full=True).run()
