from __future__ import annotations

import pytest

from tickr.broker.base import RateLimited
from tickr.broker.budget import BudgetExhausted, RequestBudget, RouteLimit


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


LIMIT = RouteLimit(max_requests=3, window_seconds=60, pause_after_429_seconds=30)


def make(limits=None) -> tuple[RequestBudget, Clock]:
    clock = Clock()
    return RequestBudget(LIMIT, limits, clock=clock), clock


def test_refuses_over_limit_without_recording():
    budget, _ = make()
    for _ in range(3):
        budget.take("/r")
    with pytest.raises(BudgetExhausted) as err:
        budget.take("/r")
    assert isinstance(err.value, RateLimited)
    assert err.value.cause == RequestBudget.CAUSE_WINDOW_FULL
    assert err.value.retry_after_seconds == pytest.approx(60)
    assert budget.usage("/r").used == 3


def test_rolling_window_never_admits_more_than_limit_in_any_window():
    # A refilling token bucket would admit 3 at t=0 and more before t=60.
    budget, clock = make()
    admitted: list[float] = []
    for step in range(0, 240):
        clock.now = 1000.0 + step * 0.5
        try:
            budget.take("/r")
            admitted.append(clock.now)
        except BudgetExhausted:
            pass
    for t in admitted:
        inside = [a for a in admitted if t <= a < t + LIMIT.window_seconds]
        assert len(inside) <= LIMIT.max_requests


def test_requests_leave_the_window():
    budget, clock = make()
    for _ in range(3):
        budget.take("/r")
    clock.now += 60
    budget.take("/r")


def test_routes_are_independent_and_overridable():
    budget, _ = make({"/detail": RouteLimit(1, 2, 2)})
    budget.take("/detail")
    with pytest.raises(BudgetExhausted):
        budget.take("/detail")
    budget.take("/other")


def test_can_take_counts_entry_and_stop_together():
    budget, _ = make()
    budget.take("/place")
    assert budget.can_take("/place", 2)
    assert not budget.can_take("/place", 3)
    assert not budget.can_take("/place", 4)  # more than a window can ever hold
    assert budget.usage("/place").used == 1  # can_take takes nothing


def test_pause_after_429_refuses_until_it_ends():
    budget, clock = make()
    budget.pause("/r")
    with pytest.raises(BudgetExhausted) as err:
        budget.take("/r")
    assert err.value.cause == RequestBudget.CAUSE_PAUSED_AFTER_429
    assert budget.usage("/r").remaining == 0
    clock.now += 30
    budget.take("/r")


def test_limit_validation():
    with pytest.raises(ValueError):
        RouteLimit(0, 60, 0)
    with pytest.raises(ValueError):
        RouteLimit(1, 0, 0)


def test_reserve_keeps_requests_for_other_callers():
    budget, _ = make()  # 3 per window
    budget.take("/r", reserve=1)
    budget.take("/r", reserve=1)
    with pytest.raises(BudgetExhausted) as err:
        budget.take("/r", reserve=1)
    assert err.value.cause == RequestBudget.CAUSE_RESERVED
    assert budget.usage("/r").used == 2
    budget.take("/r")  # a caller without a reserve still gets the kept request
    with pytest.raises(BudgetExhausted) as err:
        budget.take("/r", reserve=1)
    assert err.value.cause == RequestBudget.CAUSE_WINDOW_FULL
