"""Entries filling together, seen by one order-history read (`Engine._read_history`) instead
of one Detail read each. Detail still finishes every order; a history row only brings a fill
forward and can never move an order back."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from tickr.broker.base import BrokerError, RateLimited
from tickr.broker.fake import FakeBroker
from tickr.engine.core import HISTORY_READ_FAILED, HISTORY_ROW_IGNORED, ORDER_OBSERVED, Leg
from tickr.types import OrderState

from .test_several_positions import (
    CFG,
    MAX,
    POLL,
    SYMBOLS,
    Desk,
    fill_together,
    wide,
)

pytestmark = pytest.mark.skipif(MAX < 2, reason="needs several slots")

EVERY = float(CFG.orders.history_fill_poll_seconds)
LISTED = CFG.orders.stop_listing_polls
UNPROTECTED = float(CFG.orders.max_unprotected_seconds)


def working(tmp_path, broker: FakeBroker, slots: int = MAX, **overrides: object) -> Desk:
    """`slots` entries working, every order read in turn."""
    desk = Desk(tmp_path, wide(slots, max_daily_loss_usd=D(MAX * 10), max_working_entries=slots),
                broker)
    for symbol in SYMBOLS[:slots]:
        assert desk.enter(symbol, **overrides).admission == "taken"
    desk.step(2 * slots)
    return desk


def entry(desk: Desk, symbol: str):
    return desk.engine.orders(desk.trade_of(symbol).trade_id)[0]


def journal(desk: Desk, kind: str) -> list:
    return desk.store.query("SELECT key, body FROM journal WHERE kind = ?", (kind,))


def test_every_slot_filling_at_once_is_protected_within_one_history_interval(tmp_path):
    """At the most slots config allows, all entries fill in the same poll: one history read
    shows every fill, every stop goes out at once and the next open-orders read verifies
    them, so the last position waits one history interval plus the listing polls, whatever
    the number of slots, with a poll to spare."""
    desk = working(tmp_path, FakeBroker())
    done = fill_together(desk, SYMBOLS)
    assert len(done) == MAX
    bound = -(-EVERY // POLL) + 1 + LISTED
    assert done[-1] <= bound
    assert done[-1] * POLL <= UNPROTECTED - POLL
    sources = [b for _, b in journal(desk, ORDER_OBSERVED) if '"history"' in b]
    assert len(sources) >= MAX - 1  # one fill may have been Detail's turn


def test_without_the_history_fills_are_read_by_detail_in_turn(tmp_path):
    """A failing history changes nothing but speed: each fill is read by Detail in turn,
    as before, and the failure streak is journaled once."""
    broker = FakeBroker(history_down=True)
    slots = 3
    desk = working(tmp_path, broker, slots)
    assert fill_together(desk, SYMBOLS[:slots]) == [k + 1 for k in range(1, slots + 1)]
    assert len(journal(desk, HISTORY_READ_FAILED)) == 1


def test_the_history_is_read_only_when_detail_cannot_keep_up(tmp_path):
    """No working entry, or no more orders due than one step's Detail reads: no read."""
    broker = FakeBroker()
    desk = Desk(tmp_path, wide(2), broker)
    assert desk.enter(SYMBOLS[0]).admission == "taken"
    desk.step(10)  # one working entry, nothing else due
    assert broker.history_reads == 0
    desk.fill(SYMBOLS[0])  # a position and its stop, no entry working
    desk.step(10)
    assert broker.history_reads == 0
    assert desk.enter(SYMBOLS[1]).admission == "taken"  # an entry and a stop due
    desk.step(10)
    assert 0 < broker.history_reads <= -(-10 * POLL // EVERY)


def test_a_history_fill_is_recorded_and_detail_still_finishes_the_order(tmp_path):
    broker = FakeBroker()
    desk = working(tmp_path, broker, 2)
    first = entry(desk, SYMBOLS[0])
    broker.fill(first.client_order_id, D(4), D("10.05"))  # partial
    # Detail of the entry cannot be read for now; the history lists the fill.
    real = broker.order_detail

    def unreadable(cid):
        if cid == first.client_order_id:
            raise BrokerError("detail down")
        return real(cid)

    broker.order_detail = unreadable
    steps = 0
    while not desk.stops(SYMBOLS[0]):
        desk.step()
        steps += 1
        assert steps <= -(-EVERY // POLL)
    row = entry(desk, SYMBOLS[0])
    assert (row.filled_quantity, row.average_fill_price) == (D(4), D("10.05"))
    assert row.broker_state is OrderState.ACCEPTED  # the state is Detail's alone
    assert [s.quantity for s in desk.stops(SYMBOLS[0])] == [D(4)]
    broker.order_detail = real
    desk.step(4)
    assert entry(desk, SYMBOLS[0]).broker_state is OrderState.PARTIALLY_FILLED
    assert [(s.quantity, s.verified) for s in desk.stops(SYMBOLS[0])] == [(D(4), True)]


def test_a_stale_history_row_never_moves_a_fill_back(tmp_path):
    broker = FakeBroker()
    desk = working(tmp_path, broker, 2)
    first = entry(desk, SYMBOLS[0])
    broker.fill(first.client_order_id, D(6), D("10.05"))
    desk.step(6)
    assert entry(desk, SYMBOLS[0]).filled_quantity == D(6)
    listed = broker.orders[first.client_order_id]
    broker.order_history = lambda wanted, pages: [replace(listed, filled_quantity=D(2))]
    desk.step(10)
    assert entry(desk, SYMBOLS[0]).filled_quantity == D(6)


def test_a_history_row_unlike_the_entry_as_sent_is_ignored_and_journaled(tmp_path):
    broker = FakeBroker()
    desk = working(tmp_path, broker, 2)
    first = entry(desk, SYMBOLS[0])
    broker.history_hidden.add(first.client_order_id)
    odd = replace(broker.orders[first.client_order_id], quantity=D(99),
                  filled_quantity=D(5), average_fill_price=D("10.05"))
    real = broker.order_history
    broker.order_history = lambda wanted, pages: [*real(wanted, pages), odd]
    broker.order_detail = lambda cid, real_detail=broker.order_detail: (
        real_detail(cid) if cid != first.client_order_id
        else replace(odd, quantity=first.quantity, filled_quantity=D(0),
                     average_fill_price=None, state=OrderState.ACCEPTED))
    desk.step(-(-int(EVERY) // int(POLL)) + 1)
    assert entry(desk, SYMBOLS[0]).filled_quantity == 0
    assert not desk.stops(SYMBOLS[0])
    assert [k for k, _ in journal(desk, HISTORY_ROW_IGNORED)][:1] == [first.client_order_id]


def test_a_read_refused_by_the_budget_is_tried_again_next_step(tmp_path):
    broker = FakeBroker()
    desk = working(tmp_path, broker, 2)
    calls: list[int] = []
    real = broker.order_history

    def refused_once(wanted, pages):
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited("budget")
        return real(wanted, pages)

    broker.order_history = refused_once
    desk.step(2)
    assert len(calls) == 2  # refused, then read the very next step
    assert len(journal(desk, HISTORY_READ_FAILED)) == 1
    assert all(o.leg is not Leg.STOP for o in desk.engine.orders())


def test_a_history_fill_ahead_of_the_position_never_sizes_a_stop(tmp_path):
    """Fable's #614 case: the broker filled 4 of 10, the history row says 10. The engine waits
    until the positions read holds what the trade would then hold; Detail sizes the stop for
    the 4 meanwhile, never one for shares not held."""
    broker = FakeBroker()
    desk = working(tmp_path, broker, 2)
    first = entry(desk, SYMBOLS[0])
    broker.fill(first.client_order_id, D(4), D("10.05"))
    ahead = replace(broker.orders[first.client_order_id], filled_quantity=first.quantity,
                    state=OrderState.FILLED)
    real_history, real_detail = broker.order_history, broker.order_detail
    broker.order_history = lambda wanted, pages: [
        ahead if o.client_order_id == first.client_order_id else o
        for o in real_history(wanted, pages)]

    def unreadable(cid):
        if cid == first.client_order_id:
            raise BrokerError("detail down")
        return real_detail(cid)

    broker.order_detail = unreadable
    desk.step(3 * int(-(-EVERY // POLL)))
    assert entry(desk, SYMBOLS[0]).filled_quantity == 0  # positions hold 4, not 10
    assert not desk.stops(SYMBOLS[0])
    broker.order_detail = real_detail
    desk.step(3)
    assert entry(desk, SYMBOLS[0]).filled_quantity == D(4)
    assert sum((s.quantity for s in desk.stops(SYMBOLS[0]) if s.live), D(0)) == D(4)


def test_no_history_read_without_a_positions_read(tmp_path):
    broker = FakeBroker()
    desk = working(tmp_path, broker, 2)
    reads = broker.history_reads
    broker.positions = lambda: (_ for _ in ()).throw(BrokerError("down"))
    desk.step(6)
    assert broker.history_reads == reads
