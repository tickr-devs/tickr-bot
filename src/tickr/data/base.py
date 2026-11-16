"""Market-data interface. Every provider (fake, Webull) satisfies `MarketData`.

Only `broker/webull.py` may import the Webull SDK (CLAUDE.md rule 9), so the Webull
implementation of this protocol lives there; everything in `tickr.data` depends only on this
module. Providers return typed values (Decimal prices, aware datetimes) and raise typed errors:

- `tickr.broker.base.RateLimited` when the request budget refuses a call (nothing was sent);
- `DelayedData` when the provider says the data is delayed (Webull bars `delay_minutes`);
- any other `MarketDataError` (or the adapter's `BrokerError`) for a failed or malformed read.

They never return raw provider payloads, except `ScreenerRow.fields`, which keeps the complete
provider row as strings so the capture file can record it verbatim for later replay.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from tickr.broker.base import RateLimited
from tickr.types import Bar, Quote


class ScreenerKind(StrEnum):
    """The three screeners discovery merges (spec section 6)."""

    GAINERS = "GAINERS"
    LOSERS = "LOSERS"
    TOP_ACTIVES = "TOP_ACTIVES"


class ScreenerSession(StrEnum):
    """Which session's change the gainers and losers rank by: the regular session's (also
    discovery's), the pre-market's or the post-market's. The top actives have one ranking."""

    REGULAR = "REGULAR"
    PRE = "PRE"
    POST = "POST"


@dataclass(frozen=True)
class InstrumentProfile:
    """One instrument profile row: identity, the type discovery filters on, and the complete
    provider row (`fields`, every value a string: shortable, easy_to_borrow, the margin
    requirements, status ...) for capture."""

    instrument_id: str
    symbol: str
    security_type: str | None
    fields: Mapping[str, str] = field(default_factory=dict)


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


class BarSpan(StrEnum):
    """Bar sizes read for charts (`MarketData.history_bars`), as Webull names its timespans.
    Every other chart interval is aggregated from these by `tickr.api.chart`."""

    M1 = "M1"  # one minute, unadjusted (SDK docstring); pre-, regular and post-market
    D = "D"  # one day, forward-adjusted (SDK docstring)


@dataclass(frozen=True)
class SessionBar:
    """A chart bar and the provider's session label: "PRE", "RTH" or "ATH" for M1, None for D.
    The label is checked against the session calendar, never trusted alone."""

    bar: Bar
    session: str | None


class MarketDataError(Exception):
    """Base class for typed market-data failures."""


class DelayedData(MarketDataError):
    """The provider marked the data as delayed. Delayed data never feeds a decision."""

    def __init__(self, symbols: Sequence[str], delay_minutes: Decimal) -> None:
        super().__init__(f"data delayed by {delay_minutes} min for {', '.join(symbols)}")
        self.symbols = tuple(symbols)
        self.delay_minutes = delay_minutes


class SymbolsRefused(MarketDataError):
    """A read was refused whole because a requested symbol is unknown to that route (Webull:
    HTTP 417 INVALID_SYMBOL, seen 2026-11-14 for bars of listed-but-odd symbols such as 1ESC011
    and 2026-11-16 for the snapshot of DJT, a symbol the provider's own screener listed). The
    response names no symbol in a structured field, so the caller finds it by splitting."""

    def __init__(self, route: str, http_status: int | None, code: str | None) -> None:
        super().__init__(f"{route}: http_status={http_status} code={code}")
        self.route = route
        self.http_status = http_status
        self.code = code


def read_without_refused[T](read: Callable[[Sequence[str]], list[T]], symbols: Sequence[str],
                           found: Callable[[str], None]) -> list[T]:
    """`read(symbols)`; when the route refuses the request for an unknown symbol
    (`SymbolsRefused`), the symbols are split in halves until each refused symbol is alone, and
    `found(symbol)` is called for it at once, so a split the budget interrupts still keeps what
    it isolated. Returns the rows read. Every split is a new request; RateLimited and
    DelayedData propagate. Used by discovery (snapshot, bars) and history (bars pages)."""
    try:
        return read(symbols)
    except SymbolsRefused:
        if len(symbols) == 1:
            found(symbols[0])
            return []
    middle = len(symbols) // 2
    left = read_without_refused(read, symbols[:middle], found)
    return left + read_without_refused(read, symbols[middle:], found)


class SubscriptionLimit(MarketDataError):
    """A stream subscribe was refused for the per-connection symbol limit. The symbols already
    subscribed stay subscribed (probe M3); the refused ones were not added."""

    def __init__(self, symbols: Iterable[str], limit: int, by_provider: bool) -> None:
        self.symbols = tuple(sorted(symbols))
        self.limit = limit
        self.by_provider = by_provider
        who = "provider" if by_provider else "configured"
        refused = ", ".join(self.symbols)
        super().__init__(f"{who} stream limit of {limit} symbols: refused {refused}")


@dataclass(frozen=True)
class StreamNotice:
    """A stream status notice (Webull type 1001): the provider's counters as received. Their
    meaning and units are unproven (docs/webull-facts.md M3); kept for research only."""

    receipt_time: datetime
    drop: int
    rtt: int
    sent: int


class QuoteListener(Protocol):
    """Receives stream events. Called on the provider's thread; keep the work tiny."""

    def on_quote(self, quote: Quote) -> None: ...

    def on_notice(self, notice: StreamNotice) -> None: ...

    def on_transport(self, up: bool, at: datetime, detail: str) -> None:
        """Transport went up (connected and subscribed) or down (`detail` says why)."""
        ...


class MarketData(Protocol):
    @property
    def provider(self) -> str:
        """Provider name recorded with every captured record (for example "webull")."""
        ...

    def screener(self, kind: ScreenerKind,
                 session: ScreenerSession = ScreenerSession.REGULAR) -> list[ScreenerRow]: ...

    def profiles(self, symbols: Sequence[str], *,
                 budget_reserve: int = 0) -> list[InstrumentProfile]:
        """The instrument profile rows of `symbols` (several rows per symbol possible; match by
        instrument_id), leaving `budget_reserve` requests of the route free."""
        ...

    def bars(self, symbols: Sequence[str], count: int, *, budget_reserve: int = 0) -> list[Bar]:
        """Up to `count` most recent M1 bars per symbol, oldest first per symbol.

        May include the bar of the minute in progress; callers keep completed bars only.
        `budget_reserve`: requests of the bars route this read must leave free in the budget
        window (discovery keeps them for evaluation and history); refused as `RateLimited`
        (cause `reserved`) when only those are left.
        """
        ...

    def bars_before(self, symbols: Sequence[str], end: datetime, count: int, *,
                    budget_reserve: int = 0) -> list[Bar]:
        """Up to `count` regular-session M1 bars PER SYMBOL (probe 2026-11-16) of each of
        `symbols` (at most `[market_data] bars_symbols_per_request`) that START strictly before
        the aware `end`, oldest first per symbol. One request: one page of history for all of
        them; `tickr.data.history` pages and caches. `budget_reserve` as for `bars`. Refused
        whole as `SymbolsRefused` when one symbol is unknown to the route."""
        ...

    def history_bars(self, symbol: str, span: BarSpan, end: datetime,
                     count: int) -> list[SessionBar]:
        """Up to `count` bars of size `span` of one symbol that START strictly before the aware
        `end`, oldest first, for charts only (never read by the trading loop). M1 includes the pre-
        and post-market minutes, each labelled with its session."""
        ...

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]: ...

    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        """Stream L1 quotes for `symbols` (added to the current set) to `listener`."""
        ...

    def unsubscribe(self, symbols: Iterable[str]) -> None: ...

    def subscribe_optional(self, symbols: Iterable[str], listener: QuoteListener,
                           budget_reserve: int) -> None:
        """Like `subscribe`, but for symbols nobody trades on (observers): only on a live
        stream, only while at least `budget_reserve` subscribe requests would stay free, and a
        failure is raised without disturbing the connection (no reconnect)."""
        ...

    def unsubscribe_optional(self, symbols: Iterable[str], budget_reserve: int) -> None:
        """Drop symbols added by `subscribe_optional`, with the same guarantees."""
        ...


class FakeMarketData:
    """In-memory `MarketData` for tests: scripted rows, bars and quotes; a call budget per method.

    `limit(method, calls)` makes the (calls + 1)-th call of that method raise `RateLimited`,
    like the request budget. `delay_bars(minutes)` makes `bars` raise `DelayedData`.
    """

    def __init__(self, provider: str = "fake") -> None:
        self._provider = provider
        self._rows: dict[ScreenerKind, list[ScreenerRow]] = {}
        self._bars: dict[str, list[Bar]] = {}
        self._history: dict[tuple[str, BarSpan], list[SessionBar]] = {}
        self._quotes: dict[str, Quote] = {}
        self._profiles: dict[str, list[InstrumentProfile]] = {}
        self._limits: dict[str, int] = {}
        self._bars_delay: Decimal | None = None
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.bars_reserves: list[int] = []  # budget_reserve of each bars and bars_before call
        self.subscribed: set[str] = set()
        self._listener: QuoteListener | None = None

    @property
    def provider(self) -> str:
        return self._provider

    # -- scripting
    def set_profile(self, profile: InstrumentProfile) -> None:
        self._profiles.setdefault(profile.symbol, []).append(profile)

    def set_screener(self, kind: ScreenerKind, rows: list[ScreenerRow]) -> None:
        self._rows[kind] = list(rows)

    def add_bars(self, bars: Iterable[Bar]) -> None:
        for bar in bars:
            self._bars.setdefault(bar.symbol, []).append(bar)

    def add_history_bars(self, span: BarSpan, bars: Iterable[SessionBar]) -> None:
        for item in bars:
            self._history.setdefault((item.bar.symbol, span), []).append(item)

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

    def screener(self, kind: ScreenerKind,
                 session: ScreenerSession = ScreenerSession.REGULAR) -> list[ScreenerRow]:
        self._call("screener", [kind.value] if session is ScreenerSession.REGULAR
                   else [kind.value, session.value])
        return list(self._rows.get(kind, []))

    def profiles(self, symbols: Sequence[str], *,
                 budget_reserve: int = 0) -> list[InstrumentProfile]:
        self._call("profiles", symbols)
        return [p for s in symbols for p in self._profiles.get(s, [])]

    def bars(self, symbols: Sequence[str], count: int, *, budget_reserve: int = 0) -> list[Bar]:
        self.bars_reserves.append(budget_reserve)
        self._call("bars", symbols)
        if self._bars_delay is not None:
            raise DelayedData(symbols, self._bars_delay)
        return [bar for symbol in symbols for bar in self._bars.get(symbol, [])[-count:]]

    def bars_before(self, symbols: Sequence[str], end: datetime, count: int, *,
                    budget_reserve: int = 0) -> list[Bar]:
        self.bars_reserves.append(budget_reserve)
        self._call("bars_before", symbols)
        if self._bars_delay is not None:
            raise DelayedData(symbols, self._bars_delay)
        out: list[Bar] = []
        for symbol in symbols:
            older = sorted((b for b in self._bars.get(symbol, []) if b.start < end),
                           key=lambda b: b.start)
            out.extend(older[-count:] if count > 0 else [])
        return out

    def history_bars(self, symbol: str, span: BarSpan, end: datetime,
                     count: int) -> list[SessionBar]:
        self._call("history_bars", [symbol, span.value])
        older = sorted((b for b in self._history.get((symbol, span), []) if b.bar.start < end),
                       key=lambda b: b.bar.start)
        return older[-count:] if count > 0 else []

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]:
        self._call("snapshot", symbols)
        return [self._quotes[s] for s in symbols if s in self._quotes]

    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        self._listener = listener
        self.subscribed.update(symbols)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        self.subscribed.difference_update(symbols)

    def subscribe_optional(self, symbols: Iterable[str], listener: QuoteListener,
                           budget_reserve: int) -> None:
        self.subscribe(symbols, listener)

    def unsubscribe_optional(self, symbols: Iterable[str], budget_reserve: int) -> None:
        self.unsubscribe(symbols)
