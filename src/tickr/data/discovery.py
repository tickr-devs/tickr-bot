"""Discovery: screeners -> merged instruments -> universe -> quote -> bars -> candidates.

One pass (spec section 6):
1. Read gainers, losers and top actives; merge rows by `instrument_id` (first-seen order).
   Rows of one instrument that disagree on symbol, exchange or currency, or one symbol under two
   instrument ids, are refused as conflicting, never resolved by picking one.
2. Universe: exchange, currency and security type must be in the configured lists. A row without
   a security type is refused (`SECURITY_TYPE_UNKNOWN`). Webull screener rows carry no type; the
   provider fills it from its instrument profile (no symbol lists, owner 2026-11-14).
3. Quote: one snapshot quote per symbol, fresh (provider time within `max_quote_age` and not
   ahead of our clock by more than `max_clock_skew`), bid and ask positive and not crossed.
   Then the active strategy's `quote_liquidity` rules (the spread).
4. Bars: M1 bars, at least one completed. Discovery shares the bars route with the evaluation of
   positions, WATCH symbols and candidates (and their history pages), so every discovery bars
   read leaves `[market_data] discovery_bars_reserve_requests` of the route's window free: when
   only those are left the pass stops visibly ("bars budget") and the symbols not reached wait
   for the next pass; discovery never starves the trading path. Then the strategy's
   `session_liquidity` rules on the completed minutes of the current regular session (bar
   start >= session open, bar end <= now), such as the session dollar volume.

The platform holds no liquidity threshold (owner 2026-11-15): spread, dollar volume and any
other liquidity rule are the strategy's, refused here as `STRATEGY_LIQUIDITY` with the
strategy's failed check, so fewer bar reads, history reads and stream slots go to symbols the
strategy would refuse anyway.

Every refusal has one cause and the measured value. When the request budget refuses a call
(`RateLimited`) or the provider reports delayed data, the pass stops at that point and the result
says so ("evaluated 140 of 200: bars budget"); nothing is cut silently.

When a snapshot or bars read is refused whole because one requested symbol is unknown to the
route (`SymbolsRefused`; Webull names it only in free text), the chunk is split in halves until
each refused symbol is alone; those are refused as `SYMBOL_REFUSED` and the rest is read as
usual. A refused symbol is remembered (`RefusedSymbols`) and refused again without a request
for `[market_data] refused_symbol_recheck_seconds`, then asked again (a symbol the provider
did not know yet, such as a new listing, comes back); the memory is also cleared when the next
session starts. So the split costs requests once per symbol per recheck period, not every pass.

Every screener row, snapshot quote and completed bar read is handed to the capture sink with the
receipt time taken right after the call returned.

Each pass also measures the active strategy's fast movers (when the strategy defines them,
`FastMovers`) on the session bars it already read, and the result keeps every instrument's
outcome: candidate, refused with its cause, or not reached when the pass stopped early.
`pass_record` turns a result into the line the bot appends to the day's `discovery.jsonl`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from tickr.broker.base import RateLimited
from tickr.config import Config
from tickr.data.base import DelayedData, MarketData, ScreenerKind, ScreenerRow, SymbolsRefused
from tickr.data.movers import Mover, Signal
from tickr.strategy.base import Check, FastMovers, Liquidity
from tickr.types import Bar, Quote

BAR_LENGTH = timedelta(minutes=1)


class Cause(StrEnum):
    """The single recorded cause of a discovery refusal for one instrument."""

    CONFLICTING_ROWS = "CONFLICTING_ROWS"
    EXCHANGE_NOT_ALLOWED = "EXCHANGE_NOT_ALLOWED"
    CURRENCY_NOT_ALLOWED = "CURRENCY_NOT_ALLOWED"
    SECURITY_TYPE_UNKNOWN = "SECURITY_TYPE_UNKNOWN"
    SECURITY_TYPE_NOT_ALLOWED = "SECURITY_TYPE_NOT_ALLOWED"
    NO_QUOTE = "NO_QUOTE"
    QUOTE_STALE = "QUOTE_STALE"
    QUOTE_FROM_FUTURE = "QUOTE_FROM_FUTURE"
    QUOTE_INVALID = "QUOTE_INVALID"
    NO_BARS = "NO_BARS"
    SYMBOL_REFUSED = "SYMBOL_REFUSED"
    STRATEGY_LIQUIDITY = "STRATEGY_LIQUIDITY"


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
    stage: Stage
    """Where the pass refused it: `screener` (merge and universe), `snapshot` or `bars`."""
    check: Check | None = None
    """The strategy's failed check, for `STRATEGY_LIQUIDITY`; None for a platform cause."""


@dataclass(frozen=True)
class Candidate:
    instrument: Instrument
    quote: Quote
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
    instruments: tuple[Instrument, ...] = ()
    """Every merged instrument without conflicting rows, with the screeners that listed it."""
    not_reached: tuple[str, ...] = ()
    """Universe symbols the pass stopped before deciding (`stopped` says where and why)."""
    movers: tuple[Mover, ...] = ()
    """The active strategy's fast movers among the symbols whose bars were read."""
    movers_measured: bool = False
    """Whether the active strategy defines fast movers (`FastMovers`); without it `movers` is
    empty because nothing was measured, not because nothing moved."""

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
    max_quote_age: timedelta
    max_clock_skew: timedelta
    snapshot_symbols_per_request: int
    bars_symbols_per_request: int
    bars_per_symbol: int
    refused_symbol_recheck: timedelta  # a symbol refused as unknown is asked again after this
    bars_budget_reserve: int

    @classmethod
    def from_config(cls, config: Config) -> DiscoverySettings:
        md = config.market_data
        return cls(
            allowed_exchanges=frozenset(md.allowed_exchanges),
            allowed_currencies=frozenset(md.allowed_currencies),
            allowed_security_types=frozenset(md.allowed_security_types),
            max_quote_age=timedelta(seconds=float(config.risk.max_quote_age_seconds)),
            max_clock_skew=timedelta(seconds=float(config.risk.max_clock_skew_seconds)),
            snapshot_symbols_per_request=md.snapshot_symbols_per_request,
            bars_symbols_per_request=md.bars_symbols_per_request,
            bars_per_symbol=md.bars_per_symbol,
            refused_symbol_recheck=timedelta(seconds=float(md.refused_symbol_recheck_seconds)),
            bars_budget_reserve=md.discovery_bars_reserve_requests,
        )


class CaptureSink(Protocol):
    def screener(self, rows: Sequence[ScreenerRow], provider: str, receipt_time: datetime) -> None:
        ...

    def bars(self, bars: Sequence[Bar], provider: str, receipt_time: datetime) -> None: ...

    def quotes(self, quotes: Sequence[Quote], provider: str, source: str) -> None: ...


SNAPSHOT_SOURCE = "snapshot"


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _read_without_refused[T](read: Callable[[Sequence[str]], list[T]], symbols: Sequence[str],
                             found: Callable[[str], None]) -> list[T]:
    """`read(symbols)`; when the route refuses the request for an unknown symbol, the chunk is
    split in halves until each refused symbol is alone, and `found(symbol)` is called for it at
    once, so a split the budget interrupts still keeps what it isolated. Returns the rows read.
    Every split is a new request; RateLimited and DelayedData propagate."""
    try:
        return read(symbols)
    except SymbolsRefused:
        if len(symbols) == 1:
            found(symbols[0])
            return []
    middle = len(symbols) // 2
    left = _read_without_refused(read, symbols[:middle], found)
    return left + _read_without_refused(read, symbols[middle:], found)


class RefusedSymbols:
    """Symbols a market-data route refused as unknown, with the stage that refused them and
    when. A refusal is forgotten `recheck` after it was recorded, so the symbol is asked again,
    and every refusal is forgotten when the next regular session (keyed by its open) starts."""

    def __init__(self) -> None:
        self._session: datetime | None = None
        self._refused: dict[str, tuple[Stage, datetime]] = {}

    def active(self, session_open: datetime, now: datetime,
               recheck: timedelta) -> dict[str, Stage]:
        """The symbols still refused at `now` (expired ones are forgotten), by stage."""
        if self._session != session_open:
            self._session, self._refused = session_open, {}
        self._refused = {symbol: (stage, at) for symbol, (stage, at) in self._refused.items()
                         if now < at + recheck}
        return {symbol: stage for symbol, (stage, _) in self._refused.items()}

    def remember(self, symbol: str, stage: Stage, now: datetime) -> None:
        self._refused[symbol] = (stage, now)


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
                                        "one identity per instrument", Stage.SCREENER))
            continue
        kinds = tuple(dict.fromkeys(r.kind for r in group))
        instruments.append(Instrument(iid, first.symbol, first.exchange_code, first.currency,
                                      first.security_type, kinds))
    return instruments, rejections


def universe_cause(instrument: Instrument, settings: DiscoverySettings) -> Rejection | None:
    def reject(cause: Cause, measured: str, allowed: frozenset[str]) -> Rejection:
        return Rejection(instrument.instrument_id, instrument.symbol, cause, measured,
                         ",".join(sorted(allowed)), Stage.SCREENER)

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


class NoStrategy:
    """Liquidity rules that refuse every symbol, for when the active strategy did not load:
    discovery still reads and captures screeners and snapshot quotes (replay input), but no
    symbol becomes a candidate, so nothing is subscribed or evaluated for it."""

    name, version = "none", "-"
    REFUSED = (Check("strategy_loaded", None, "is", None, False, "no active strategy"),)

    def quote_liquidity(self, quote: Quote) -> tuple[Check, ...]:
        return self.REFUSED

    def session_liquidity(self, session_bars: Sequence[Bar]) -> tuple[Check, ...]:
        return self.REFUSED


def strategy_refusal(instrument: Instrument, checks: Sequence[Check],
                     screen: Liquidity, stage: Stage) -> Rejection | None:
    """The first failed check of the strategy's liquidity rules as a refusal, or None."""
    failed = next((c for c in checks if not c.passed), None)
    if failed is None:
        return None
    return Rejection(instrument.instrument_id, instrument.symbol, Cause.STRATEGY_LIQUIDITY,
                     f"{screen.name} {screen.version} {failed.rule} {failed.measured}",
                     f"{failed.operator} {failed.threshold}", stage, failed)


def quote_check(instrument: Instrument, quote: Quote | None, now: datetime,
                settings: DiscoverySettings, screen: Liquidity) -> Rejection | None:
    """Refusal of the snapshot quote (platform validity, then the strategy's rules), or None."""
    def reject(cause: Cause, measured: str, threshold: str) -> Rejection:
        return Rejection(instrument.instrument_id, instrument.symbol, cause, measured, threshold,
                         Stage.SNAPSHOT)

    if quote is None:
        return reject(Cause.NO_QUOTE, "none", "one snapshot quote")
    age = now - quote.provider_time
    if -age > settings.max_clock_skew:
        return reject(Cause.QUOTE_FROM_FUTURE, f"{(-age).total_seconds()}s ahead",
                      f"<= {settings.max_clock_skew.total_seconds()}s")
    if age > settings.max_quote_age:
        return reject(Cause.QUOTE_STALE, f"{age.total_seconds()}s",
                      f"<= {settings.max_quote_age.total_seconds()}s")
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return reject(Cause.QUOTE_INVALID, f"bid {quote.bid} ask {quote.ask}",
                      "0 < bid <= ask")
    return strategy_refusal(instrument, screen.quote_liquidity(quote), screen, Stage.SNAPSHOT)


def completed_session_bars(bars: Sequence[Bar], session_open: datetime,
                           now: datetime) -> tuple[Bar, ...]:
    """Bars of the current regular session whose minute has ended by `now`, oldest first."""
    kept = (b for b in bars if b.start >= session_open and b.start + BAR_LENGTH <= now)
    return tuple(sorted(kept, key=lambda b: b.start))


def discover(
    source: MarketData,
    settings: DiscoverySettings,
    screen: Liquidity,
    session_open: datetime,
    clock: Callable[[], datetime],
    capture: CaptureSink | None = None,
    refused_symbols: RefusedSymbols | None = None,
) -> DiscoveryResult:
    """Run one discovery pass with `screen` (the active strategy) as the liquidity rules.
    `clock` returns the aware current time. `refused_symbols` carries the symbols refused as
    unknown from pass to pass within the session (a fresh memory when None)."""
    memory = refused_symbols if refused_symbols is not None else RefusedSymbols()
    refused = memory.active(session_open, clock(), settings.refused_symbol_recheck)

    def refuse(instrument: Instrument, stage: Stage, earlier: bool) -> Rejection:
        when = "earlier this session" if earlier else "this pass"
        return Rejection(instrument.instrument_id, instrument.symbol, Cause.SYMBOL_REFUSED,
                         f"{stage} read refused the symbol as unknown {when}",
                         "a symbol the market-data routes know", stage)
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

    movers: list[Mover] = []

    def result(candidates: list[Candidate], stop: Stopped | None) -> DiscoveryResult:
        decided = {r.symbol for r in rejections} | {c.instrument.symbol for c in candidates}
        not_reached = tuple(i.symbol for i in universe if i.symbol not in decided)
        return DiscoveryResult(started, tuple(read), merged, len(universe),
                               tuple(candidates), tuple(rejections), stop, tuple(instruments),
                               not_reached, tuple(movers), isinstance(screen, FastMovers))

    if stopped is not None:
        return result([], stopped)

    # Quote validity and the strategy's quote rules, from snapshot quotes.
    by_symbol = {i.symbol: i for i in universe}
    asked: list[str] = []
    for instrument in universe:
        if instrument.symbol in refused:
            rejections.append(refuse(instrument, refused[instrument.symbol], earlier=True))
        else:
            asked.append(instrument.symbol)
    quote_ok: list[tuple[Instrument, Quote]] = []
    def refused_by(stage: Stage, unknown: list[str]) -> Callable[[str], None]:
        def found(symbol: str) -> None:  # remembered at once: survives a budget stop
            memory.remember(symbol, stage, clock())
            unknown.append(symbol)
            rejections.append(refuse(by_symbol[symbol], stage, earlier=False))
        return found

    for chunk in _chunks(asked, settings.snapshot_symbols_per_request):
        unknown: list[str] = []
        try:
            quotes = _read_without_refused(source.snapshot, chunk,
                                           refused_by(Stage.SNAPSHOT, unknown))
        except RateLimited as exc:
            return result([], Stopped(Stage.SNAPSHOT, StopCause.BUDGET, str(exc)))
        chunk = [s for s in chunk if s not in unknown]
        quotes = [q for q in quotes if q.symbol in by_symbol]
        if capture is not None:
            capture.quotes(quotes, provider, SNAPSHOT_SOURCE)
        latest = {q.symbol: q for q in quotes}
        now = clock()
        for symbol in chunk:
            instrument = by_symbol[symbol]
            refusal = quote_check(instrument, latest.get(symbol), now, settings, screen)
            if refusal is None:
                quote_ok.append((instrument, latest[symbol]))
            else:
                rejections.append(refusal)

    # The strategy's session rules, from M1 bars.
    candidates: list[Candidate] = []
    entries = {i.symbol: (i, q) for i, q in quote_ok}
    for chunk in _chunks([i.symbol for i, _ in quote_ok], settings.bars_symbols_per_request):
        unknown = []
        try:
            bars = _read_without_refused(
                lambda symbols: source.bars(symbols, settings.bars_per_symbol,
                                            budget_reserve=settings.bars_budget_reserve), chunk,
                refused_by(Stage.BARS, unknown))
        except RateLimited as exc:
            return result(candidates, Stopped(Stage.BARS, StopCause.BUDGET, str(exc)))
        except DelayedData as exc:
            return result(candidates, Stopped(Stage.BARS, StopCause.DELAYED_DATA, str(exc)))
        chunk = [s for s in chunk if s not in unknown]
        now = clock()
        completed = [b for b in bars if b.symbol in entries and b.start + BAR_LENGTH <= now]
        if capture is not None:
            capture.bars(completed, provider, now)
        for symbol in chunk:
            instrument, quote = entries[symbol]
            own = [b for b in completed if b.symbol == symbol]
            if not own:
                rejections.append(Rejection(instrument.instrument_id, symbol, Cause.NO_BARS,
                                            "0 bars", "at least one completed M1 bar",
                                            Stage.BARS))
                continue
            session = completed_session_bars(own, session_open, now)
            if not session:
                rejections.append(Rejection(instrument.instrument_id, symbol, Cause.NO_BARS,
                                            "0 session bars",
                                            "at least one completed bar of this session",
                                            Stage.BARS))
                continue
            if isinstance(screen, FastMovers):
                mover = screen.fast_mover(symbol, session)
                if mover is not None:
                    movers.append(mover)
            refusal = strategy_refusal(instrument, screen.session_liquidity(session), screen,
                                       Stage.BARS)
            if refusal is not None:
                rejections.append(refusal)
                continue
            candidates.append(Candidate(instrument, quote, session))

    return result(candidates, None)


def _check_record(check: Check | None) -> dict[str, Any] | None:
    if check is None:
        return None
    return {"rule": check.rule, "measured": _text(check.measured), "operator": check.operator,
            "threshold": _text(check.threshold), "passed": check.passed, "note": check.note}


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _signal(signal: Signal) -> dict[str, Any]:
    return {"value": str(signal.value), "threshold": str(signal.threshold),
            "fired": signal.fired}


def pass_record(result: DiscoveryResult, finished_at: datetime) -> dict[str, Any]:
    """One discovery pass as a JSON object (Decimal values as strings, times ISO-8601 UTC):
    the screeners read, where it stopped, every instrument's sources, every refusal with its
    cause, measured value, threshold and the strategy's failed check, the candidates, the
    symbols not reached, and the fast movers with their signals."""
    def at(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    stopped = result.stopped
    return {
        "started_at": at(result.started_at),
        "finished_at": at(finished_at),
        "screeners_read": [k.value for k in result.screeners_read],
        "stopped": None if stopped is None else {
            "stage": stopped.stage.value, "cause": stopped.cause.value,
            "detail": stopped.detail},
        "instruments": [{"symbol": i.symbol, "instrument_id": i.instrument_id,
                         "kinds": [k.value for k in i.kinds]} for i in result.instruments],
        "rejections": [{"symbol": r.symbol, "instrument_id": r.instrument_id,
                        "cause": r.cause.value, "stage": r.stage.value, "measured": r.measured,
                        "threshold": r.threshold, "check": _check_record(r.check)}
                       for r in result.rejections],
        "candidates": [c.instrument.symbol for c in result.candidates],
        "not_reached": list(result.not_reached),
        "movers_measured": result.movers_measured,
        "movers": [{"symbol": m.symbol, "bar_start": at(m.bar_start), "close": str(m.close),
                    "low": str(m.low), "bar_dollar_volume": str(m.bar_dollar_volume),
                    "volume_ratio": _signal(m.volume_ratio), "move_atr": _signal(m.move_atr),
                    "dollar_volume_ratio": _signal(m.dollar_volume_ratio),
                    "spike_up": m.spike_up} for m in result.movers],
    }

