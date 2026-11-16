"""Discovery on its own thread: the loop never waits for a pass, one pass at a time, outcomes
journaled by the loop, shutdown never hangs. Synchronized with events only (no timing sleeps):
the `wait` timeouts below only stop a broken test from hanging, they never order anything."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal as D

from tickr.broker.webull import MalformedResponse
from tickr.calendar import NEW_YORK
from tickr.data.base import FakeMarketData, ScreenerKind, ScreenerRow, ScreenerSession
from tickr.engine.core import Leg
from tickr.runtime import service
from tickr.runtime.evaluation import EVALUATION_STOPPED
from tickr.runtime.market import DISCOVERY, DISCOVERY_FAILED, daemon_thread
from tickr.runtime.protection import busy
from tickr.types import Quote

from .test_evaluation import OPEN
from .test_evaluation import Rig as EvalRig
from .test_market import CFG, INTERVAL, Rig

HANG = 10  # seconds: only a safety net against a broken test, never a synchronization


class Gate(FakeMarketData):
    """Its first screener read blocks until `release` (after `entered`); `error` is raised
    by that read once released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error: Exception | None = None
        self.blocked = False

    def screener(self, kind: ScreenerKind, session=ScreenerSession.REGULAR) -> list[ScreenerRow]:
        if not self.blocked:
            self.blocked = True
            self.entered.set()
            assert self.release.wait(HANG)
            if self.error is not None:
                raise self.error
        return super().screener(kind, session)


class Threads:
    """`spawn` on a real daemon thread, keeping each thread so a test can wait for its end."""

    def __init__(self) -> None:
        self.started: list[threading.Thread] = []

    def __call__(self, run):
        thread = daemon_thread(run)
        self.started.append(thread)
        return thread

    def finish(self) -> None:
        for thread in self.started:
            thread.join(HANG)
            assert not thread.is_alive()


def threaded(tmp_path, source=None, now=None):
    threads = Threads()
    source = source or Gate()
    kwargs = {} if now is None else {"now": now}
    return Rig(tmp_path, source=source, spawn=threads, **kwargs), source, threads


def test_a_pass_runs_off_the_loop_and_is_journaled_on_a_later_loop_pass(tmp_path):
    rig, source, threads = threaded(tmp_path)
    rows: list[tuple[str, str]] = []
    append = rig.store.append

    def recording(kind, body, *args, **kwargs):
        rows.append((kind, threading.current_thread().name))
        return append(kind, body, *args, **kwargs)

    rig.store.append = recording  # type: ignore[method-assign]
    rig.market.tick()  # starts the pass and returns while it is blocked
    assert source.entered.wait(HANG)
    assert rig.market.discovering() and rig.store.journal(DISCOVERY) == []
    rig.advance(1)
    rig.market.tick()  # the loop goes on; nothing to collect yet
    assert rig.store.journal(DISCOVERY) == []
    rig.advance(2)  # the pass took 3 s of the rig's monotonic clock
    source.release.set()
    threads.finish()
    assert rig.store.journal(DISCOVERY) == []  # finished, but only the loop journals it
    rig.market.tick()
    (row,) = rig.store.journal(DISCOVERY)
    assert row.body["seconds"] == 3.0 and not rig.market.discovering()
    assert ("discovery", threading.main_thread().name) in rows
    assert all(name == threading.main_thread().name for _, name in rows)


def test_one_pass_at_a_time_and_a_missed_minute_starts_nothing(tmp_path):
    rig, source, threads = threaded(tmp_path)
    rig.market.tick()
    assert source.entered.wait(HANG)
    rig.advance(INTERVAL)  # the next minute's first pass while the first pass is in flight
    rig.market.tick()
    assert len(threads.started) == 1
    source.release.set()
    threads.finish()
    rig.advance(1)
    rig.market.tick()  # collected; not a minute's first pass: no new start
    assert len(rig.store.journal(DISCOVERY)) == 1 and len(threads.started) == 1
    rig.advance(INTERVAL - 1)
    rig.market.tick()  # the next minute's first pass starts the next one
    assert len(threads.started) == 2
    threads.finish()
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 2


def test_protection_never_starts_a_pass_but_lets_one_in_flight_finish(tmp_path):
    rig, source, threads = threaded(tmp_path)
    rig.market.tick(discover=False)  # protection needs the loop: no start
    assert threads.started == [] and not source.entered.is_set()
    rig.advance(INTERVAL)
    rig.market.tick()
    assert source.entered.wait(HANG)
    rig.advance(1)
    rig.market.tick(discover=False)  # an order needs the loop now: the pass goes on
    source.release.set()
    threads.finish()
    rig.market.tick(discover=False)  # collected and journaled while protection holds
    assert len(rig.store.journal(DISCOVERY)) == 1


def test_a_failed_pass_is_journaled_by_the_loop_and_never_raised(tmp_path):
    for error, name in ((MalformedResponse("screener: missing field"), "MalformedResponse"),
                        (KeyError("bug"), "KeyError")):
        rig, source, threads = threaded(tmp_path / name)
        source.error = error
        rig.market.tick()
        source.release.set()
        threads.finish()
        rig.market.tick()
        (failed,) = rig.store.journal(DISCOVERY_FAILED)
        assert failed.body == {"error": name, "detail": str(error)}
        assert rig.store.journal(DISCOVERY) == [] and rig.market.discovery is None
        rig.advance(INTERVAL)
        rig.market.tick()  # tried again at the next interval
        threads.finish()
        rig.market.tick()
        assert len(rig.store.journal(DISCOVERY)) == 1


def test_shutdown_never_waits_for_a_pass_in_flight(tmp_path):
    rig, source, threads = threaded(tmp_path)
    rig.market.tick()
    assert source.entered.wait(HANG)
    assert rig.market.close(0) is False  # bounded: the blocked read is left behind
    source.release.set()  # the read in flight returns; the next one is never sent
    threads.finish()
    assert [name for name, _ in source.calls] == ["screener"]
    assert rig.market.close(0) is True
    rig.advance(INTERVAL)
    rig.market.tick()  # (the real loop has ended by now; were it to tick:)
    assert len(threads.started) == 1  # no pass starts after the stop
    (failed,) = rig.store.journal(DISCOVERY_FAILED)  # the abandoned pass, never a result
    assert failed.body["error"] == "DiscoveryCancelled"
    assert rig.store.journal(DISCOVERY) == []


def test_close_without_a_pass_returns_at_once(tmp_path):
    rig, _, _ = threaded(tmp_path)
    assert rig.market.close(0) is True


def test_broker_work_goes_on_while_a_pass_is_in_flight(tmp_path):
    """The service loop: a fill seen while discovery is blocked gets its stop sent and
    verified on the next loop passes; the pass is journaled only once it is released."""
    rig, source, threads = threaded(tmp_path)
    svc = service.Service(rig.engine, now=lambda: rig.now, calendar=rig.market.calendar,
                          strategy=service.strategy_id(CFG),
                          paths=service.ServicePaths(tmp_path / "status.json",
                                                     tmp_path / "cmd" / "inbox",
                                                     tmp_path / "cmd" / "processed"),
                          market=rig.market)
    passes = [0]

    def sleep(seconds: float) -> None:
        rig.advance(seconds)
        passes[0] += 1
        if passes[0] == 2:  # discovery is blocked from pass 1 on
            assert source.entered.wait(HANG)
            trade = rig.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
            rig.broker.fill(rig.engine.orders(trade)[0].client_order_id, D(2), D(10))

    svc.run(should_stop=lambda: passes[0] >= 6, sleep=sleep, monotonic=lambda: rig.clock)
    assert not source.release.is_set() and rig.market.discovering()
    (trade,) = rig.engine.trades()
    stops = [o for o in rig.engine.orders(trade.trade_id) if o.leg is Leg.STOP]
    assert stops and busy(rig.engine) is None  # stop sent and verified meanwhile
    assert rig.store.journal(DISCOVERY) == []
    assert rig.store.journal(service.EVALUATION_SLOW) == []
    source.release.set()
    threads.finish()
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 1


# --- the evaluation of candidates after a pass is collected ---------------------------------


class PassAll:
    name, version = "all", "1"

    def quote_liquidity(self, quote):
        return ()

    def session_liquidity(self, session_bars):
        return ()


class GateOver:
    """Another source's reads, its first screener read blocked like `Gate`."""

    def __init__(self, inner: FakeMarketData) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.entered = threading.Event()
        self.release = threading.Event()

    def screener(self, kind: ScreenerKind, session=ScreenerSession.REGULAR) -> list[ScreenerRow]:
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(HANG)
        return self.inner.screener(kind, session)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_candidates_are_evaluated_from_the_collected_pass_without_reading_bars_again(tmp_path):
    from tickr.data.health import StreamHealth
    from tickr.runtime.market import MarketRuntime

    rig = EvalRig(tmp_path)
    rig.symbol("CAN")
    rig.source.set_screener(ScreenerKind.GAINERS, [ScreenerRow(
        ScreenerKind.GAINERS, 1, "id-CAN", "CAN", "NSQ", "USD", "COMMON_STOCK", {})])
    rig.source.set_quote(Quote("CAN", D("10.04"), D("10.05"), D(100), D(100), rig.now, rig.now))
    gate, threads = GateOver(rig.source), Threads()
    market = MarketRuntime(rig.engine, CFG, gate, health=StreamHealth.from_config(CFG),  # type: ignore[arg-type]
                           inbox=rig.inbox, capture=None, calendar=rig.evaluator.calendar,
                           today_bars=rig.today_bars, now=lambda: rig.now,
                           monotonic=lambda: 0.0, spawn=threads)
    market.screen = PassAll()
    market.tick(discover=False)  # as the service does with an evaluator
    rig.evaluator.tick(market)  # the minute's positions (none), then discovery starts
    assert gate.entered.wait(HANG) and rig.strategy.seen == []
    rig.now += timedelta(seconds=1)
    market.tick(discover=False)
    rig.evaluator.tick(market)  # still in flight: the candidates wait
    assert rig.strategy.seen == []
    gate.release.set()
    threads.finish()
    rig.source.calls.clear()
    rig.now += timedelta(seconds=1)
    market.tick(discover=False)  # collected on the loop
    assert market.candidates() == ["CAN"]
    rig.evaluator.tick(market)
    assert [c.symbol for c in rig.strategy.seen] == ["CAN"]
    assert [c for c in rig.source.calls if c[0] == "bars"] == []  # discovery's read served
    today = [b.start for b in rig.strategy.seen[0].bars if b.start >= OPEN]
    assert today[-1] == datetime(2026, 11, 16, 9, 34, tzinfo=NEW_YORK)
    rig.evaluator.tick(market)
    assert len(rig.strategy.seen) == 1  # once per minute


class InFlight:
    """The evaluation's view of the market with a pass that stays in flight until told."""

    def __init__(self, symbols) -> None:
        self.symbols = list(symbols)
        self.flying = False
        self.due = True  # a minute's first pass with the interval over (the market decides)
        self.starts = 0

    def discover(self) -> None:
        if self.due and not self.flying:
            self.flying = True
            self.starts += 1
        self.due = False

    def collect(self) -> None:
        """Nothing to do: the test lands the pass by clearing `flying`."""

    def discovering(self) -> bool:
        return self.flying

    def candidates(self) -> list[str]:
        return list(self.symbols)


def test_a_pass_that_outlives_its_minute_never_evaluates_that_minute_late(tmp_path):
    rig = EvalRig(tmp_path)
    rig.symbol("CAN")
    market = InFlight(["CAN"])
    rig.evaluator.tick(market)  # 09:35:20: bar 09:34, the pass starts
    rig.now += timedelta(seconds=30)
    rig.evaluator.tick(market)
    assert rig.strategy.seen == []
    rig.now = datetime(2026, 11, 16, 9, 36, 5, tzinfo=NEW_YORK)
    market.flying, market.due = False, True  # the pass lands after 09:35 was over
    rig.evaluator.tick(market)  # a new minute: 09:34's candidates are dropped, journaled
    (late,) = rig.store.journal(EVALUATION_STOPPED)
    assert late.body == {"bar_start": "2026-11-16T09:34:00-05:00", "stage": "candidates-late"}
    assert rig.strategy.seen == [] and market.starts == 2
    market.flying = False
    rig.now += timedelta(seconds=1)
    rig.evaluator.tick(market)  # the current minute, on its own bar
    (context,) = rig.strategy.seen
    assert context.bars[-1].start == datetime(2026, 11, 16, 9, 35, tzinfo=NEW_YORK)


def test_without_a_pass_started_the_candidates_are_evaluated_at_once(tmp_path):
    rig = EvalRig(tmp_path)
    rig.symbol("CAN")
    market = InFlight(["CAN"])
    market.flying = True  # a pass from an earlier minute still in flight: none starts
    rig.evaluator.tick(market)
    assert rig.strategy.seen == [] and market.starts == 0
    market.flying = False  # it lands in this minute: the candidates of this minute run
    rig.evaluator.tick(market)
    assert [c.symbol for c in rig.strategy.seen] == ["CAN"]
    assert rig.store.journal(EVALUATION_STOPPED) == []
