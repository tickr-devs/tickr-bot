from __future__ import annotations

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
from aperture.runtime.entries import EntryDesk
from aperture.runtime.evaluation import (
    EVALUATION_STOPPED,
    HISTORY_REFUSED,
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
    assert [c.symbol for c in rig.strategy.seen] == ["ABC"]  # XYZ waits for the next minute
    (stopped,) = rig.store.journal(EVALUATION_STOPPED)
    assert stopped.body["stage"] == "protect-first" and stopped.body["symbol"] == "ABC"
    assert stopped.body["cause"].endswith("entry working")


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
