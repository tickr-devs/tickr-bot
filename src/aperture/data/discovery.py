"""Discovery: screeners -> merged instruments -> universe -> liquidity -> candidates.

One pass (spec section 6):
1. Read gainers, losers and top actives; merge rows by `instrument_id` (first-seen order).
   Rows of one instrument that disagree on symbol, exchange or currency, or one symbol under two
   instrument ids, are refused as conflicting, never resolved by picking one.
2. Universe: exchange, currency and security type must be in the configured lists. A row without
   a security type is refused (`SECURITY_TYPE_UNKNOWN`).
3. Spread: one snapshot quote per symbol, fresh (provider time within `max_quote_age`), bid and
   ask positive and not crossed, (ask - bid) / mid within the limit.
4. Session dollar volume: M1 bars; sum of close x volume over completed minutes of the current
   regular session (bar start >= session open, bar end <= now) must reach the minimum.

Every refusal has one cause and the measured value. When the request budget refuses a call
(`RateLimited`) or the provider reports delayed data, the pass stops at that point and the result
says so ("evaluated 140 of 200: bars budget"); nothing is cut silently.

Every screener row, snapshot quote and completed bar read is handed to the capture sink with the
receipt time taken right after the call returned.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from aperture.broker.base import RateLimited
from aperture.config import Config
from aperture.data.base import DelayedData, MarketData, ScreenerKind, ScreenerRow
from aperture.types import Bar, Quote

BAR_LENGTH = timedelta(minutes=1)
HALF = Decimal(2)


class Cause(StrEnum):
    """The single recorded cause of a discovery refusal for one instrument."""

    CONFLICTING_ROWS = "CONFLICTING_ROWS"
    EXCHANGE_NOT_ALLOWED = "EXCHANGE_NOT_ALLOWED"
    CURRENCY_NOT_ALLOWED = "CURRENCY_NOT_ALLOWED"
    SECURITY_TYPE_UNKNOWN = "SECURITY_TYPE_UNKNOWN"
    SECURITY_TYPE_NOT_ALLOWED = "SECURITY_TYPE_NOT_ALLOWED"
    NO_QUOTE = "NO_QUOTE"
    QUOTE_STALE = "QUOTE_STALE"
    QUOTE_INVALID = "QUOTE_INVALID"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    NO_BARS = "NO_BARS"
    DOLLAR_VOLUME_TOO_LOW = "DOLLAR_VOLUME_TOO_LOW"


class Stage(StrEnum):
    SCREENER = "screener"
    SNAPSHOT = "snapshot"
    BARS = "bars"


class StopCause(StrEnum):
    BUDGET = "budget"
    DELAYED_DATA = "delayed data"


@dataclass(frozen=True)
class Stopped:
    """Why the pass ended early, and at which stage."""

    stage: Stage
    cause: StopCause
    detail: str


@dataclass(frozen=True)
class Instrument:
    """One merged instrument and the screeners it appeared in."""

    instrument_id: str
    symbol: str
    exchange_code: str
    currency: str
    security_type: str | None
    kinds: tuple[ScreenerKind, ...]


@dataclass(frozen=True)
class Rejection:
    instrument_id: str
    symbol: str
    cause: Cause
    measured: str
    threshold: str


@dataclass(frozen=True)
class Candidate:
    instrument: Instrument
    quote: Quote
    spread_ratio: Decimal
    session_dollar_volume: Decimal
    session_bars: tuple[Bar, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    started_at: datetime
    screeners_read: tuple[ScreenerKind, ...]
    merged: int
    in_universe: int
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]
    stopped: Stopped | None

    @property
    def evaluated(self) -> int:
        """Universe instruments whose liquidity evaluation finished (accepted or refused)."""
        return len(self.candidates) + sum(
            1 for r in self.rejections if r.cause not in _UNIVERSE_CAUSES)

    def summary(self) -> str:
        """One line for the status screen, e.g. "evaluated 140 of 200: bars budget"."""
        text = f"evaluated {self.evaluated} of {self.in_universe}"
        if len(self.screeners_read) < len(ScreenerKind):
            text += f" (screeners {len(self.screeners_read)} of {len(ScreenerKind)})"
        if self.stopped is not None:
            text += f": {self.stopped.stage} {self.stopped.cause}"
        return text


_UNIVERSE_CAUSES = frozenset({
    Cause.CONFLICTING_ROWS, Cause.EXCHANGE_NOT_ALLOWED, Cause.CURRENCY_NOT_ALLOWED,
    Cause.SECURITY_TYPE_UNKNOWN, Cause.SECURITY_TYPE_NOT_ALLOWED,
})


@dataclass(frozen=True)
class DiscoverySettings:
    allowed_exchanges: frozenset[str]
    allowed_currencies: frozenset[str]
    allowed_security_types: frozenset[str]
    max_spread_ratio: Decimal
    min_session_dollar_volume_usd: Decimal
    max_quote_age: timedelta
    snapshot_symbols_per_request: int
    bars_symbols_per_request: int
    bars_per_symbol: int

    @classmethod
    def from_config(cls, config: Config) -> DiscoverySettings:
        md = config.market_data
        return cls(
            allowed_exchanges=frozenset(md.allowed_exchanges),
            allowed_currencies=frozenset(md.allowed_currencies),
            allowed_security_types=frozenset(md.allowed_security_types),
            max_spread_ratio=md.max_spread_ratio,
            min_session_dollar_volume_usd=md.min_session_dollar_volume_usd,
            max_quote_age=timedelta(seconds=float(config.risk.max_quote_age_seconds)),
            snapshot_symbols_per_request=md.snapshot_symbols_per_request,
            bars_symbols_per_request=md.bars_symbols_per_request,
            bars_per_symbol=md.bars_per_symbol,
        )


class CaptureSink(Protocol):
    def screener(self, rows: Sequence[ScreenerRow], provider: str, receipt_time: datetime) -> None:
        ...

    def bars(self, bars: Sequence[Bar], provider: str, receipt_time: datetime) -> None: ...

    def quotes(self, quotes: Sequence[Quote], provider: str, source: str) -> None: ...


SNAPSHOT_SOURCE = "snapshot"


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def merge(rows: Sequence[ScreenerRow]) -> tuple[list[Instrument], list[Rejection]]:
    """Merge screener rows by instrument_id. Conflicts are refused, never resolved."""
    order: list[str] = []
    grouped: dict[str, list[ScreenerRow]] = {}
    for row in rows:
        if row.instrument_id not in grouped:
            order.append(row.instrument_id)
            grouped[row.instrument_id] = []
        grouped[row.instrument_id].append(row)

    instruments: list[Instrument] = []
    rejections: list[Rejection] = []
    ids_by_symbol: dict[str, set[str]] = {}
    for iid in order:
        for row in grouped[iid]:
            ids_by_symbol.setdefault(row.symbol, set()).add(iid)
    for iid in order:
        group = grouped[iid]
        first = group[0]
        identities = {(r.symbol, r.exchange_code, r.currency, r.security_type) for r in group}
        shared = [r.symbol for r in group if len(ids_by_symbol[r.symbol]) > 1]
        if len(identities) > 1 or shared:
            measured = "; ".join(sorted("/".join(map(str, i)) for i in identities))
            if shared:
                measured += f"; symbol {shared[0]} under several instrument ids"
            rejections.append(Rejection(iid, first.symbol, Cause.CONFLICTING_ROWS, measured,
                                        "one identity per instrument"))
            continue
        kinds = tuple(dict.fromkeys(r.kind for r in group))
        instruments.append(Instrument(iid, first.symbol, first.exchange_code, first.currency,
                                      first.security_type, kinds))
    return instruments, rejections


def universe_cause(instrument: Instrument, settings: DiscoverySettings) -> Rejection | None:
    def reject(cause: Cause, measured: str, allowed: frozenset[str]) -> Rejection:
        return Rejection(instrument.instrument_id, instrument.symbol, cause, measured,
                         ",".join(sorted(allowed)))

    if instrument.exchange_code not in settings.allowed_exchanges:
        return reject(Cause.EXCHANGE_NOT_ALLOWED, instrument.exchange_code,
                      settings.allowed_exchanges)
    if instrument.currency not in settings.allowed_currencies:
        return reject(Cause.CURRENCY_NOT_ALLOWED, instrument.currency,
                      settings.allowed_currencies)
    if instrument.security_type is None:
        return reject(Cause.SECURITY_TYPE_UNKNOWN, "missing", settings.allowed_security_types)
    if instrument.security_type not in settings.allowed_security_types:
        return reject(Cause.SECURITY_TYPE_NOT_ALLOWED, instrument.security_type,
                      settings.allowed_security_types)
    return None


def spread_check(instrument: Instrument, quote: Quote | None, now: datetime,
                 settings: DiscoverySettings) -> tuple[Rejection | None, Decimal]:
    """Refusal (or None) and the measured spread ratio of the snapshot quote."""
    def reject(cause: Cause, measured: str, threshold: str) -> tuple[Rejection, Decimal]:
        return Rejection(instrument.instrument_id, instrument.symbol, cause, measured,
                         threshold), Decimal(0)

    if quote is None:
        return reject(Cause.NO_QUOTE, "none", "one snapshot quote")
    age = now - quote.provider_time
    if age > settings.max_quote_age:
        return reject(Cause.QUOTE_STALE, f"{age.total_seconds()}s",
                      f"<= {settings.max_quote_age.total_seconds()}s")
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return reject(Cause.QUOTE_INVALID, f"bid {quote.bid} ask {quote.ask}",
                      "0 < bid <= ask")
    ratio = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / HALF)
    if ratio > settings.max_spread_ratio:
        return reject(Cause.SPREAD_TOO_WIDE, str(ratio), f"<= {settings.max_spread_ratio}")
    return None, ratio


def completed_session_bars(bars: Sequence[Bar], session_open: datetime,
                           now: datetime) -> tuple[Bar, ...]:
    """Bars of the current regular session whose minute has ended by `now`, oldest first."""
    kept = (b for b in bars if b.start >= session_open and b.start + BAR_LENGTH <= now)
    return tuple(sorted(kept, key=lambda b: b.start))


def session_dollar_volume(bars: Sequence[Bar]) -> Decimal:
    return sum((b.close * b.volume for b in bars), Decimal(0))


def discover(
    source: MarketData,
    settings: DiscoverySettings,
    session_open: datetime,
    clock: Callable[[], datetime],
    capture: CaptureSink | None = None,
) -> DiscoveryResult:
    """Run one discovery pass. `clock` returns the aware current time."""
    started = clock()
    provider = source.provider
    rows: list[ScreenerRow] = []
    read: list[ScreenerKind] = []
    stopped: Stopped | None = None

    for kind in ScreenerKind:
        try:
            got = source.screener(kind)
        except RateLimited as exc:
            stopped = Stopped(Stage.SCREENER, StopCause.BUDGET, str(exc))
            break
        if capture is not None:
            capture.screener(got, provider, clock())
        rows.extend(got)
        read.append(kind)

    instruments, rejections = merge(rows)
    merged = len(instruments) + len(rejections)
    universe: list[Instrument] = []
    for instrument in instruments:
        refusal = universe_cause(instrument, settings)
        if refusal is None:
            universe.append(instrument)
        else:
            rejections.append(refusal)

    def result(candidates: list[Candidate], stop: Stopped | None) -> DiscoveryResult:
        return DiscoveryResult(started, tuple(read), merged, len(universe),
                               tuple(candidates), tuple(rejections), stop)

    if stopped is not None:
        return result([], stopped)

    # Spread, from snapshot quotes.
    by_symbol = {i.symbol: i for i in universe}
    spread_ok: list[tuple[Instrument, Quote, Decimal]] = []
    for chunk in _chunks([i.symbol for i in universe], settings.snapshot_symbols_per_request):
        try:
            quotes = source.snapshot(chunk)
        except RateLimited as exc:
            return result([], Stopped(Stage.SNAPSHOT, StopCause.BUDGET, str(exc)))
        quotes = [q for q in quotes if q.symbol in by_symbol]
        if capture is not None:
            capture.quotes(quotes, provider, SNAPSHOT_SOURCE)
        latest = {q.symbol: q for q in quotes}
        now = clock()
        for symbol in chunk:
            instrument = by_symbol[symbol]
            refusal, ratio = spread_check(instrument, latest.get(symbol), now, settings)
            if refusal is None:
                spread_ok.append((instrument, latest[symbol], ratio))
            else:
                rejections.append(refusal)

    # Session dollar volume, from M1 bars.
    candidates: list[Candidate] = []
    entries = {i.symbol: (i, q, r) for i, q, r in spread_ok}
    for chunk in _chunks([i.symbol for i, _, _ in spread_ok], settings.bars_symbols_per_request):
        try:
            bars = source.bars(chunk, settings.bars_per_symbol)
        except RateLimited as exc:
            return result(candidates, Stopped(Stage.BARS, StopCause.BUDGET, str(exc)))
        except DelayedData as exc:
            return result(candidates, Stopped(Stage.BARS, StopCause.DELAYED_DATA, str(exc)))
        now = clock()
        completed = [b for b in bars if b.symbol in entries and b.start + BAR_LENGTH <= now]
        if capture is not None:
            capture.bars(completed, provider, now)
        for symbol in chunk:
            instrument, quote, ratio = entries[symbol]
            own = [b for b in completed if b.symbol == symbol]
            if not own:
                rejections.append(Rejection(instrument.instrument_id, symbol, Cause.NO_BARS,
                                            "0 bars", "at least one completed M1 bar"))
                continue
            session = completed_session_bars(own, session_open, now)
            volume = session_dollar_volume(session)
            if volume < settings.min_session_dollar_volume_usd:
                rejections.append(Rejection(
                    instrument.instrument_id, symbol, Cause.DOLLAR_VOLUME_TOO_LOW, str(volume),
                    f">= {settings.min_session_dollar_volume_usd}"))
                continue
            candidates.append(Candidate(instrument, quote, ratio, volume, session))

    return result(candidates, None)
