"""L1 stream health per symbol: fresh, quiet or lost (spec section 6).

- LOST: the transport is down (or never came up). Every tracked symbol is lost.
- QUIET: transport up, but no quote received for the symbol within `quiet_after` since the
  transport last came up. Quiet is not loss: a thin symbol may simply not trade.
- FRESH: transport up and a quote received within `quiet_after`.

Health uses receipt time (is the pipe delivering?). Whether a quote may feed a decision is a
separate rule on provider time (`max_quote_age_seconds`), checked by admission.

`StreamHealth` is a `QuoteListener`; wrap it with `FanOut` to also feed capture or the bot loop.
A query for a symbol that is not tracked raises `UntrackedSymbol` instead of answering.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from aperture.config import Config
from aperture.data.base import QuoteListener
from aperture.types import Quote


class StreamState(StrEnum):
    FRESH = "fresh"
    QUIET = "quiet"
    LOST = "lost"


class UntrackedSymbol(KeyError):
    """Health was asked about a symbol it does not track."""


@dataclass(frozen=True)
class TransportStatus:
    up: bool
    since: datetime | None
    detail: str


class StreamHealth:
    """Thread safe: the provider thread writes, the bot loop and the API read."""

    def __init__(self, quiet_after: timedelta) -> None:
        self._quiet_after = quiet_after
        self._lock = threading.Lock()
        self._tracked: set[str] = set()
        self._last: dict[str, datetime] = {}
        self._transport = TransportStatus(False, None, "never connected")

    @classmethod
    def from_config(cls, config: Config) -> StreamHealth:
        return cls(timedelta(seconds=float(config.market_data.quiet_after_seconds)))

    def track(self, symbols: Iterable[str]) -> None:
        with self._lock:
            self._tracked.update(symbols)

    def untrack(self, symbols: Iterable[str]) -> None:
        with self._lock:
            for symbol in symbols:
                self._tracked.discard(symbol)
                self._last.pop(symbol, None)

    @property
    def transport(self) -> TransportStatus:
        with self._lock:
            return self._transport

    # -- QuoteListener
    def on_quote(self, quote: Quote) -> None:
        with self._lock:
            if quote.symbol not in self._tracked or not self._transport.up:
                return
            previous = self._last.get(quote.symbol)
            if previous is None or quote.receipt_time > previous:
                self._last[quote.symbol] = quote.receipt_time

    def on_transport(self, up: bool, at: datetime, detail: str) -> None:
        with self._lock:
            # Quotes from before a transport change never prove the new connection works.
            self._last.clear()
            self._transport = TransportStatus(up, at, detail)

    # -- queries
    def _state(self, symbol: str, now: datetime) -> StreamState:
        if symbol not in self._tracked:
            raise UntrackedSymbol(symbol)
        if not self._transport.up:
            return StreamState.LOST
        last = self._last.get(symbol)
        if last is None or now - last > self._quiet_after:
            return StreamState.QUIET
        return StreamState.FRESH

    def state(self, symbol: str, now: datetime) -> StreamState:
        with self._lock:
            return self._state(symbol, now)

    def states(self, now: datetime) -> dict[str, StreamState]:
        with self._lock:
            return {symbol: self._state(symbol, now) for symbol in sorted(self._tracked)}


class FanOut:
    """Forwards every stream event to several listeners, in order, each isolated.

    A listener that raises (for example capture with a full disk) never stops the others from
    receiving the event and never propagates into the provider's thread: the failure is handed
    to `on_error(listener, event, error)`, which must record it (journal, alert) and not raise.
    """

    def __init__(self, *listeners: QuoteListener,
                 on_error: Callable[[QuoteListener, str, Exception], None]) -> None:
        self._listeners = listeners
        self._on_error = on_error

    def _each(self, event: str, call: Callable[[QuoteListener], None]) -> None:
        for listener in self._listeners:
            try:
                call(listener)
            except Exception as exc:  # noqa: BLE001 - isolation is the point; reported below
                self._on_error(listener, event, exc)

    def on_quote(self, quote: Quote) -> None:
        self._each("quote", lambda listener: listener.on_quote(quote))

    def on_transport(self, up: bool, at: datetime, detail: str) -> None:
        self._each("transport", lambda listener: listener.on_transport(up, at, detail))
