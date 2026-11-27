from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from baseline import mac_text

from tickr import config as config_module
from tickr.api import contract
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK
from tickr.cli import data_paths
from tickr.lock import SingleInstanceLock
from tickr.runtime import __main__ as runtime_main
from tickr.runtime import stopmarker, watchdog
from tickr.runtime.alerts import Alerter
from tickr.store import Store
from tickr.types import OrderDetail, OrderState, OrderType, Position, Side

MONDAY_OPEN = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)  # 09:30 ET
STATUS_EXAMPLE = Path(__file__).resolve().parents[1] / "api" / "fixtures" / "status-v1.example.json"
MORNING = datetime(2026, 11, 16, 14, 5, tzinfo=UTC)       # 09:05 ET, the job's start
FIRST = datetime(2026, 11, 16, 20, 54, tzinfo=UTC)    # 15:45 ET flatten + 540 s
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


class Runs:
    """The injected command runner: records each command, never runs one."""

    def __init__(self, result: int | Exception = 0) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.result = result

    def __call__(self, command, timeout: float) -> int:
        self.calls.append((list(command), timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make(cfg, clock: Clock, connect, tmp_path: Path, runner: Runs | None = None):
    database, lock_path = data_paths(cfg)
    recorder = Recorder()
    day = watchdog.market_day(cfg, clock())
    guard = watchdog.Watchdog(
        cfg, day, connect=connect, alerter=Alerter(cfg.alerts, "Tickr", recorder, clock),
        journal=watchdog.Journal(tmp_path / "watchdog.jsonl"), clock=clock, sleep=clock.sleep,
        lock_path=lock_path, database=database, status_path=status_path(cfg),
        runner=runner if runner is not None else Runs())
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
    assert first.astimezone(NEW_YORK).strftime("%H:%M:%S") == "12:54:00"
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
    assert "15:54:00 ET" in recorder.shown[0] and "15:59:00 ET" in recorder.shown[1]
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


# Fixed ids: the default id of the second case is the process id, which differs in every
# pytest-xdist worker, and workers must collect the same test names.
@pytest.mark.parametrize("pid", [1, os.getpid()], ids=["pid 1", "this process"])
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
    clock = Clock(datetime(2026, 11, 14, 14, tzinfo=UTC))
    monkeypatch.setattr(runtime_main, "WATCHDOG_NOW", clock)
    monkeypatch.setattr(runtime_main, "WATCHDOG_SLEEP", clock.sleep)
    assert runtime_main.main(["--config", str(config_path), "watchdog"]) == 3
    # No session, but the sentinel still checks until the New York day ends.
    assert clock.now == datetime(2026, 11, 15, 5, tzinfo=UTC)
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
    # No channel (Telegram not set up), whatever the test host is.
    monkeypatch.setattr(runtime_main, "default_notifier", lambda cfg, **kwargs: None)
    assert runtime_main.main(["--config", str(config_path), "watchdog"]) == 1
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    kinds = [x["record"] for x in lines]
    assert kinds[:1] == [watchdog.CHANNEL_REFUSED]
    assert [x["cause"] for x in lines[:1]] == ["alert-channel-missing"]
    assert {x["delivery"] for x in lines if x["record"] == "alert"} == {"no-notifier"}
    assert kinds.count("alert") == 2 and broker.sent == []
    journal = cfg.root / cfg.run.data_dir / cfg.runtime.watchdog_journal_file
    assert [json.loads(x) for x in journal.read_text("utf-8").splitlines()] == lines


@pytest.mark.parametrize(("channel", "code"), [(True, 0), (False, 1)])
def test_cli_watchdog_without_a_channel_never_exits_0(config_path, monkeypatch, capsys,
                                                      channel, code):
    clock = Clock(datetime(2026, 11, 16, 20, 56, tzinfo=UTC))
    monkeypatch.setattr(runtime_main, "WATCHDOG_NOW", clock)
    monkeypatch.setattr(runtime_main, "WATCHDOG_SLEEP", clock.sleep)
    monkeypatch.setattr(runtime_main, "WATCHDOG_BROKER", lambda c, hook: trap(FakeBroker()))
    monkeypatch.setattr(runtime_main, "default_notifier",
                        lambda cfg, **kwargs: Recorder() if channel else None)
    # A flat broker: every check clears, so only a missing channel can make the exit non-zero.
    assert runtime_main.main(["--config", str(config_path), "watchdog"]) == code
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["record"] for x in lines].count("alert") == 0
    refused = any(x["record"] == watchdog.CHANNEL_REFUSED
                  and x.get("cause") == "alert-channel-missing" for x in lines)
    assert refused is not channel


# --- the one restart a day ----------------------------------------------------------------

FLATTEN = datetime(2026, 11, 16, 20, 45, tzinfo=UTC)  # 15:45 ET


@pytest.fixture(autouse=True)
def no_real_restart(monkeypatch):
    """No test ever runs launchctl: the CLI's runner fails the test if it is called."""
    def refuse(command, timeout):
        raise AssertionError(f"a test ran the restart command {command}")

    monkeypatch.setattr(runtime_main, "WATCHDOG_RUNNER", refuse)


def restart_at(cfg) -> datetime:
    return FLATTEN - timedelta(seconds=cfg.runtime.watchdog_restart_before_flatten_seconds)


def down_with_trade(cfg) -> None:
    database, _ = data_paths(cfg)
    database.parent.mkdir(parents=True, exist_ok=True)
    _open_trade(database)


def kinds(recorder: Recorder) -> list[str]:
    return [s.split(" | ")[0].split(": ")[1] for s in recorder.shown]


def test_restart_time_is_before_the_flatten_and_after_the_last_entry(cfg, tmp_path):
    guard, _ = make(cfg, Clock(AT), None, tmp_path)
    assert guard.restart_at == restart_at(cfg)
    last_entry = FLATTEN - timedelta(minutes=cfg.session.last_entry_minutes_before_close
                                     - cfg.session.flatten_minutes_before_close)
    assert last_entry < guard.restart_at < FLATTEN


@pytest.mark.parametrize("case,expected,started", [
    ("alive", [], False),
    ("down-flat", [], False),
    ("down-before", ["bot-down-with-open-trade"], False),
    ("down-after", ["watchdog-restarted-bot"], True),
    ("marker", ["watchdog-restart-refused"], False),
    ("restarted-today", ["bot-down-with-open-trade"], False),
    ("restarted-yesterday", ["watchdog-restarted-bot"], True),
    ("record-unreadable", ["watchdog-restart-refused"], False),
])
def test_restart_decision_table(cfg, tmp_path, case, expected, started):
    now = restart_at(cfg) - timedelta(seconds=1) if case == "down-before" else restart_at(cfg)
    if case != "down-flat":
        down_with_trade(cfg)
    if case == "alive":
        write_status(cfg, now)
    record = watchdog.restart_file(cfg)
    record.parent.mkdir(parents=True, exist_ok=True)
    if case == "marker":
        stopmarker.write(cfg, "cmd-1", now)
    elif case == "restarted-today":
        record.write_text('{"ny_date": "2026-11-16"}')
    elif case == "restarted-yesterday":
        record.write_text('{"ny_date": "2026-11-13"}')
    elif case == "record-unreadable":
        record.write_text("{not json")
    runs = Runs()
    guard, recorder = make(cfg, Clock(now), None, tmp_path, runs)
    _, lock_path = data_paths(cfg)
    if case == "alive":
        with SingleInstanceLock(lock_path):
            guard.bot_check(now)
    else:
        guard.bot_check(now)
    assert kinds(recorder) == expected
    assert bool(runs.calls) is started
    if started:
        ((command, timeout),) = runs.calls
        # The shipped (server) command: the restart request inside the watchdog's container.
        assert command == ["python", "-m", "tickr.runtime", "--config", "/app/config/bot.toml",
                           "request-restart", "--cause", "watchdog"]
        assert timeout == cfg.runtime.watchdog_restart_timeout_seconds
        assert watchdog.restarted_on(record) == date(2026, 11, 16)


def test_restart_happens_once_a_day_and_is_recorded_before_the_command(cfg, tmp_path):
    down_with_trade(cfg)
    record = watchdog.restart_file(cfg)
    seen = []

    def runner(command, timeout):
        seen.append(watchdog.restarted_on(record))  # durable before the command runs
        return 0

    now = restart_at(cfg)
    guard, recorder = make(cfg, Clock(now), None, tmp_path, runner)
    guard.bot_check(now)
    guard.bot_check(now + timedelta(seconds=60))
    assert seen == [date(2026, 11, 16)]
    assert kinds(recorder) == ["watchdog-restarted-bot", "bot-down-with-open-trade"]
    # A new watchdog process the same day (job restarted) reads the record: no second attempt.
    again, recorder = make(cfg, Clock(now), None, tmp_path, runner)
    again.bot_check(now + timedelta(seconds=120))
    assert seen == [date(2026, 11, 16)] and kinds(recorder) == ["bot-down-with-open-trade"]
    lines = [r for r in records(tmp_path / "watchdog.jsonl") if r["record"] == watchdog.RESTART]
    assert [(r["started"], r["command"][-1]) for r in lines] == [(True, "watchdog")]


@pytest.mark.parametrize("result,cause", [(1, "restart command exited 1"),
                                          (FileNotFoundError(), "FileNotFoundError"),
                                          (subprocess.TimeoutExpired("x", 1), "TimeoutExpired")])
def test_a_failed_restart_command_is_the_loud_alert_and_is_not_repeated(cfg, tmp_path, result,
                                                                        cause):
    down_with_trade(cfg)
    runs = Runs(result)
    now = restart_at(cfg)
    guard, recorder = make(cfg, Clock(now), None, tmp_path, runs)
    guard.bot_check(now)
    guard.bot_check(now + timedelta(seconds=60))
    assert len(runs.calls) == 1
    assert kinds(recorder) == ["watchdog-restart-failed", "bot-down-with-open-trade"]
    assert cause in recorder.shown[0] and "URGENT" in recorder.shown[0]


def test_unknown_calendar_day_refuses_the_restart(cfg, tmp_path):
    down_with_trade(cfg)
    runs = Runs()
    guard, recorder = make(cfg, Clock(restart_at(cfg)), None, tmp_path, runs)
    tomorrow = restart_at(cfg) + timedelta(days=1)
    assert guard.restart(tomorrow, "no status file", 1)
    assert runs.calls == [] and kinds(recorder) == ["watchdog-restart-refused"]
    assert "not the calendar day" in recorder.shown[0]
    assert not watchdog.restart_file(cfg).exists()


def test_still_down_or_not_flat_after_a_restart_is_the_loud_alert(cfg, tmp_path):
    """A full day: the bot dies with a trade, the watchdog starts it once, the started bot
    never comes up, and both later checks say so loudly (not watchdog-not-flat)."""
    down_with_trade(cfg)
    broker = trap(FakeBroker())
    broker.holdings["ABCD"] = Position("ABCD", Decimal(10), Decimal(10))
    runs = Runs()
    clock = Clock(datetime(2026, 11, 16, 20, 45, tzinfo=UTC))
    guard, recorder = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(broker), tmp_path, runs)
    assert guard.run() == 1
    assert len(runs.calls) == 1
    loud = [s for s in recorder.shown if "watchdog-restart-failed" in s]
    assert len(loud) == 2 and not any("watchdog-not-flat" in s for s in recorder.shown)
    assert all("position ABCD 10" in s and "bot still not running" in s for s in loud)
    assert broker.sent == [] and broker.replaced == []


def test_after_a_restart_a_flat_broker_and_running_bot_are_quiet(cfg, tmp_path):
    down_with_trade(cfg)
    _, lock_path = data_paths(cfg)
    clock = Clock(datetime(2026, 11, 16, 20, 45, tzinfo=UTC))
    lock = SingleInstanceLock(lock_path)

    def bot_comes_up(command, timeout):  # "launchd starts the bot": lock taken, status fresh
        lock.__enter__()
        clock.on_sleep = lambda now: write_status(cfg, now)
        write_status(cfg, clock.now)
        return 0

    guard, recorder = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(trap(FakeBroker())),
                           tmp_path, bot_comes_up)
    try:
        assert guard.run() == 1  # it alerted the restart
    finally:
        lock.__exit__(None, None, None)
    assert "watchdog-restarted-bot" in kinds(recorder)
    assert not {"watchdog-restart-failed", "watchdog-not-flat"} & set(kinds(recorder))


def test_restart_command_names_the_bot_job_label(config_path):
    """On the Mac: launchd's kickstart of the bot job, with this user's id."""
    config_path.write_text(mac_text(config_path.read_text("utf-8")), "utf-8")
    cfg = config_module.load(config_path)
    assert watchdog.restart_command(cfg)[2] == f"gui/{os.getuid()}/local.tickr.bot"
    template = (cfg.root / "ops" / "launchd" / "local.tickr.bot.plist.template").read_text()
    label = cfg.runtime.watchdog_restart_command[-1].rsplit("/", 1)[1]
    assert f"<key>Label</key>\n  <string>{label}</string>" in template
    assert "-k" not in cfg.runtime.watchdog_restart_command  # never kills a running job


def _resting_trade(database: Path, *, entry_state: str | None = "ACCEPTED",
                   rest: bool = True) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    store = Store.open(database, busy_timeout_seconds=Decimal(5))
    store._db.execute(
        "INSERT INTO trades (trade_id, run_id, symbol, state, entry_quantity, entry_limit_price,"
        " stop_price, planned_loss_usd, opened_at, entry_low_price, entry_rest_until) VALUES "
        "('t1', 'r1', 'ABCD', 'opening', '10', '9.9', '9.6', '3', '2026-11-16T15:00:00+00:00',"
        " '9.8', ?)", ("2026-11-16T15:06:00+00:00" if rest else None,))
    store._db.execute(
        "INSERT INTO orders (client_order_id, trade_id, leg, symbol, side, order_type, quantity,"
        " limit_price, send_state, broker_state, updated_at) VALUES ('ap-t1-e1', 't1', 'entry',"
        " 'ABCD', 'BUY', 'LIMIT', '10', '9.9', 'acknowledged', ?, '2026-11-16T15:00:00+00:00')",
        (entry_state,))
    store.close()


EARLY = datetime(2026, 11, 16, 15, 3, tzinfo=UTC)  # 10:03 ET, long before the restart time


@pytest.mark.parametrize("entry_state,rest,expected,started", [
    ("ACCEPTED", True, ["watchdog-restarted-bot"], True),
    ("PARTIALLY_FILLED", True, ["watchdog-restarted-bot"], True),
    (None, True, ["watchdog-restarted-bot"], True),  # sent, Detail not read yet
    ("CANCELLED", True, ["bot-down-with-open-trade"], False),
    ("ACCEPTED", False, ["bot-down-with-open-trade"], False),  # a marketable entry
])
def test_a_resting_entry_restarts_a_down_bot_at_once(cfg, tmp_path, entry_state, rest,
                                                     expected, started):
    database, _ = data_paths(cfg)
    _resting_trade(database, entry_state=entry_state, rest=rest)
    runs = Runs()
    guard, recorder = make(cfg, Clock(EARLY), None, tmp_path, runs)
    assert EARLY < guard.restart_at
    guard.bot_check(EARLY)
    assert kinds(recorder) == expected
    assert bool(runs.calls) is started
    if started:
        assert watchdog.restarted_on(watchdog.entry_restart_file(cfg)) == date(2026, 11, 16)
        assert not watchdog.restart_file(cfg).exists()  # the flatten restart stays available
        assert "cancels it at startup" in recorder.shown[0]


def test_the_early_restart_is_once_a_day_and_leaves_the_flatten_restart(cfg, tmp_path):
    database, _ = data_paths(cfg)
    _resting_trade(database)
    runs = Runs()
    guard, recorder = make(cfg, Clock(EARLY), None, tmp_path, runs)
    guard.bot_check(EARLY)
    guard.bot_check(EARLY + timedelta(minutes=1))
    assert len(runs.calls) == 1
    assert kinds(recorder)[-1] == "bot-down-with-open-trade"
    guard.bot_check(restart_at(cfg))
    assert len(runs.calls) == 2  # the flatten restart still happens
    assert watchdog.restarted_on(watchdog.restart_file(cfg)) == date(2026, 11, 16)


def test_the_early_restart_respects_the_operator_stop(cfg, tmp_path):
    database, _ = data_paths(cfg)
    _resting_trade(database)
    stopmarker.write(cfg, "cmd-1", EARLY)
    runs = Runs()
    guard, recorder = make(cfg, Clock(EARLY), None, tmp_path, runs)
    guard.bot_check(EARLY)
    assert kinds(recorder) == ["watchdog-restart-refused"] and runs.calls == []


# --- the sentinel: every hour, every day ------------------------------------------------------

def sentinel(cfg, clock: Clock, tmp_path: Path, free_mb: int | None = None):
    _, lock_path = data_paths(cfg)
    recorder = Recorder()
    free = (lambda path: 1 << 50) if free_mb is None else (
        lambda path: free_mb * watchdog.BYTES_PER_MB)
    guard = watchdog.Sentinel(
        cfg, alerter=Alerter(cfg.alerts, "Tickr", recorder, clock),
        journal=watchdog.Journal(tmp_path / "watchdog.jsonl"), lock_path=lock_path,
        status_path=status_path(cfg), data_dir=cfg.root / cfg.run.data_dir, free=free)
    return guard, recorder


def write_state(cfg, written_at: datetime, state: contract.BotState) -> None:
    example = contract.loads(contract.StatusV1, STATUS_EXAMPLE.read_bytes())
    contract.write_status(status_path(cfg), dataclasses.replace(
        example, written_at=written_at, bot_state=state))


SATURDAY_NIGHT = datetime(2026, 11, 22, 3, tzinfo=UTC)  # 22:00 ET Saturday: no session


def test_crash_at_any_hour_alerts_bot_down_once_per_crash(cfg, tmp_path):
    crashed = SATURDAY_NIGHT - timedelta(minutes=5)
    write_state(cfg, crashed, contract.BotState.RUNNING)  # a crash leaves "running" behind
    guard, recorder = sentinel(cfg, Clock(SATURDAY_NIGHT), tmp_path)
    guard.check(SATURDAY_NIGHT)
    assert recorder.shown == []  # one check is not enough (a Mac just woke up)
    guard.check(SATURDAY_NIGHT + timedelta(minutes=1))
    guard.check(SATURDAY_NIGHT + timedelta(minutes=2))
    (shown,) = recorder.shown
    assert "bot-down" in shown and "did not stop cleanly" in shown
    assert "its last status (running) at 21:55:00 ET" in shown
    # Started again, then crashed again: a new status, a new alert.
    write_state(cfg, SATURDAY_NIGHT + timedelta(minutes=10), contract.BotState.RUNNING)
    guard.check(SATURDAY_NIGHT + timedelta(minutes=20))
    guard.check(SATURDAY_NIGHT + timedelta(minutes=21))
    assert len(recorder.shown) == 2
    down = [r for r in records(tmp_path / "watchdog.jsonl") if r["record"] == watchdog.BOT_DOWN]
    assert len(down) == 2


def test_same_finding_new_process_no_second_alert(cfg, tmp_path):
    write_state(cfg, SATURDAY_NIGHT - timedelta(minutes=5), contract.BotState.RUNNING)
    shown = []
    for relaunch in range(3):  # launchd relaunches a watchdog that ended
        guard, recorder = sentinel(cfg, Clock(SATURDAY_NIGHT), tmp_path, free_mb=5000)
        at = SATURDAY_NIGHT + timedelta(minutes=relaunch)
        guard.check(at)
        guard.check(at + timedelta(seconds=30))
        shown += recorder.shown
    assert sorted(x.split(" | ")[0] for x in shown) == ["Tickr: bot-down", "Tickr: disk-low"]
    state = json.loads((cfg.root / cfg.run.data_dir
                        / cfg.runtime.watchdog_state_file).read_text("utf-8"))
    assert state["disk_level"] == cfg.runtime.disk_warn_free_mb


def test_state_that_cannot_be_written_still_alerts_once_per_process(cfg, tmp_path):
    write_state(cfg, SATURDAY_NIGHT - timedelta(minutes=5), contract.BotState.RUNNING)
    state = cfg.root / cfg.run.data_dir / cfg.runtime.watchdog_state_file
    state.mkdir()  # a folder where the file should be: unreadable and unwritable
    guard, recorder = sentinel(cfg, Clock(SATURDAY_NIGHT), tmp_path, free_mb=5000)
    for minute in range(3):
        guard.check(SATURDAY_NIGHT + timedelta(minutes=minute))
    assert len(recorder.shown) == 2  # bot-down and disk-low, each once
    failed = [r for r in records(tmp_path / "watchdog.jsonl")
              if r["record"] == watchdog.STATE_FAILED]
    assert failed and "read" in failed[0]


def test_hung_bot_with_a_live_pid_alerts_bot_down(cfg, tmp_path):
    _, lock_path = data_paths(cfg)
    write_pid(lock_path, os.getpid())
    write_state(cfg, AT - timedelta(seconds=cfg.runtime.watchdog_status_stale_seconds + 1),
                contract.BotState.RUNNING)
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    os.utime(lock_path, (0, 0))  # took its lock long ago: not starting
    guard.check(AT)
    guard.check(AT + timedelta(seconds=1))
    (shown,) = recorder.shown
    assert "bot-down" in shown and "alive but not answering (status file older than" in shown
    assert "It is hung" in shown and "start the bot job" not in shown


def test_running_bot_clean_stop_and_no_bot_ever_stay_quiet(cfg, tmp_path):
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    guard.check(AT)  # this data folder never had a bot
    _, lock_path = data_paths(cfg)
    write_pid(lock_path, os.getpid())
    write_state(cfg, AT, contract.BotState.RUNNING)
    guard.check(AT)  # running
    lock_path.write_text("")
    write_state(cfg, AT, contract.BotState.STOPPED)
    guard.check(AT + timedelta(hours=8))  # stopped cleanly (release, operator, SIGTERM)
    assert recorder.shown == []


def test_stopping_is_quiet_until_launchd_would_have_killed_it(cfg, tmp_path):
    write_state(cfg, AT, contract.BotState.STOPPING)
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    limit = timedelta(seconds=cfg.runtime.bot_exit_timeout_seconds)
    guard.check(AT + limit)
    assert recorder.shown == []
    guard.check(AT + limit + timedelta(seconds=1))
    guard.check(AT + limit + timedelta(seconds=2))
    (shown,) = recorder.shown
    assert "its last status (stopping)" in shown


def test_unreadable_status_of_a_stopped_bot_alerts_once(cfg, tmp_path):
    status_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    status_path(cfg).write_bytes(b"{not json")
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    for minute in range(3):
        guard.check(AT + timedelta(minutes=minute))
    (shown,) = recorder.shown
    assert "an unreadable status file" in shown


def test_low_disk_alerts_once_per_level_and_again_after_recovery(cfg, tmp_path):
    runtime = cfg.runtime
    free = {"mb": runtime.disk_warn_free_mb}
    clock = Clock(AT)
    guard, recorder = sentinel(cfg, clock, tmp_path)
    guard._free = lambda path: free["mb"] * watchdog.BYTES_PER_MB
    guard.check(AT)
    assert recorder.shown == []  # at the level: enough
    for mb in (runtime.disk_warn_free_mb - 1, runtime.min_free_disk_mb,
               runtime.min_free_disk_mb - 1, runtime.min_free_disk_mb - 2,
               runtime.disk_warn_free_mb - 1):
        free["mb"] = mb
        guard.check(AT)
    assert len(recorder.shown) == 2
    assert all("disk-low" in s for s in recorder.shown)
    assert f"below {runtime.disk_warn_free_mb} MB" in recorder.shown[0]
    assert f"below {runtime.min_free_disk_mb} MB" in recorder.shown[1]
    free["mb"] = runtime.disk_warn_free_mb
    guard.check(AT)  # recovered: re-armed
    free["mb"] = runtime.disk_warn_free_mb - 1
    clock.now = AT + timedelta(seconds=float(cfg.alerts.dedupe_window_seconds))
    guard.check(clock.now)
    assert len(recorder.shown) == 3


def test_disk_read_failure_is_journaled_not_guessed(cfg, tmp_path):
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)

    def broken(path):
        raise PermissionError("no")

    guard._free = broken
    guard.check(AT)
    assert recorder.shown == []
    (row,) = [r for r in records(tmp_path / "watchdog.jsonl") if r["record"] == watchdog.DISK]
    assert row["error"] == "PermissionError"


def test_session_day_run_keeps_the_sentinel_going_until_midnight(cfg, tmp_path):
    clock = Clock(MORNING)
    broker = trap(FakeBroker())
    guard, _ = make(cfg, clock, lambda: watchdog.ReadOnlyBroker(broker), tmp_path)
    dog, recorder = sentinel(cfg, clock, tmp_path)
    guard.sentinel = dog
    midnight = watchdog.next_ny_midnight(MORNING)
    assert midnight == datetime(2026, 11, 17, 5, tzinfo=UTC)

    def crash_at_night(now: datetime) -> None:
        if now >= datetime(2026, 11, 16, 22, tzinfo=UTC) and not status_path(cfg).exists():
            write_state(cfg, now, contract.BotState.RUNNING)  # 17:00 ET, then silence

    clock.on_sleep = crash_at_night
    assert guard.run(midnight) == 1
    assert clock.now == midnight
    (shown,) = recorder.shown
    assert "bot-down" in shown


def test_cli_watchdog_alerts_a_weekend_crash_and_low_disk(config_path, monkeypatch, capsys):
    cfg = config_module.load(config_path)
    clock = Clock(SATURDAY_NIGHT)
    write_state(cfg, SATURDAY_NIGHT - timedelta(hours=1), contract.BotState.RUNNING)
    monkeypatch.setattr(runtime_main, "WATCHDOG_NOW", clock)
    monkeypatch.setattr(runtime_main, "WATCHDOG_SLEEP", clock.sleep)
    monkeypatch.setattr(runtime_main, "WATCHDOG_DISK_FREE",
                        lambda path: (cfg.runtime.disk_warn_free_mb - 1) * watchdog.BYTES_PER_MB)
    monkeypatch.setattr(runtime_main, "default_notifier", lambda cfg, **kwargs: Recorder())
    assert runtime_main.main(["--config", str(config_path), "watchdog"]) == 3
    alerts = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    kinds = [x["kind"] for x in alerts if x["record"] == "alert"]
    assert sorted(kinds) == ["bot-down", "disk-low"]


@pytest.mark.parametrize("state", [contract.BotState.STARTING, contract.BotState.RUNNING])
def test_a_starting_bot_is_quiet_for_the_grace_then_hung(cfg, tmp_path, state):
    """A live bot whose status is "starting", or older than the process (the last run's
    "running", before its first write: token wait, startup reconcile)."""
    _, lock_path = data_paths(cfg)
    write_pid(lock_path, os.getpid())
    started = datetime.fromtimestamp(lock_path.stat().st_mtime, UTC)
    written = started if state is contract.BotState.STARTING else started - timedelta(hours=9)
    write_state(cfg, written, state)
    grace = timedelta(seconds=cfg.runtime.watchdog_start_grace_seconds)
    guard, recorder = sentinel(cfg, Clock(started), tmp_path)
    for at in (started + timedelta(seconds=61), started + timedelta(seconds=120), started + grace):
        guard.check(at)
    assert recorder.shown == []
    guard.check(started + grace + timedelta(seconds=1))
    guard.check(started + grace + timedelta(seconds=2))
    (shown,) = recorder.shown
    assert "alive but still starting" in shown and "It is hung" in shown
    assert "start the bot job" not in shown


def test_invalid_config_alerts_once_then_waits_instead_of_ending(config_path, monkeypatch,
                                                                capsys):
    text = re.sub(r"(?m)^watchdog_bot_check_seconds = .*$", "watchdog_bot_check_seconds = 0",
                  config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    days = 2
    monkeypatch.setattr(runtime_main, "WATCHDOG_HOLD",
                        lambda again: [again() for _ in range(days)])
    assert runtime_main.main(["--config", str(config_path), "watchdog"]) == 2
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    alerts = [x for x in out if x["record"] == "alert"]
    # Once at the start, then once a day while it waits; never per relaunch.
    assert len(alerts) == 1 + days
    assert all(a["kind"] == "watchdog-cannot-check" and "config invalid" in a["text"]
               for a in alerts)


def test_invalid_config_repeats_a_day_after_the_first_alert_not_at_midnight(monkeypatch):
    slept, calls = [], []

    class Stop(Exception):
        pass

    def sleep(seconds):
        if len(slept) == 2:
            raise Stop
        slept.append(seconds)

    monkeypatch.setattr(runtime_main.time, "sleep", sleep)
    with pytest.raises(Stop):
        runtime_main._hold_daily(lambda: calls.append(1))
    assert slept == [runtime_main.ONE_DAY.total_seconds()] * 2
    assert len(calls) == 2


# --- the API's device-session alerts, relayed to Telegram -------------------------------------

def feed(cfg) -> Path:
    return cfg.root / cfg.run.data_dir / cfg.alerts.feed_file


def feed_line(path: Path, kind: str, text: str, at: str = "2026-11-27T17:00:00Z") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": at, "title": f"Tickr sandbox {kind}", "text": text}) + "\n")


def test_device_session_lines_of_the_api_are_relayed_once(cfg, tmp_path):
    path = feed(cfg)
    feed_line(path, "device-session", "device 'old' signed in")  # before the first run
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    guard.relay()
    assert recorder.shown == []  # a first run starts at the end of the feed
    feed_line(path, "device-session", "device 'mac' signed in")
    feed_line(path, "disk-low", "not relayed")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    guard.relay()
    assert recorder.shown == ["Tickr: device-session | " + watchdog.RELAYED
                              + " device 'mac' signed in (at 2026-11-27T17:00:00Z)"]
    guard.relay()
    assert len(recorder.shown) == 1
    # A relaunched watchdog reads the offset back and relays nothing twice.
    again, recorder2 = sentinel(cfg, Clock(AT), tmp_path)
    again.relay()
    assert recorder2.shown == []


def test_the_relayed_copy_is_never_relayed_again(cfg, tmp_path):
    path = feed(cfg)
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    guard.relay()
    feed_line(path, "device-session", f"{watchdog.RELAYED} device 'mac' signed in")
    guard.relay()
    assert recorder.shown == []


def test_a_line_still_being_written_waits_and_a_replaced_feed_is_read_from_its_start(cfg, tmp_path):
    path = feed(cfg)
    feed_line(path, "disk-low", "x" * 200)
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    guard.relay()
    half = json.dumps({"at": "a", "title": "Tickr sandbox device-session", "text": "one"})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(half[:10])
    guard.relay()
    assert recorder.shown == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write(half[10:] + "\n")
    guard.relay()
    assert len(recorder.shown) == 1 and "one (at a)" in recorder.shown[0]
    path.write_text("", "utf-8")  # replaced by a shorter file
    feed_line(path, "device-session", "two")
    guard.relay()
    assert len(recorder.shown) == 2 and "two" in recorder.shown[1]


def test_check_relays_and_a_missing_feed_is_not_a_finding(cfg, tmp_path):
    guard, recorder = sentinel(cfg, Clock(AT), tmp_path)
    guard.relay()
    assert recorder.shown == [] and not feed(cfg).exists()
    guard.relay()  # still no feed: first run never recorded an offset, nothing to relay
    feed_line(feed(cfg), "device-session", "three")
    guard.check(AT)  # the first read of a feed starts at its end
    feed_line(feed(cfg), "device-session", "four")
    guard.check(AT)
    assert [s for s in recorder.shown if "device-session" in s][-1].endswith(
        "four (at 2026-11-27T17:00:00Z)")


def test_journal_writes_each_line_whole_under_its_lock(tmp_path, monkeypatch, capsys):
    """The watchdog loop and the Live Activity thread share one Journal: a line is printed and
    appended while the lock is held, so two writers never interleave."""
    journal = watchdog.Journal(tmp_path / "watchdog.jsonl")
    held = []
    real_print = print

    def checking_print(*args, **kwargs):
        held.append(journal._lock.locked())
        real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", checking_print)
    big = "x" * (1 << 16)  # longer than one buffered write

    def writer(name):
        for n in range(50):
            journal.write(name, {"n": n, "pad": big})

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("loop", "live")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert held and all(held)
    lines = (tmp_path / "watchdog.jsonl").read_text("utf-8").splitlines()
    assert sorted((json.loads(line)["record"], json.loads(line)["n"]) for line in lines) == \
        sorted((name, n) for name in ("loop", "live") for n in range(50))
