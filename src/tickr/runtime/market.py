"""Market data for the bot loop (spec 6): the L1 stream's events, the subscription set, stream
health, discovery on its interval (each pass starting at a minute's start, its bars reads kept in
`TodayBars` for the strategy's evaluation), and the market exit when the stream is lost with a
position.

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

from tickr.api import contract as api
from tickr.broker.base import BrokerError
from tickr.calendar import CalendarRefused, SessionCalendar
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError, QuoteListener, StreamNotice
from tickr.data.capture import CaptureWriter
from tickr.data.discovery import (
    Candidate,
    DiscoveryResult,
    DiscoverySettings,
    NoStrategy,
    RefusedSymbols,
    discover,
    pass_record,
)
from tickr.data.health import FanOut, StreamHealth, StreamState
from tickr.data.todaybars import TodayBars
from tickr.engine.core import Engine, Leg
from tickr.strategy.base import Liquidity
from tickr.types import Quote

STREAM_TRANSPORT = "stream-transport"
STREAM_ERROR = "stream-error"
DISCOVERY = "discovery"
DISCOVERY_FAILED = "discovery-failed"
STREAM_SUBSCRIPTIONS = "stream-subscriptions"
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

    def on_notice(self, notice: StreamNotice) -> None:
        """Notice counters go to capture only (about one a minute), not to the journal."""

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
                 calendar: SessionCalendar | None, today_bars: TodayBars,
                 now: Callable[[], datetime], monotonic: Callable[[], float]) -> None:
        self.engine = engine
        self.config = config
        self.source = source
        self.health = health
        self.inbox = inbox
        self.capture = capture
        self.calendar = calendar
        self.today_bars = today_bars  # discovery's bars reads, shared with the evaluation
        self.now = now
        self.monotonic = monotonic
        listeners: list[QuoteListener] = [health, inbox]
        if capture is not None:
            listeners.append(capture)
        self.listener = FanOut(*listeners, on_error=inbox.on_listener_error)
        self.settings = DiscoverySettings.from_config(config)
        self.subscribed: set[str] = set()
        self.watch: set[str] = set()  # the strategy's WATCH symbols, set by the service
        # Observers' WATCH and ENTER symbols, set by the service; lowest priority on the stream
        self.observer_watch: set[str] = set()
        self.discovery: DiscoveryState | None = None
        self._refused_symbols = RefusedSymbols()  # unknown to a route, kept for the session
        # The active strategy's liquidity rules, set by the caller once the strategy is loaded;
        # until then discovery still captures screeners and quotes but keeps no candidate.
        self.screen: Liquidity = NoStrategy()
        self._candidates_retry_at = monotonic()  # candidates off until then after a refusal
        self._observers_retry_at = monotonic()  # observer symbols off until then after one
        self._observers_next_sync = monotonic()  # observer tier changes at most once until then
        self._observers_applied: set[str] = set()  # the observer tier as last chosen
        self._optional: set[str] = set()  # subscribed through subscribe_optional
        self._pass_minute: datetime | None = None  # the minute of the latest loop pass
        self._first_pass = False  # this loop pass is the first of its minute (or the first)
        self._discovery_anchor: datetime | None = None  # interval counted from here
        self._started = now()

    def bind(self) -> None:
        """Bind the stream listener before the stream starts, so the first connect is seen."""
        try:
            self.source.subscribe([], self.listener)
        except Exception as exc:  # noqa: BLE001 - see _sync_subscriptions
            self.inbox.on_error("bind", exc)

    # --- one pass, called by the service after the engine's broker work ---------------------

    def tick(self, *, discover: bool = True) -> None:
        """`discover=False` while protection needs the loop (runtime/protection.py), and
        whenever the strategy's evaluation runs discovery itself (`discover()`, after the
        positions and WATCH symbols of the minute): the stream, subscriptions and the
        stream-loss exit still run."""
        minute = self.now().replace(second=0, microsecond=0)
        self._first_pass = minute != self._pass_minute
        self._pass_minute = minute
        self._journal_stream_events()
        self._sync_subscriptions()  # new candidates: on the next pass, a second later
        self._exit_on_stream_loss()
        if discover:
            self.discover()

    def candidates(self) -> list[str]:
        """The symbols of the latest discovery's candidates, best first."""
        found = self.discovery
        return [] if found is None else [c.instrument.symbol for c in found.candidates]

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

    def _entry_symbols(self) -> set[str]:
        """Symbols with a working entry: their quotes decide whether it may keep resting."""
        return {o.symbol for o in self.engine.orders()
                if o.leg is Leg.ENTRY and o.live}

    def _candidate_symbols(self) -> set[str]:
        # Only on a live transport: a subscribe while down is queued silently and sent by the
        # next connect's resubscribe of the full set, where a refusal cannot be rolled back.
        if (self.discovery is None or not self.health.transport.up
                or self.monotonic() < self._candidates_retry_at):
            return set()
        best = self.discovery.candidates[:self.config.market_data.candidate_subscriptions]
        return {c.instrument.symbol for c in best}

    def _trim(self, symbols: set[str], room: int) -> set[str]:
        """At most `room` of `symbols`, those already subscribed first, then by name."""
        ordered = sorted(symbols, key=lambda s: (s not in self.subscribed, s))
        return set(ordered[:max(room, 0)])

    def _observer_symbols(self, taken: set[str], trading: bool) -> set[str]:
        """The observer tier: symbols the observers watch that the higher tiers (`taken`) do
        not, at most `observer_subscriptions` and never more than the slots left free after
        the higher tiers and `stream_reserved_symbols`, so it shrinks at once when they need
        room. Otherwise it changes at most once per `observer_resync_seconds`, so it costs at
        most one subscribe and one unsubscribe request per period; symbols already subscribed
        keep their slot. While `trading` (a position or a working entry) it is not changed
        except to give slots back: any failed (un)subscribe reconnects the whole stream,
        which must not happen to a position's quotes for an observer. Same live-transport
        rule as candidates."""
        now = self.monotonic()
        if not self.health.transport.up:
            self._observers_applied = set()
            return set()
        md = self.config.market_data
        free = md.stream_max_symbols - md.stream_reserved_symbols - len(taken)
        cap = max(min(md.observer_subscriptions, free), 0)
        live = self._observers_applied & self.subscribed  # what this tier really holds
        if now >= self._observers_next_sync and not trading and now >= self._observers_retry_at:
            self._observers_next_sync = now + float(md.observer_resync_seconds)
            # A symbol a higher tier subscribed and is dropping leaves by that tier's path;
            # the observer tier may take it again on a later resync, optionally.
            wanted = self.observer_watch - taken - (self.subscribed - self._optional)
            self._observers_applied = self._trim(wanted, cap)
        else:
            # No new symbol while trading, in a retry pause or between resyncs: only what is
            # live stays, trimmed to the room left.
            self._observers_applied = self._trim(live - taken, cap)
        return set(self._observers_applied)

    def _sync_subscriptions(self) -> None:
        """One pass of `_apply_subscriptions`; a change of the bot's subscribed set is
        journaled as `stream-subscriptions`: the symbols `added` and `removed` by this pass,
        then the whole set (`subscribed`) and the part of it on the optional path
        (`optional`). It is the bot's own record of what it asked for and was not refused,
        so a day's quotes and latency can be read against what was subscribed when; the
        symbols a failed unsubscribe may have left on the connection are not in it."""
        before = set(self.subscribed)
        try:
            self._apply_subscriptions()
        finally:
            if self.subscribed != before:
                store = self.engine.store
                with store.transaction():
                    store.append(STREAM_SUBSCRIPTIONS, {
                        "added": sorted(self.subscribed - before),
                        "removed": sorted(before - self.subscribed),
                        "subscribed": sorted(self.subscribed),
                        "optional": sorted(self._optional & self.subscribed)},
                        run_id=self.engine.run_id)

    def _apply_subscriptions(self) -> None:
        """L1 for the open position and the strategy's WATCH symbols (spec 6), then the best
        discovery candidates, so a quote is there the first minute the strategy says ENTER,
        then the observers' WATCH symbols so their results are not limited to the active
        strategy's symbols. Candidates and observer symbols are kept only while the transport
        is up, and each tier goes in its own request after the one above it; lower tiers give
        their slots back first. A refused tier stays off for `candidate_retry_seconds`.
        Candidates and observer symbols go through `subscribe_optional` /
        `unsubscribe_optional`: no position or entry depends on them, so they never take the
        subscribe budget reserve, and their failure never reconnects the stream (a reconnect
        cuts the position's quotes and can end in the stream-loss exit). Only subscribing the
        position, entry and WATCH symbols uses the required path; every unsubscribe is
        optional."""
        trading = self._position_symbols() | self._entry_symbols()
        essential = trading | self.watch
        md = self.config.market_data
        # Lower tiers give their slots back first, so the essential request fits whenever it
        # fits the connection at all.
        candidates = self._trim(self._candidate_symbols() - essential,
                                md.stream_max_symbols - len(essential))
        observed = self._observer_symbols(essential | candidates, bool(trading))
        desired = essential | candidates | observed
        removed = self.subscribed - desired
        if removed:
            # Every removal goes the optional way, whatever tier subscribed the symbol: no
            # quote anyone needs is lost by it, and its failure must never reconnect the
            # stream (the leftovers are counted as maybe-held instead).
            try:
                self.source.unsubscribe_optional(sorted(removed),
                                                 md.stream_reserved_subscribe_requests)
            except Exception as exc:  # noqa: BLE001 - never ends the loop, no reconnect
                self.inbox.on_error("unsubscribe", exc)
            self._optional -= removed
            self.health.untrack(removed)
            for symbol in removed:
                self.inbox.forget(symbol)
            self.subscribed -= removed
        if not self._subscribe(essential - self.subscribed, "subscribe"):
            return
        retry = float(self.config.market_data.candidate_retry_seconds)
        if not self._subscribe(candidates - self.subscribed, "subscribe-candidates",
                               optional=True):
            self._candidates_retry_at = self.monotonic() + retry
            return
        if not self._subscribe(observed - self.subscribed, "subscribe-observers",
                               optional=True):
            # The live part of the tier stays; only the refused additions wait.
            self._observers_retry_at = self.monotonic() + retry

    def _subscribe(self, added: set[str], where: str, optional: bool = False) -> bool:
        if not added:
            return True
        self.health.track(added)
        try:
            if optional:
                self.source.subscribe_optional(
                    sorted(added), self.listener,
                    self.config.market_data.stream_reserved_subscribe_requests)
            else:
                self.source.subscribe(sorted(added), self.listener)
        except Exception as exc:  # noqa: BLE001 - a stream problem never ends the loop
            self.inbox.on_error(where, exc)
            self.health.untrack(added)
            return False
        self.subscribed |= added
        if optional:
            self._optional |= added
        return True

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

    def discover(self) -> None:
        """One discovery pass, in the regular session, only in the first loop pass of a minute
        (after `tick` of that pass) and at least `discovery_interval_seconds` after the
        previous start, so the bars route's 60 s window is not asked twice for a whole pass.
        The interval is counted from the start of the minute the previous pass began in (the
        first pass of a minute starts a fraction of a second after it, so counting from the
        exact start would skip every other minute), except after the bot's very first pass,
        which may be mid-minute: then from that exact start. With 60 s every pass starts at
        the start of a minute, so its bars reads were sent after the minute just completed and
        the evaluation of that minute reuses them (TodayBars)."""
        if not self._first_pass:
            return
        self._first_pass = False  # at most once per loop pass
        now = self.now()
        interval = timedelta(seconds=float(self.config.market_data.discovery_interval_seconds))
        if self._discovery_anchor is not None and now - self._discovery_anchor < interval:
            return
        regular, session_open = self._in_regular_session(now)
        if not regular or session_open is None:
            return
        startup = self._discovery_anchor is None and now - self._started < interval
        self._discovery_anchor = now if startup else now.replace(second=0, microsecond=0)
        try:
            result = discover(self.source, self.settings, self.screen, session_open,
                              self.now, self.capture, self._refused_symbols, self.today_bars)
        except (MarketDataError, BrokerError) as exc:  # MalformedResponse is a BrokerError
            # `detail` is the error's own text: the route, HTTP status and provider error code
            # for a failed read (never a body, credential or account id), cut to
            # `failure_detail_max_chars`; a cut row also has the whole text's length.
            detail = str(exc)
            cap = self.config.market_data.failure_detail_max_chars
            body: dict[str, object] = {"error": type(exc).__name__, "detail": detail[:cap]}
            if len(detail) > cap:
                body["detail_length"] = len(detail)
            with self.engine.store.transaction():
                self.engine.store.append(DISCOVERY_FAILED, body, run_id=self.engine.run_id)
            return  # tried again at the next interval
        candidates, kept = self._with_earlier(result)
        summary = result.summary()
        if kept:
            summary += f"; kept {len(kept)} earlier candidates not reached"
        self.discovery = DiscoveryState(now, summary, candidates)
        if self.capture is not None:
            finished = self.now()
            self.capture.discovery(pass_record(result, finished), finished)
        with self.engine.store.transaction():
            self.engine.store.append(DISCOVERY, {
                "summary": summary, "evaluated": result.evaluated,
                "candidates": [c.instrument.symbol for c in candidates], "kept": kept},
                run_id=self.engine.run_id)

    def _with_earlier(self, result: DiscoveryResult) -> tuple[tuple[Candidate, ...], list[str]]:
        """A pass that stopped early (bars budget, delayed data, a screener or snapshot
        budget) never shrinks the candidate list by what it did not reach: the previous
        pass's candidates whose symbol this pass neither accepted nor refused stay, after this
        pass's own. Returns the candidates and the symbols kept from before."""
        if result.stopped is None or self.discovery is None:
            return result.candidates, []
        reached = ({c.instrument.symbol for c in result.candidates}
                   | {r.symbol for r in result.rejections})
        earlier = tuple(c for c in self.discovery.candidates
                        if c.instrument.symbol not in reached)
        return result.candidates + earlier, [c.instrument.symbol for c in earlier]

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
