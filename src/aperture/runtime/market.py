"""Market data for the bot loop (spec 6): the L1 stream's events, the subscription set, stream
health, discovery on its interval, and the market exit when the stream is lost with a position.

Stream callbacks arrive on the provider's thread. They only touch `StreamInbox` (a lock and a
few dicts); everything that writes the database happens on the trading loop in `tick()`.
Strategy evaluation is `runtime.evaluation`; the service copies its WATCH set into `watch`.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aperture.api import contract as api
from aperture.broker.base import BrokerError
from aperture.calendar import CalendarRefused, SessionCalendar
from aperture.config import Config
from aperture.data.base import MarketData, MarketDataError, QuoteListener
from aperture.data.capture import CaptureWriter
from aperture.data.discovery import Candidate, DiscoverySettings, discover
from aperture.data.health import FanOut, StreamHealth, StreamState
from aperture.engine.core import Engine
from aperture.types import Quote

STREAM_TRANSPORT = "stream-transport"
STREAM_ERROR = "stream-error"
DISCOVERY = "discovery"
DISCOVERY_FAILED = "discovery-failed"
STREAM_LOST_EXIT = "stream-lost"


class StreamInbox:
    """QuoteListener plus `on_error` sink for the stream thread. Thread safe; never raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Quote] = {}
        self._events: deque[tuple[str, dict[str, Any]]] = deque()

    def on_quote(self, quote: Quote) -> None:
        with self._lock:
            previous = self._latest.get(quote.symbol)
            if previous is None or quote.provider_time >= previous.provider_time:
                self._latest[quote.symbol] = quote

    def on_transport(self, up: bool, at: datetime, detail: str) -> None:
        with self._lock:
            self._events.append((STREAM_TRANSPORT, {"up": up, "at": at.isoformat(),
                                                    "detail": detail}))

    def on_error(self, where: str, error: Exception) -> None:
        """For `StreamSession(on_error=...)` and `FanOut(on_error=...)` (listener ignored).
        Only the exception type is kept: a message may carry request details."""
        with self._lock:
            self._events.append((STREAM_ERROR, {"where": where,
                                                "error": type(error).__name__}))

    def on_listener_error(self, listener: QuoteListener, event: str, error: Exception) -> None:
        self.on_error(f"{type(listener).__name__}.{event}", error)

    def latest(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._latest.get(symbol)

    def forget(self, symbol: str) -> None:
        with self._lock:
            self._latest.pop(symbol, None)

    def last_receipt(self) -> datetime | None:
        with self._lock:
            times = [q.receipt_time for q in self._latest.values()]
        return max(times) if times else None

    def take_events(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events


@dataclass(frozen=True)
class DiscoveryState:
    at: datetime
    summary: str
    candidates: tuple[Candidate, ...]


class MarketRuntime:
    def __init__(self, engine: Engine, config: Config, source: MarketData, *,
                 health: StreamHealth, inbox: StreamInbox, capture: CaptureWriter | None,
                 calendar: SessionCalendar | None, now: Callable[[], datetime],
                 monotonic: Callable[[], float]) -> None:
        self.engine = engine
        self.config = config
        self.source = source
        self.health = health
        self.inbox = inbox
        self.capture = capture
        self.calendar = calendar
        self.now = now
        self.monotonic = monotonic
        listeners: list[QuoteListener] = [health, inbox]
        if capture is not None:
            listeners.append(capture)
        self.listener = FanOut(*listeners, on_error=inbox.on_listener_error)
        self.settings = DiscoverySettings.from_config(config)
        self.subscribed: set[str] = set()
        self.watch: set[str] = set()  # the strategy's WATCH symbols, set by the service
        self.discovery: DiscoveryState | None = None
        self._next_discovery = monotonic()
        self._started = now()

    def bind(self) -> None:
        """Bind the stream listener before the stream starts, so the first connect is seen."""
        try:
            self.source.subscribe([], self.listener)
        except Exception as exc:  # noqa: BLE001 - see _sync_subscriptions
            self.inbox.on_error("bind", exc)

    # --- one pass, called by the service after the engine's broker work ---------------------

    def tick(self) -> None:
        self._journal_stream_events()
        self._sync_subscriptions()
        self._exit_on_stream_loss()
        self._discover_if_due()

    def _journal_stream_events(self) -> None:
        events = self.inbox.take_events()
        if not events:
            return
        store = self.engine.store
        with store.transaction():
            for kind, body in events:
                store.append(kind, body, run_id=self.engine.run_id)

    def _position_symbols(self) -> set[str]:
        symbols = set()
        for trade in self.engine.trades():
            if self.engine.expected_quantity(self.engine.orders(trade.trade_id)) > 0:
                symbols.add(trade.symbol)
        return symbols

    def _sync_subscriptions(self) -> None:
        """L1 for the open position and the strategy's WATCH symbols (spec 6)."""
        desired = self._position_symbols() | self.watch
        added, removed = desired - self.subscribed, self.subscribed - desired
        if removed:
            try:
                self.source.unsubscribe(sorted(removed))
            except Exception as exc:  # noqa: BLE001 - a stream problem never ends the loop
                self.inbox.on_error("unsubscribe", exc)
            self.health.untrack(removed)
            for symbol in removed:
                self.inbox.forget(symbol)
            self.subscribed -= removed
        if added:
            self.health.track(added)
            try:
                self.source.subscribe(sorted(added), self.listener)
            except Exception as exc:  # noqa: BLE001 - a stream problem never ends the loop
                self.inbox.on_error("subscribe", exc)
                self.health.untrack(added)
                return
            self.subscribed |= added

    def _in_regular_session(self, now: datetime) -> tuple[bool, datetime | None]:
        if self.calendar is None:
            return False, None
        try:
            day = self.calendar.day_at(now)
        except CalendarRefused:
            return False, None
        return day.in_regular_session(now), day.regular_open

    def _exit_on_stream_loss(self) -> None:
        """Transport lost with a position open, in the regular session: market exit through
        the normal path, after `stream_lost_exit_seconds` (spec 6)."""
        now = self.now()
        regular, _ = self._in_regular_session(now)
        if not regular:
            return
        transport = self.health.transport
        if transport.up:
            return
        since = transport.since or self._started
        if now - since < timedelta(seconds=float(self.config.market_data.stream_lost_exit_seconds)):
            return
        positions = self._position_symbols()
        for trade in self.engine.trades():
            if trade.symbol in positions and not trade.exit_requested:
                self.engine.request_exit(trade.trade_id, STREAM_LOST_EXIT)

    def _discover_if_due(self) -> None:
        if self.monotonic() < self._next_discovery:
            return
        now = self.now()
        regular, session_open = self._in_regular_session(now)
        if not regular or session_open is None:
            return
        self._next_discovery = (self.monotonic()
                                + float(self.config.market_data.discovery_interval_seconds))
        try:
            result = discover(self.source, self.settings, session_open, self.now,
                              self.capture)
        except (MarketDataError, BrokerError) as exc:  # MalformedResponse is a BrokerError
            with self.engine.store.transaction():
                self.engine.store.append(DISCOVERY_FAILED, {"error": type(exc).__name__},
                                         run_id=self.engine.run_id)
            return  # tried again at the next interval
        self.discovery = DiscoveryState(now, result.summary(), result.candidates)
        with self.engine.store.transaction():
            self.engine.store.append(DISCOVERY, {
                "summary": result.summary(), "evaluated": result.evaluated,
                "candidates": [c.instrument.symbol for c in result.candidates]},
                run_id=self.engine.run_id)

    # --- status ----------------------------------------------------------------------------

    def freshness(self, last_broker_read_at: datetime | None) -> api.Freshness:
        positions = sorted(self._position_symbols() & self.subscribed)
        if not positions:
            stream = api.StreamHealth.OFF
        else:
            state = self.health.state(positions[0], self.now())
            stream = {StreamState.FRESH: api.StreamHealth.FRESH,
                      StreamState.QUIET: api.StreamHealth.QUIET,
                      StreamState.LOST: api.StreamHealth.LOST}[state]
        return api.Freshness(last_broker_read_at, self.inbox.last_receipt(), stream)
