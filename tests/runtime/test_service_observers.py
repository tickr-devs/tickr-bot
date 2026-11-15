from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from aperture import config
from aperture.observers.loader import LoadedStrategy
from aperture.observers.log import DecisionLog
from aperture.observers.runner import ObserverSet
from aperture.runtime import service
from aperture.runtime.alerts import Alerter, AlertKind
from aperture.runtime.watch import ALERT, Watch
from aperture.strategy.base import Decision, SymbolContext, Verdict
from aperture.types import Bar

from .test_service import Bot

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")


class Observer:
    name, version = "obs", "1"

    def __init__(self, raises: Exception | None = None,
                 gate: threading.Event | None = None) -> None:
        self.raises = raises
        self.gate = gate
        self.seen: list[str] = []

    def evaluate(self, context: SymbolContext) -> Decision:
        if self.gate is not None:
            self.gate.wait(5)
        self.seen.append(context.symbol)
        if self.raises is not None:
            raise self.raises
        return Decision(self.name, self.version, context.symbol, Verdict.WATCH, ())


class Evaluator:
    """Hands the service one context per symbol per tick, like runtime.evaluation."""

    def __init__(self, bot: Bot, symbols: list[str]) -> None:
        self.bot = bot
        self.symbols = symbols
        self.watch: set[str] = set()
        self.contexts: list[SymbolContext] = []
        self.evaluated_at = None

    def tick(self, candidates) -> None:
        now = self.bot.now
        start = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
        self.evaluated_at = now
        self.contexts = [SymbolContext(s, (Bar(s, start, D(1), D(1), D(1), D(1), D(1)),))
                         for s in self.symbols]


class Market:
    watch: set[str] = set()
    discovery = None

    def tick(self, *, discover: bool = True) -> None:
        pass

    def freshness(self, last_broker_read_at):
        from aperture.api import contract as api
        return api.Freshness(last_broker_read_at, None, api.StreamHealth.OFF)


def wire(tmp_path: Path, observer: Observer, symbols: list[str], queue_size: int = 8):
    bot = Bot(tmp_path)
    observers = ObserverSet([LoadedStrategy("obs", observer)],
                            DecisionLog(tmp_path / "decisions"),
                            CFG.observers.max_evaluation_seconds, queue_size,
                            CFG.risk.max_quote_age_seconds, CFG.risk.max_clock_skew_seconds)
    bot.service.market = Market()  # type: ignore[assignment]
    bot.service.evaluator = Evaluator(bot, symbols)  # type: ignore[assignment]
    bot.service.observers = observers
    bot.service.watch = Watch(bot.engine, CFG, Alerter(CFG.alerts, "t", None), lambda: bot.now)
    return bot, observers


def test_observers_get_the_contexts_and_failures_are_journaled(tmp_path):
    observer = Observer(raises=ValueError("observer bug"))
    bot, observers = wire(tmp_path, observer, ["ABC"])
    bot.service._evaluate()
    assert observers.wait_idle(5)
    bot.service._evaluate()  # failures are taken on the next pass
    assert observer.seen == ["ABC"]
    (row,) = bot.store.journal(service.OBSERVER_FAILURE)
    assert row.body["identity"] == "obs v1" and "ValueError" in row.body["cause"]
    assert bot.service.observers is observers  # one bad observer: the set stays
    observers.close(5)


def test_queue_overflow_switches_observers_off_with_one_alert(tmp_path):
    gate = threading.Event()
    observer = Observer(gate=gate)
    bot, observers = wire(tmp_path, observer, ["A", "B", "C"], queue_size=1)
    try:
        bot.service._evaluate()  # A runs (blocked), B waits in the queue, C overflows
        assert bot.service.observers is None
        (stopped,) = bot.store.journal(service.OBSERVERS_STOPPED)
        # The hung observer did not finish.
        assert stopped.body == {"error": "queue-full", "finished": False}
        failures = bot.store.journal(service.OBSERVER_FAILURE)
        assert any("queue full" in f.body["cause"] for f in failures)
        alerts = [a.body for a in bot.store.journal(ALERT)]
        assert [a["kind"] for a in alerts] == [AlertKind.OBSERVERS_STOPPED.value]
        bot.service._evaluate()  # nothing more is fed or alerted
        assert len(bot.store.journal(ALERT)) == 1
    finally:
        gate.set()


def test_an_error_in_the_hook_switches_observers_off_and_the_loop_goes_on(tmp_path):
    bot, observers = wire(tmp_path, Observer(), ["ABC"])

    def broken(context, at):
        raise RuntimeError("hook bug")

    observers.on_evaluation = broken  # type: ignore[method-assign]
    bot.service._evaluate()
    assert bot.service.observers is None
    (stopped,) = bot.store.journal(service.OBSERVERS_STOPPED)
    assert stopped.body["error"] == "RuntimeError"
    assert [a.body["kind"] for a in bot.store.journal(ALERT)] == [
        AlertKind.OBSERVERS_STOPPED.value]
