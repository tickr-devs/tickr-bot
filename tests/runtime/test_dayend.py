"""Day end (spec 1, 9, 11): after flatten + grace, broker reads must show nothing of ours."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from baseline import load_baseline

from tickr.broker.base import BrokerError
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.engine.core import Engine, TradeState
from tickr.runtime import service
from tickr.runtime.alerts import Alerter, AlertKind
from tickr.runtime.dayend import DAY_END
from tickr.runtime.watch import ALERT, Watch
from tickr.store import Store

ROOT = Path(__file__).resolve().parents[2]
CFG = load_baseline()
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)
FLATTEN = CALENDAR.day_at(MONDAY).session_times(
    CFG.session.last_entry_minutes_before_close, CFG.session.flatten_minutes_before_close).flatten
POLL = float(CFG.orders.detail_poll_seconds)
GRACE_PASSES = int(float(CFG.session.flatten_grace_seconds) / POLL) + 5
WINDOW_PASSES = int(float(CFG.alerts.dedupe_window_seconds) / POLL)


class Market(FakeBroker):
    """Entries and exits fill on every tick (unless halted); stops rest. Reads can fail."""

    halted = False
    reads_fail = False

    def tick(self) -> None:
        for cid, order in list(self.orders.items()):
            if order.state.terminal or order.order_type == "STOP_LOSS" or self.halted:
                continue
            self.fill(cid, order.quantity - order.filled_quantity, D(10))

    def positions(self):
        if self.reads_fail:
            raise BrokerError("read failed")
        return super().positions()

    def open_orders(self):
        if self.reads_fail:
            raise BrokerError("read failed")
        return super().open_orders()


class Recorder:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def notify(self, title: str, text: str) -> None:
        self.shown.append((title, text))


class Day:
    def __init__(self, tmp_path: Path, broker: Market | None = None) -> None:
        self.tmp_path = tmp_path
        self.broker = broker or Market()
        self.now = FLATTEN - timedelta(seconds=10)
        self.clock = [0.0]
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                now=lambda: self.now)
        self.notifier = Recorder()
        self.start()

    def start(self) -> None:
        """A (re)started bot process on the same database and broker."""
        self.engine = Engine(self.store, self.broker, CFG, "day00001",
                             lambda: GateResult(True, "t"), clock=lambda: self.clock[0])
        watch = Watch(self.engine, CFG, Alerter(CFG.alerts, "t", self.notifier, lambda: self.now),
                      lambda: self.now)
        paths = service.ServicePaths(self.tmp_path / "status.json", self.tmp_path / "in",
                                     self.tmp_path / "done")
        self.service = service.Service(self.engine, now=lambda: self.now, calendar=CALENDAR,
                                       strategy=service.strategy_id(CFG), paths=paths,
                                       watch=watch)

    def open(self) -> str:
        t = self.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
        self.broker.fill(self.engine.orders(t)[0].client_order_id, D(2), D(10))
        for _ in range(3):
            self.engine.step()
        return t

    def sleep(self, seconds: float) -> None:
        self.clock[0] += seconds
        self.now += timedelta(seconds=seconds)
        self.broker.tick()

    def run(self, passes: int) -> None:
        left = [passes]

        def should_stop() -> bool:
            left[0] -= 1
            return left[0] < 0

        self.service.run(should_stop=should_stop, sleep=self.sleep,
                         monotonic=lambda: self.clock[0])

    def records(self) -> list[dict]:
        return [r.body for r in self.store.journal(DAY_END)]

    def not_flat_alerts(self) -> list[dict]:
        return [r.body for r in self.store.journal(ALERT)
                if r.body["kind"] == AlertKind.NOT_FLAT_AFTER_FLATTEN]


def test_flat_day_is_recorded_clean_without_an_alert(tmp_path):
    day = Day(tmp_path)
    t = day.open()
    day.run(GRACE_PASSES)
    assert day.engine.trade(t).state is TradeState.CLOSED
    (record,) = day.records()
    assert record == {
        "day": "2026-11-16", "broker_read": True, "broker_flat": True, "own_positions": 0,
        "own_open_orders": 0, "trades_today": 1, "trades_closed": 1, "open_trades": 0,
        "agree": True, "day_clean": True}
    assert day.not_flat_alerts() == []


def test_position_left_after_flatten_alerts_and_is_not_clean(tmp_path):
    day = Day(tmp_path)
    day.broker.halted = True  # the closing MARKET order never fills
    t = day.open()
    day.run(GRACE_PASSES)
    assert day.engine.trade(t).state is not TradeState.CLOSED
    (record,) = day.records()
    assert (record["broker_flat"], record["own_positions"], record["own_open_orders"],
            record["open_trades"], record["day_clean"]) == (False, 1, 1, 1, False)
    (alert,) = day.not_flat_alerts()
    assert "position ABC 2" in alert["text"] and "Webull app" in alert["text"]
    assert alert["delivery"] == "shown"
    # The same condition is not repeated inside the dedupe window, and is shown again after.
    day.run(WINDOW_PASSES - GRACE_PASSES)
    assert len(day.not_flat_alerts()) == 1
    day.run(GRACE_PASSES)
    assert [a["delivery"] for a in day.not_flat_alerts()] == ["shown", "shown"]
    assert len(day.records()) == 1


def test_failing_broker_reads_never_give_a_clean_day(tmp_path):
    day = Day(tmp_path)
    t = day.open()
    day.run(12)  # flatten reached, the trade closes
    assert day.engine.trade(t).state is TradeState.CLOSED
    day.broker.reads_fail = True
    day.run(GRACE_PASSES)
    (record,) = day.records()
    assert (record["broker_read"], record["broker_flat"], record["agree"],
            record["day_clean"]) == (False, None, False, None)
    assert day.not_flat_alerts() == []


def test_reads_from_before_the_flatten_time_do_not_count(tmp_path):
    day = Day(tmp_path)
    day.run(2)  # a good reconcile before the flatten time
    assert day.engine.broker_view is not None
    day.broker.reads_fail = True
    day.run(GRACE_PASSES)
    assert day.records()[0]["day_clean"] is None


def test_foreign_position_is_not_our_alert_but_the_day_is_not_clean(tmp_path):
    day = Day(tmp_path)
    day.broker.add_foreign_position("XYZ", D(5), D(3))
    day.run(GRACE_PASSES)
    (record,) = day.records()
    assert (record["broker_flat"], record["own_positions"], record["agree"],
            record["day_clean"]) == (False, 0, False, False)
    assert day.not_flat_alerts() == []


def test_day_end_is_recorded_once_per_day_across_restarts(tmp_path):
    day = Day(tmp_path)
    day.run(GRACE_PASSES)
    day.start()
    day.run(GRACE_PASSES)
    assert len(day.records()) == 1


def test_a_run_started_after_flatten_and_grace_records_a_known_clean_day(tmp_path):
    day = Day(tmp_path)
    day.now = FLATTEN + timedelta(seconds=float(CFG.session.flatten_grace_seconds) + 60)
    day.start()  # a restart past flatten + grace on a flat account
    day.run(GRACE_PASSES)  # flatten_at is the start itself; the grace counts from there
    (record,) = day.records()
    assert (record["broker_read"], record["agree"], record["day_clean"]) == (True, True, True)


def test_one_failed_read_at_the_grace_waits_for_a_known_view(tmp_path):
    day = Day(tmp_path)
    t = day.open()
    day.run(12)  # flatten reached, the trade closes
    assert day.engine.trade(t).state is TradeState.CLOSED
    day.broker.reads_fail = True
    recover_at = FLATTEN + timedelta(seconds=float(CFG.session.flatten_grace_seconds) + 3 * POLL)
    seen = []
    tick = day.sleep

    def sleep(seconds):
        tick(seconds)
        if day.broker.reads_fail and day.now >= recover_at:
            seen.append(day.records())  # past the grace with only failed reads: no row yet
            day.broker.reads_fail = False

    day.sleep = sleep
    day.run(GRACE_PASSES + 6)
    assert seen == [[]]
    (record,) = day.records()
    assert (record["broker_read"], record["agree"], record["day_clean"]) == (True, True, True)


def test_a_day_without_any_known_view_is_recorded_unknown_at_stop(tmp_path):
    day = Day(tmp_path)
    day.broker.reads_fail = True
    day.service.flatten_due()
    day.now = FLATTEN + timedelta(seconds=float(CFG.session.flatten_grace_seconds) + 1)
    day.service.flatten_due()
    assert day.records() == []  # not yet: a later read may still show the day
    day.service.day_end.finish()
    (record,) = day.records()
    assert (record["broker_read"], record["day_clean"]) == (False, None)
