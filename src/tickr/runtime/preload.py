"""Pre-open history preload: the prior sessions of the screeners' symbols, before the open.

From `[market_data] history_preload_minutes_before_open` before the regular open until the open,
the bot loop (through `Evaluator.tick`) reads the three screeners every
`history_preload_screener_seconds`, keeps the symbols that pass the universe checks (exchange,
currency, security type: the same rules as discovery, no quote or liquidity check before the
open) and queues those not seen yet today, in screener order. Each later loop pass loads the
prior-session history of ONE batch of queued symbols (`HistoryReader.prior_sessions`, at most
`bars_symbols_per_request` symbols, a few pages), leaving `discovery_bars_reserve_requests` of
the bars route free, so the work per pass stays bounded. The history goes to the reader's
memory and disk cache, where the strategy's evaluation finds it from the first minute on.

Only reads: nothing here decides, places or asks anything. A batch that settled symbols writes
one `history-preload` journal row (counts, requests, symbols still queued); a failed screener
read or a failed history read (not a budget refusal) writes one row with the error. After any
error, budget included, the preload pauses `history_preload_retry_seconds`. Symbols refused by
history are not retried this day (the reader remembers the refusal); symbols the open reached
first are loaded by the evaluation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tickr.broker.base import BrokerError, RateLimited
from tickr.calendar import MarketDay
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError, ScreenerKind, ScreenerRow
from tickr.data.discovery import DiscoverySettings, merge, universe_cause
from tickr.data.history import HistoryReader
from tickr.engine.core import Engine

PRELOAD = "history-preload"


class HistoryPreload:
    def __init__(self, engine: Engine, config: Config, source: MarketData,
                 history: HistoryReader) -> None:
        self.engine = engine
        self.config = config
        self.source = source
        self.history = history
        self.settings = DiscoverySettings.from_config(config)
        self._day: date | None = None
        self._queue: list[str] = []
        self._seen: set[str] = set()
        self._next_screen: datetime | None = None
        self._paused_until: datetime | None = None

    def _journal(self, body: dict[str, object]) -> None:
        with self.engine.store.transaction():
            self.engine.store.append(PRELOAD, body, run_id=self.engine.run_id)

    def in_window(self, day: MarketDay, now: datetime) -> bool:
        minutes = self.config.market_data.history_preload_minutes_before_open
        return day.regular_open - timedelta(minutes=minutes) <= now < day.regular_open

    def tick(self, day: MarketDay, now: datetime) -> None:
        """At most one screener pass or one history batch; nothing outside the window."""
        if not self.in_window(day, now):
            return
        if self._day != day.ny_date:
            self._day, self._queue, self._seen = day.ny_date, [], set()
            self._next_screen, self._paused_until = None, None
        if self._paused_until is not None and now < self._paused_until:
            return
        md = self.config.market_data
        if self._next_screen is None or now >= self._next_screen:
            self._next_screen = now + timedelta(seconds=float(md.history_preload_screener_seconds))
            self._screen(now)
            return
        if not self._queue:
            return
        batch = self._queue[:md.bars_symbols_per_request]
        read = self.history.prior_sessions(batch, day.ny_date, md.history_sessions,
                                           budget_reserve=md.discovery_bars_reserve_requests)
        settled = set(read.found) | set(read.refused)
        self._queue = [s for s in self._queue if s not in settled]
        error = read.error
        if settled or (error is not None and not isinstance(error, RateLimited)):
            body: dict[str, object] = {
                "symbols": len(batch), "loaded": len(set(read.found) & set(batch)),
                "refused": len(set(read.refused) & set(batch)), "requests": read.requests,
                "queued": len(self._queue)}
            if error is not None:
                body["error"] = type(error).__name__
            self._journal(body)
        if error is not None:
            self._paused_until = now + timedelta(seconds=float(md.history_preload_retry_seconds))

    def _screen(self, now: datetime) -> None:
        rows: list[ScreenerRow] = []
        try:
            for kind in ScreenerKind:
                rows.extend(self.source.screener(kind))
        except (MarketDataError, BrokerError) as exc:  # RateLimited included
            self._journal({"stage": "screener", "error": type(exc).__name__})
            self._paused_until = now + timedelta(
                seconds=float(self.config.market_data.history_preload_retry_seconds))
            return
        instruments, _ = merge(rows)
        fresh = [i.symbol for i in instruments
                 if universe_cause(i, self.settings) is None and i.symbol not in self._seen]
        self._seen.update(fresh)
        self._queue.extend(fresh)

