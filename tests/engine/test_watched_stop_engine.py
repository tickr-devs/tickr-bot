"""The bot-watched stop in the engine (design v2.1 section 3): an extended-hours trade gets no
broker stop, is checked against the book on every pass, and a BREACH or BLIND verdict asks for
its exit once, journaled with the cause. `open_trade` still refuses extended windows
(`PROTECTED_WINDOWS`), so these tests open one through that barrier on purpose."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.fake import FakeBroker
from tickr.config import GateResult
from tickr.engine import core
from tickr.engine.core import WATCH_VERDICT, Engine, EntryRefused, Fence, Leg, TradeState
from tickr.risk.timers import WindowKind
from tickr.risk.watched_stop import WatchQuote
from tickr.store import Store
from tickr.types import OrderType, Quote, TradingSession

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
POLL = float(CFG.orders.detail_poll_seconds)
CONFIRM = CFG.session.extended_stop_confirm_checks
BLIND = float(CFG.session.extended_blind_seconds)
GAP = float(CFG.session.extended_watch_max_gap_seconds)


class Rig:
    """Engine and store on one clock; a book for ABC the test moves."""

    def __init__(self, tmp_path: Path) -> None:
        self.broker = FakeBroker()
        self.epoch = 1_000_000.0
        self.bid: D | None = D("10.00")
        self.alive = True
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                now=self.now)
        self.engine = Engine(self.store, self.broker, replace(CFG, root=tmp_path), "watch001",
                             lambda: GateResult(True, "t"), clock=lambda: self.epoch,
                             watch_quote=self.quote)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, UTC)

    def quote(self, symbol: str) -> WatchQuote | None:
        if self.bid is None:
            return None
        at = self.now() - timedelta(seconds=1)
        return WatchQuote(Quote(symbol, self.bid, self.bid + D("0.01"), D(100), D(100), at, at),
                          self.alive)

    def run(self, steps: int = 1, every: float = POLL) -> None:
        for _ in range(steps):
            self.epoch += every
            self.engine.step()

    def open(self, monkeypatch: pytest.MonkeyPatch, window: WindowKind = WindowKind.POST,
             qty: str = "2") -> str:
        monkeypatch.setattr(core, "PROTECTED_WINDOWS", frozenset(WindowKind))
        self.engine.recover()
        t = self.engine.open_trade("ABC", D(qty), D(10), D("9.80"), D(1), window=window,
                                   reserved_usd=D(20))
        entry = [o for o in self.engine.orders(t) if o.leg is Leg.ENTRY][0]
        self.broker.fill(entry.client_order_id, D(qty), D(10))
        self.run()
        return t

    def verdicts(self, t: str) -> list[dict]:
        return [r.body for r in self.store.journal(WATCH_VERDICT, t)]


@pytest.fixture
def rig(tmp_path: Path):
    r = Rig(tmp_path)
    yield r
    r.store.close()


def test_open_trade_still_refuses_an_extended_window(rig: Rig) -> None:
    rig.engine.recover()
    with pytest.raises(EntryRefused, match="no protection path"):
        rig.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1), window=WindowKind.POST,
                              reserved_usd=D(20))


@pytest.mark.parametrize("window", [WindowKind.PRE, WindowKind.POST])
def test_extended_orders_go_out_session_all_and_no_stop_is_placed(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, window: WindowKind) -> None:
    t = rig.open(monkeypatch, window)
    rig.run(5)
    assert [r.session for r in rig.broker.sent] == [TradingSession.ALL]
    assert not [o for o in rig.engine.orders(t) if o.leg is Leg.STOP]
    assert rig.engine.trade(t).state is TradeState.OPEN
    assert not any(n.startswith(Fence.UNPROTECTED) for n in rig.engine.fences())
    assert rig.verdicts(t) == []


def test_regular_orders_stay_core(rig: Rig) -> None:
    rig.engine.recover()
    t = rig.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = [o for o in rig.engine.orders(t) if o.leg is Leg.ENTRY][0]
    rig.broker.fill(entry.client_order_id, D(2), D(10))
    rig.run(3)
    assert {r.session for r in rig.broker.sent} == {TradingSession.CORE}
    assert [o.order_type for o in rig.engine.orders(t) if o.leg is Leg.STOP] == [
        OrderType.STOP_LOSS]


def test_breach_confirmed_asks_the_exit_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    t = rig.open(monkeypatch)
    rig.bid = D("9.80")
    rig.run(CONFIRM - 1)
    assert rig.engine.trade(t).exit_requested is None
    rig.run()
    trade = rig.engine.trade(t)
    assert trade.exit_requested == "watched-stop-breach"
    rig.run(5)
    assert rig.engine.trade(t).state is TradeState.EXITING
    [verdict] = rig.verdicts(t)
    assert verdict["verdict"] == "breach"
    assert D(str(verdict["stop"])) == D("9.80")


def test_a_bid_back_above_the_stop_clears_the_count(rig: Rig,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    t = rig.open(monkeypatch)
    for _ in range(3):
        rig.bid = D("9.79")
        rig.run(CONFIRM - 1)
        rig.bid = D("9.90")
        rig.run()
    assert rig.engine.trade(t).exit_requested is None


def test_no_book_goes_blind_and_asks_the_exit(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    t = rig.open(monkeypatch)
    rig.bid = None
    steps = int(BLIND // POLL)
    rig.run(steps)
    assert rig.engine.trade(t).exit_requested is None
    rig.run(2)
    assert rig.engine.trade(t).exit_requested == "watched-stop-blind"
    [verdict] = rig.verdicts(t)
    assert D(str(verdict["last_usable_bid"])) == D("10.00")


def test_a_watch_gap_goes_blind(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    t = rig.open(monkeypatch)
    rig.run(every=GAP + 1)
    assert rig.engine.trade(t).exit_requested == "watched-stop-blind"
    assert "watch gap" in rig.verdicts(t)[0]["cause"]


def test_exit_holds_under_an_operator_fence_without_market_or_stop(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    t = rig.open(monkeypatch)
    rig.bid = D("9.70")
    rig.run(CONFIRM + 5)
    assert f"{Fence.EXIT_REFUSED}:{t}" in rig.engine.fences()
    assert [r.order_type for r in rig.broker.sent] == [OrderType.LIMIT]  # the entry only
    assert rig.engine.trade(t).state is TradeState.EXITING


def test_a_working_entry_is_cancelled_on_exit(rig: Rig,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "PROTECTED_WINDOWS", frozenset(WindowKind))
    rig.engine.recover()
    t = rig.engine.open_trade("ABC", D(4), D(10), D("9.80"), D(1), window=WindowKind.PRE,
                              reserved_usd=D(40))
    entry = [o for o in rig.engine.orders(t) if o.leg is Leg.ENTRY][0]
    rig.broker.fill(entry.client_order_id, D(2), D(10))
    rig.run()
    rig.bid = D("9.70")
    rig.run(CONFIRM + 3)
    assert rig.engine.order(entry.client_order_id).cancel_state is not None
    assert not [o for o in rig.engine.orders(t) if o.leg is Leg.STOP]


def test_no_partial_sale_outside_the_regular_session(rig: Rig,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    t = rig.open(monkeypatch)
    assert "no partial sale" in (rig.engine.reduce_refusal(t, D(1)) or "")


def test_nothing_held_is_not_watched(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "PROTECTED_WINDOWS", frozenset(WindowKind))
    rig.engine.recover()
    t = rig.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1), window=WindowKind.POST,
                              reserved_usd=D(20))
    rig.bid = None
    rig.run(int(BLIND // POLL) + 3)
    assert rig.engine.trade(t).exit_requested is None
