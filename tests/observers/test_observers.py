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
    read_values,
)
from tickr.observers.log import DecisionLog
from tickr.observers.runner import Limits, ObserverSet, ProcessSpec, _Encoder
from tickr.research.records import Admission, load_records
from tickr.settings_version import for_observer, platform_part
from tickr.strategy.base import (
    Decision,
    PositionView,
    Role,
    SizingLimits,
    SymbolContext,
    Verdict,
)
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


def observer_set(tmp_path, *fakes, max_seconds="0.5", clock=None, queue_size=8,
                 slow_in_row=1):
    loaded = [LoadedStrategy(f"folder-{i}", f) for i, f in enumerate(fakes)]
    log = DecisionLog(tmp_path / "decisions")
    observers = ObserverSet(loaded, log, Decimal(max_seconds), queue_size, Decimal(5), Decimal(1),
                            clock or (lambda: 0.0), slow_in_row=slow_in_row)
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



def test_a_slow_evaluation_alone_is_tolerated_only_slow_ones_in_a_row_switch_off(tmp_path):
    """2026-11-17: one 0.509 s evaluation (limit 0.5) switched an observer off for the day."""
    # Per evaluation: start, end. Slow, fast, slow, slow -> off at the second slow in a row.
    ticks = iter([0.0, 2.0, 2.0, 2.1, 2.1, 4.1, 4.1, 6.1])
    observers, log = observer_set(tmp_path, Fake("slow"), clock=lambda: next(ticks),
                                  slow_in_row=2)
    for minute in range(3, 6):
        observers.on_evaluation(context(minute), START + minute * M1)
        assert settle(observers) == ()
    observers.on_evaluation(context(6), START + 6 * M1)
    (failure,) = settle(observers)
    assert failure.cause == "evaluation took 2.000 s, limit 0.5 s (2 in a row)"
    assert observers.close(5)
    assert len(load_records([log.path(date(2026, 11, 10))])) == 4


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


@pytest.mark.parametrize("key", ["planned_loss_usd", "max_position_notional_usd"])
def test_a_strategy_with_its_own_sizing_is_refused_at_load(tmp_path, key):
    """The planned loss and notional cap live only in config/bot.toml [risk] (owner
    2026-11-15, T2/T3): a strategy file that brings back its own [sizing] is refused when it
    loads, as preflight and the bot load it."""
    cfg = _config(tmp_path)
    load_active(cfg)
    path = tmp_path / "strategies" / "price-action-v1" / "strategy.toml"
    path.write_text(path.read_text() + f"\n[sizing]\n{key} = 50\n")
    with pytest.raises(ObserverError, match="unknown .*sizing"):
        load_active(cfg)


def test_the_shipped_active_strategy_fits_the_shipped_platform_limits():
    load_active(config.load(ROOT / "config" / "bot.toml"))


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


# ------------------------------------------------------------------ worker: bars sent once


def test_the_worker_gets_only_new_bars_and_rebuilds_the_same_context(tmp_path):
    fake = Fake()
    observers, _ = observer_set(tmp_path, fake)
    prior = (bars(2, "ABC"),)
    first = SymbolContext("ABC", bars(3), prior)
    encoder = _Encoder(timedelta.max)  # what on_evaluation does, step by step
    sent, commit = encoder.message(first, START + 3 * M1)
    assert sent[4] == prior and sent[5:7] == (0, first.bars)  # everything, the first time
    commit()
    grown = SymbolContext("ABC", bars(5), prior)
    sent, _ = encoder.message(grown, START + 5 * M1)
    assert sent[4] is None and sent[5:7] == (3, grown.bars[3:])  # only the two new bars
    revised = list(bars(5))
    revised[1] = replace(revised[1], close=Decimal(11))  # the provider revised an old bar
    sent, _ = encoder.message(SymbolContext("ABC", tuple(revised), prior), START + 5 * M1)
    assert sent[5:7] == (0, tuple(revised))  # all again
    for ctx, at in ((first, START + 3 * M1), (grown, START + 5 * M1),
                    (SymbolContext("ABC", tuple(revised), prior), START + 5 * M1)):
        assert observers.on_evaluation(ctx, at) == ()
    assert settle(observers) == () and observers.close(5)
    assert [s.bars for s in fake.seen] == [first.bars, grown.bars, tuple(revised)]
    assert all(s.prior_sessions == prior for s in fake.seen)


def test_the_worker_gets_the_platform_sizing_limits(tmp_path):
    """Observers size on the same [risk] values as the active strategy (T2/T3)."""
    fake = Fake()
    observers, _ = observer_set(tmp_path, fake)
    limits = SizingLimits(Decimal(50), Decimal(1000))
    for sizing in (limits, None):
        ctx = SymbolContext("ABC", bars(3), (), sizing=sizing)
        assert observers.on_evaluation(ctx, START + 3 * M1) == ()
        assert settle(observers) == ()
    assert observers.close(5)
    assert [s.sizing for s in fake.seen] == [limits, None]


def test_the_worker_gets_the_proven_session_open(tmp_path):
    fake = Fake()
    observers, _ = observer_set(tmp_path, fake)
    for opened in (START, None):
        ctx = SymbolContext("ABC", bars(3), (), session_open=opened)
        assert observers.on_evaluation(ctx, START + 3 * M1) == ()
        assert settle(observers) == ()
    assert observers.close(5)
    assert [s.session_open for s in fake.seen] == [START, None]


def test_a_dropped_evaluation_is_sent_whole_next_time(tmp_path):
    hung = Blocking()
    observers, _ = observer_set(tmp_path, hung, queue_size=1)
    assert observers.on_evaluation(context(3), START + 3 * M1) == ()
    assert hung.entered.wait(5)
    assert observers.on_evaluation(context(4), START + 4 * M1) == ()  # waits in the queue
    (dropped,) = observers.on_evaluation(context(5), START + 5 * M1)
    assert dropped.cause.startswith("queue full (1)")
    hung.release.set()
    assert observers.wait_idle(5)
    assert observers.on_evaluation(context(6), START + 6 * M1) == ()
    assert observers.wait_idle(5) and observers.close(5)
    assert [len(s.bars) for s in hung.seen] == [3, 4, 6]


# ------------------------------------------------------------------ worker process


def _process_set(tmp_path, folder="pa-lookback-8"):
    cfg = _config(tmp_path, [folder])
    _variant(tmp_path, folder, "2")
    log = DecisionLog(tmp_path / "decisions")
    return ObserverSet(load_observers(cfg), log, Decimal(5), cfg.observers.queue_size,
                       cfg.risk.max_quote_age_seconds, cfg.risk.max_clock_skew_seconds,
                       process=ProcessSpec(tmp_path, tuple(cfg.observers.strategy_modules),
                                           log.directory, platform_part(cfg))), log


def test_a_real_strategy_runs_in_the_observer_process(tmp_path):
    observers, log = _process_set(tmp_path)
    assert observers._process is not None
    for n in (3, 4, 5):
        assert observers.on_evaluation(context(n), START + n * M1) == ()
    assert settle(observers) == ()
    assert observers.close(10) and not observers._process.is_alive()
    records = load_records([log.path(date(2026, 11, 10))])
    # Three bars, refused for the same cause each time: one compact record.
    assert [(r.decision.strategy, r.decision.version, r.role, r.decision.verdict)
            for r in records] == [("price-action-v1", "2", Role.OBSERVER, Verdict.IGNORE)]
    assert records[0].bar_start == START + 2 * M1
    # The child tags its records with the settings version of the values it loaded (S3).
    platform = platform_part(config.load(ROOT / "config" / "bot.toml"))
    assert records[0].settings_version == for_observer(
        platform, read_values(tmp_path, "pa-lookback-8")).value


def test_an_observer_process_that_ends_is_reported_once(tmp_path):
    observers, _ = _process_set(tmp_path)
    observers._process.kill()
    observers._process.join(10)
    assert not observers.alive
    (failure,) = observers.take_failures()
    assert failure.identity == "all observers" and "observer process ended" in failure.cause
    assert observers.take_failures() == ()
    assert observers.on_evaluation(context(), START + 3 * M1) == ()  # nothing is sent
    assert observers.enabled == ()
    observers.close(1)


def test_a_repeated_ignore_is_written_once_until_the_decision_changes(tmp_path):
    fake = Fake("i", verdict=Verdict.IGNORE)
    observers, log = observer_set(tmp_path, fake)
    for n, verdict in ((3, Verdict.IGNORE), (4, Verdict.IGNORE), (5, Verdict.IGNORE),
                       (6, Verdict.WATCH), (7, Verdict.IGNORE), (8, Verdict.IGNORE)):
        fake.verdict = verdict
        assert observers.on_evaluation(context(n), START + n * M1) == ()
        assert settle(observers) == ()
    assert observers.close(5)
    records = load_records([log.path(date(2026, 11, 10))])
    assert [(r.bar_start, r.decision.verdict) for r in records] == [
        (START + 2 * M1, Verdict.IGNORE), (START + 5 * M1, Verdict.WATCH),
        (START + 6 * M1, Verdict.IGNORE)]
    assert len(fake.seen) == 6  # every bar was evaluated


def test_a_symbol_not_sent_for_a_while_is_forgotten_on_both_sides(tmp_path):
    fake = Fake()
    log = DecisionLog(tmp_path / "decisions")
    observers = ObserverSet([LoadedStrategy("f", fake)], log, Decimal(5), 8, Decimal(5),
                            Decimal(1), lambda: 0.0,
                            limits=Limits(timedelta(minutes=10), 8))
    xyz = lambda n: SymbolContext("XYZ", bars(n, "XYZ"))  # noqa: E731
    assert observers.on_evaluation(context(3), START + 3 * M1) == ()
    assert observers.on_evaluation(xyz(15), START + 15 * M1) == ()  # ABC idle 12 minutes
    assert settle(observers) == ()
    assert not observers._encoder.held("ABC") and observers._encoder.held("XYZ")
    assert observers.on_evaluation(context(16), START + 16 * M1) == ()  # sent whole again
    assert settle(observers) == () and observers.close(5)
    assert [len(s.bars) for s in fake.seen] == [3, 15, 16]


def test_whole_histories_per_pass_are_capped_and_the_rest_wait(tmp_path):
    fake = Fake()
    log = DecisionLog(tmp_path / "decisions")
    observers = ObserverSet([LoadedStrategy("f", fake)], log, Decimal(5), 8, Decimal(5),
                            Decimal(1), lambda: 0.0, limits=Limits(timedelta.max, 1))
    at = START + 3 * M1
    other = SymbolContext("XYZ", bars(3, "XYZ"))
    assert observers.on_evaluation(context(3), at) == ()
    (dropped,) = observers.on_evaluation(other, at)
    assert dropped.cause.startswith("queue full (1 whole histories this pass)")
    later = START + 4 * M1
    assert observers.on_evaluation(context(4), later) == ()  # a delta does not count
    assert observers.on_evaluation(SymbolContext("XYZ", bars(4, "XYZ")), later) == ()
    assert settle(observers) == (dropped,) and observers.close(5)
    assert [s.symbol for s in fake.seen] == ["ABC", "ABC", "XYZ"]
