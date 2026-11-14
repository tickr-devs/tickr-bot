from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import config
from aperture.broker.base import RateLimited
from aperture.broker.fake import FakeBroker
from aperture.broker.webull import BrokerReadFailed, MalformedResponse
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.config import GateResult
from aperture.data.base import FakeMarketData, MarketDataError
from aperture.data.history import HistoryReader, HistorySettings, HistoryUnavailable
from aperture.engine.core import Engine
from aperture.observers.log import DecisionLog
from aperture.research.records import load_records
from aperture.runtime import evaluation
from aperture.runtime.entries import EntryDesk
from aperture.runtime.evaluation import (
    EVALUATION_STOPPED,
    HISTORY_REFUSED,
    REMAINDER,
    STRATEGY_ERROR,
    Evaluator,
)
from aperture.runtime.market import StreamInbox
from aperture.store import Store
from aperture.strategy.base import Check, Decision, SymbolContext, Verdict
from aperture.types import Bar, Quote

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
        self.evaluator = Evaluator(self.engine, CFG, self.source, strategy=self.strategy,
                                   desk=desk, history=reader, inbox=self.inbox,
                                   calendar=CALENDAR, now=lambda: self.now)

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
    rig.evaluator.tick(["ABC"])
    (context,) = rig.strategy.seen
    assert rig.store.journal(HISTORY_REFUSED) == []
    assert len(context.prior_sessions) == CFG.market_data.history_sessions
    assert all(len(session) == 3 for session in context.prior_sessions)
    today = [b for b in context.bars if b.start.date() == MONDAY]
    assert [b.start.minute for b in today] == [30, 31, 32, 33, 34]
    assert context.quote is not None and context.quote.ask == D("10.05")
    assert rig.evaluator.contexts == [context]


def test_runs_once_per_completed_minute_and_only_in_the_session(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.evaluator.tick(["ABC"])
    rig.now = NOW + timedelta(seconds=30)
    rig.evaluator.tick(["ABC"])
    assert len(rig.strategy.seen) == 1 and rig.evaluator.contexts == []
    rig.now = NOW + timedelta(minutes=1)
    rig.evaluator.tick(["ABC"])
    assert len(rig.strategy.seen) == 2
    rig.now = datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK)  # Saturday
    rig.evaluator.tick(["ABC"])
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
    rig.evaluator.tick(["CAN", "WAT"])
    seen = rig.strategy.seen
    assert [c.symbol for c in seen] == ["POS", "WAT", "CAN"]
    assert seen[0].position is not None and seen[0].position.quantity == D(1)


def test_watch_set_follows_the_verdicts(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.strategy.verdicts["ABC"] = Verdict.WATCH
    rig.evaluator.tick(["ABC"])
    assert rig.evaluator.watch == {"ABC"}
    rig.strategy.verdicts["ABC"] = Verdict.IGNORE
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick([])  # still evaluated: it is watched
    assert rig.evaluator.watch == set()


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_enter_goes_through_the_desk_and_every_decision_is_recorded(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    rig.symbol("XYZ")
    rig.strategy.verdicts["ABC"] = Verdict.ENTER
    rig.evaluator.tick(["XYZ", "ABC"])  # the ENTER comes last: see the next test
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
    rig.evaluator.tick(["ABC", "XYZ"])
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
    rig.evaluator.tick(list(BREAKOUT))
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
                     now=lambda: rig.now)


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_the_rest_of_a_breakout_minute_is_recorded_once_protection_is_done(tmp_path):
    rig = breakout(tmp_path)
    protect(rig)
    rig.now += timedelta(seconds=20)  # 09:35:40: still the 09:34 decision minute
    rig.evaluator.tick(list(BREAKOUT))
    late = rig.strategy.seen[1:]
    assert [c.symbol for c in late] == ["DEF", "GHI"]
    assert all(c.bars[-1].start.strftime("%H:%M") == "09:34" for c in late)
    assert all(c.quote is not None and c.quote.provider_time == NOW for c in late)  # saved
    assert len(rig.engine.trades()) == 1  # record only: nothing sent
    rig.evaluator.tick(list(BREAKOUT))  # done: nothing twice
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
    rig.evaluator.tick(list(BREAKOUT))
    (stopped,) = rig.store.journal(EVALUATION_STOPPED)
    assert stopped.body["loss_budget_usd"] == "50"
    protect(rig)
    rig.evaluator = restarted(rig)  # the budget comes back from the journal row too
    rig.evaluator.strategy = rig.strategy
    rig.now += timedelta(seconds=20)
    rig.evaluator.tick(list(BREAKOUT))
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
    rig.evaluator.tick(list(BREAKOUT))
    assert [c.bars[-1].start.strftime("%H:%M") for c in rig.strategy.seen[1:]] == [
        "09:34", "09:34"]
    rig.evaluator.tick(list(BREAKOUT))  # the next pass evaluates the current minute
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
    rig.evaluator.tick(list(BREAKOUT))
    rig.evaluator = restarted(rig)  # restart after: it is not run again
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(list(BREAKOUT))
    assert [r[:2] for r in records(rig)] == [
        ("ABC", "09:34"), ("DEF", "09:34"), ("GHI", "09:34"),
        ("ABC", "09:35"), ("DEF", "09:35"), ("GHI", "09:35")]


@pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")
def test_a_remainder_that_cannot_be_read_is_journaled_never_silent(tmp_path):
    rig = breakout(tmp_path)
    protect(rig)
    rig.source.delay_bars(D(15))
    rig.now += timedelta(seconds=20)
    rig.evaluator.tick(list(BREAKOUT))
    row = rig.store.journal(EVALUATION_STOPPED)[-1]
    assert (row.body["stage"], row.body["symbols"]) == ("remainder-bars", ["DEF", "GHI"])
    assert [r[0] for r in records(rig)] == ["ABC"]


def test_a_strategy_exception_is_journaled_and_other_symbols_go_on(tmp_path):
    rig = Rig(tmp_path)
    rig.symbol("BAD")
    rig.symbol("ABC")
    rig.strategy.broken.add("BAD")
    rig.evaluator.tick(["BAD", "ABC"])
    (row,) = rig.store.journal(STRATEGY_ERROR)
    assert row.body["symbol"] == "BAD" and row.body["error"] == "ValueError"
    assert [c.symbol for c in rig.evaluator.contexts] == ["ABC"]


class RefusingHistory:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def prior_sessions(self, symbol, before, sessions):
        self.calls += 1
        raise self.error


def test_history_refusal_is_recorded_once_a_day_and_the_strategy_still_decides(tmp_path):
    history = RefusingHistory(HistoryUnavailable("ABC", "PAGE_LIMIT", "too many pages"))
    rig = Rig(tmp_path, history=history)
    rig.symbol("ABC")
    rig.evaluator.tick(["ABC"])
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(["ABC"])
    assert len(rig.store.journal(HISTORY_REFUSED)) == 1
    assert [len(c.prior_sessions) for c in rig.strategy.seen] == [0, 0]


def test_budget_or_delayed_data_stops_the_minute(tmp_path):
    rig = Rig(tmp_path, history=RefusingHistory(RateLimited("history")))
    rig.symbol("ABC")
    rig.evaluator.tick(["ABC"])
    assert rig.strategy.seen == []
    (row,) = rig.store.journal(EVALUATION_STOPPED)
    assert row.body["stage"] == "history"
    rig.source.delay_bars(D(15))
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(["ABC"])
    assert rig.store.journal(EVALUATION_STOPPED)[-1].body == {
        "bar_start": "2026-11-16T09:35:00-05:00", "stage": "bars", "error": "DelayedData"}


@pytest.mark.parametrize("error", [MalformedResponse("bars page"),
                                   BrokerReadFailed("bars", None, None),
                                   MarketDataError("http 500")])
def test_a_failed_history_read_stops_the_minute_and_is_retried(tmp_path, error):
    rig = Rig(tmp_path)
    rig.symbol("ABC")
    healthy = rig.source.bars_before

    def broken(symbol, end, count):
        raise error

    rig.source.bars_before = broken  # type: ignore[method-assign]
    rig.evaluator.tick(["ABC"])  # never raises into the service loop
    (row,) = rig.store.journal(EVALUATION_STOPPED)
    assert row.body["stage"] == "history" and row.body["error"] == type(error).__name__
    rig.source.bars_before = healthy  # type: ignore[method-assign]
    rig.now += timedelta(minutes=1)
    rig.evaluator.tick(["ABC"])
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
    rig.evaluator.tick(["ABC"])
    (context,) = rig.strategy.seen
    assert context.loss_budget_usd == budget
