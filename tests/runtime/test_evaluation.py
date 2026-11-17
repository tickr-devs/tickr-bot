from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import RateLimited
from tickr.broker.fake import FakeBroker
from tickr.broker.webull import BrokerReadFailed, MalformedResponse
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.data.base import FakeMarketData, MarketDataError, ScreenerKind, ScreenerRow
from tickr.data.history import HistoryRead, HistoryReader, HistorySettings, HistoryUnavailable
from tickr.data.todaybars import TodayBars
from tickr.engine.core import Engine
from tickr.observers.log import DecisionLog
from tickr.research.records import Admission as RecordAdmission
from tickr.research.records import load_records
from tickr.risk.causes import AdmissionCause
from tickr.runtime import evaluation
from tickr.runtime.entries import EntryDesk
from tickr.runtime.evaluation import (
    EVALUATION_STOPPED,
    HISTORY_REFUSED,
    REMAINDER,
    STRATEGY_ERROR,
    Evaluator,
)
from tickr.runtime.market import StreamInbox
from tickr.runtime.preload import PRELOAD
from tickr.runtime.screeners import ScreenerRecorder
from tickr.store import Store
from tickr.strategy.base import Check, Decision, SymbolContext, Verdict
from tickr.types import Bar, Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY = date(2026, 11, 16)
OPEN = datetime(2026, 11, 16, 9, 30, tzinfo=NEW_YORK)
NOW = datetime(2026, 11, 16, 9, 35, 20, tzinfo=NEW_YORK)  # 09:34 is the last completed bar
GATE_OPEN = config.trading_gate(CFG).open


def bars(symbol: str, start: datetime, count: int) -> list[Bar]:
    return [Bar(symbol, start + timedelta(minutes=i), D(10), D("10.05"), D("9.95"), D(10),
                D(1000)) for i in range(count)]


def prior_bars(symbol: str) -> list[Bar]:
    out: list[Bar] = []
    for day in (date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11),
                date(2026, 11, 12), date(2026, 11, 13)):
        out += bars(symbol, datetime(day.year, day.month, day.day, 9, 30, tzinfo=NEW_YORK), 3)
    return out


class Market:
    """The evaluation's view of runtime.market: fixed candidates, discovery passes counted."""

    def __init__(self, symbols, on_discover=None, recorder=None) -> None:
        self.symbols = list(symbols)
        self.discovered = 0
        self.on_discover = on_discover
        self.recorder = recorder  # runtime.screeners.ScreenerRecorder, or none outside tests of it

    def record_screeners(self, day, now):
        return None if self.recorder is None else self.recorder.tick(day, now)

    def discover(self) -> None:
        self.discovered += 1
        if self.on_discover is not None:
            self.on_discover()

    def collect(self) -> None:
        """Every pass runs in place here: nothing is ever in flight."""

    def discovering(self) -> bool:
        return False

    def candidates(self) -> list[str]:
        return list(self.symbols)


class Scripted:
    """A strategy that returns scripted verdicts and remembers what it was shown."""

    name = CFG.strategy.active
    version = "1"

    def __init__(self) -> None:
        self.verdicts: dict[str, Verdict] = {}
        self.seen: list[SymbolContext] = []
        self.broken: set[str] = set()

    def evaluate(self, context: SymbolContext) -> Decision:
        self.seen.append(context)
        if context.symbol in self.broken:
            raise ValueError("strategy bug")
        verdict = self.verdicts.get(context.symbol, Verdict.IGNORE)
        values: dict[str, object] = {}
        if verdict is Verdict.ENTER:
            values = dict(setup="breakout", trigger=D(10), invalidation=D("9.5"),
                          entry_low=D(10), entry_high=D("10.10"), stop=D("9.75"),
                          quantity=D(10))
        return Decision(self.name, self.version, context.symbol, verdict,
                        (Check("rule", D(1), ">=", D(1), True),), **values)


class Rig:
    def __init__(self, tmp_path: Path, history: object | None = None) -> None:
        self.broker = FakeBroker()
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
        self.engine = Engine(self.store, self.broker, CFG, "eval0001",
                             lambda: GateResult(True, "t"))
        self.engine.recover()
        self.source = FakeMarketData()
        self.inbox = StreamInbox()
        self.now = NOW
        self.strategy = Scripted()
        self.dir = tmp_path / "decisions"
        desk = EntryDesk(self.engine, CFG, calendar=CALENDAR, last_broker_read_at=lambda: self.now,
                         log=DecisionLog(self.dir))
        reader = history or HistoryReader(self.source, CALENDAR, HistorySettings.from_config(CFG),
                                          None)
        self.today_bars = TodayBars()
        self.evaluator = Evaluator(self.engine, CFG, self.source, strategy=self.strategy,
                                   desk=desk, history=reader, inbox=self.inbox,
                                   calendar=CALENDAR, today_bars=self.today_bars,
                                   now=lambda: self.now)

    def symbol(self, symbol: str, with_quote: bool = True) -> None:
        self.source.add_bars(prior_bars(symbol))
        # Pre-market bar, the five completed regular bars and the forming 09:35 bar.
        self.source.add_bars(bars(symbol, OPEN - timedelta(minutes=1), 7))
        if with_quote:
            self.inbox.on_quote(Quote(symbol, D("10.04"), D("10.05"), D(100), D(100),
                                      self.now, self.now))


def test_strategy_sees_prior_sessions_and_completed_regular_bars_only(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.evaluator.tick(Market(["ABC"]))
    (context,) = rig.strategy.seen
    assert rig.store.journal(HISTORY_REFUSED) == []
    assert len(context.prior_sessions) == CFG.market_data.history_sessions
    assert all(len(session) == 3 for session in context.prior_sessions)
    today = [b for b in context.bars if b.start.date() == MONDAY]
    assert [b.start.minute for b in today] == [30, 31, 32, 33, 34]
    assert context.quote is not None and context.quote.ask == D("10.05")
    assert rig.evaluator.contexts == [context]


def test_the_strategy_gets_the_open_its_bars_are_proven_to_reach(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")  # the read reaches the pre-market bar before the open
    rig.evaluator.tick(Market(["ABC"]))
    (context,) = rig.strategy.seen
    assert context.session_open == OPEN


def test_runs_once_per_completed_minute_and_only_in_the_session(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.evaluator.tick(Market(["ABC"]))
    rig.now = NOW + timedelta(seconds=30)
    rig.evaluator.tick(Market(["ABC"]))
    assert len(rig.strategy.seen) == 1 and rig.evaluator.contexts == []
    rig.now = NOW + timedelta(minutes=1)
    rig.evaluator.tick(Market(["ABC"]))
    assert len(rig.strategy.seen) == 2
    rig.now = datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK)  # Saturday
    rig.evaluator.tick(Market(["ABC"]))
    assert len(rig.strategy.seen) == 2


def test_order_is_positions_then_watch_then_candidates(tmp_path):
    rig = Rig(tmp_path)
    for symbol in ("POS", "WAT", "CAN"):
        rig.symbol(symbol)
    trade = rig.engine.open_trade("POS", D(1), D(10), D(9), D(1))
    rig.broker.fill(rig.engine.orders(trade)[0].client_order_id, D(1), D(10))
    for _ in range(3):
        rig.engine.step()
    rig.evaluator.watch.add("WAT")
    rig.evaluator.tick(Market(["CAN", "WAT"]))
    seen = rig.strategy.seen
    assert [c.symbol for c in seen] == ["POS", "WAT", "CAN"]
    assert seen[0].position is not None and seen[0].position.quantity == D(1)


def test_watch_set_follows_the_verdicts(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.strategy.verdicts["ABC"] = Verdict.WATCH
    rig.evaluator.tick(Market(["ABC"]))
    assert rig.evaluator.watch == {"ABC"}
    rig.strategy.verdicts["ABC"] = Verdict.IGNORE
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(Market([]))  # still evaluated: it is watched
    assert rig.evaluator.watch == set()


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_enter_goes_through_the_desk_and_every_decision_is_recorded(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.symbol("XYZ")
    rig.strategy.verdicts["ABC"] = Verdict.ENTER
    rig.evaluator.tick(Market(["XYZ", "ABC"]))  # the ENTER comes last: see the next test
    (trade,) = rig.engine.trades()
    assert trade.symbol == "ABC"
    rig.evaluator.desk.log.close()
    records = load_records([rig.dir / MONDAY.isoformat() / "decisions.jsonl"])
    assert [(r.symbol, r.admission) for r in records] == [("XYZ", "not_applicable"),
                                                          ("ABC", "taken")]


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_an_entry_sent_drops_the_rest_of_the_minute_so_its_fill_is_seen_first(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.symbol("XYZ")
    rig.strategy.verdicts["ABC"] = Verdict.ENTER
    rig.evaluator.tick(Market(["ABC", "XYZ"]))
    assert [c.symbol for c in rig.strategy.seen] == ["ABC"]  # XYZ waits: see below
    (stopped,) = rig.store.journal(EVALUATION_STOPPED)
    assert stopped.body["stage"] == "protect-first" and stopped.body["symbol"] == "ABC"
    assert stopped.body["cause"].endswith("entry working")
    assert stopped.body["remaining"] == ["XYZ"]


BREAKOUT = ("ABC", "DEF", "GHI")


def breakout(tmp_path: Path) -> Rig:
    """Three symbols say ENTER in the 09:34 minute; ABC is taken, DEF and GHI put aside."""
    rig = Rig(tmp_path)
    for symbol in BREAKOUT:
        rig.symbol(symbol)
        rig.strategy.verdicts[symbol] = Verdict.ENTER
    rig.evaluator.tick(Market(list(BREAKOUT)))
    assert [c.symbol for c in rig.strategy.seen] == ["ABC"]
    return rig


def protect(rig: Rig) -> None:
    """ABC fills and its stop is verified: the service would run the strategy again."""
    (trade,) = rig.engine.trades()
    rig.broker.fill(rig.engine.orders(trade.trade_id)[0].client_order_id, D(10), D("10.05"))
    for _ in range(2):
        rig.engine.step()


def records(rig: Rig) -> list[tuple[str, str, str]]:
    rig.evaluator.desk.log.close()
    return [(r.symbol, r.bar_start.astimezone(NEW_YORK).strftime("%H:%M"), r.admission) for r in
            load_records([rig.dir / MONDAY.isoformat() / "decisions.jsonl"])]


def restarted(rig: Rig) -> Evaluator:
    """A new evaluator on the same database and decision folder (a bot restart)."""
    rig.evaluator.desk.log.close()
    desk = EntryDesk(rig.engine, CFG, calendar=CALENDAR, last_broker_read_at=lambda: rig.now,
                     log=DecisionLog(rig.dir))
    return Evaluator(rig.engine, CFG, rig.source, strategy=rig.strategy, desk=desk,
                     history=rig.evaluator.history, inbox=rig.inbox, calendar=CALENDAR,
                     today_bars=rig.today_bars, now=lambda: rig.now)


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_the_rest_of_a_breakout_minute_is_recorded_once_protection_is_done(tmp_path):
    rig = breakout(tmp_path)
    protect(rig)
    rig.now += timedelta(seconds=20)  # 09:35:40: still the 09:34 decision minute
    rig.evaluator.tick(Market(list(BREAKOUT)))
    late = rig.strategy.seen[1:]
    assert [c.symbol for c in late] == ["DEF", "GHI"]
    assert all(c.bars[-1].start.strftime("%H:%M") == "09:34" for c in late)
    assert all(c.quote is not None and c.quote.provider_time == NOW for c in late)  # saved
    assert len(rig.engine.trades()) == 1  # record only: nothing sent
    rig.evaluator.tick(Market(list(BREAKOUT)))  # done: nothing twice
    assert len(rig.strategy.seen) == 3
    assert records(rig) == [("ABC", "09:34", "taken"), ("DEF", "09:34", "not_evaluated"),
                            ("GHI", "09:34", "not_evaluated")]
    (done,) = rig.store.journal(REMAINDER)
    assert done.body == {"symbols": ["DEF", "GHI"]}


class Sizing(Scripted):
    """Sizes an ENTER from the daily loss budget it is given: one share per 5 USD of room."""

    def evaluate(self, context: SymbolContext) -> Decision:
        decision = super().evaluate(context)
        if decision.verdict is not Verdict.ENTER or context.loss_budget_usd is None:
            return decision
        return replace(decision, quantity=context.loss_budget_usd // D(5))


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_a_remainder_is_sized_with_the_budget_of_its_own_minute(tmp_path, monkeypatch):
    # 50 USD of room in the breakout minute; the entry's reservation leaves 20 afterwards.
    monkeypatch.setattr(evaluation, "daily_loss_room",
                        lambda engine, config, now: D(20) if engine.trades() else D(50))
    rig = Rig(tmp_path)
    rig.strategy = rig.evaluator.strategy = Sizing()
    for symbol in BREAKOUT:
        rig.symbol(symbol)
        rig.strategy.verdicts[symbol] = Verdict.ENTER
    rig.evaluator.tick(Market(list(BREAKOUT)))
    (stopped,) = rig.store.journal(EVALUATION_STOPPED)
    assert stopped.body["loss_budget_usd"] == "50"
    protect(rig)
    rig.evaluator = restarted(rig)  # the budget comes back from the journal row too
    rig.evaluator.strategy = rig.strategy
    rig.now += timedelta(seconds=20)
    rig.evaluator.tick(Market(list(BREAKOUT)))
    assert [c.loss_budget_usd for c in rig.strategy.seen] == [D(50), D(50), D(50)]
    assert evaluation.daily_loss_room(rig.engine, CFG, rig.now) == D(20)  # not used late
    rig.evaluator.desk.log.close()
    late = load_records([rig.dir / MONDAY.isoformat() / "decisions.jsonl"])
    assert [(r.symbol, r.decision.quantity, r.admission) for r in late] == [
        ("ABC", D(10), "taken"), ("DEF", D(10), "not_evaluated"),
        ("GHI", D(10), "not_evaluated")]


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_a_remainder_freed_after_the_next_bar_still_uses_its_own_bar(tmp_path):
    rig = breakout(tmp_path)
    protect(rig)
    rig.now += timedelta(minutes=2)  # 09:37:20: the 09:35 bar has completed meanwhile
    rig.evaluator.tick(Market(list(BREAKOUT)))
    assert [c.bars[-1].start.strftime("%H:%M") for c in rig.strategy.seen[1:]] == [
        "09:34", "09:34"]
    rig.evaluator.tick(Market(list(BREAKOUT)))  # the next pass evaluates the current minute
    assert records(rig) == [
        ("ABC", "09:34", "taken"), ("DEF", "09:34", "not_evaluated"),
        ("GHI", "09:34", "not_evaluated"), ("ABC", "09:35", "refused"),
        ("DEF", "09:35", "refused"), ("GHI", "09:35", "refused")]


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_a_remainder_is_recorded_once_across_restarts(tmp_path):
    rig = breakout(tmp_path)
    protect(rig)
    rig.evaluator = restarted(rig)  # restart before the remainder ran: it is not lost
    rig.now += timedelta(seconds=20)
    rig.evaluator.tick(Market(list(BREAKOUT)))
    rig.evaluator = restarted(rig)  # restart after: it is not run again
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(Market(list(BREAKOUT)))
    assert [r[:2] for r in records(rig)] == [
        ("ABC", "09:34"), ("DEF", "09:34"), ("GHI", "09:34"),
        ("ABC", "09:35"), ("DEF", "09:35"), ("GHI", "09:35")]


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_a_remainder_that_cannot_be_read_is_journaled_never_silent(tmp_path):
    rig = breakout(tmp_path)
    protect(rig)
    rig.source.delay_bars(D(15))
    rig.evaluator.today_bars = TodayBars()  # as after a restart: the kept reads are gone
    rig.now += timedelta(seconds=20)
    rig.evaluator.tick(Market(list(BREAKOUT)))
    row = rig.store.journal(EVALUATION_STOPPED)[-1]
    assert (row.body["stage"], row.body["symbols"]) == ("remainder-bars", ["DEF", "GHI"])
    assert [r[0] for r in records(rig)] == ["ABC"]


def test_a_strategy_exception_is_journaled_and_other_symbols_go_on(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("BAD")
    rig.symbol("ABC")
    rig.strategy.broken.add("BAD")
    rig.evaluator.tick(Market(["BAD", "ABC"]))
    (row,) = rig.store.journal(STRATEGY_ERROR)
    assert row.body["symbol"] == "BAD" and row.body["error"] == "ValueError"
    assert [c.symbol for c in rig.evaluator.contexts] == ["ABC"]


class RefusingHistory:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def prior_sessions(self, symbols, before, sessions, *, budget_reserve=0):
        self.calls += 1
        if isinstance(self.error, HistoryUnavailable):
            return HistoryRead({}, {s: self.error for s in symbols}, None, 0)
        return HistoryRead({}, {}, self.error, 0)


def test_history_refusal_is_recorded_once_a_day_and_the_strategy_still_decides(tmp_path):
    history = RefusingHistory(HistoryUnavailable("ABC", "PAGE_LIMIT", "too many pages"))
    rig = Rig(tmp_path, history=history)
    rig.symbol("ABC")
    rig.evaluator.tick(Market(["ABC"]))
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(Market(["ABC"]))
    assert len(rig.store.journal(HISTORY_REFUSED)) == 1
    assert [len(c.prior_sessions) for c in rig.strategy.seen] == [0, 0]


def test_budget_or_delayed_data_stops_the_minute(tmp_path):
    rig = Rig(tmp_path, history=RefusingHistory(RateLimited("history")))
    rig.symbol("ABC")
    rig.evaluator.tick(Market(["ABC"]))
    assert rig.strategy.seen == []
    (row,) = rig.store.journal(EVALUATION_STOPPED)
    assert row.body["stage"] == "history"
    rig.source.delay_bars(D(15))
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(Market(["ABC"]))
    assert rig.store.journal(EVALUATION_STOPPED)[-1].body == {
        "bar_start": "2026-11-16T09:35:00-05:00", "stage": "bars", "error": "DelayedData"}


@pytest.mark.parametrize("error", [MalformedResponse("bars page"),
                                   BrokerReadFailed("bars", None, None),
                                   MarketDataError("http 500")])
def test_a_failed_history_read_stops_the_minute_and_is_retried(tmp_path, error):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    healthy = rig.source.bars_before

    def broken(symbols, end, count, *, budget_reserve=0):
        raise error

    rig.source.bars_before = broken  # type: ignore[method-assign]
    rig.evaluator.tick(Market(["ABC"]))  # never raises into the service loop
    (row,) = rig.store.journal(EVALUATION_STOPPED)
    assert row.body["stage"] == "history" and row.body["error"] == type(error).__name__
    rig.source.bars_before = healthy  # type: ignore[method-assign]
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(Market(["ABC"]))
    assert [c.symbol for c in rig.strategy.seen] == ["ABC"]


@pytest.mark.parametrize(("room", "budget"), [
    (None, None),                                  # money unknown: admission refuses it
    (CFG.risk.min_daily_room_usd - D("0.01"), None),  # below the minimum: admission refuses
    (CFG.risk.min_daily_room_usd, CFG.risk.min_daily_room_usd),
    (D(120), D(120)),
])
def test_the_strategy_sizes_into_the_daily_room_only_above_the_minimum(tmp_path, monkeypatch,
                                                                       room, budget):
    monkeypatch.setattr(evaluation, "daily_loss_room", lambda engine, config, now: room)
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.evaluator.tick(Market(["ABC"]))
    (context,) = rig.strategy.seen
    assert context.loss_budget_usd == budget


def _reads_take(rig: Rig, delay: timedelta, quote_on_the_way: bool) -> None:
    """Make the bar read last `delay`, optionally with an L1 quote arriving during it."""
    read = rig.source.bars

    def slow(symbols, count):
        rig.now += delay
        if quote_on_the_way:
            rig.inbox.on_quote(Quote("ABC", D("10.04"), D("10.05"), D(100), D(100),
                                     rig.now, rig.now))
        return read(symbols, count)

    rig.source.bars = slow
    rig.evaluator.desk.last_broker_read_at = lambda: rig.now


def test_a_quote_received_during_the_bar_read_is_admitted(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.strategy.verdicts["ABC"] = Verdict.ENTER
    _reads_take(rig, timedelta(milliseconds=300), quote_on_the_way=True)
    rig.evaluator.tick(Market(["ABC"]))
    (record,) = load_records(list(rig.dir.rglob("*.jsonl")))
    assert record.refusal_cause is None and record.admission is RecordAdmission.TAKEN
    assert record.time == rig.now


def test_a_slow_bar_read_cannot_hide_the_age_of_the_quote(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")  # its quote is received before the read starts
    rig.strategy.verdicts["ABC"] = Verdict.ENTER
    slow = timedelta(seconds=float(CFG.risk.max_quote_age_seconds) + 1)
    _reads_take(rig, slow, quote_on_the_way=False)
    rig.evaluator.tick(Market(["ABC"]))
    (record,) = load_records(list(rig.dir.rglob("*.jsonl")))
    assert record.admission is RecordAdmission.REFUSED
    assert record.refusal_cause.startswith(AdmissionCause.QUOTE_STALE)
    assert rig.broker.sent == []


# --- one bars read shared with discovery, history in batches, pre-open preload ---------------


def bars_calls(rig: Rig, method: str = "bars") -> list[tuple[str, ...]]:
    return [args for name, args in rig.source.calls if name == method]


def test_bars_discovery_read_after_the_minute_closed_are_not_read_again(tmp_path):
    rig = Rig(tmp_path)
    for symbol in ("ABC", "DEF", "GHI"):
        rig.symbol(symbol)
    after = datetime(2026, 11, 16, 9, 35, 0, 500000, tzinfo=NEW_YORK)  # 09:34 was over
    before = datetime(2026, 11, 16, 9, 34, 59, tzinfo=NEW_YORK)  # 09:34 still forming
    rig.today_bars.put(OPEN, ["ABC"], rig.source.bars(["ABC"], 390), after)
    rig.today_bars.put(OPEN, ["DEF"], rig.source.bars(["DEF"], 390), before)
    rig.source.calls.clear()
    rig.evaluator.tick(Market(["ABC", "DEF", "GHI"]))
    assert bars_calls(rig) == [("DEF", "GHI")]  # ABC came from discovery's read
    assert [c.symbol for c in rig.strategy.seen] == ["ABC", "DEF", "GHI"]
    abc = [b.start.strftime("%H:%M") for b in rig.strategy.seen[0].bars
           if b.start.date() == MONDAY]
    assert abc == ["09:30", "09:31", "09:32", "09:33", "09:34"]  # never the forming 09:35


def test_bars_kept_for_another_session_are_dropped(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.today_bars.put(OPEN - timedelta(days=3), ["ABC"], rig.source.bars(["ABC"], 390), NOW)
    rig.source.calls.clear()
    rig.evaluator.tick(Market(["ABC"]))
    assert bars_calls(rig) == [("ABC",)]


def test_history_for_new_symbols_goes_twenty_per_request(tmp_path):
    rig = Rig(tmp_path)
    symbols = [f"S{i:02d}" for i in range(45)]
    for symbol in symbols:
        rig.symbol(symbol)
    rig.evaluator.watch = {"S44"}
    rig.evaluator.tick(Market(symbols))
    history = bars_calls(rig, "bars_before")
    # The WATCH symbol first, on its own; then the candidates 20 at a time in discovery
    # order. Three sessions of 3 bars each fit one page per symbol.
    assert [len(c) for c in history] == [1, 20, 20, 4]
    assert history[0] == ("S44",) and history[1][0] == "S00"
    assert bars_calls(rig)[0] == ("S44",)
    assert len(rig.strategy.seen) == 45 and rig.store.journal(EVALUATION_STOPPED) == []
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(Market(symbols))
    assert len(bars_calls(rig, "bars_before")) == 4  # the next minute: history from memory


def test_a_history_budget_stop_evaluates_the_symbols_it_found_in_order(tmp_path):
    rig = Rig(tmp_path)
    symbols = [f"S{i:02d}" for i in range(25)]
    for symbol in symbols:
        rig.symbol(symbol)
    rig.source.limit("bars_before", 1)  # the first batch of 20 only
    rig.evaluator.tick(Market(symbols))
    assert [c.symbol for c in rig.strategy.seen] == symbols[:20]
    (row,) = rig.store.journal(EVALUATION_STOPPED)
    assert row.body["stage"] == "history" and row.body["symbol"] == "S20"
    assert row.body["error"] == "RateLimited"


PRE = datetime(2026, 11, 16, 8, 59, 59, tzinfo=NEW_YORK)  # one second before the window


def screener(rig: Rig, symbols: list[str], kind: ScreenerKind = ScreenerKind.GAINERS,
             security: str = "COMMON_STOCK") -> None:
    rig.source.set_screener(kind, [ScreenerRow(kind, n, f"id-{s}", s, "NSQ", "USD", security)
                                   for n, s in enumerate(symbols, start=1)])


def test_preload_loads_screener_history_before_the_open_in_bounded_batches(tmp_path):
    rig = Rig(tmp_path)
    symbols = [f"P{i:02d}" for i in range(25)]
    for symbol in symbols:
        rig.source.add_bars(prior_bars(symbol))
    screener(rig, symbols)
    screener(rig, ["ETF1"], ScreenerKind.TOP_ACTIVES, security="ETF")  # not in the universe
    market = Market([], recorder=ScreenerRecorder(CFG, rig.source, None))
    rig.now = PRE
    rig.evaluator.tick(market)
    # Before the window the recorder reads its pre-market round; nothing is queued.
    assert [n for n, _ in rig.source.calls] == ["screener"] * 3
    rig.now += timedelta(seconds=1)  # 09:00:00, the recorder's next round
    rig.evaluator.tick(market)
    assert [n for n, _ in rig.source.calls] == ["screener"] * 6  # this pass: screeners only
    for _ in range(3):
        rig.now += timedelta(seconds=1)
        rig.evaluator.tick(market)
    loads = bars_calls(rig, "bars_before")
    assert [len(c) for c in loads] == [20, 5]  # one batch per pass, then nothing left
    assert rig.source.bars_reserves == [CFG.market_data.discovery_bars_reserve_requests] * 2
    assert [r.body for r in rig.store.journal(PRELOAD)] == [
        {"symbols": 20, "loaded": 20, "refused": 0, "requests": 1, "queued": 5},
        {"symbols": 5, "loaded": 5, "refused": 0, "requests": 1, "queued": 0}]
    assert rig.strategy.seen == []  # nothing decides before the open
    rig.now = NOW  # 09:35:20: the strategy runs on the preloaded history
    for symbol in symbols[:2]:
        rig.source.add_bars(bars(symbol, OPEN, 6))
    rig.evaluator.tick(Market(symbols[:2]))
    assert len(bars_calls(rig, "bars_before")) == 2  # no new history request
    assert [len(c.prior_sessions) for c in rig.strategy.seen] == [5, 5]


def test_preload_pauses_after_a_budget_refusal_and_stops_at_the_open(tmp_path):
    rig = Rig(tmp_path)
    rig.source.add_bars(prior_bars("AAA"))
    screener(rig, ["AAA"])
    rig.source.limit("bars_before", 0)
    market = Market([], recorder=ScreenerRecorder(CFG, rig.source, None))
    rig.now = datetime(2026, 11, 16, 9, 10, tzinfo=NEW_YORK)
    rig.evaluator.tick(market)  # screeners
    rig.now += timedelta(seconds=1)
    rig.evaluator.tick(market)  # refused by the budget: no row, a pause
    rig.now += timedelta(seconds=1)
    rig.evaluator.tick(market)
    assert rig.store.journal(PRELOAD) == []
    rig.source.limit("bars_before", 100)
    assert bars_calls(rig, "bars_before") == []
    rig.now += timedelta(seconds=float(CFG.market_data.history_preload_retry_seconds))
    rig.evaluator.tick(market)  # the recorder's next round takes this pass
    rig.evaluator.tick(market)
    assert bars_calls(rig, "bars_before") == [("AAA",)]
    assert rig.store.journal(PRELOAD)[-1].body["loaded"] == 1
    screener(rig, ["BBB"])
    rig.source.add_bars(prior_bars("BBB"))
    screens = [n for n, _ in rig.source.calls].count("screener")
    rig.now = OPEN  # the window ends at the open, whatever is queued
    rig.evaluator.tick(market)
    assert [n for n, _ in rig.source.calls].count("screener") == screens  # discovery's job now
    assert bars_calls(rig, "bars_before") == [("AAA",)]


def _held(rig: Rig, symbol: str) -> None:
    trade = rig.engine.open_trade(symbol, D(1), D(10), D(9), D(1))
    rig.broker.fill(rig.engine.orders(trade)[0].client_order_id, D(1), D(10))
    for _ in range(3):
        rig.engine.step()


@pytest.mark.parametrize("held", [True, False])
def test_a_held_position_is_decided_only_on_a_fresh_quote(tmp_path, held):
    """No admission checks an exit, so the strategy never decides a held position on an old
    quote; an entry keeps it and admission refuses it as stale."""
    rig = Rig(tmp_path)
    rig.symbol("ABC")  # its quote is received before the read starts
    if held:
        _held(rig, "ABC")
    _reads_take(rig, timedelta(seconds=float(CFG.risk.max_quote_age_seconds) + 1),
                quote_on_the_way=False)
    rig.evaluator.tick(Market([] if held else ["ABC"]))
    (context,) = rig.strategy.seen
    assert (context.position is not None) is held
    assert (context.quote is None) is held


def test_a_held_position_keeps_a_fresh_quote(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    _held(rig, "ABC")
    rig.evaluator.tick(Market([]))
    (context,) = rig.strategy.seen
    assert context.position is not None and context.quote is not None


def test_positions_and_watch_are_decided_before_discovery_runs(tmp_path):
    rig = Rig(tmp_path)
    for symbol in ("WAT", "CAN"):
        rig.symbol(symbol)
    rig.evaluator.watch = {"WAT"}
    seen_at_discovery: list[list[str]] = []
    market = Market(["CAN", "WAT"], on_discover=lambda: seen_at_discovery.append(
        [c.symbol for c in rig.strategy.seen]))
    rig.evaluator.tick(market)
    assert seen_at_discovery == [["WAT"]]  # WAT decided before discovery started
    assert [c.symbol for c in rig.strategy.seen] == ["WAT", "CAN"]  # WAT once
    rig.evaluator.tick(market)  # same minute: discovery may be asked (it gates itself)
    assert len(rig.strategy.seen) == 2 and market.discovered == 2


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_a_watch_entry_puts_the_candidates_aside_without_running_discovery(tmp_path):
    rig = Rig(tmp_path)
    for symbol in ("WAT", "CAN"):
        rig.symbol(symbol)
    rig.evaluator.watch = {"WAT"}
    rig.strategy.verdicts["WAT"] = Verdict.ENTER
    market = Market(["CAN"])
    rig.evaluator.tick(market)
    assert market.discovered == 0 and [c.symbol for c in rig.strategy.seen] == ["WAT"]
    (row,) = rig.store.journal(EVALUATION_STOPPED)
    assert row.body["stage"] == "protect-first" and row.body["remaining"] == ["CAN"]


class BreaksOn:
    """History that raises an unexpected error for one symbol (a bug, not a typed refusal)."""

    def __init__(self, inner: HistoryReader, symbol: str) -> None:
        self.inner, self.symbol = inner, symbol

    def prior_sessions(self, symbols, before, sessions, *, budget_reserve=0):
        if self.symbol in symbols:
            raise KeyError("bug")
        return self.inner.prior_sessions(symbols, before, sessions,
                                         budget_reserve=budget_reserve)


def test_a_candidate_bug_never_skips_the_positions_minute(tmp_path):
    rig = Rig(tmp_path)
    rig.evaluator.history = BreaksOn(rig.evaluator.history, "CAN")
    for symbol in ("WAT", "CAN"):
        rig.symbol(symbol)
    rig.evaluator.watch = {"WAT"}
    rig.strategy.verdicts["WAT"] = Verdict.WATCH
    with pytest.raises(KeyError):  # the service journals it as evaluation-failed
        rig.evaluator.tick(Market(["CAN"]))
    assert [c.symbol for c in rig.strategy.seen] == ["WAT"]
    rig.evaluator.history = BreaksOn(rig.evaluator.history.inner, "WAT")
    rig.now += timedelta(minutes=1)
    with pytest.raises(KeyError):  # the positions' phase itself fails: retried next pass
        rig.evaluator.tick(Market(["CAN"]))
    rig.evaluator.history = rig.evaluator.history.inner
    rig.evaluator.tick(Market(["CAN"]))
    assert [c.symbol for c in rig.strategy.seen] == ["WAT", "WAT", "CAN"]
