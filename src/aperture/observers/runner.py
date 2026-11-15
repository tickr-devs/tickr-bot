"""The engine hook: evaluate every observer on the context the active strategy just saw.

`ObserverSet.on_evaluation(context, at)` is called once per active evaluation, after it, with
the same SymbolContext and the decision time. It never evaluates anything on the caller's
thread: it freezes the context (bars as tuples, position removed: observers never hold one)
and puts it on a bounded queue, without blocking. One worker thread evaluates the observers in
order and appends each decision to the decision log with role `observer` (admission
`not_evaluated` for ENTER). So a slow or hung observer can never delay Detail polls, stops or
exits on the trading thread.

An observer is never allowed to hurt trading:
- queue full: that evaluation is dropped and recorded as an ObserverFailure (returned to the
  caller at once); nothing waits;
- on the worker, any exception from evaluate(), or a decision that breaks the contract (not a
  Decision, wrong name/version tag, wrong symbol), switches that observer off for the run;
- an evaluation slower than `[observers] max_evaluation_seconds` is still recorded, then the
  observer is switched off (a hung one keeps the worker busy; the queue then overflows and
  every further evaluation is dropped and recorded, still without blocking);
- a failed decision log write switches every observer off.
Worker failures are collected; the caller takes them with `take_failures()` (for example on
each engine step) and journals and alerts. Each failure has one cause; nothing is retried.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from aperture.observers.loader import LoadedStrategy
from aperture.observers.log import DecisionLog
from aperture.research.records import M1, Admission, DecisionRecord
from aperture.strategy.base import Decision, Role, SymbolContext, Verdict
from aperture.types import Quote

ALL_OBSERVERS = "all observers"
_STOP = object()
# A WATCH counts while its bar started within this many minutes: the completed bar (1), the
# one before it while the worker is mid-bar (2), plus one for the pass timing. Structural.
WATCHING_BARS = 3


@dataclass(frozen=True)
class ObserverFailure:
    folder: str
    identity: str
    at: datetime
    cause: str


def _contract_problem(observer: LoadedStrategy, symbol: str, decision: object) -> str | None:
    if not isinstance(decision, Decision):
        return f"evaluate returned {type(decision).__name__}, not Decision"
    if not isinstance(decision.verdict, Verdict):
        return f"verdict {decision.verdict!r} is not a Verdict"
    strategy = observer.strategy
    if (decision.strategy, decision.version) != (strategy.name, strategy.version):
        return f"decision tagged {decision.strategy} v{decision.version}"
    if decision.symbol != symbol:
        return f"decision for {decision.symbol} while evaluating {symbol}"
    return None


def _usable_quote(quote: Quote | None, at: datetime, max_age: timedelta,
                  max_skew: timedelta) -> Quote | None:
    """The quote when entry admission would accept its times at `at`: aware provider and
    receipt times, received no later than `at`, provider time at most `max_skew` ahead and at
    most `max_age` old. Otherwise None."""
    if quote is None:
        return None
    times = (quote.provider_time, quote.receipt_time)
    if any(t.tzinfo is None or t.utcoffset() is None for t in times):
        return None
    if quote.receipt_time > at:
        return None
    age = at - quote.provider_time
    return quote if -max_skew <= age <= max_age else None


def freeze(context: SymbolContext, at: datetime, max_quote_age: timedelta,
           max_clock_skew: timedelta) -> SymbolContext:
    """An immutable copy for the worker: tuples of bars, no position, no daily loss budget
    (that room is the active strategy's; an observer sizes as if it traded alone), and no
    quote whose times entry admission would refuse at `at` (stale, from the future, naive)."""
    quote = _usable_quote(context.quote, at, max_quote_age, max_clock_skew)
    return replace(
        context,
        bars=tuple(context.bars),
        prior_sessions=tuple(tuple(session) for session in context.prior_sessions),
        position=None,
        loss_budget_usd=None,
        quote=quote,
    )


class ObserverSet:
    def __init__(
        self,
        observers: Sequence[LoadedStrategy],
        log: DecisionLog,
        max_evaluation_seconds: Decimal,
        queue_size: int,
        max_quote_age_seconds: Decimal,
        max_clock_skew_seconds: Decimal,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self.log = log
        self.max_seconds = float(max_evaluation_seconds)
        self.max_quote_age = timedelta(seconds=float(max_quote_age_seconds))
        self.max_clock_skew = timedelta(seconds=float(max_clock_skew_seconds))
        self.monotonic = monotonic
        self._enabled = list(observers)
        self._lock = threading.Lock()
        self._failures: list[ObserverFailure] = []
        # observer folder -> symbol -> bar of its latest WATCH/ENTER
        self._watching: dict[str, dict[str, datetime]] = {}
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_size)
        self._idle = threading.Condition()
        self._pending = 0
        self._worker: threading.Thread | None = None
        if self._enabled:
            self._worker = threading.Thread(target=self._run, name="observers", daemon=True)
            self._worker.start()

    @property
    def enabled(self) -> tuple[LoadedStrategy, ...]:
        with self._lock:
            return tuple(self._enabled)

    # ----------------------------------------------------------- caller (trading) thread

    def on_evaluation(self, context: SymbolContext, at: datetime) -> tuple[ObserverFailure, ...]:
        """Queue one evaluation for every enabled observer; never blocks, never evaluates.

        Returns a failure only when the queue is full (the evaluation is dropped). `at` before
        the end of the last bar is a caller bug (ValueError)."""
        if not context.bars:
            raise ValueError("observers need at least one completed bar")
        if at < context.bars[-1].start + M1:
            raise ValueError(f"decision time {at.isoformat()} is before the bar completed")
        if self._worker is None or not self.enabled:
            return ()
        frozen = freeze(context, at, self.max_quote_age, self.max_clock_skew)
        with self._idle:
            try:
                self._queue.put_nowait((frozen, at))
            except queue.Full:
                failure = ObserverFailure(
                    ALL_OBSERVERS, ALL_OBSERVERS, at,
                    f"queue full ({self._queue.maxsize}): evaluation of {context.symbol} dropped",
                )
                with self._lock:
                    self._failures.append(failure)
                return (failure,)
            self._pending += 1
        return ()

    def take_failures(self) -> tuple[ObserverFailure, ...]:
        """Failures recorded since the last call (worker and overflow), oldest first."""
        with self._lock:
            taken = tuple(self._failures)
            self._failures.clear()
        return taken

    def watching(self, now: datetime) -> frozenset[str]:
        """Symbols whose latest decision by an enabled observer was WATCH or ENTER on a bar
        that started at most WATCHING_BARS minutes before `now` (the latest completed bar or
        the one before, while the worker is mid-bar): the observers' own quote wishes, so the
        platform can subscribe them below the active strategy's. A symbol no longer evaluated,
        or every symbol once evaluation stops (after the close), drops out."""
        since = now - WATCHING_BARS * M1
        with self._lock:
            enabled = {o.folder for o in self._enabled}
            return frozenset(s for folder, symbols in self._watching.items()
                             if folder in enabled
                             for s, bar in symbols.items() if bar >= since)

    def wait_idle(self, timeout: float) -> bool:
        """True once every queued evaluation is done (tests, orderly shutdown)."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def close(self, timeout: float) -> bool:
        """Stop the worker after the queued work; False if it did not finish in time
        (a hung observer; the daemon thread is then abandoned). Closes the log when done."""
        if self._worker is None:
            self.log.close()
            return True
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return False
        self._worker.join(timeout)
        if self._worker.is_alive():
            return False
        self.log.close()
        return True

    # ----------------------------------------------------------- worker thread

    def _fail(self, observer: LoadedStrategy, at: datetime, cause: str) -> None:
        with self._lock:
            self._failures.append(ObserverFailure(observer.folder, observer.identity, at, cause))
            self._enabled.remove(observer)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                context, at = item  # type: ignore[misc]
                self._evaluate(context, at)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def _evaluate(self, context: SymbolContext, at: datetime) -> None:
        bar_start = context.bars[-1].start
        for observer in self.enabled:
            started = self.monotonic()
            try:
                decision = observer.strategy.evaluate(context)
            except Exception as exc:  # an observer must never stop the worker
                self._fail(observer, at, f"evaluate raised {type(exc).__name__}: {exc}")
                continue
            elapsed = self.monotonic() - started
            problem = _contract_problem(observer, context.symbol, decision)
            if problem is not None:
                self._fail(observer, at, problem)
                continue
            assert isinstance(decision, Decision)
            enter = decision.verdict is Verdict.ENTER
            with self._lock:
                symbols = self._watching.setdefault(observer.folder, {})
                if enter or decision.verdict is Verdict.WATCH:
                    symbols[context.symbol] = bar_start
                else:
                    symbols.pop(context.symbol, None)
            record = DecisionRecord(
                time=at,
                bar_start=bar_start,
                role=Role.OBSERVER,
                decision=decision,
                admission=Admission.NOT_EVALUATED if enter else Admission.NOT_APPLICABLE,
            )
            try:
                self.log.append(record)
            except (OSError, ValueError) as exc:
                for other in self.enabled:
                    self._fail(other, at, f"decision log write failed: {exc}")
                return
            if elapsed > self.max_seconds:
                self._fail(observer, at,
                           f"evaluation took {elapsed:.3f} s, limit {self.max_seconds} s")
