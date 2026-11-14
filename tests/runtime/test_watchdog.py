from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aperture import config as config_module
from aperture.api import contract
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK
from aperture.cli import data_paths
from aperture.lock import SingleInstanceLock
from aperture.runtime import __main__ as runtime_main
from aperture.runtime import watchdog
from aperture.runtime.alerts import Alerter
from aperture.store import Store
from aperture.types import OrderDetail, OrderState, OrderType, Position, Side

MONDAY_OPEN = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)  # 09:30 ET
STATUS_EXAMPLE = Path(__file__).resolve().parents[1] / "api" / "fixtures" / "status-v1.example.json"
MORNING = datetime(2026, 11, 16, 14, 5, tzinfo=UTC)       # 09:05 ET, the job's start
FIRST = datetime(2026, 11, 16, 20, 57, 30, tzinfo=UTC)    # 15:55 ET flatten + 150 s
LAST = datetime(2026, 11, 16, 20, 59, tzinfo=UTC)         # 16:00 ET close - 60 s


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.sleeps = 0
        self.on_sleep = None

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.sleeps += 1
        self.now += timedelta(seconds=seconds)
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class Recorder:
    def __init__(self) -> None:
        self.shown: list[str] = []

    def notify(self, title: str, text: str) -> None:
        self.shown.append(f"{title} | {text}")


def trap(broker: FakeBroker) -> FakeBroker:
    """A broker whose trading requests fail the test if anyone calls them."""
    def boom(*_a, **_k):
        raise AssertionError("the watchdog sent a trading request")

    broker.place = boom  # type: ignore[method-assign]
    broker.cancel = boom  # type: ignore[method-assign]
    broker.replace_stop_price = boom  # type: ignore[method-assign]
    return broker


def stop_order() -> OrderDetail:
    return OrderDetail("ap-1-s", "ABCD", Side.SELL, OrderType.STOP_LOSS, Decimal(10), Decimal(0),
                       None, OrderState.ACCEPTED, "SUBMITTED", Decimal("9.5"))


@pytest.fixture
def cfg(config_path):
    return config_module.load(config_path)


def make(cfg, clock: Clock, connect, tmp_path: Path):
    database, lock_path = data_paths(cfg)
    recorder = Recorder()
    day = watchdog.market_day(cfg, clock())
    guard = watchdog.Watchdog(
        cfg, day, connect=connect, alerter=Alerter(cfg.alerts, "Aperture", recorder, clock),
        journal=watchdog.Journal(tmp_path / "watchdog.jsonl"), clock=clock, sleep=clock.sleep,
        lock_path=lock_path, database=database, status_path=status_path(cfg))
    return guard, recorder


def status_path(cfg) -> Path:
    return cfg.root / cfg.run.data_dir / cfg.api.status_file


def write_status(cfg, written_at: datetime) -> None:
    """The example status document, re-dated, written where the bot writes it."""
    example = contract.loads(contract.StatusV1, STATUS_EXAMPLE.read_bytes())
    contract.write_status(status_path(cfg), dataclasses.replace(example, written_at=written_at))


def write_pid(lock_path: Path, pid: int) -> None:
    """A lock file naming a live process, without holding the lock (a crashed bot's pid,
    reused after a reboot by another process)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(pid))


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_check_times_follow_the_calendar(cfg):
    normal = watchdog.market_day(cfg, MONDAY_OPEN)
    assert watchdog.check_times(cfg, normal) == (FIRST, LAST)
    half = watchdog.market_day(cfg, datetime(2026, 11, 27, 15, tzinfo=UTC))  # closes 13:00 ET
    first, last = watchdog.check_times(cfg, half)
    assert first.astimezone(NEW_YORK).strftime("%H:%M:%S") == "12:57:30"
    assert last.astimezone(NEW_YORK).strftime("%H:%M:%S") == "12:59:00"


def test_flat_broker_two_checks_no_alert(cfg, tmp_path):
    clock = Clock(MORNING)
    broker = trap(FakeBroker())
    guard, recorder = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(broker), tmp_path)
    assert guard.run() == 0
    assert recorder.shown == []
    checks = [r for r in records(tmp_path / "watchdog.jsonl") if r["record"] == watchdog.CHECK]
    assert [c["at"] for c in checks] == [FIRST.isoformat(), LAST.isoformat()]
    assert all(c["broker_read"] and c["positions"] == 0 for c in checks)
    assert broker.sent == [] and broker.replaced == []


def test_position_and_order_alert_at_both_checks_without_any_trading_request(cfg, tmp_path):
    broker = trap(FakeBroker())
    broker.holdings["ABCD"] = Position("ABCD", Decimal(10), Decimal(10))
    broker.orders["ap-1-s"] = stop_order()
    clock = Clock(MORNING)
    guard, recorder = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(broker), tmp_path)
    assert guard.run() == 1
    assert len(recorder.shown) == 2  # each text names its check time: not deduplicated
    assert all("watchdog-not-flat" in s and "position ABCD 10" in s
               and "order ap-1-s ABCD SELL STOP_LOSS" in s for s in recorder.shown)
    assert "15:57:30 ET" in recorder.shown[0] and "15:59:00 ET" in recorder.shown[1]
    assert broker.sent == [] and broker.replaced == []
    assert not hasattr(watchdog.ReadOnlyBroker(broker), "place")


def test_read_failure_alerts_and_connects_again_at_the_next_check(cfg, tmp_path):
    attempts = []

    def connect():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no network")
        return watchdog.ReadOnlyBroker(trap(FakeBroker()))

    clock = Clock(MORNING)
    guard, recorder = make(cfg, clock, connect, tmp_path)
    assert guard.run() == 1
    assert len(attempts) == 2
    assert len(recorder.shown) == 1 and "watchdog-cannot-check" in recorder.shown[0]
    assert "OSError" in recorder.shown[0]


def test_started_after_both_checks_runs_one_check_at_once(cfg, tmp_path):
    late = datetime(2026, 11, 16, 22, tzinfo=UTC)  # 17:00 ET: the stop has expired
    broker = trap(FakeBroker())
    broker.holdings["ABCD"] = Position("ABCD", Decimal(10), None)
    clock = Clock(late)
    guard, recorder = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(broker), tmp_path)
    assert guard.run() == 1
    assert clock.sleeps == 0 and len(recorder.shown) == 1


def _open_trade(database: Path) -> None:
    store = Store.open(database, busy_timeout_seconds=Decimal(5))
    store._db.execute(
        "INSERT INTO trades (trade_id, run_id, symbol, state, entry_quantity, entry_limit_price,"
        " stop_price, planned_loss_usd, opened_at) VALUES ('t1', 'r1', 'ABCD', 'open', '10',"
        " '10', '9.5', '5', '2026-11-16T15:00:00+00:00')")
    store.close()


def test_bot_down_with_an_open_trade_alerts_during_the_session(cfg, tmp_path):
    database, lock_path = data_paths(cfg)
    database.parent.mkdir(parents=True)
    _open_trade(database)
    before = database.read_bytes()
    clock = Clock(MORNING)
    guard, recorder = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(trap(FakeBroker())),
                           tmp_path)
    assert guard.run() == 1
    down = [s for s in recorder.shown if "bot-down-with-open-trade" in s]
    # Once per dedupe window over the session (09:30 to 15:59 ET), not every minute.
    session = (LAST - MONDAY_OPEN).total_seconds()
    assert 1 < len(down) <= session // float(cfg.alerts.dedupe_window_seconds) + 1
    assert database.read_bytes() == before  # read-only
    assert not lock_path.exists()  # the watchdog never creates or takes the bot's lock


def test_running_bot_with_fresh_status_stays_silent(cfg, tmp_path):
    database, lock_path = data_paths(cfg)
    database.parent.mkdir(parents=True)
    _open_trade(database)
    clock = Clock(datetime(2026, 11, 16, 20, 50, tzinfo=UTC))
    clock.on_sleep = lambda now: write_status(cfg, now)  # "the bot" writes every loop pass
    write_status(cfg, clock.now)
    guard, recorder = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(trap(FakeBroker())),
                           tmp_path)
    with SingleInstanceLock(lock_path):  # "the bot": the watchdog runs beside it
        assert guard.run() == 0
    assert recorder.shown == []


AT = datetime(2026, 11, 16, 18, tzinfo=UTC)  # 13:00 ET, in the session


@pytest.mark.parametrize("pid", [1, os.getpid()])
def test_alive_pid_with_stale_status_is_bot_down(cfg, tmp_path, pid):
    database, lock_path = data_paths(cfg)
    database.parent.mkdir(parents=True)
    _open_trade(database)
    write_pid(lock_path, pid)  # alive, but not our bot: reused after a crash, or a hung bot
    write_status(cfg, AT - timedelta(seconds=cfg.runtime.watchdog_status_stale_seconds + 1))
    guard, recorder = make(cfg, Clock(AT), None, tmp_path)
    guard.bot_check(AT)
    (shown,) = recorder.shown
    assert "bot-down-with-open-trade" in shown and "status file older than" in shown


@pytest.mark.parametrize("status,why", [
    (None, "no status file"),
    (b"{not json", "status file unreadable"),
    ("future", "status file dated in the future"),
    ("lead", None),  # written after the watchdog read its clock: not "future"
    ("fresh", None),
])
def test_alive_pid_needs_a_fresh_readable_status(cfg, tmp_path, status, why):
    _, lock_path = data_paths(cfg)
    write_pid(lock_path, os.getpid())
    lead = timedelta(seconds=float(cfg.risk.max_clock_skew_seconds))
    if status == "future":
        write_status(cfg, AT + lead + timedelta(milliseconds=1))
    elif status == "lead":
        write_status(cfg, AT + lead)
    elif status == "fresh":
        write_status(cfg, AT - timedelta(seconds=cfg.runtime.watchdog_status_stale_seconds))
    elif status is not None:
        status_path(cfg).write_bytes(status)
    guard, _ = make(cfg, Clock(AT), None, tmp_path)
    assert guard.not_running(AT) == why


def test_fresh_status_without_a_live_pid_is_not_running(cfg, tmp_path):
    write_status(cfg, AT)
    guard, _ = make(cfg, Clock(AT), None, tmp_path)
    assert guard.not_running(AT) == "no live pid in its lock file"


def test_no_database_means_no_open_trade(cfg, tmp_path):
    database, _ = data_paths(cfg)
    assert watchdog.open_trades(database) == 0
    assert not database.exists()


def test_cli_watchdog_on_a_weekend_exits_3_without_alert(config_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime_main, "WATCHDOG_NOW",
                        lambda: datetime(2026, 11, 14, 14, tzinfo=UTC))
    assert runtime_main.main(["--config", str(config_path), "watchdog"]) == 3
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["record"] for x in lines if x["record"] == "alert"] == []


def test_cli_watchdog_day_through_the_read_only_broker(config_path, monkeypatch, capsys):
    cfg = config_module.load(config_path)
    clock = Clock(datetime(2026, 11, 16, 20, 56, tzinfo=UTC))
    broker = trap(FakeBroker())
    broker.holdings["ABCD"] = Position("ABCD", Decimal(5), None)
    monkeypatch.setattr(runtime_main, "WATCHDOG_NOW", clock)
    monkeypatch.setattr(runtime_main, "WATCHDOG_SLEEP", clock.sleep)
    monkeypatch.setattr(runtime_main, "WATCHDOG_BROKER", lambda c, hook: broker)
    monkeypatch.setitem(os.environ, cfg.alerts.imessage_recipient_variable, "")
    assert runtime_main.main(["--config", str(config_path), "watchdog"]) == 1
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    kinds = [x["record"] for x in lines]
    assert kinds[0] == watchdog.CHANNEL_REFUSED
    assert lines[0]["cause"] == "alert-recipient-missing"
    assert kinds.count("alert") == 2 and broker.sent == []
    journal = cfg.root / cfg.run.data_dir / cfg.runtime.watchdog_journal_file
    assert [json.loads(x) for x in journal.read_text("utf-8").splitlines()] == lines
