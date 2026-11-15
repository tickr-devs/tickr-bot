"""A closing order not filled within [orders] max_exit_seconds: operator fence and alert,
nothing resent or added."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from tickr import config
from tickr.broker.fake import FakeBroker
from tickr.config import GateResult
from tickr.engine.core import OPERATOR_FENCES, Engine, Fence, Leg, TradeState
from tickr.runtime.alerts import Alerter, AlertKind
from tickr.runtime.watch import ALERT, Watch
from tickr.store import Store
from tickr.types import OrderType

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
POLL = float(CFG.orders.detail_poll_seconds)
LIMIT = float(CFG.orders.max_exit_seconds)


class Recorder:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def notify(self, title: str, text: str) -> None:
        self.shown.append((title, text))


class Rig:
    """Engine and store on one clock; market sells never fill unless a test fills them."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.broker = FakeBroker()
        self.epoch = 1_000_000.0
        self.open()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, UTC)

    def open(self) -> None:
        """A (re)start on the same database and broker."""
        self.store = Store.open(self.tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                now=self.now)
        self.engine = Engine(self.store, self.broker, replace(CFG, root=self.tmp_path),
                             "exit0001", lambda: GateResult(True, "t"),
                             clock=lambda: self.epoch)
        self.notifier = Recorder()
        self.watch = Watch(self.engine, self.engine.config,
                           Alerter(CFG.alerts, "Tickr test", self.notifier, self.now),
                           self.now)

    def run(self, steps: int = 1) -> None:
        for _ in range(steps):
            self.epoch += POLL
            self.engine.step()
            self.watch.check()

    def position(self) -> str:
        """Two shares filled, stop verified, then an exit asked and its close sent."""
        self.engine.recover()
        t = self.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
        self.broker.fill(self.cid(t, Leg.ENTRY), D(2), D(10))
        self.run(3)
        self.engine.request_exit(t, "test")
        self.run(1)  # stops cancelled at once (fake), close sent
        self.run(1)
        assert self.closes() and not self.stops_working()
        return t

    def cid(self, trade_id: str, leg: Leg) -> str:
        return [o for o in self.engine.orders(trade_id) if o.leg is leg][-1].client_order_id

    def closes(self) -> list[str]:
        return [r.client_order_id for r in self.broker.sent if r.order_type == OrderType.MARKET]

    def stops_working(self) -> D:
        return sum((o.quantity - o.filled_quantity for o in self.broker.open_orders()
                    if o.order_type == OrderType.STOP_LOSS), D(0))

    def fenced(self, trade_id: str) -> bool:
        return f"{Fence.EXIT_NOT_FILLED}:{trade_id}" in self.engine.fences()

    def steps_until(self, seconds: float) -> int:
        return int(seconds // POLL) + 1


def test_a_close_not_filled_in_time_fences_alerts_and_sends_nothing_more(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    sent, state = len(rig.broker.sent), rig.broker.orders[rig.closes()[0]].state
    while not rig.fenced(t):
        assert rig.epoch - 1_000_000.0 < 60 + LIMIT * 2  # it does come
        rig.run(1)
    attempted = rig.store.journal("order-attempted", rig.closes()[0])[0].at_utc
    assert rig.now() - datetime.fromisoformat(attempted) >= timedelta(seconds=LIMIT)
    assert rig.now() - datetime.fromisoformat(attempted) < timedelta(seconds=LIMIT + POLL)
    rig.run(10)
    assert len(rig.broker.sent) == sent  # never resent, nothing else placed
    assert rig.broker.orders[rig.closes()[0]].state is state  # never cancelled
    assert Fence.EXIT_NOT_FILLED in OPERATOR_FENCES
    kinds = [r.body["kind"] for r in rig.store.journal(ALERT)]
    assert kinds.count(AlertKind.EXIT_NOT_FILLED) == 1  # once while it stays set
    # The shares in the working close have no stop: that alerts on its own (existing rule).
    assert set(kinds) <= {AlertKind.EXIT_NOT_FILLED, AlertKind.UNPROTECTED_POSITION}
    (cause,) = [c for n, c in rig.engine.fences().items() if n.startswith("exit-not-filled")]
    assert rig.closes()[0] in cause and "nothing more is sent" in cause


def test_a_close_filled_in_time_sets_no_fence(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.epoch += LIMIT - 2 * POLL
    rig.broker.fill(rig.closes()[0], D(2), D("10.10"))
    rig.run(3)
    assert rig.engine.trade(t).state is TradeState.CLOSED
    assert not rig.fenced(t) and rig.store.journal(ALERT) == []


def test_a_partly_filled_close_is_still_not_filled(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.broker.fill(rig.closes()[0], D(1), D("10.10"))
    rig.run(rig.steps_until(LIMIT))
    assert rig.fenced(t)
    assert "filled 1" in rig.engine.fences()[f"{Fence.EXIT_NOT_FILLED}:{t}"]


def test_after_a_stuck_close_ends_the_stop_is_back_and_no_close_until_the_operator(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.run(rig.steps_until(LIMIT))
    assert rig.fenced(t)
    rig.broker.cancel(rig.closes()[0])  # the operator cancels it in the Webull app
    rig.run(4)
    assert rig.stops_working() == D(2)  # what is held is protected again
    assert len(rig.closes()) == 1  # no second close
    assert rig.engine.clear_operator_fence(f"{Fence.EXIT_NOT_FILLED}:{t}")
    rig.run(3)
    assert len(rig.closes()) == 2  # the operator looked: one new close
    rig.broker.fill(rig.closes()[1], D(2), D("10.10"))
    rig.run(3)
    assert rig.engine.trade(t).state is TradeState.CLOSED


def test_the_limit_counts_from_the_journaled_send_across_a_restart(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.store.close()
    rig.epoch += LIMIT  # the bot was down meanwhile
    rig.open()
    rig.engine.recover()
    rig.watch.check()
    assert rig.fenced(t)
    assert len(rig.closes()) == 1
