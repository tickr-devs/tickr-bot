"""Bar-by-bar replay of a strategy over completed M1 bars. Every result is SIMULATED.

Timeline per symbol and NY session. Bar `i` is processed in three steps:

1. An open simulated position is managed on bar `i`: a pending strategy EXIT or the flatten
   time closes it at the bar's open; otherwise a bar whose low reaches the stop closes it at
   the stop, or at the open when the bar opens through the stop (gap).
2. A pending entry may fill on bar `i` if the bar trades inside the decision's price window.
   The fill price is always the TOP of the window plus slippage, never better. If the same
   bar also reaches the stop, the stop is assumed hit after the fill (worst case).
3. The strategy is evaluated with the last `history_sessions` sessions plus bars `0..i` of
   this session in `bars` (bar `i` is now complete), the same sessions in `prior_sessions`,
   and, if enabled, a synthetic quote around bar `i`'s close stamped at the bar's end. An
   ENTER becomes a pending entry for the following bars; an EXIT is executed on bar `i + 1`.

The strategy never sees bar `i + 1` before its decision on bar `i`, so it cannot look ahead.
Every exit pays adverse slippage. Each symbol is replayed independently (no portfolio limit),
with one simulated share: R and percent do not depend on quantity.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from tickr.calendar import ny_date_of
from tickr.replay.clock import ClockFor, SessionClock
from tickr.replay.errors import ReplayError, StrategyContractError
from tickr.replay.settings import ReplaySettings
from tickr.strategy.base import Decision, PositionView, Strategy, SymbolContext, Verdict
from tickr.types import Bar, Quote

LABEL = "simulated"
SIMULATED_QUANTITY = Decimal(1)
M1 = timedelta(minutes=1)


class ExitReason(StrEnum):
    STOP = "stop"
    STRATEGY_EXIT = "strategy_exit"
    FLATTEN = "flatten"
    END_OF_DATA = "end_of_data"


class SignalOutcome(StrEnum):
    FILLED = "filled"
    MISSED = "missed"
    REJECTED = "rejected"
    IGNORED = "ignored"


@dataclass(frozen=True)
class SimulatedTrade:
    strategy: str
    version: str
    symbol: str
    session_date: date
    setup: str | None
    signal_bar_start: datetime
    entry_time: datetime
    entry_price: Decimal
    stop: Decimal
    exit_time: datetime
    exit_price: Decimal
    exit_reason: ExitReason
    bars_held: int
    quantity: Decimal | None = None
    """The strategy's `Decision.quantity` (None if it gave none); R and percent ignore it."""
    entry_high: Decimal | None = None
    """Top of the decision's price window (the live limit price, before slippage)."""

    @property
    def pnl_usd(self) -> Decimal | None:
        """(exit - entry) x the strategy's quantity, gross of commissions and fees."""
        if self.quantity is None:
            return None
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def risk_per_share(self) -> Decimal:
        return self.entry_price - self.stop

    @property
    def r_multiple(self) -> Decimal:
        return (self.exit_price - self.entry_price) / self.risk_per_share

    @property
    def return_percent(self) -> Decimal:
        return (self.exit_price - self.entry_price) / self.entry_price * 100


@dataclass(frozen=True)
class Signal:
    """One ENTER decision and what the simulation did with it."""

    strategy: str
    version: str
    symbol: str
    session_date: date
    bar_start: datetime
    setup: str | None
    entry_low: Decimal | None
    entry_high: Decimal | None
    stop: Decimal | None
    outcome: SignalOutcome
    cause: str


@dataclass(frozen=True)
class ReplayResult:
    strategy: str
    version: str
    trades: tuple[SimulatedTrade, ...]
    signals: tuple[Signal, ...]
    verdict_counts: Mapping[str, int]
    sessions: int
    bars: int


@dataclass
class _Pending:
    decision: Decision
    bar_start: datetime
    bars_left: int


@dataclass
class _Open:
    decision: Decision
    signal_bar_start: datetime
    entry_time: datetime
    entry_price: Decimal
    entry_index: int
    exit_requested: bool = False


def _check_decision(strategy: Strategy, symbol: str, decision: Decision) -> None:
    if not isinstance(decision, Decision):
        raise StrategyContractError(f"evaluate returned {type(decision).__name__}, not Decision")
    if not isinstance(decision.verdict, Verdict):
        raise StrategyContractError(f"verdict {decision.verdict!r} is not a Verdict")
    if (decision.strategy, decision.version) != (strategy.name, strategy.version):
        raise StrategyContractError(
            f"decision tagged {decision.strategy} v{decision.version}, "
            f"strategy is {strategy.name} v{strategy.version}"
        )
    if decision.symbol != symbol:
        raise StrategyContractError(f"decision for {decision.symbol} while replaying {symbol}")


def _entry_problem(decision: Decision) -> str | None:
    """Why an ENTER decision cannot be simulated, or None."""
    if decision.rest_bars is not None:
        # A resting limit below the ask fills only on a pullback; simulating it as a
        # marketable entry would book fills the order could not get.
        return "resting limit entries (rest_bars) are not modelled"
    if decision.scale_out is not None:
        # Replay closes the whole simulated position on any EXIT; a partial sale would be
        # booked as a full one.
        return "scale-out (partial exits) is not modelled"
    if decision.entry_high is None:
        return "ENTER without entry_high"
    if decision.stop is None:
        return "ENTER without stop"
    if decision.entry_low is not None and decision.entry_low > decision.entry_high:
        return "entry_low above entry_high"
    if decision.stop >= decision.entry_high:
        return "stop not below entry_high"
    if decision.stop <= 0:
        return "stop not positive"
    return None


class _SessionReplay:
    def __init__(
        self,
        strategy: Strategy,
        symbol: str,
        day: date,
        bars: Sequence[Bar],
        prior: tuple[tuple[Bar, ...], ...],
        settings: ReplaySettings,
        clock: SessionClock,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.day = day
        self.bars = tuple(bars)
        self.prior = prior
        self.history = tuple(b for session in prior for b in session)
        self.settings = settings
        self.session_clock = clock
        self.pending: _Pending | None = None
        self.open: _Open | None = None
        self.trades: list[SimulatedTrade] = []
        self.signals: list[Signal] = []
        self.verdicts: dict[str, int] = {}

    def _signal(
        self, decision: Decision, bar_start: datetime, outcome: SignalOutcome, cause: str
    ) -> None:
        self.signals.append(
            Signal(
                strategy=decision.strategy,
                version=decision.version,
                symbol=self.symbol,
                session_date=self.day,
                bar_start=bar_start,
                setup=decision.setup,
                entry_low=decision.entry_low,
                entry_high=decision.entry_high,
                stop=decision.stop,
                outcome=outcome,
                cause=cause,
            )
        )

    def _close(self, index: int, raw_price: Decimal, at: datetime, reason: ExitReason) -> None:
        position = self.open
        assert position is not None and position.decision.stop is not None
        price = raw_price - self.settings.slippage(raw_price)
        if price <= 0:
            raise ReplayError(
                f"{self.symbol} {at.isoformat()}: simulated exit price {price} is not positive"
            )
        self.trades.append(
            SimulatedTrade(
                strategy=position.decision.strategy,
                version=position.decision.version,
                symbol=self.symbol,
                session_date=self.day,
                setup=position.decision.setup,
                signal_bar_start=position.signal_bar_start,
                entry_time=position.entry_time,
                entry_price=position.entry_price,
                stop=position.decision.stop,
                exit_time=at,
                exit_price=price,
                exit_reason=reason,
                bars_held=index - position.entry_index + 1,
                quantity=position.decision.quantity,
                entry_high=position.decision.entry_high,
            )
        )
        self.open = None

    def _manage_position(self, index: int, bar: Bar) -> None:
        position = self.open
        if position is None:
            return
        stop = position.decision.stop
        assert stop is not None
        if position.exit_requested:
            self._close(index, bar.open, bar.start, ExitReason.STRATEGY_EXIT)
        elif bar.start >= self.session_clock.flatten:
            self._close(index, bar.open, bar.start, ExitReason.FLATTEN)
        elif bar.low <= stop:
            self._close(index, min(bar.open, stop), bar.start, ExitReason.STOP)

    def _try_fill(self, index: int, bar: Bar) -> None:
        pending = self.pending
        if pending is None:
            return
        decision = pending.decision
        top = decision.entry_high
        stop = decision.stop
        assert top is not None and stop is not None
        if bar.start >= self.session_clock.last_entry:
            self.pending = None
            self._signal(decision, pending.bar_start, SignalOutcome.MISSED, "last entry time")
            return
        bottom = decision.entry_low if decision.entry_low is not None else bar.low
        if bar.low <= top and bar.high >= bottom:
            self.pending = None
            self.open = _Open(
                decision=decision,
                signal_bar_start=pending.bar_start,
                entry_time=bar.start,
                entry_price=top + self.settings.slippage(top),
                entry_index=index,
            )
            self._signal(decision, pending.bar_start, SignalOutcome.FILLED, "")
            if bar.low <= stop:
                self._close(index, stop, bar.start, ExitReason.STOP)
            return
        pending.bars_left -= 1
        if pending.bars_left == 0:
            self.pending = None
            self._signal(decision, pending.bar_start, SignalOutcome.MISSED, "price outside window")

    def _quote(self, bar: Bar) -> Quote | None:
        """Synthetic quote at the end of the completed bar, or None when disabled."""
        settings = self.settings
        if not settings.synthesize_quote:
            return None
        half = settings.synthetic_spread(bar.close) / 2
        bid, ask = bar.close - half, bar.close + half
        if bid <= 0:
            raise ReplayError(
                f"{self.symbol} {bar.start.isoformat()}: synthetic bid {bid} is not positive"
            )
        end = bar.start + M1
        size = settings.synthetic_quote_size_shares
        return Quote(self.symbol, bid, ask, size, size, end, end)

    def _evaluate(self, index: int, bar: Bar) -> None:
        view = None
        if self.open is not None:
            assert self.open.decision.stop is not None
            view = PositionView(
                entry_price=self.open.entry_price,
                quantity=SIMULATED_QUANTITY,
                stop=self.open.decision.stop,
            )
        context = SymbolContext(
            symbol=self.symbol,
            bars=self.history + self.bars[: index + 1],
            prior_sessions=self.prior,
            quote=self._quote(bar),
            position=view,
            # Replay input is the recorded session itself: its bars are the whole session.
            session_open=self.session_clock.regular_open,
        )
        decision = self.strategy.evaluate(context)
        _check_decision(self.strategy, self.symbol, decision)
        self.verdicts[decision.verdict.value] = self.verdicts.get(decision.verdict.value, 0) + 1
        if decision.verdict is Verdict.EXIT and self.open is not None:
            self.open.exit_requested = True
        elif decision.verdict is Verdict.ENTER:
            self._enter(decision, bar)

    def _enter(self, decision: Decision, bar: Bar) -> None:
        if self.open is not None:
            self._signal(decision, bar.start, SignalOutcome.IGNORED, "position open")
            return
        if self.pending is not None:
            self._signal(decision, bar.start, SignalOutcome.IGNORED, "entry pending")
            return
        refused = self.session_clock.entries_refused
        if refused is not None:
            self._signal(decision, bar.start, SignalOutcome.REJECTED, refused)
            return
        if bar.start < self.session_clock.regular_open:
            self._signal(decision, bar.start, SignalOutcome.REJECTED, "before regular open")
            return
        if bar.start + M1 >= self.session_clock.last_entry:
            self._signal(decision, bar.start, SignalOutcome.REJECTED, "after last entry time")
            return
        problem = _entry_problem(decision)
        if problem is not None:
            self._signal(decision, bar.start, SignalOutcome.REJECTED, problem)
            return
        self.pending = _Pending(decision, bar.start, self.settings.entry_wait_bars)

    def run(self) -> None:
        for index, bar in enumerate(self.bars):
            self._manage_position(index, bar)
            self._try_fill(index, bar)
            self._evaluate(index, bar)
        last = len(self.bars) - 1
        if self.open is not None:
            self._close(last, self.bars[last].close, self.bars[last].start, ExitReason.END_OF_DATA)
        if self.pending is not None:
            pending = self.pending
            self.pending = None
            self._signal(
                pending.decision, pending.bar_start, SignalOutcome.MISSED, "no further bars"
            )


def split_sessions(bars: Sequence[Bar]) -> list[tuple[date, tuple[Bar, ...]]]:
    """Group one symbol's bars (oldest first) by New York session date."""
    sessions: list[tuple[date, list[Bar]]] = []
    for bar in bars:
        day = ny_date_of(bar.start)
        if not sessions or sessions[-1][0] != day:
            sessions.append((day, []))
        sessions[-1][1].append(bar)
    return [(day, tuple(day_bars)) for day, day_bars in sessions]


def replay(
    create_strategy: Callable[[], Strategy],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    settings: ReplaySettings,
    clock_for: ClockFor,
) -> ReplayResult:
    """Replay every symbol through a fresh strategy instance; `clock_for` gives each day's clock."""
    trades: list[SimulatedTrade] = []
    signals: list[Signal] = []
    verdicts: dict[str, int] = {}
    session_count = 0
    bar_count = 0
    name = version = None
    for symbol in sorted(bars_by_symbol):
        strategy = create_strategy()
        if name is None:
            name, version = strategy.name, strategy.version
        elif (strategy.name, strategy.version) != (name, version):
            raise StrategyContractError("strategy factory returned different name/version")
        prior: list[tuple[Bar, ...]] = []
        for day, day_bars in split_sessions(bars_by_symbol[symbol]):
            session = _SessionReplay(
                strategy, symbol, day, day_bars, tuple(prior), settings, clock_for(day)
            )
            session.run()
            trades.extend(session.trades)
            signals.extend(session.signals)
            for verdict, count in session.verdicts.items():
                verdicts[verdict] = verdicts.get(verdict, 0) + count
            prior.append(day_bars)
            if len(prior) > settings.history_sessions:
                prior.pop(0)
            session_count += 1
            bar_count += len(day_bars)
    if name is None or version is None:
        raise ReplayError("no bars to replay")
    return ReplayResult(
        strategy=name,
        version=version,
        trades=tuple(trades),
        signals=tuple(signals),
        verdict_counts=dict(sorted(verdicts.items())),
        sessions=session_count,
        bars=bar_count,
    )
