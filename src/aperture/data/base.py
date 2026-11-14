"""Market-data interface. Every provider (fake, Webull) satisfies `MarketData`.

Only `broker/webull.py` may import the Webull SDK (CLAUDE.md rule 9), so the Webull
implementation of this protocol lives there; everything in `aperture.data` depends only on this
module. Providers return typed values (Decimal prices, aware datetimes) and raise typed errors:

- `aperture.broker.base.RateLimited` when the request budget refuses a call (nothing was sent);
- `DelayedData` when the provider says the data is delayed (Webull bars `delay_minutes`);
- any other `MarketDataError` (or the adapter's `BrokerError`) for a failed or malformed read.

They never return raw provider payloads, except `ScreenerRow.fields`, which keeps the complete
provider row as strings so the capture file can record it verbatim for later replay.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from aperture.broker.base import RateLimited
from aperture.types import Bar, Quote


class ScreenerKind(StrEnum):
    """The three screeners discovery merges (spec section 6)."""

    GAINERS = "GAINERS"
    LOSERS = "LOSERS"
    TOP_ACTIVES = "TOP_ACTIVES"


@dataclass(frozen=True)
class ScreenerRow:
    """One screener row. `rank` counts from 1 in provider order.

    `security_type` is None when the provider did not say; discovery refuses such rows.
    `fields` is the complete provider row, every value a string, for capture.
    """

    kind: ScreenerKind
    rank: int
    instrument_id: str
    symbol: str
    exchange_code: str
    currency: str
    security_type: str | None
    fields: Mapping[str, str] = field(default_factory=dict)


class MarketDataError(Exception):
    """Base class for typed market-data failures."""


class DelayedData(MarketDataError):
    """The provider marked the data as delayed. Delayed data never feeds a decision."""

    def __init__(self, symbols: Sequence[str], delay_minutes: Decimal) -> None:
        super().__init__(f"data delayed by {delay_minutes} min for {', '.join(symbols)}")
        self.symbols = tuple(symbols)
        self.delay_minutes = delay_minutes


class QuoteListener(Protocol):
    """Receives stream events. Called on the provider's thread; keep the work tiny."""

    def on_quote(self, quote: Quote) -> None: ...

    def on_transport(self, up: bool, at: datetime, detail: str) -> None:
        """Transport went up (connected and subscribed) or down (`detail` says why)."""
        ...


class MarketData(Protocol):
    @property
    def provider(self) -> str:
        """Provider name recorded with every captured record (for example "webull")."""
        ...

    def screener(self, kind: ScreenerKind) -> list[ScreenerRow]: ...

    def bars(self, symbols: Sequence[str], count: int) -> list[Bar]:
        """Up to `count` most recent M1 bars per symbol, oldest first per symbol.

        May include the bar of the minute in progress; callers keep completed bars only.
        """
        ...

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]: ...

    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        """Stream L1 quotes for `symbols` (added to the current set) to `listener`."""
        ...

    def unsubscribe(self, symbols: Iterable[str]) -> None: ...


class FakeMarketData:
    """In-memory `MarketData` for tests: scripted rows, bars and quotes; a call budget per method.

    `limit(method, calls)` makes the (calls + 1)-th call of that method raise `RateLimited`,
    like the request budget. `delay_bars(minutes)` makes `bars` raise `DelayedData`.
    """

    def __init__(self, provider: str = "fake") -> None:
        self._provider = provider
        self._rows: dict[ScreenerKind, list[ScreenerRow]] = {}
        self._bars: dict[str, list[Bar]] = {}
        self._quotes: dict[str, Quote] = {}
        self._limits: dict[str, int] = {}
        self._bars_delay: Decimal | None = None
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.subscribed: set[str] = set()
        self._listener: QuoteListener | None = None

    @property
    def provider(self) -> str:
        return self._provider

    # -- scripting
    def set_screener(self, kind: ScreenerKind, rows: list[ScreenerRow]) -> None:
        self._rows[kind] = list(rows)

    def add_bars(self, bars: Iterable[Bar]) -> None:
        for bar in bars:
            self._bars.setdefault(bar.symbol, []).append(bar)

    def set_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol] = quote

    def limit(self, method: str, calls: int) -> None:
        self._limits[method] = calls

    def delay_bars(self, minutes: Decimal) -> None:
        self._bars_delay = minutes

    def push_quote(self, quote: Quote) -> None:
        if self._listener is not None and quote.symbol in self.subscribed:
            self._listener.on_quote(quote)

    def transport(self, up: bool, at: datetime, detail: str = "") -> None:
        if self._listener is not None:
            self._listener.on_transport(up, at, detail)

    # -- MarketData
    def _call(self, method: str, args: Iterable[str]) -> None:
        used = sum(1 for name, _ in self.calls if name == method)
        if method in self._limits and used >= self._limits[method]:
            raise RateLimited(f"fake {method} budget")
        self.calls.append((method, tuple(args)))

    def screener(self, kind: ScreenerKind) -> list[ScreenerRow]:
        self._call("screener", [kind.value])
        return list(self._rows.get(kind, []))

    def bars(self, symbols: Sequence[str], count: int) -> list[Bar]:
        self._call("bars", symbols)
        if self._bars_delay is not None:
            raise DelayedData(symbols, self._bars_delay)
        return [bar for symbol in symbols for bar in self._bars.get(symbol, [])[-count:]]

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]:
        self._call("snapshot", symbols)
        return [self._quotes[s] for s in symbols if s in self._quotes]

    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        self._listener = listener
        self.subscribed.update(symbols)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        self.subscribed.difference_update(symbols)
