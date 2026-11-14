from __future__ import annotations

from collections import Counter
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import config
from aperture.broker.base import OutcomeUnknown, RateLimited
from aperture.broker.fake import FakeBroker
from aperture.config import GateResult
from aperture.engine.core import Engine, EntryRefused, Fence, Leg, SendState, TradeState
from aperture.store import Store
from aperture.types import OrderRequest, OrderState, OrderType, Position, Side, TimeInForce
from aperture.types import TradingSession as Session

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
OPEN = GateResult(True, "test")
POLL = float(CFG.orders.detail_poll_seconds)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    yield s
    s.close()


def make(store, broker, gate=OPEN) -> Engine:
    run = store.start_run(environment="test", config_digest="d", strategy="s",
                          strategy_version="1", code_version="x")
    return Engine(store, broker, CFG, run, lambda: gate, clock=Clock())


def run(engine: Engine, steps: int = 1) -> None:
    """Advance the clock one poll interval per step, like the loop does."""
    for _ in range(steps):
        engine.clock.now += POLL
        engine.step()


def enter(engine, qty="2") -> str:
    return engine.open_trade("ABC", D(qty), D("10.00"), D("9.80"), D("1"))


def legs(engine, trade_id, leg):
    return [o for o in engine.orders(trade_id) if o.leg is leg]


def working_stops(broker) -> D:
    return sum((o.quantity - o.filled_quantity for o in broker.open_orders()
                if o.order_type == OrderType.STOP_LOSS), D(0))


def has(engine, fence: Fence) -> bool:
    return any(n.split(":", 1)[0] == fence for n in engine.fences())


def assert_single_sends(broker):
    counts = Counter(r.client_order_id for r in broker.sent)
    assert all(n == 1 for n in counts.values()), counts


def opened(store, qty="2"):
    """A trade with `qty` filled and its stop verified working."""
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, qty)
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(qty), D(10))
    run(e, 3)
    assert working_stops(b) == D(qty) and not has(e, Fence.UNPROTECTED)
    return b, e, t


def test_happy_path(store):
    b, e, t = opened(store)
    (stop,) = legs(e, t, Leg.STOP)
    assert stop.order_type is OrderType.STOP_LOSS and stop.stop_price == D("9.80")
    assert e.trade(t).state is TradeState.OPEN
    e.request_exit(t, "operator-flatten")
    run(e, 3)
    (exit_,) = legs(e, t, Leg.EXIT)
    assert exit_.quantity == D(2) and exit_.order_type is OrderType.MARKET
    assert legs(e, t, Leg.STOP)[0].broker_state is OrderState.CANCELLED
    b.fill(exit_.client_order_id, D(2), D("10.10"))
    run(e, 2)
    assert e.trade(t).state is TradeState.CLOSED
    assert e.fences() == {}
    assert b.positions() == []
    assert_single_sends(b)
    kinds = [r.kind for r in store.journal()]
    assert kinds.index("order-intent") < kinds.index("order-attempted")
    assert kinds.index("order-attempted") < kinds.index("order-acknowledged")


def test_partial_fills_are_each_protected(store):
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "3")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.fill(cid, D(1), D(10))
    run(e, 2)
    assert [s.quantity for s in legs(e, t, Leg.STOP)] == [D(1)]
    b.fill(cid, D(2), D(10))
    run(e, 4)
    assert [s.quantity for s in legs(e, t, Leg.STOP)] == [D(1), D(2)]
    run(e, 2)
    assert not has(e, Fence.UNPROTECTED)
    assert working_stops(b) == D(3)


def test_stop_fill_closes_trade(store):
    b, e, t = opened(store, "1")
    b.fill(legs(e, t, Leg.STOP)[0].client_order_id, D(1), D("9.79"))
    run(e, 2)
    assert e.trade(t).state is TradeState.CLOSED
    assert store.query("SELECT close_cause FROM trades")[0][0] == "stopped-out"


def test_stop_refused_retries_with_spacing_then_exit(store):
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "1")
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(1), D(10))
    attempts = CFG.orders.stop_retry_attempts + 1
    b.script(*["refuse"] * attempts)
    e.clock.now += POLL
    e.step()
    assert len(legs(e, t, Leg.STOP)) == 1
    e.step()  # same instant: spacing holds the retry back
    assert len(legs(e, t, Leg.STOP)) == 1
    run(e, 3 * attempts)
    assert len(legs(e, t, Leg.STOP)) == attempts
    assert has(e, Fence.STOP_REJECTED)
    (exit_,) = legs(e, t, Leg.EXIT)
    b.fill(exit_.client_order_id, D(1), D(10))
    run(e, 2)
    assert e.trade(t).state is TradeState.CLOSED
    with pytest.raises(EntryRefused, match="stop-rejected"):
        enter(e)
    for name in list(e.fences()):
        assert e.clear_operator_fence(name)
    enter(e)


def test_unknown_outcome_resolved_by_detail_never_resent(store):
    b = FakeBroker()
    b.script("unknown")  # reached the broker, reply lost
    e = make(store, b)
    t = enter(e)
    assert legs(e, t, Leg.ENTRY)[0].send_state is SendState.UNKNOWN
    assert has(e, Fence.UNKNOWN_ORDER)
    run(e)
    assert legs(e, t, Leg.ENTRY)[0].send_state is SendState.ACKNOWLEDGED
    assert e.fences() == {}
    assert_single_sends(b)


def test_unknown_outcome_absent_after_reads(store):
    b = FakeBroker()
    b.script("unknown-lost")
    e = make(store, b)
    t = enter(e)
    run(e, CFG.orders.unknown_absent_reads)
    assert legs(e, t, Leg.ENTRY)[0].send_state is SendState.ABSENT
    run(e)
    assert e.trade(t).state is TradeState.CLOSED
    assert e.fences() == {}
    assert len(b.sent) == 1


def test_refused_entry_closes_trade(store):
    b = FakeBroker()
    b.script("refuse")
    e = make(store, b)
    t = enter(e)
    run(e)
    assert e.trade(t).state is TradeState.CLOSED


class Crash(BaseException):
    pass


class CrashAfterSend(FakeBroker):
    """The request reaches the broker, then the process dies before recording the outcome."""

    def place(self, request):
        super().place(request)
        raise Crash()


def test_crash_during_send_recovers_without_resend(store):
    b = CrashAfterSend()
    e = make(store, b)
    with pytest.raises(Crash):
        enter(e)
    assert e.orders()[0].send_state is SendState.ATTEMPTED
    healthy = FakeBroker(orders=b.orders, holdings=b.holdings, sent=b.sent)
    restarted = Engine(store, healthy, CFG, "run2", lambda: OPEN, clock=Clock())
    restarted.recover()
    assert restarted.orders()[0].send_state is SendState.ACKNOWLEDGED
    assert restarted.fences() == {}
    assert len(healthy.sent) == 1
    healthy.fill(restarted.orders()[0].client_order_id, D(2), D(10))
    run(restarted, 2)
    assert legs(restarted, restarted.trades()[0].trade_id, Leg.STOP)


def test_intent_without_attempt_is_never_sent(store):
    b = FakeBroker()
    e = make(store, b)
    with store.transaction():
        store.execute(
            "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
            "entry_limit_price, stop_price, planned_loss_usd, opened_at) "
            "VALUES ('t9', 'r', 'ABC', 'opening', '1', '10', '9', '1', 'now')")
        e._write_intent("t9", Leg.ENTRY, "ABC", Side.BUY, OrderType.LIMIT, D(1),
                        limit_price=D(10))
    e.recover()
    assert e.orders()[0].send_state is SendState.ABSENT
    assert b.sent == []
    assert e.trade("t9").state is TradeState.CLOSED


def test_foreign_position_and_order_fence_entries(store):
    b = FakeBroker()
    b.add_foreign_position("XYZ", D(5), D(3))
    b.place(OrderRequest("manual-1", "XYZ", Side.SELL, OrderType.LIMIT, D(5), TimeInForce.DAY,
                         Session.CORE, limit_price=D(4)))
    e = make(store, b)
    e.recover()
    names = set(e.fences())
    assert f"{Fence.FOREIGN_POSITION}:XYZ" in names
    assert f"{Fence.FOREIGN_ORDER}:manual-1" in names
    assert f"{Fence.STARTUP_RECONCILE}" not in names
    with pytest.raises(EntryRefused):
        enter(e)
    b.cancel("manual-1")
    del b.holdings["XYZ"]
    e.reconcile()
    assert e.fences() == {}


def test_gate_closed_refuses_entry(store):
    e = make(store, FakeBroker(), gate=GateResult(False, "disabled"))
    with pytest.raises(EntryRefused, match="gate"):
        enter(e)


# --- review findings on #10: each test failed on the first version ----------------------------


def test_f1_positions_lag_after_stop_fill_sends_nothing(store):
    b, e, t = opened(store)
    b.fill(legs(e, t, Leg.STOP)[0].client_order_id, D(2), D("9.8"))
    b.holdings["ABC"] = Position("ABC", D(2), D(10))  # positions lag Detail
    run(e, 3)
    assert len(legs(e, t, Leg.STOP)) == 1
    assert legs(e, t, Leg.EXIT) == []
    assert has(e, Fence.QUANTITY_MISMATCH)
    del b.holdings["ABC"]  # positions catch up
    run(e)
    assert e.trade(t).state is TradeState.CLOSED
    assert_single_sends(b)


def test_f2_exit_with_persistent_mismatch_keeps_a_working_stop(store):
    b, e, t = opened(store)
    b.add_foreign_position("ABC", D(1), D(10))  # manual buy of the same symbol
    e.request_exit(t, "x")
    run(e, 10)
    assert e.trade(t).state is TradeState.EXITING
    assert working_stops(b) == D(2)  # the Detail quantity stays protected
    assert legs(e, t, Leg.EXIT) == []  # never sell on a disagreeing quantity
    assert has(e, Fence.QUANTITY_MISMATCH)


def test_f2_entry_cancelled_before_stops_late_fill_is_protected(store):
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "3")
    entry = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.fill(entry, D(1), D(10))
    run(e, 3)
    assert working_stops(b) == D(1)
    e.request_exit(t, "x")
    run(e)
    # the entry cancel is sent first; the stop must still be working
    assert legs(e, t, Leg.ENTRY)[0].cancel_state == "sent"
    assert legs(e, t, Leg.STOP)[0].cancel_state is None
    assert working_stops(b) == D(1)


def test_f2_late_entry_fill_during_teardown_gets_a_stop(store):
    class SlowCancel(FakeBroker):
        def cancel(self, client_order_id):
            pass  # accepted, takes effect later

    b = SlowCancel()
    e = make(store, b)
    t = enter(e, "3")
    entry = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.fill(entry, D(1), D(10))
    run(e, 3)
    e.request_exit(t, "x")
    run(e)
    b.fill(entry, D(1), D(10))  # fills before the cancel takes effect
    run(e, 4)
    assert working_stops(b) == D(2)
    assert legs(e, t, Leg.EXIT) == []  # entry still live: no close yet


def test_f3_absent_requires_open_orders_and_late_fill_is_resurrected(store):
    b = FakeBroker()
    b.script("unknown")
    e = make(store, b)
    t = enter(e)
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    hidden = b.orders.pop(cid)  # broker does not show it yet
    run(e, CFG.orders.unknown_absent_reads + 1)
    assert legs(e, t, Leg.ENTRY)[0].send_state is SendState.ABSENT
    assert e.trade(t).state is TradeState.CLOSED
    b.orders[cid] = hidden  # it appears, and fills
    b.fill(cid, D(2), D(10))
    run(e, 4)
    assert e.trade(t).state is TradeState.OPEN
    assert working_stops(b) == D(2)
    assert has(e, Fence.RESURRECTED)


def test_f3_absent_not_decided_while_order_is_in_open_orders(store):
    class DetailBlind(FakeBroker):
        def order_detail(self, client_order_id):
            from aperture.broker.base import OrderNotFound
            raise OrderNotFound(client_order_id)

    b = DetailBlind()
    b.script("unknown")
    e = make(store, b)
    t = enter(e)
    run(e, CFG.orders.unknown_absent_reads + 2)
    assert legs(e, t, Leg.ENTRY)[0].send_state is not SendState.ABSENT


def test_f3_reconcile_resurrects_own_order_we_gave_up_on(store):
    b = FakeBroker()
    b.script("unknown")
    e = make(store, b)
    t = enter(e)
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    hidden = b.orders.pop(cid)
    run(e, CFG.orders.unknown_absent_reads + 1)
    assert e.trade(t).state is TradeState.CLOSED
    b.orders[cid] = hidden
    e.reconcile()
    assert legs(e, t, Leg.ENTRY)[0].send_state is SendState.ACKNOWLEDGED
    assert e.trade(t).state is not TradeState.CLOSED
    assert has(e, Fence.RESURRECTED)


def test_f4_cancel_retried_after_detail_shows_it_did_not_take(store):
    b, e, t = opened(store)
    calls = []

    def lost(cid):
        calls.append(cid)
        if len(calls) == 1:
            raise OutcomeUnknown("lost")
        FakeBroker.cancel(b, cid)

    b.cancel = lost
    e.request_exit(t, "x")
    run(e, 12)
    assert len(calls) == 2
    (exit_,) = legs(e, t, Leg.EXIT)
    b.fill(exit_.client_order_id, D(2), D(10))
    run(e, 2)
    assert e.trade(t).state is TradeState.CLOSED


def test_f4_cancel_attempts_are_bounded_then_operator_fence(store):
    b, e, t = opened(store)

    def never(cid):
        raise OutcomeUnknown("lost")

    b.cancel = never
    e.request_exit(t, "x")
    run(e, 40)
    stop = legs(e, t, Leg.STOP)[0]
    assert stop.cancel_attempts == CFG.orders.cancel_retry_attempts + 1
    assert has(e, Fence.CANCEL_FAILED)
    assert working_stops(b) == D(2)  # still protected


def test_f4_rate_limited_cancel_is_not_unknown(store):
    b, e, t = opened(store)
    calls = []

    def limited(cid):
        calls.append(cid)
        if len(calls) == 1:
            raise RateLimited("budget")
        FakeBroker.cancel(b, cid)

    b.cancel = limited
    e.request_exit(t, "x")
    run(e)
    assert legs(e, t, Leg.STOP)[0].cancel_state == "not-sent"
    assert not has(e, Fence.CANCEL_UNKNOWN)
    run(e, 4)
    assert legs(e, t, Leg.EXIT)


def test_f5_detail_reads_capped_and_budget_refusal_visible(store):
    b, e, t = opened(store)
    reads = []
    real = b.order_detail

    def counting(cid):
        reads.append(cid)
        return real(cid)

    b.order_detail = counting
    run(e, 5)
    assert len(reads) <= 5 * CFG.orders.detail_reads_per_step

    def limited(cid):
        raise RateLimited("budget")

    b.order_detail = limited
    run(e)
    assert has(e, Fence.DETAIL_BUDGET)
    b.order_detail = real
    run(e)
    assert not has(e, Fence.DETAIL_BUDGET)


def test_f6_startup_fence_clears_on_a_later_successful_reconcile(store):
    b = FakeBroker()
    e = make(store, b)
    real = b.open_orders

    def fail():
        raise RateLimited("x")

    b.open_orders = fail
    e.recover()
    assert has(e, Fence.STARTUP_RECONCILE)
    b.open_orders = real
    assert e.reconcile()
    assert e.fences() == {}


def test_f7_acknowledged_order_missing_is_fenced(store):
    b, e, t = opened(store)
    del b.orders[legs(e, t, Leg.STOP)[0].client_order_id]
    run(e, 2)
    assert has(e, Fence.ORDER_MISSING)


def test_f7_unprotected_too_long_exits(store):
    class StatusUnknown(FakeBroker):
        def order_detail(self, client_order_id):
            from dataclasses import replace
            detail = super().order_detail(client_order_id)
            if detail.order_type == OrderType.STOP_LOSS:
                return replace(detail, state=OrderState.UNKNOWN, raw_status="WEIRD")
            return detail

    b = StatusUnknown()
    e = make(store, b)
    t = enter(e, "1")
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(1), D(10))
    run(e, int(float(CFG.orders.max_unprotected_seconds) / POLL) + 3)
    assert has(e, Fence.UNPROTECTED_TIMEOUT)
    assert e.trade(t).exit_requested == "unprotected-too-long"


def test_exit_refused_restores_protection_and_blocks_repeat_requests(store):
    b, e, t = opened(store, "1")
    e.request_exit(t, "x")
    b.script("refuse")  # the closing order will be refused
    run(e, 3)
    assert has(e, Fence.EXIT_REFUSED)
    assert not e.request_exit(t, "again")
    run(e, 4)
    assert working_stops(b) == D(1)
    assert len(legs(e, t, Leg.EXIT)) == 1


def test_r1_a_stop_with_unreadable_detail_cannot_starve_the_entry(store):
    from aperture.broker.base import BrokerError

    class StuckStopDetail(FakeBroker):
        stuck: set = set()

        def order_detail(self, cid):
            if cid in self.stuck:
                raise BrokerError("DETAIL_NOT_VISIBLE")
            return super().order_detail(cid)

    b = StuckStopDetail()
    b.stuck = set()
    e = make(store, b)
    t = enter(e, "4")
    entry = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.fill(entry, D(1), D(10))
    run(e)
    b.stuck.add(legs(e, t, Leg.STOP)[0].client_order_id)
    b.fill(entry, D(3), D(10))
    run(e, 8)
    assert legs(e, t, Leg.ENTRY)[0].filled_quantity == D(4)
    assert working_stops(b) == D(4)
