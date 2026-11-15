"""Findings of an independent review of main, as deterministic tests on the real Engine over
FakeBroker subclasses (3 s per pass, a reconcile every third pass, like the loop).

Test names say what each does: `test_bug_*` asserts the spec-correct behaviour and is
`xfail(strict=True)` until a fix flips it; `test_pins_*` pins current behaviour that agrees
with the spec (or with an unproven broker fact named in docs/webull-facts.md), so a change to
it is deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal as D

import pytest

from tickr.broker.base import BrokerError, OrderNotFound, OutcomeUnknown
from tickr.broker.fake import FakeBroker
from tickr.engine.core import Engine, Fence, SendState
from tickr.store import Store
from tickr.types import OrderState, OrderType, Side

from .test_engine import CFG, OPEN, Clock, Leg, enter, legs, make, working_stops


class Unreadable(BrokerError):
    """A broker body the adapter cannot parse."""


@pytest.fixture
def store():
    s = Store.open(":memory:", busy_timeout_seconds=0)
    yield s
    s.close()


def loop(engine: Engine, passes: int) -> None:
    for i in range(passes):
        engine.clock.now += 3
        if i % 3 == 2:
            engine.reconcile()
        else:
            engine.step()


def fill_entry(broker: FakeBroker, engine: Engine, trade: str) -> None:
    broker.fill(legs(engine, trade, Leg.ENTRY)[0].client_order_id, D(2), D(10))


def held(broker: FakeBroker) -> dict[str, D]:
    return {p.symbol: p.quantity for p in FakeBroker.positions(broker)}


def sent(broker: FakeBroker, order_type: OrderType) -> int:
    return sum(1 for r in broker.sent if r.order_type is order_type)


# --- A ---------------------------------------------------------------------------------------


@dataclass
class TeardownWatch(FakeBroker):
    """FakeBroker that watches spec 9's exit teardown (cancel stop -> confirm CANCELLED ->
    re-read held quantity -> close) from the broker's side. Subclasses answer Detail in
    `read_detail` and take a cancel in `take_cancel`."""

    failing: set[str] = field(default_factory=set)  # orders whose latest Detail read failed
    shown_ended: set[str] = field(default_factory=set)  # orders Detail showed ended
    stop_cancels: list[str] = field(default_factory=list)  # stop cancels sent
    blind_cancels: list[str] = field(default_factory=list)  # ... while its last read failed
    early_closes: list[str] = field(default_factory=list)  # stops cancelled, not shown ended,
    # when a MARKET order was sent: a possible double sale

    def read_detail(self, client_order_id):
        return FakeBroker.order_detail(self, client_order_id)

    def take_cancel(self, client_order_id):
        order = FakeBroker.order_detail(self, client_order_id)
        self.orders[client_order_id] = replace(order, state=OrderState.CANCELLED,
                                               raw_status="CANCELLED")

    def order_detail(self, client_order_id):
        try:
            order = self.read_detail(client_order_id)
        except OrderNotFound:
            self.failing.discard(client_order_id)
            raise
        except BrokerError:
            self.failing.add(client_order_id)
            raise
        self.failing.discard(client_order_id)
        if order.state.terminal:
            self.shown_ended.add(client_order_id)
        return order

    def cancel(self, client_order_id):
        if FakeBroker.order_detail(self, client_order_id).order_type is OrderType.STOP_LOSS:
            self.stop_cancels.append(client_order_id)
            if client_order_id in self.failing:
                self.blind_cancels.append(client_order_id)
        self.take_cancel(client_order_id)

    def place(self, request):
        if request.order_type is OrderType.MARKET:
            self.early_closes += [c for c in self.stop_cancels if c not in self.shown_ended]
        return FakeBroker.place(self, request)


def assert_unreadable_stop_kept(broker: TeardownWatch, engine: Engine, quantity: D) -> None:
    """Hard rule 2 and spec 9 teardown with a stop whose Detail cannot be read: the stop is
    not cancelled while its latest Detail read failed, so it keeps working for the shares;
    a cancel sent while it was readable is followed by no MARKET order until Detail shows the
    stop ended (else stop and close could both sell); and entries are fenced (alerted)."""
    assert not broker.blind_cancels, "stop cancelled while its Detail could not be read"
    assert not broker.early_closes, "close sent before the cancelled stop was shown ended"
    if not broker.stop_cancels:
        assert working_stops(broker) >= quantity
    assert engine.fences(), "held shares with an unreadable stop, and no fence"


class StopDetailUnreadable(TeardownWatch):
    """The stop works at the broker, its Detail cannot be parsed, a cancel takes."""

    def read_detail(self, client_order_id):
        order = FakeBroker.order_detail(self, client_order_id)
        if order.order_type is OrderType.STOP_LOSS:
            raise Unreadable("malformed")
        return order


def test_bug_a_unreadable_stop_is_not_cancelled_into_a_bare_position(store):
    broker = StopDetailUnreadable()
    engine = make(store, broker)
    trade = enter(engine, "2")
    fill_entry(broker, engine, trade)
    loop(engine, 10)  # 30 s: the stop works at the broker, never verified
    assert working_stops(broker) == 2
    assert f"{Fence.UNPROTECTED}:{trade}" in engine.fences()
    loop(engine, 50)  # 180 s: past max_unprotected_seconds, the exit was requested
    assert engine.trade(trade).exit_requested == "unprotected-too-long"
    # The text says the stop is kept and the close waits, not "exiting".
    assert "a stop's Detail cannot be read" in engine.fences()[
        f"{Fence.UNPROTECTED_TIMEOUT}:{trade}"]
    assert held(broker) == {"ABC": D(2)}
    assert_unreadable_stop_kept(broker, engine, D(2))


# --- B ---------------------------------------------------------------------------------------


class PositionsUnreadable(FakeBroker):
    """A non-empty positions body cannot be parsed."""

    def positions(self):
        rows = FakeBroker.positions(self)
        if rows:
            raise Unreadable("malformed")
        return rows


def test_pins_b_no_close_while_positions_unreadable_stop_kept_and_fenced(store):
    """Spec 9: a closing order is sent only for a re-read held quantity, so a flatten sends
    nothing while positions cannot be read. The stop stays working, and `broker-read-failed`
    is set, which runtime/watch.py alerts as `broker-reads-failing` (not an operator fence:
    it clears itself once reads work). Pinned: the close waits for the read."""
    broker = PositionsUnreadable()
    engine = make(store, broker)
    trade = enter(engine, "2")
    fill_entry(broker, engine, trade)
    loop(engine, 6)
    engine.request_exit(trade, "session-flatten")
    loop(engine, 20)  # 60 s after the flatten request
    assert held(broker) == {"ABC": D(2)}
    assert working_stops(broker) == 2
    assert sent(broker, OrderType.MARKET) == 0
    assert Fence.BROKER_READ in engine.fences()


# --- C ---------------------------------------------------------------------------------------


class FillDetailUnreadable(FakeBroker):
    """Detail of a filled BUY cannot be parsed."""

    def order_detail(self, client_order_id):
        order = FakeBroker.order_detail(self, client_order_id)
        if order.side is Side.BUY and order.filled_quantity > 0:
            raise Unreadable("malformed")
        return order


def test_bug_c_unexplained_shares_with_unreadable_entry_detail_are_fenced(store):
    broker = FillDetailUnreadable()
    engine = make(store, broker)
    trade = enter(engine, "2")
    loop(engine, 2)
    fill_entry(broker, engine, trade)
    loop(engine, 20)  # 60 s
    assert held(broker) == {"ABC": D(2)}
    assert working_stops(broker) == 0
    # Spec 9 reconciliation: a quantity mismatch blocks entries (and alerts) until explained;
    # CLAUDE.md rule 7: refuse, never guess. The live entry excuses the mismatch forever.
    assert engine.fences(), "unexplained broker position without any fence"


# --- D ---------------------------------------------------------------------------------------


class UnlistedRefusal(FakeBroker):
    """Every stop and close is refused with a code not in PLACE_REFUSAL_CODES: the Webull
    adapter raises OutcomeUnknown and the order is never created."""

    def place(self, request):
        if request.order_type is not OrderType.LIMIT:
            self.sent.append(request)
            raise OutcomeUnknown("unlisted refusal code")
        return FakeBroker.place(self, request)


def test_bug_d_stops_that_never_existed_count_against_the_retry_limit(store):
    broker = UnlistedRefusal()
    engine = make(store, broker)
    trade = enter(engine, "2")
    fill_entry(broker, engine, trade)
    loop(engine, 200)  # 600 s
    # Spec 9: "A rejected stop: retry per config, then exit and block entries, alert."
    assert sent(broker, OrderType.STOP_LOSS) <= 1 + CFG.orders.stop_retry_attempts
    assert f"{Fence.STOP_REJECTED}:{trade}" in engine.fences()



def test_a_stop_proved_never_created_counts_once_then_is_placed_again(store):
    """The entry is acknowledged, the first stop is lost on the way. Resolved ABSENT, it
    counts one stop failure; later rechecks of the ABSENT row add nothing more."""
    broker = FakeBroker()
    broker.script("ack", "unknown-lost")
    engine = make(store, broker)
    trade = enter(engine, "2")
    fill_entry(broker, engine, trade)
    loop(engine, 20)
    stops = legs(engine, trade, Leg.STOP)
    assert [s.send_state for s in stops] == [SendState.ABSENT, SendState.ACKNOWLEDGED]
    assert engine.trade(trade).stop_failures == 1
    assert sent(broker, OrderType.STOP_LOSS) == 2
    assert working_stops(broker) == D(2)
    assert f"{Fence.STOP_REJECTED}:{trade}" not in engine.fences()


def test_a_stop_rejected_by_detail_then_gone_counts_once(store):
    """Detail REJECTED counts the failure; the row is then acknowledged, so the broker later
    not knowing it is a conflict fence, never a second count as ABSENT."""
    broker = FakeBroker()
    engine = make(store, broker)
    trade = enter(engine, "2")
    fill_entry(broker, engine, trade)
    engine.clock.now += 3
    engine.step()
    stop = legs(engine, trade, Leg.STOP)[0].client_order_id
    broker.orders[stop] = replace(broker.orders[stop], state=OrderState.REJECTED,
                                  raw_status="REJECTED")
    engine.clock.now += 3
    engine.step()
    assert engine.trade(trade).stop_failures == 1
    del broker.orders[stop]
    loop(engine, 20)
    assert engine.order(stop).send_state is SendState.ACKNOWLEDGED
    assert engine.trade(trade).stop_failures == 1


def test_a_stop_counted_absent_that_reappears_is_resurrected_and_protects(store):
    """The lost stop counted as a failure shows up at the broker after all: it is tracked
    again and fenced; the counted failure stays (the conservative side of the limit)."""
    broker = FakeBroker()
    broker.script("ack", "unknown")
    engine = make(store, broker)
    trade = enter(engine, "2")
    fill_entry(broker, engine, trade)
    engine.clock.now += 3
    engine.step()
    stop = legs(engine, trade, Leg.STOP)[0].client_order_id
    hidden = broker.orders.pop(stop)
    loop(engine, 20)
    assert engine.order(stop).send_state is SendState.ABSENT
    assert engine.trade(trade).stop_failures == 1
    broker.orders[stop] = hidden
    engine.reconcile()
    assert engine.order(stop).send_state is SendState.ACKNOWLEDGED
    assert f"{Fence.RESURRECTED}:{stop}" in engine.fences()
    assert engine.trade(trade).stop_failures == 1
    loop(engine, 6)
    assert f"{Fence.STOP_REJECTED}:{trade}" not in engine.fences()


# --- after the close ---------------------------------------------------------------------------


def test_pins_day_stop_placed_again_after_expiry_outside_the_session(store):
    """With a trade still open after the close (`market-closed`: no MARKET order), the DAY
    stop expires and `_protect` places a new STOP_LOSS, DAY, CORE. What Webull answers to that
    outside the regular session is unproven (docs/webull-facts.md F7). Pinned so a change is
    deliberate once F7 is probed."""
    broker = FakeBroker()
    run_id = store.start_run(environment="test", config_digest="d", strategy="s",
                             strategy_version="1", code_version="x")
    engine = Engine(store, broker, CFG, run_id, lambda: OPEN, clock=Clock(),
                    close_refusal=lambda: "after the regular close")
    trade = enter(engine, "2")
    fill_entry(broker, engine, trade)
    loop(engine, 4)
    assert working_stops(broker) == 2
    engine.request_exit(trade, "session-flatten")
    loop(engine, 3)
    assert f"{Fence.MARKET_CLOSED}:{trade}" in engine.fences()
    assert sent(broker, OrderType.MARKET) == 0
    stop = legs(engine, trade, Leg.STOP)[0].client_order_id
    broker.orders[stop] = replace(broker.orders[stop], state=OrderState.EXPIRED,
                                  raw_status="EXPIRED")
    loop(engine, 6)
    assert sent(broker, OrderType.STOP_LOSS) == 2
    assert working_stops(broker) == 2
    assert sent(broker, OrderType.MARKET) == 0
