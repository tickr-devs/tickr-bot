"""Broker reads that disagree with the order as sent (Codex review 2026-11-24, three P1s).

Each scenario checks the broker calls (no protective sale above the proven position), entry
admission, and what the operator sees."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from tickr.broker.fake import FakeBroker
from tickr.engine.core import Engine, EntryRefused, Fence, Leg, SendState
from tickr.store import Store
from tickr.types import OrderDetail, OrderState, OrderType, Side

from .test_engine import CFG, POLL, Clock, enter, has, legs, make, run, working_stops


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    yield s
    s.close()


def sells(broker: FakeBroker) -> list[tuple[OrderType, D]]:
    return [(r.order_type, r.quantity) for r in broker.sent if r.side is Side.SELL]


def opened(store, b: FakeBroker, symbol: str, qty: str = "2") -> tuple[Engine, str]:
    e = make(store, b)
    t = e.open_trade(symbol, D(qty), D("10.00"), D("9.80"), D("1"))
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(qty), D(10))
    run(e, 4)
    return e, t


def test_fill_above_the_order_quantity_is_never_recorded_or_protected(store):
    """Detail: order of 2, filled 20 (a contradictory report, state UNKNOWN). Nothing is
    recorded as bought, no SELL is sent, entries stay blocked, the evidence is journaled."""
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "2")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.orders[cid] = replace(b.orders[cid], filled_quantity=D(20), average_fill_price=D(10),
                            state=OrderState.UNKNOWN, raw_status="FILLED")
    run(e, 12)
    assert sells(b) == []
    assert legs(e, t, Leg.ENTRY)[0].filled_quantity == 0
    assert e.expected_quantity(e.orders(t)) == 0
    assert f"{Fence.ORDER_CONFLICT}:{cid}" in e.fences()
    with pytest.raises(EntryRefused):
        e.open_trade("XYZ", D(1), D("10.00"), D("9.80"), D("1"))
    (row,) = store.journal("order-conflict", cid)
    assert row.body["filled_quantity"] == "20" and row.body["quantity"] == "2"


def test_fill_above_the_order_quantity_after_a_real_partial_keeps_the_partial(store):
    """1 of 3 filled and protected; then Detail says 30 filled: the stop stays at 1."""
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "3")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.fill(cid, D(1), D(10))
    run(e, 4)
    assert working_stops(b) == D(1)
    b.orders[cid] = replace(b.orders[cid], filled_quantity=D(30), state=OrderState.UNKNOWN)
    run(e, 8)
    assert [q for _, q in sells(b)] == [D(1)]
    assert legs(e, t, Leg.ENTRY)[0].filled_quantity == D(1)
    assert has(e, Fence.ORDER_CONFLICT)


def test_listed_row_above_the_order_quantity_is_not_recorded(store):
    """The same check on the open-orders path: a crashed send resolved by reconcile."""
    b = FakeBroker()
    e = make(store, b)
    b.script("unknown")
    t = enter(e, "2")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    assert legs(e, t, Leg.ENTRY)[0].send_state is SendState.UNKNOWN
    b.orders[cid] = replace(b.orders[cid], filled_quantity=D(20),
                            state=OrderState.PARTIALLY_FILLED)
    e.reconcile()
    assert legs(e, t, Leg.ENTRY)[0].filled_quantity == 0
    assert sells(b) == []
    assert f"{Fence.ORDER_CONFLICT}:{cid}" in e.fences()


def test_stop_changed_at_the_broker_is_no_protection(store):
    """A working stop 2 @ 9.80 becomes 1 @ 6.00 under the same id: within one Detail read it is
    no longer verified protection, entries are blocked, the operator sees why, and the trade
    is closed after max_unprotected_seconds. No stop above the position is ever sent."""
    b = FakeBroker()
    e, t = opened(store, b, "ABC")
    (stop,) = legs(e, t, Leg.STOP)
    b.orders[stop.client_order_id] = replace(b.orders[stop.client_order_id], quantity=D(1),
                                             stop_price=D("6.00"))
    reads = 0
    while not has(e, Fence.ORDER_CONFLICT):
        run(e)
        reads += 1
        assert reads <= 2
    assert has(e, Fence.UNPROTECTED)
    assert e.entry_fences("XYZ")
    cause = e.fences()[f"{Fence.ORDER_CONFLICT}:{stop.client_order_id}"]
    assert "6.00" in cause and "9.80" in cause
    run(e, 12)
    assert e.trade(t).exit_requested == "unprotected-too-long"
    run(e, 6)
    stops_sent = [q for kind, q in sells(b) if kind is OrderType.STOP_LOSS]
    assert stops_sent == [D(2)]
    assert [q for kind, q in sells(b) if kind is OrderType.MARKET] == [D(2)]


def test_stop_price_rounded_to_cents_is_not_a_change(store):
    """Detail shows sub-dollar prices rounded to cents (docs/webull-facts.md): 0.1815 as 0.18."""
    b = FakeBroker()
    e = make(store, b)
    t = e.open_trade("ABC", D(100), D("0.2333"), D("0.1815"), D("6"))
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(100), D("0.2333"))
    run(e, 4)
    (stop,) = legs(e, t, Leg.STOP)
    assert stop.stop_price == D("0.1815")
    b.orders[stop.client_order_id] = replace(b.orders[stop.client_order_id],
                                             stop_price=D("0.18"))
    run(e, 6)
    assert not has(e, Fence.ORDER_CONFLICT) and not has(e, Fence.UNPROTECTED)


def restarted(store, first: Engine, b: FakeBroker) -> Engine:
    run_id = store.start_run(environment="test", config_digest="d", strategy="s",
                             strategy_version="1", code_version="x")
    clock = Clock()
    clock.now = first.clock.now + 600
    return Engine(store, b, CFG, run_id, first.gate, clock=clock)


def four_open(store, b: FakeBroker) -> tuple[Engine, dict[str, str]]:
    first = None
    trades = {}
    for symbol in ("AAA", "BBB", "CCC", "DDD"):
        e, t = opened(store, b, symbol)
        first = first or e
        trades[symbol] = t
    for symbol, t in trades.items():
        assert legs(first, t, Leg.STOP)[0].verified, symbol
    return first, trades


def assert_entry_only_when_protected(e: Engine, b: FakeBroker, steps: int) -> None:
    """Step by step: an entry is admissible only while every position has working broker
    stops covering exactly what it holds."""
    for _ in range(steps):
        if not e.entry_fences("EEE"):
            for p in b.positions():
                covered = sum((o.quantity - o.filled_quantity for o in b.open_orders()
                               if o.symbol == p.symbol and o.order_type is OrderType.STOP_LOSS),
                              D(0))
                assert covered == p.quantity, p.symbol
        run(e)


def test_restart_does_not_admit_an_entry_while_a_known_stop_is_gone(store):
    """Four open trades; while the bot is down the broker cancels one stop. From recover() on,
    no fifth entry is admitted until that position is protected again."""
    b = FakeBroker()
    first, trades = four_open(store, b)
    gone = legs(first, trades["CCC"], Leg.STOP)[0].client_order_id
    b.orders[gone] = replace(b.orders[gone], state=OrderState.CANCELLED, raw_status="CANCELLED")
    e = restarted(store, first, b)
    e.recover()
    assert e.entry_fences("EEE")
    with pytest.raises(EntryRefused):
        e.open_trade("EEE", D(1), D("10.00"), D("9.80"), D("1"))
    assert_entry_only_when_protected(e, b, 8)
    assert working_stops(b) == D(8)
    assert not e.entry_fences("EEE")
    assert not any(r.symbol == "EEE" for r in b.sent)


def test_unlisted_stop_is_read_first_and_blocks_entries_until_then(store):
    """The cancelled stop is the last one the Detail queue would read: the reconcile that misses
    it in open orders blocks entries and puts its read first (one Detail read per step)."""
    b = FakeBroker()
    first, trades = four_open(store, b)
    gone = legs(first, trades["DDD"], Leg.STOP)[0].client_order_id
    b.orders[gone] = replace(b.orders[gone], state=OrderState.CANCELLED, raw_status="CANCELLED")
    e = restarted(store, first, b)
    with store.transaction():  # read most recently, so the common queue would read it last
        store.execute("UPDATE orders SET last_detail_at = ? WHERE client_order_id = ?",
                      (e.clock.now - 1, gone))
    e.reconcile()
    (stop,) = [o for o in legs(e, trades["DDD"], Leg.STOP) if o.client_order_id == gone]
    assert stop.broker_state is OrderState.CANCELLED
    assert_entry_only_when_protected(e, b, 8)


def test_stop_changed_while_down_is_caught_by_the_reconcile(store):
    """The stop is still listed, but at 1 share and 6.00: the startup reconcile itself fences
    it, before any Detail read."""
    b = FakeBroker()
    first, trades = four_open(store, b)
    changed = legs(first, trades["BBB"], Leg.STOP)[0].client_order_id
    b.orders[changed] = replace(b.orders[changed], quantity=D(1), stop_price=D("6.00"))
    e = restarted(store, first, b)
    e.recover()
    assert f"{Fence.ORDER_CONFLICT}:{changed}/listed" in e.fences()
    assert e.entry_fences("EEE")
    assert_entry_only_when_protected(e, b, 6)
    assert has(e, Fence.UNPROTECTED)


class SplitBroker(FakeBroker):
    """Detail (`orders`) and the open-orders list disagree about one order: while it works,
    the list shows `listed_as` instead of what Detail shows."""

    def __init__(self) -> None:
        super().__init__()
        self.listed_as: dict[str, OrderDetail] = {}

    def open_orders(self) -> list[OrderDetail]:
        return [self.listed_as.get(o.client_order_id, o) for o in super().open_orders()]


def reconciling(e: Engine, steps: int) -> list[tuple[bool, bool, set[str]]]:
    """Steps with a reconcile every third, as the loop does (reconcile_interval_seconds 10,
    detail_poll_seconds 3); per step: (an entry in a new symbol admissible, every open trade's
    stops protect, the symbols of the open trades)."""
    seen = []
    for index in range(steps):
        if index % 3 == 0:
            e.clock.now += POLL
            e.reconcile()
        else:
            run(e)
        protected = all(
            sum((s.open_quantity for s in legs(e, t.trade_id, Leg.STOP) if e.protects(s)), D(0))
            >= e.expected_quantity(e.orders(t.trade_id)) for t in e.trades())
        seen.append((not e.entry_fences("EEE"), protected, {t.symbol for t in e.trades()}))
    return seen


@pytest.mark.parametrize("changed_in", ["detail", "list"])
def test_one_read_agreeing_never_clears_what_the_other_found(store, changed_in):
    """Fable on #742: Detail shows the BBB stop changed (1 @ 6.00) while open orders still
    list it as sent, or the other way round. Whichever read disagrees, the stop stays unproven
    across reconciles: no step admits an entry while BBB is open, and the trade is closed
    after max_unprotected_seconds."""
    b = SplitBroker()
    first, trades = four_open(store, b)
    stop = legs(first, trades["BBB"], Leg.STOP)[0].client_order_id
    as_sent = b.orders[stop]
    changed = replace(as_sent, quantity=D(1), stop_price=D("6.00"))
    if changed_in == "detail":
        b.orders[stop] = changed
        b.listed_as[stop] = as_sent
    else:
        b.listed_as[stop] = changed
    e = restarted(store, first, b)
    e.recover()
    seen = reconciling(e, 45)
    assert all(not protected for _, protected, symbols in seen if "BBB" in symbols)
    assert not any(admissible for admissible, _, symbols in seen if "BBB" in symbols)
    assert e.trade(trades["BBB"]).exit_requested == "unprotected-too-long"
    assert [q for kind, q in sells(b) if kind is OrderType.MARKET] == [D(2)]


def test_an_ended_order_clears_its_listed_conflict(store):
    """A conflict only the list showed ends with the order: an agreeing Detail read showing the
    stop cancelled clears it (the list never shows an ended order again)."""
    b = SplitBroker()
    e, t = opened(store, b, "ABC")
    (stop,) = legs(e, t, Leg.STOP)
    cid = stop.client_order_id
    b.listed_as[cid] = replace(b.orders[cid], quantity=D(1), stop_price=D("6.00"))
    e.clock.now += POLL
    e.reconcile()
    assert f"{Fence.ORDER_CONFLICT}:{cid}/listed" in e.fences()
    b.orders[cid] = replace(b.orders[cid], state=OrderState.CANCELLED, raw_status="CANCELLED")
    run(e, 4)
    assert not has(e, Fence.ORDER_CONFLICT)
