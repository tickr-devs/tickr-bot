"""Per-route request budget for broker HTTP calls.

Webull limits requests per route over a rolling window (for example 30 per 60 s per route,
see docs/webull-facts.md). The budget keeps, per route, the times of the requests taken in
the current window and refuses a request that would exceed the limit. It never sleeps and
never queues: a refused call raises `RateLimited` and the caller decides what to do.

A plain token bucket refilling continuously can admit up to twice the limit inside one
window (a full bucket plus the refill), which is exactly what the broker punishes with 429.
The rolling-window log used here never admits more than `max_requests` inside any window.

After the broker itself answers 429, the route is paused for a configured time: every
request on that route is refused until the pause ends, whatever the local count says.

All numbers come from configuration through `RouteLimit`; this module holds none.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tickr.broker.base import RateLimited

Clock = Callable[[], float]
"""Monotonic clock in seconds."""


@dataclass(frozen=True)
class RouteLimit:
    """Request limit of one route. Values come from config."""

    max_requests: int
    window_seconds: float
    pause_after_429_seconds: float

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.pause_after_429_seconds < 0:
            raise ValueError("pause_after_429_seconds must not be negative")


class BudgetExhausted(RateLimited):
    """The local budget refused the call; nothing was sent."""

    def __init__(self, route: str, cause: str, retry_after_seconds: float) -> None:
        super().__init__(f"{route}: {cause}, retry after {retry_after_seconds:.3f}s")
        self.route = route
        self.cause = cause
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class RouteUsage:
    """Read-only view of one route, for status screens and admission checks."""

    route: str
    used: int
    limit: int
    paused_for_seconds: float

    @property
    def remaining(self) -> int:
        if self.paused_for_seconds > 0:
            return 0
        return max(self.limit - self.used, 0)


class RequestBudget:
    """Rolling-window request budget, one window per route.

    `limits` maps route keys to their limits. A route without an entry uses `default`.
    Thread safe: the bot loop and the local API may both read usage.
    """

    CAUSE_WINDOW_FULL = "window_full"
    CAUSE_PAUSED_AFTER_429 = "paused_after_429"
    CAUSE_RESERVED = "reserved"  # free requests remain, but they are kept for other callers

    def __init__(
        self,
        default: RouteLimit,
        limits: Mapping[str, RouteLimit] | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._default = default
        self._limits = dict(limits or {})
        self._clock = clock
        self._taken: dict[str, deque[float]] = {}
        self._paused_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def limit_for(self, route: str) -> RouteLimit:
        return self._limits.get(route, self._default)

    def _expire(self, route: str, now: float) -> deque[float]:
        window = self.limit_for(route).window_seconds
        taken = self._taken.setdefault(route, deque())
        while taken and taken[0] <= now - window:
            taken.popleft()
        return taken

    def _refusal(self, route: str, count: int, now: float) -> BudgetExhausted | None:
        """Why `count` requests cannot be taken now, or None if they can."""
        paused_until = self._paused_until.get(route)
        if paused_until is not None and now < paused_until:
            return BudgetExhausted(route, self.CAUSE_PAUSED_AFTER_429, paused_until - now)
        limit = self.limit_for(route)
        if count > limit.max_requests:
            # Can never fit in one window: refuse permanently rather than wait forever.
            return BudgetExhausted(route, self.CAUSE_WINDOW_FULL, float("inf"))
        taken = self._expire(route, now)
        free = limit.max_requests - len(taken)
        if count <= free:
            return None
        # The (count - free)-th oldest request must leave the window first.
        release_at = taken[count - free - 1] + limit.window_seconds
        return BudgetExhausted(route, self.CAUSE_WINDOW_FULL, release_at - now)

    def can_take(self, route: str, count: int = 1) -> bool:
        """True if `count` requests on `route` would be admitted now. Takes nothing."""
        with self._lock:
            return self._refusal(route, count, self._clock()) is None

    def take(self, route: str, reserve: int = 0) -> None:
        """Record one request on `route` or raise `BudgetExhausted` without recording. With
        `reserve`, the request is admitted only if `reserve` more would still fit in the window
        afterwards, so a lower-priority caller never takes the requests kept for the others
        (cause `reserved` when only the reserve is left)."""
        with self._lock:
            now = self._clock()
            refusal = self._refusal(route, 1 + reserve, now)
            if refusal is not None:
                if reserve and self._refusal(route, 1, now) is None:
                    raise BudgetExhausted(route, self.CAUSE_RESERVED, refusal.retry_after_seconds)
                raise refusal
            self._taken[route].append(now)

    def pause(self, route: str) -> None:
        """The broker answered 429 on `route`: refuse it for the configured pause."""
        with self._lock:
            now = self._clock()
            until = now + self.limit_for(route).pause_after_429_seconds
            self._paused_until[route] = max(self._paused_until.get(route, now), until)

    def usage(self, route: str) -> RouteUsage:
        with self._lock:
            now = self._clock()
            taken = self._expire(route, now)
            paused_until = self._paused_until.get(route, now)
            return RouteUsage(
                route=route,
                used=len(taken),
                limit=self.limit_for(route).max_requests,
                paused_for_seconds=max(paused_until - now, 0.0),
            )
