"""Protect first: strategy work waits while an order needs the loop; fill-to-stop latency."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from aperture import config
from aperture.api import contract as api
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.config import GateResult
from aperture.engine.core import Engine, Leg
from aperture.runtime import entries, service
from aperture.runtime.protection import PROTECTION_LATENCY, LatencyLog, busy
from aperture.store import Store
from aperture.types import Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY_11 = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)
POLL = float(CFG.orders.detail_poll_seconds)
BUDGET = float(CFG.runtime.evaluation_budget_seconds)


class Rig:
    """Engine, store and service on one scripted clock; `actions[n]` runs in pass n's sleep."""

    def __init__(self, tmp_path: Path, broker: FakeBroker | None = None) -> None:
        self.tmp_path = tmp_path
        self.broker = broker or FakeBroker()
        self.now = MONDAY_11
        self.clock = 0.0
        self.passes = 0
        self.actions: dict[int, Callable[[], None]] = {}
        self.open()

    def open(self) -> None:
        """A (re)start: new store connection, engine and service on the same database."""
        self.store = Store.open(self.tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                now=lambda: self.now)
        self.engine = Engine(self.store, self.broker, CFG, "prot0001",
                             lambda: GateResult(True, "t"), clock=lambda: self.clock)
        self.market = StubMarket(self)
        self.evaluator = StubEvaluator(self)
        self.service = service.Service(
            self.engine, now=lambda: self.now, calendar=CALENDAR,
            strategy=service.strategy_id(CFG),
            paths=service.ServicePaths(self.tmp_path / "status.json",
                                       self.tmp_path / "cmd" / "inbox",
                                       self.tmp_path / "cmd" / "processed"),
            market=self.market, evaluator=self.evaluator)  # type: ignore[arg-type]

    def advance(self, seconds: float) -> None:
        self.clock += seconds
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        self.passes += 1
        action = self.actions.get(self.passes)
        if action is not None:
            action()

    def run(self, passes: int) -> None:
        stop_after = self.passes + passes
        self.service.run(should_stop=lambda: self.passes >= stop_after, sleep=self.sleep,
                         monotonic=lambda: self.clock)

    def entry(self) -> str:
        (trade,) = self.engine.trades(open_only=False)
        return self.engine.orders(trade.trade_id)[0].client_order_id

    def latency(self) -> dict:
        (row,) = self.store.journal(PROTECTION_LATENCY)
        return row.body


class StubMarket:
    """A fresh quote inside the entry's window every pass, so the entry guard lets it rest."""

    watch: set[str] = set()
    discovery = None

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.inbox = self
        self.discovers: list[bool] = []

    def latest(self, symbol: str) -> Quote:
        return Quote(symbol, D("9.99"), D(10), D(1), D(1), self.rig.now, self.rig.now)

    def tick(self, *, discover: bool = True) -> None:
        self.discovers.append(discover)

    def freshness(self, last_broker_read_at):
        return api.Freshness(last_broker_read_at, None, api.StreamHealth.OFF)


class StubEvaluator:
    """Opens one trade on its first call (as the desk would on ENTER); each call costs
    `seconds` of the rig's clock (bars and history reads)."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.watch: set[str] = set()
        self.calls: list[int] = []  # pass numbers
        self.seconds = 0.0
        self.enter = True

    def tick(self, candidates) -> None:
        self.calls.append(self.rig.passes)
        if self.enter:
            self.enter = False
            self.rig.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
        self.rig.advance(self.seconds)


def test_evaluation_waits_while_an_entry_works_or_a_fill_is_unprotected(tmp_path):
    rig = Rig(tmp_path)
    rig.actions[4] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.run(8)
    # Pass 1 sent the entry; 2-3 it works; 4 the fill is seen and the stop sent; 5 Detail
    # shows the stop working, so the strategy runs again in that same pass.
    assert rig.evaluator.calls == [1, 5, 6, 7, 8]
    assert rig.market.discovers == [True, False, False, False, True, True, True, True]
    (skipped,) = rig.store.journal(service.EVALUATION_SKIPPED)  # one row per episode
    assert skipped.body["cause"] == "t1: entry working"
    assert busy(rig.engine) is None


def test_an_exit_in_progress_is_a_new_episode(tmp_path):
    rig = Rig(tmp_path)
    rig.actions[2] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.actions[5] = lambda: rig.engine.request_exit("t1", "test")
    rig.run(6)
    causes = [r.body["cause"] for r in rig.store.journal(service.EVALUATION_SKIPPED)]
    assert causes == ["t1: filled 2, verified stop covers 0", "t1: exit in progress"]
    assert rig.evaluator.calls == [1, 3, 4]


def test_a_slow_strategy_no_longer_delays_the_stop_after_a_fill(tmp_path):
    class FillsAtOnce(FakeBroker):
        def place(self, request):
            ack = super().place(request)
            if request.order_type == "LIMIT":
                self.fill(request.client_order_id, request.quantity, D(10))
            return ack

    rig = Rig(tmp_path, broker=FillsAtOnce())
    rig.evaluator.seconds = 8.0  # every evaluation pass costs 8 s of reads
    rig.run(4)
    # Before: passes 2 and 3 would each spend 8 s in the strategy before the next Detail read.
    assert rig.evaluator.calls == [1, 3, 4]
    body = rig.latency()
    assert body["observed_to_sent_seconds"] == 0.0
    assert body["observed_to_verified_seconds"] == POLL
    slow = rig.store.journal(service.EVALUATION_SLOW)
    assert [r.body["seconds"] for r in slow] == [8.0, 8.0, 8.0] and 8.0 > BUDGET
    assert slow[0].body["budget"] == BUDGET


def test_latency_record_on_a_scripted_timeline(tmp_path):
    rig = Rig(tmp_path)
    engine, log = rig.engine, LatencyLog(rig.engine)
    engine.recover()
    t = engine.open_trade("ABC", D(3), D(10), D("9.80"), D(1))
    start = rig.now
    rig.advance(2)
    rig.broker.fill(rig.entry(), D(1), D(10))  # partial: the first fill is what is measured
    rig.advance(1)
    engine.step()  # +3 s: fill observed, stop for 1 share sent
    log.record()
    assert rig.store.journal(PROTECTION_LATENCY) == []  # not verified yet
    rig.broker.fill(rig.entry(), D(2), D(10))
    rig.advance(POLL)
    engine.step()  # one Detail read: the unverified stop goes first and is seen working
    log.record()
    body = rig.latency()
    def at(seconds: float) -> datetime:
        return start + timedelta(seconds=seconds)

    assert body["fill_observed_at"] == rig.store.journal("order-observed", rig.entry())[0].at_utc
    assert datetime.fromisoformat(body["fill_observed_at"]) == at(3)
    assert datetime.fromisoformat(body["stop_sent_at"]) == at(3)
    assert datetime.fromisoformat(body["stop_verified_at"]) == at(3 + POLL)
    assert body["fill_quantity"] == "1" and body["symbol"] == "ABC"
    assert (body["observed_to_sent_seconds"], body["observed_to_verified_seconds"]) == (
        0.0, POLL)
    assert body["fill_to_observed_seconds"] is None and body["fill_time"]
    assert body["closed_before_verified"] is False
    for _ in range(4):  # the second fill is protected too; no second row for this trade
        rig.advance(POLL)
        engine.step()
        log.record()
    assert len(rig.store.journal(PROTECTION_LATENCY, t)) == 1


def test_a_trade_closed_before_its_stop_was_seen_working_is_recorded_as_such(tmp_path):
    rig = Rig(tmp_path)
    engine, log = rig.engine, LatencyLog(rig.engine)
    engine.recover()
    engine.open_trade("ABC", D(1), D(10), D("9.80"), D(1))
    rig.broker.fill(rig.entry(), D(1), D(10))
    rig.advance(POLL)
    engine.step()  # fill observed, stop sent
    (stop,) = [o for o in rig.broker.open_orders() if o.order_type == "STOP_LOSS"]
    rig.broker.fill(stop.client_order_id, D(1), D("9.80"))  # gapped through before any read
    for _ in range(3):
        rig.advance(POLL)
        engine.step()
        log.record()
    body = rig.latency()
    assert body["closed_before_verified"] is True and body["stop_verified_at"] is None
    assert body["stop_sent_at"] is not None and body["observed_to_verified_seconds"] is None
    (trade,) = rig.service.status().trades_today
    assert trade.stop_confirmed_after_seconds is None  # never confirmed: unknown, not zero
    assert trade.planned_loss_usd == D(1)


def test_a_trade_never_filled_has_no_latency_row(tmp_path):
    rig = Rig(tmp_path)
    rig.actions[3] = lambda: rig.engine.cancel_entries()
    rig.run(6)
    assert rig.engine.trades() == []
    assert rig.store.journal(PROTECTION_LATENCY) == []


def test_no_duplicate_latency_row_across_restarts(tmp_path):
    rig = Rig(tmp_path)
    rig.actions[2] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.run(2)  # fill observed and stop sent, then a stop (the stop rests at the broker)
    assert rig.store.journal(PROTECTION_LATENCY) == []
    observed = rig.store.journal("order-observed", rig.entry())[0].at_utc
    rig.store.close()
    rig.open()  # restart: the stop is verified by the new run
    rig.evaluator.enter = False
    rig.run(3)
    assert rig.latency()["fill_observed_at"] == observed  # from the first run's journal
    rig.store.close()
    rig.open()
    rig.evaluator.enter = False
    rig.run(3)
    assert len(rig.store.journal(PROTECTION_LATENCY)) == 1


def test_a_failing_measurement_never_ends_the_loop(tmp_path):
    rig = Rig(tmp_path)

    def broken() -> None:
        raise KeyError("filled_quantity")

    rig.service.latency.record = broken  # type: ignore[union-attr,method-assign]
    rig.actions[2] = lambda: rig.broker.fill(rig.entry(), D(2), D(10))
    rig.run(5)
    (failed,) = rig.store.journal(service.LATENCY_FAILED)
    assert failed.body["error"] == "KeyError"
    assert rig.evaluator.calls == [1, 3, 4, 5]  # protection and the strategy went on


def _stopped_out_trade(rig: Rig) -> LatencyLog:
    """A fake-broker trade: 2 shares filled, stop seen working one poll later, stopped out."""
    engine, log = rig.engine, LatencyLog(rig.engine)
    engine.recover()
    engine.open_trade("ABC", D(2), D(10), D("9.80"), D("0.40"))
    rig.broker.fill(rig.entry(), D(2), D(10))
    rig.advance(1)
    engine.step()  # fill observed, stop sent
    rig.advance(POLL)
    engine.step()  # stop seen working
    log.record()
    (stop,) = [o for o in rig.broker.open_orders() if o.order_type == "STOP_LOSS"]
    rig.broker.fill(stop.client_order_id, D(2), D("9.80"))
    for _ in range(3):
        rig.advance(POLL)
        engine.step()
    return log


def test_status_trade_carries_planned_loss_and_stop_confirmation_time(tmp_path):
    """status-v1 trades_today: 1 R as recorded at entry, and the same fill-observed -> stop
    working time the protection-latency row records, from the journal of a fake-broker trade."""
    rig = Rig(tmp_path)
    _stopped_out_trade(rig)
    (trade,) = rig.service.status().trades_today
    assert trade.exit_reason and trade.planned_loss_usd == D("0.40")
    assert trade.stop_confirmed_after_seconds == D(str(POLL)).quantize(D("0.001"))
    assert float(trade.stop_confirmed_after_seconds) == (
        rig.latency()["observed_to_verified_seconds"])
    # The daily-loss path never reads the journal for it.
    (plain,) = service.trades_today(rig.engine, rig.now)
    assert plain.stop_confirmed_after_seconds is None
    assert replace(plain, stop_confirmed_after_seconds=trade.stop_confirmed_after_seconds) == trade


def test_a_malformed_fill_read_row_never_stops_the_loop_or_moves_the_daily_loss(tmp_path):
    rig = Rig(tmp_path)
    _stopped_out_trade(rig)
    loss = entries.realized_loss_today(rig.engine, rig.now)
    assert loss == D("0.40")
    (closed,) = rig.engine.trades(open_only=False)
    stop = next(o for o in rig.engine.orders(closed.trade_id) if o.leg is Leg.STOP)
    with rig.store.transaction():  # not something the engine writes: no filled_quantity
        rig.store.append("order-observed", {"state": "ACCEPTED"}, key=stop.client_order_id)
    rig.run(3)
    assert rig.passes == 3  # the loop went on
    status = api.loads(api.StatusV1, (tmp_path / "status.json").read_bytes())
    (trade,) = status.trades_today
    assert trade.stop_confirmed_after_seconds is None and trade.planned_loss_usd == D("0.40")
    assert status.realized_loss_today_usd == loss
    assert entries.realized_loss_today(rig.engine, rig.now) == loss
    (failed,) = rig.store.journal(service.STOP_TIMING_FAILED)  # once, not once per pass
    assert failed.key == closed.trade_id and failed.body["error"] == "MalformedJournal"
    rig.store.close()
    rig.open()  # a restart journals it no second time
    rig.run(2)
    assert len(rig.store.journal(service.STOP_TIMING_FAILED)) == 1

