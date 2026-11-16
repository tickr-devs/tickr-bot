from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import pytest

from tickr import config
from tickr.observers.errors import ObserverError
from tickr.observers.loader import (
    LoadedStrategy,
    load_active,
    load_observers,
    parse_modules,
)
from tickr.observers.log import DecisionLog
from tickr.observers.runner import ObserverSet
from tickr.research.records import Admission, load_records
from tickr.strategy.base import Decision, PositionView, Role, SymbolContext, Verdict
from tickr.types import Bar

ROOT = Path(__file__).resolve().parent.parent.parent
NY = ZoneInfo("America/New_York")
M1 = timedelta(minutes=1)
WINDOW = timedelta(minutes=3)
START = datetime.combine(date(2026, 11, 10), time(10, 0), NY).astimezone(UTC)


def bars(n: int = 3, symbol: str = "ABC") -> tuple[Bar, ...]:
    p = Decimal(10)
    return tuple(Bar(symbol, START + i * M1, p, p, p, p, Decimal(100)) for i in range(n))


def context(n: int = 3, position: PositionView | None = None) -> SymbolContext:
    return SymbolContext("ABC", bars(n), position=position)


class Fake:
    def __init__(self, name="fake", version="1", verdict=Verdict.WATCH, raises=None, tag=None):
        self.name, self.version = name, version
        self.verdict, self.raises, self.tag = verdict, raises, tag
        self.seen: list[SymbolContext] = []

    def evaluate(self, ctx: SymbolContext) -> Decision:
        self.seen.append(ctx)
        if self.raises:
            raise self.raises
        name, version = self.tag or (self.name, self.version)
        extra = {}
        if self.verdict is Verdict.ENTER:
            extra = dict(entry_low=Decimal(10), entry_high=Decimal("10.1"), stop=Decimal("9.8"))
        return Decision(name, version, ctx.symbol, self.verdict, (), **extra)


def observer_set(tmp_path, *fakes, max_seconds="0.5", clock=None, queue_size=8):
    loaded = [LoadedStrategy(f"folder-{i}", f) for i, f in enumerate(fakes)]
    log = DecisionLog(tmp_path / "decisions")
    observers = ObserverSet(loaded, log, Decimal(max_seconds), queue_size, Decimal(5), Decimal(1),
                            clock or (lambda: 0.0))
    return observers, log


def settle(observers):
    assert observers.wait_idle(5)
    return observers.take_failures()


# ------------------------------------------------------------------ runner


def test_each_observer_is_recorded_as_observer_without_the_position(tmp_path):
    watch, enter = Fake("a"), Fake("b", verdict=Verdict.ENTER)
    observers, log = observer_set(tmp_path, watch, enter)
    position = PositionView(Decimal(10), Decimal(5), Decimal(9))
    at = START + 3 * M1 + timedelta(seconds=2)
    live_bars = list(bars())
    ctx = SymbolContext("ABC", live_bars, position=position)
    assert observers.on_evaluation(ctx, at) == ()
    live_bars.append(bars(4)[-1])  # the caller's list changes after the call
    assert settle(observers) == ()
    assert observers.close(5)
    seen = watch.seen[0]
    assert seen.position is None and isinstance(seen.bars, tuple) and len(seen.bars) == 3
    records = load_records([log.path(date(2026, 11, 10))])
    assert [(r.decision.strategy, r.role, r.admission) for r in records] == [
        ("a", Role.OBSERVER, Admission.NOT_APPLICABLE),
        ("b", Role.OBSERVER, Admission.NOT_EVALUATED),
    ]
    assert records[0].bar_start == START + 2 * M1 and records[0].time == at


def test_a_raising_observer_is_switched_off_and_others_continue(tmp_path):
    bad, good = Fake("bad", raises=RuntimeError("boom")), Fake("good")
    observers, log = observer_set(tmp_path, bad, good)
    observers.on_evaluation(context(), START + 3 * M1)
    (failure,) = settle(observers)
    assert failure.identity == "bad v1" and "RuntimeError: boom" in failure.cause
    observers.on_evaluation(context(4), START + 4 * M1)
    assert settle(observers) == ()
    assert [o.identity for o in observers.enabled] == ["good v1"]
    assert len(bad.seen) == 1 and len(good.seen) == 2


def test_a_wrong_tag_is_a_contract_failure(tmp_path):
    observers, _ = observer_set(tmp_path, Fake("x", tag=("other", "9")))
    observers.on_evaluation(context(), START + 3 * M1)
    (failure,) = settle(observers)
    assert failure.cause == "decision tagged other v9"
    assert observers.enabled == ()
    assert observers.on_evaluation(context(4), START + 4 * M1) == ()  # nothing left to run


def test_a_slow_observer_is_recorded_then_switched_off(tmp_path):
    ticks = iter([0.0, 2.0])
    observers, log = observer_set(tmp_path, Fake("slow"), clock=lambda: next(ticks))
    observers.on_evaluation(context(), START + 3 * M1)
    (failure,) = settle(observers)
    assert failure.cause.startswith("evaluation took 2.000 s")
    assert observers.close(5)
    assert len(load_records([log.path(date(2026, 11, 10))])) == 1


class Blocking(Fake):
    def __init__(self):
        super().__init__("blocking")
        self.entered = threading.Event()
        self.release = threading.Event()

    def evaluate(self, ctx):
        self.entered.set()
        self.release.wait(10)
        return super().evaluate(ctx)


def test_a_hung_observer_never_blocks_the_caller_and_overflow_is_recorded(tmp_path):
    hung = Blocking()
    observers, _ = observer_set(tmp_path, hung, queue_size=2)
    started = monotonic()
    assert observers.on_evaluation(context(3), START + 3 * M1) == ()
    assert hung.entered.wait(5)  # the worker is now stuck inside evaluate()
    assert observers.on_evaluation(context(4), START + 4 * M1) == ()
    assert observers.on_evaluation(context(5), START + 5 * M1) == ()
    (dropped,) = observers.on_evaluation(context(6), START + 6 * M1)
    assert monotonic() - started < 1  # never waited on the observer
    assert dropped.identity == "all observers" and "queue full (2)" in dropped.cause
    assert not observers.close(0.05)  # still hung: reported, not waited on
    hung.release.set()
    assert observers.wait_idle(5)
    assert observers.take_failures() == (dropped,)


def test_log_write_failure_switches_every_observer_off(tmp_path):
    blocker = tmp_path / "decisions"
    blocker.write_text("not a folder")
    observers, _ = observer_set(tmp_path, Fake("a"), Fake("b"))
    observers.on_evaluation(context(), START + 3 * M1)
    failures = settle(observers)
    assert {f.identity for f in failures} == {"a v1", "b v1"}
    assert all("decision log write failed" in f.cause for f in failures)
    assert observers.enabled == ()


def test_decision_time_before_bar_end_is_a_caller_error(tmp_path):
    observers, _ = observer_set(tmp_path, Fake())
    with pytest.raises(ValueError, match="before the bar completed"):
        observers.on_evaluation(context(), START + 2 * M1)


def test_log_rolls_over_by_new_york_day(tmp_path):
    observers, log = observer_set(tmp_path, Fake())
    observers.on_evaluation(context(), START + 3 * M1)
    next_day = tuple(replace(b, start=b.start + timedelta(days=1)) for b in bars())
    observers.on_evaluation(SymbolContext("ABC", next_day), next_day[-1].start + M1)
    assert settle(observers) == () and observers.close(5)
    for day in (date(2026, 11, 10), date(2026, 11, 11)):
        assert len(load_records([log.path(day)])) == 1


# ------------------------------------------------------------------ loader


def _config(tmp_path, observers=(), modules=None, max_observers=None):
    cfg = config.load(ROOT / "config" / "bot.toml")
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    obs = cfg.observers
    if modules is not None:
        obs = replace(obs, strategy_modules=tuple(modules))
    if max_observers is not None:
        obs = replace(obs, max_observers=max_observers)
    # The variants below are of price-action-v1, so it is the active one here.
    return replace(cfg, root=tmp_path, observers=obs,
                   strategy=replace(cfg.strategy, active="price-action-v1",
                                    observers=tuple(observers)))


def _variant(tmp_path, folder, version, change=("lookback_bars = 5", "lookback_bars = 8")):
    source = (ROOT / "strategies" / "price-action-v1" / "strategy.toml").read_text()
    text = source.replace('version = "1"', f'version = "{version}"').replace(*change)
    (tmp_path / "strategies" / folder).mkdir()
    (tmp_path / "strategies" / folder / "strategy.toml").write_text(text)


def test_loads_a_value_variant_of_the_active_strategy(tmp_path):
    cfg = _config(tmp_path, ["pa-lookback-8"])
    _variant(tmp_path, "pa-lookback-8", "2")
    (observer,) = load_observers(cfg)
    assert observer.identity == "price-action-v1 v2" and observer.folder == "pa-lookback-8"


def test_no_observers_loads_nothing(tmp_path):
    assert load_observers(_config(tmp_path)) == ()


def test_same_identity_as_the_active_is_refused(tmp_path):
    cfg = _config(tmp_path, ["copy"])
    _variant(tmp_path, "copy", "1")
    with pytest.raises(ObserverError, match="same identity as price-action-v1"):
        load_observers(cfg)


@pytest.mark.parametrize(
    ("kwargs", "cause"),
    [
        ({"observers": ["missing"]}, "cannot read"),
        ({"observers": ["v2"], "modules": ["other=x.y"]}, "no module for strategy"),
        ({"observers": ["v2"], "modules": ["price-action-v1=no.such.module"]},
         "cannot be imported"),
        ({"observers": ["v2", "v2"]}, "folder twice"),
        ({"observers": ["v2"], "max_observers": 0}, "at most 0 allowed"),
    ],
)
def test_bad_observer_setup_is_refused(tmp_path, kwargs, cause):
    cfg = _config(tmp_path, **kwargs)
    _variant(tmp_path, "v2", "2")
    with pytest.raises(ObserverError, match=cause):
        load_observers(cfg)


def test_observer_needing_more_history_than_configured_is_refused(tmp_path):
    cfg = _config(tmp_path, ["rvol-20"])
    _variant(tmp_path, "rvol-20", "2",
             change=("relative_volume_sessions = 5", "relative_volume_sessions = 20"))
    with pytest.raises(ObserverError, match="rvol-20: .* needs 20 prior sessions, "
                                            r"\[market_data\] history_sessions = 5"):
        load_observers(cfg)


def test_active_strategy_needing_more_history_than_configured_is_refused(tmp_path):
    cfg = _config(tmp_path)
    assert load_active(cfg).strategy.prior_sessions_needed == 5
    short = replace(cfg, market_data=replace(cfg.market_data, history_sessions=4))
    with pytest.raises(ObserverError, match="needs 5 prior sessions"):
        load_active(short)


def test_values_refused_by_the_strategy(tmp_path):
    cfg = _config(tmp_path, ["broken"])
    _variant(tmp_path, "broken", "2", change=("lookback_bars = 5", "lookback_bars = 5\nx = 1"))
    with pytest.raises(ObserverError, match="values refused"):
        load_observers(cfg)


@pytest.mark.parametrize("entries", [["no-equals"], ["a=x", "a=y"], ["=x"]])
def test_malformed_module_list_is_refused(entries):
    with pytest.raises(ObserverError):
        parse_modules(entries)


def test_log_from_config_path(tmp_path):
    cfg = _config(tmp_path)
    log = DecisionLog.from_config(cfg)
    assert log.path(date(2026, 11, 10)) == (
        tmp_path / cfg.run.data_dir / "decisions" / cfg.environment_name / "2026-11-10"
        / "decisions.jsonl")


def test_real_strategy_variant_runs_as_observer(tmp_path):
    cfg = _config(tmp_path, ["v2"])
    _variant(tmp_path, "v2", "2")
    observers = ObserverSet(load_observers(cfg), DecisionLog(tmp_path / "d"), Decimal(5),
                            cfg.observers.queue_size, cfg.risk.max_quote_age_seconds,
                            cfg.risk.max_clock_skew_seconds)
    assert observers.on_evaluation(context(), START + 3 * M1) == ()
    assert settle(observers) == () and observers.close(5)


def test_log_is_safe_for_two_writers(tmp_path):
    from tickr.research.records import DecisionRecord

    log = DecisionLog(tmp_path / "d")
    decision = Decision("s", "1", "ABC", Verdict.WATCH, ())
    record = DecisionRecord(START + M1, START, Role.ACTIVE, decision, Admission.NOT_APPLICABLE)

    def write():
        for _ in range(300):
            log.append(record)

    threads = [threading.Thread(target=write) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    assert len(load_records([log.path(date(2026, 11, 10))])) == 600


def test_freeze_drops_the_loss_budget_and_a_stale_quote():
    from datetime import timedelta

    from tickr.observers.runner import freeze
    from tickr.types import Quote

    base = context()
    at = base.bars[-1].start + 2 * M1
    fresh = Quote(base.symbol, Decimal(1), Decimal(2), Decimal(1), Decimal(1), at, at)
    stale = Quote(base.symbol, Decimal(1), Decimal(2), Decimal(1), Decimal(1),
                  at - timedelta(seconds=6), at)
    limit, skew = timedelta(seconds=5), timedelta(seconds=1)
    kept = freeze(replace(base, quote=fresh, loss_budget_usd=Decimal(3)), at, limit, skew)
    assert kept.quote == fresh and kept.loss_budget_usd is None
    assert freeze(replace(base, quote=stale), at, limit, skew).quote is None


def _quote_at(provider, receipt):
    from tickr.types import Quote

    return Quote("ABC", Decimal(1), Decimal(2), Decimal(1), Decimal(1), provider, receipt)


@pytest.mark.parametrize("provider_offset,receipt_offset,kept", [
    (-5, 0, True),      # exactly the age limit
    (1, 0, True),       # exactly the skew limit ahead
    (2, 0, False),      # provider time too far ahead of our clock
    (0, 1, False),      # received after the evaluation time
])
def test_freeze_keeps_only_quotes_admission_would_accept(provider_offset, receipt_offset, kept):
    from tickr.observers.runner import freeze

    base = context()
    at = base.bars[-1].start + 2 * M1
    q = _quote_at(at + timedelta(seconds=provider_offset), at + timedelta(seconds=receipt_offset))
    frozen = freeze(replace(base, quote=q), at, timedelta(seconds=5), timedelta(seconds=1))
    assert (frozen.quote == q) is kept


def test_freeze_drops_a_naive_quote():
    from tickr.observers.runner import freeze

    base = context()
    at = base.bars[-1].start + 2 * M1
    naive = at.replace(tzinfo=None)
    frozen = freeze(replace(base, quote=_quote_at(naive, naive)), at, timedelta(seconds=5),
                    timedelta(seconds=1))
    assert frozen.quote is None


def test_observer_set_hands_observers_neither_budget_nor_stale_quote(tmp_path):
    fake = Fake()
    observers, _ = observer_set(tmp_path, fake)
    at = START + 4 * M1
    stale = _quote_at(at - timedelta(seconds=6), at - timedelta(seconds=6))
    observers.on_evaluation(replace(context(), quote=stale, loss_budget_usd=Decimal(3)), at)
    assert settle(observers) == ()
    assert fake.seen[0].quote is None and fake.seen[0].loss_budget_usd is None
    assert observers.close(5)


def test_watching_follows_each_observers_latest_decision(tmp_path):
    watcher, ignorer = Fake(name="w"), Fake(name="i", verdict=Verdict.IGNORE)
    observers, _ = observer_set(tmp_path, watcher, ignorer)
    assert observers.watching(START + 3 * M1, WINDOW) == frozenset()
    observers.on_evaluation(context(), START + 3 * M1)
    assert settle(observers) == ()
    assert observers.watching(START + 3 * M1, WINDOW) == {"ABC"}
    watcher.verdict = Verdict.IGNORE
    observers.on_evaluation(context(4), START + 4 * M1)
    assert settle(observers) == () and observers.watching(START + 4 * M1, WINDOW) == frozenset()
    assert observers.close(5)


def test_watching_drops_a_symbol_no_longer_evaluated(tmp_path):
    watcher = Fake()
    observers, _ = observer_set(tmp_path, watcher)
    observers.on_evaluation(context(), START + 3 * M1)
    other = SymbolContext("XYZ", bars(5, "XYZ"))
    observers.on_evaluation(other, START + 5 * M1)  # two bars later, ABC not evaluated
    assert settle(observers) == ()
    # ABC's bar started at START + 2, more than three minutes before START + 6
    assert observers.watching(START + 6 * M1, WINDOW) == {"XYZ"}
    assert observers.close(5)


def test_watching_ignores_a_switched_off_observer(tmp_path):
    bad = Fake(raises=ValueError("bug"))
    good = Fake(name="g", verdict=Verdict.IGNORE)
    observers, _ = observer_set(tmp_path, good, bad)
    observers.on_evaluation(context(), START + 3 * M1)
    settle(observers)
    assert observers.watching(START + 3 * M1, WINDOW) == frozenset()
    assert observers.close(5)


def test_watching_empties_once_evaluation_stops(tmp_path):
    observers, _ = observer_set(tmp_path, Fake())
    observers.on_evaluation(context(), START + 3 * M1)
    assert settle(observers) == ()
    assert observers.watching(START + 3 * M1, WINDOW) == {"ABC"}
    # after the close no new bars arrive
    assert observers.watching(START + 10 * M1, WINDOW) == frozenset()
    assert observers.close(5)
