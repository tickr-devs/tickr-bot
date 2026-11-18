"""The place budget (Sandbox: 30 per 60 s) against entries filling together: an entry is
admitted only while the budget keeps room for the stops of every working entry and its own,
so a burst of fills is never left waiting on our own budget for its stops."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tickr.broker.base import RateLimited
from tickr.broker.budget import RequestBudget, RouteLimit
from tickr.broker.fake import FakeBroker
from tickr.engine.core import STOP_PLACES_PER_FILL, Leg
from tickr.risk.causes import AdmissionCause as C

from .test_several_positions import MAX, SYMBOLS, Desk, wide

PLACES = 30  # the Sandbox place route: 30 per 60 s
ROUTE = "place"


class Budgeted(FakeBroker):
    """A fake whose places take a real rolling budget on the desk's clock."""

    def __init__(self, clock: list[float], limit: int = PLACES) -> None:
        super().__init__()
        self.budget = RequestBudget(RouteLimit(limit, 60, 60), clock=lambda: clock[0])
        self.refused = 0

    def can_place(self, count: int) -> bool:
        return self.budget.can_take(ROUTE, count)

    def place(self, request):
        try:
            self.budget.take(ROUTE)
        except RateLimited:
            self.refused += 1
            raise
        return super().place(request)


def budgeted_desk(tmp_path, limit: int = PLACES) -> tuple[Desk, Budgeted]:
    clock = [1_000_000.0]
    broker = Budgeted(clock, limit)
    # The budget alone bounds the working entries here (not `max_working_entries`).
    desk = Desk(tmp_path, wide(MAX, max_daily_loss_usd=D(MAX * 10), max_working_entries=MAX),
                broker)
    desk.clock = clock  # one clock for the engine and the budget
    desk.engine.clock = lambda: clock[0]
    return desk, broker


def test_entries_are_admitted_only_while_their_stops_fit_the_budget(tmp_path):
    """With 30 places a minute and 3 stop places owed per working entry, the 8th entry would
    leave room for only 30 - 7 = 23 of the 1 + 3 x 8 = 25 requests; it is refused as
    STOP_BUDGET_UNAVAILABLE."""
    desk, _ = budgeted_desk(tmp_path)
    admitted = 0
    for symbol in SYMBOLS:
        record = desk.enter(symbol)
        if record.admission != "taken":
            assert record.refusal_cause.startswith(C.STOP_BUDGET_UNAVAILABLE)
            break
        admitted += 1
    # entry k is admitted while k - 1 places are taken and 1 + 3 k more fit: k + 3 k <= 30
    assert admitted == PLACES // (1 + STOP_PLACES_PER_FILL)


@pytest.mark.parametrize(("scale_out", "price", "places"), [
    (None, D("10.05"), 1),
    (D(4), D("10.05"), 2),
    # an even scale-out a few ticks above the stop: the second half is told apart by quantity
    (D(5), D("9.80"), 3),
])
def test_every_admitted_entry_filling_at_once_gets_its_stops_without_a_budget_refusal(
        tmp_path, scale_out, price, places):
    desk, broker = budgeted_desk(tmp_path)
    taken = [s for s in SYMBOLS if desk.enter(s, scale_out=scale_out).admission == "taken"]
    desk.step(2)
    for symbol in taken:
        entry = desk.engine.orders(desk.trade_of(symbol).trade_id)[0]
        broker.fill(entry.client_order_id, D(10), price)
    desk.step(10)
    assert broker.refused == 0
    counts = []
    for symbol in taken:
        stops = [o for o in desk.engine.orders(desk.trade_of(symbol).trade_id)
                 if o.leg is Leg.STOP and o.live]
        counts.append(len(stops))
        assert sum((s.quantity for s in stops), D(0)) == D(10)
    # a fill seen while its entry still works keeps the cap's room and moves a tick instead
    assert max(counts) == places


def test_a_filled_entry_frees_its_share_of_the_budget(tmp_path):
    desk, broker = budgeted_desk(tmp_path)
    taken = [s for s in SYMBOLS if desk.enter(s).admission == "taken"]
    entry = desk.engine.orders(desk.trade_of(taken[0]).trade_id)[0]
    broker.fill(entry.client_order_id, D(10), D("10.05"))
    desk.clock[0] += 61  # the window rolls over; the first entry is filled and stopped
    desk.step(3)
    assert desk.enter(SYMBOLS[len(taken)]).admission == "taken"
