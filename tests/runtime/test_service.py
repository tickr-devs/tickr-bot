from __future__ import annotations

import json
import os
import shutil
import signal
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from baseline import load_baseline, write_baseline

from tickr import cli, config
from tickr.api import contract as api
from tickr.api import reasons
from tickr.broker.base import RateLimited
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.data.base import FakeMarketData
from tickr.engine.core import Engine, Fence, Leg, TradeState
from tickr.runtime import service, stopmarker
from tickr.runtime.watchdog import BYTES_PER_MB
from tickr.store import Store

ROOT = Path(__file__).resolve().parents[2]
CFG = load_baseline()
# The `[strategy] observers` line of the baseline config (tests/baseline.py).
SHIPPED_OBSERVERS = "observers = " + json.dumps(list(CFG.strategy.observers))
MONDAY_11 = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)


class Market(FakeBroker):
    """Entry and exit orders fill on every tick; stops rest."""

    def tick(self) -> None:
        for cid, order in list(self.orders.items()):
            if order.state.terminal or order.order_type == "STOP_LOSS":
                continue
            self.fill(cid, order.quantity - order.filled_quantity, D(10))


class Bot:
    def __init__(self, tmp_path: Path, broker: FakeBroker | None = None,
                 now: datetime = MONDAY_11) -> None:
        self.broker = broker or Market()
        self.now = now
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                now=lambda: self.now)
        self.clock = [0.0]
        self.engine = Engine(self.store, self.broker, CFG, "svc00001",
                             lambda: GateResult(True, "t"), clock=lambda: self.clock[0])
        self.paths = service.ServicePaths(tmp_path / "status.json", tmp_path / "cmd" / "inbox",
                                          tmp_path / "cmd" / "processed")
        self.service = service.Service(self.engine, now=lambda: self.now, calendar=CALENDAR,
                                       strategy=service.strategy_id(CFG), paths=self.paths)

    def command(self, kind: api.CommandKind, offset_seconds: int = 0,
                mode: api.StopMode | None = None, symbol: str | None = None) -> api.CommandV1:
        cmd = api.new_command(kind, self.now + timedelta(seconds=offset_seconds), "test",
                              symbol=symbol, mode=mode)
        api.write_command(self.paths.inbox, cmd)
        return cmd

    def sleep(self, seconds: float) -> None:
        self.clock[0] += seconds
        self.now += timedelta(seconds=seconds)
        if isinstance(self.broker, Market):
            self.broker.tick()

    def run(self, iterations: int) -> bool:
        left = [iterations]

        def should_stop() -> bool:
            left[0] -= 1
            return left[0] < 0

        return self.service.run(should_stop=should_stop, sleep=self.sleep,
                                monotonic=lambda: self.clock[0])

    def status(self) -> api.StatusV1:
        return api.read_status(self.paths.status)


def test_idle_run_writes_status_and_stops_cleanly(tmp_path):
    bot = Bot(tmp_path)
    assert bot.run(3)
    status = bot.status()
    assert status.bot_state is api.BotState.STOPPED
    assert status.strategy == service.strategy_id(CFG)
    assert status.position is None and status.trades_today == ()
    assert status.freshness.last_broker_read_at is not None
    assert bot.store.journal("shutdown-clean")


def test_entries_not_wired_is_the_block_reason_while_running(tmp_path):
    bot = Bot(tmp_path)
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    block = bot.service.status().entry_block
    gate = config.trading_gate(CFG)
    assert block == (service.NOT_WIRED if gate.open
                     else api.EntryBlock("trading_gate_closed",
                                         f"Trading gate closed: {gate.cause}.",
                                         params={"gate": "trading_disabled",
                                                 "environment": CFG.environment_name}))


class StubEvaluator:
    def __init__(self) -> None:
        self.watch = {"WAT"}
        self.calls: list[list[str]] = []

    def tick(self, market):
        market.discover()
        self.calls.append(market.candidates())


class StubMarket:
    def __init__(self) -> None:
        from tickr.runtime.market import StreamInbox

        self.inbox = StreamInbox()
        self.watch: set[str] = set()
        self.discovery = None
        self.ticks = 0
        self.discovers: list[bool] = []

    def tick(self, *, discover: bool = True) -> None:
        self.ticks += 1
        self.discovers.append(discover)

    def discover(self) -> None:
        self.discovers.append(True)

    def candidates(self) -> list[str]:
        found = self.discovery
        return [] if found is None else [c.instrument.symbol for c in found.candidates]

    def freshness(self, last_broker_read_at):
        return api.Freshness(last_broker_read_at, None, api.StreamHealth.OFF)


def test_with_an_evaluator_the_loop_runs_the_strategy_and_entries_are_open(tmp_path):
    from tickr.runtime.market import DiscoveryState

    bot = Bot(tmp_path)
    market, evaluator = StubMarket(), StubEvaluator()
    bot.service.market, bot.service.evaluator = market, evaluator  # type: ignore[assignment]
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    if config.trading_gate(CFG).open:
        assert bot.service.status().entry_block is None
    bot.run(1)
    assert evaluator.calls == [[]] and market.watch == {"WAT"}
    assert market.discovers == [False, True]  # discovery only through the evaluation

    class Candidate:
        class instrument:
            symbol = "CAN"

    market.discovery = DiscoveryState(bot.now, "x", (Candidate(),))  # type: ignore[arg-type]
    bot.run(1)
    assert evaluator.calls[-1] == ["CAN"] and market.ticks == 2


def test_later_entry_stop_command_wins_and_off_clears_only_that_fence(tmp_path):
    bot = Bot(tmp_path)
    bot.broker.add_foreign_position("XYZ", D(1), D(5))
    bot.engine.recover()
    bot.command(api.CommandKind.ENTRY_STOP_OFF, offset_seconds=-1)
    bot.command(api.CommandKind.ENTRY_STOP_ON, offset_seconds=-2)
    bot.service.process_commands()
    fences = bot.engine.fences()
    assert str(Fence.ENTRY_STOP) not in fences  # off was requested later
    assert "foreign-position:XYZ" in fences  # untouched
    bot.command(api.CommandKind.ENTRY_STOP_ON)
    bot.service.process_commands()
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()
    bot.engine.recover()  # survives a restart
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()
    assert bot.service.status().entry_stop
    assert list(bot.paths.inbox.iterdir()) == []
    assert len(list(bot.paths.processed.iterdir())) == 3
    kinds = [c.kind for c in bot.service.status().recent_commands]
    assert kinds == [api.CommandKind.ENTRY_STOP_ON, api.CommandKind.ENTRY_STOP_OFF,
                     api.CommandKind.ENTRY_STOP_ON]


def test_journaled_command_is_retired_without_acting_again(tmp_path):
    bot = Bot(tmp_path)
    cmd = bot.command(api.CommandKind.ENTRY_STOP_ON)
    with bot.store.transaction():  # crash after journal, before act and retire
        bot.store.append(service.COMMAND, {
            "kind": cmd.kind, "requested_at": api.format_time(cmd.requested_at),
            "source": "test", "outcome": "done", "cause": None}, key=cmd.command_id)
    bot.service.process_commands()
    assert str(Fence.ENTRY_STOP) not in bot.engine.fences()
    assert list(bot.paths.inbox.iterdir()) == []
    assert len(bot.store.journal(service.COMMAND)) == 1


def test_expired_command_is_refused_never_executed(tmp_path):
    bot = Bot(tmp_path)
    bot.command(api.CommandKind.ENTRY_STOP_ON,
                offset_seconds=-int(CFG.api.command_max_age_seconds) - 1)
    bot.service.process_commands()
    assert str(Fence.ENTRY_STOP) not in bot.engine.fences()
    (refused,) = bot.store.journal(service.COMMAND_FILE_REFUSED)
    assert "expired" in refused.body["cause"]


def opened(bot: Bot, fill: bool = True) -> str:
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    if fill:
        bot.broker.fill(bot.engine.orders(t)[0].client_order_id, D(2), D(10))
        for _ in range(3):
            bot.engine.step()
    return t


def test_flatten_command_closes_through_the_exit_path(tmp_path):
    bot = Bot(tmp_path)
    t = opened(bot)
    bot.service.state = api.BotState.RUNNING
    position = bot.service.status().position
    assert position is not None and position.stop.state is api.StopState.VERIFIED
    bot.command(api.CommandKind.FLATTEN)
    assert bot.run(10)
    trade = bot.engine.trade(t)
    assert trade.state is TradeState.CLOSED and trade.exit_requested == "operator-flatten"
    assert bot.broker.positions() == []
    (done,) = bot.service.status().trades_today
    assert (done.quantity, done.exit_reason, done.result_gross_usd) == (
        D(2), "operator-flatten", D(0))
    assert done.strategy is None  # opened by hand here: no strategy journaled


def test_a_closed_trade_in_status_names_the_strategy_that_opened_it(tmp_path):
    bot = Bot(tmp_path)
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1),
                              strategy=("price-action-v1", "3"))
    bot.broker.fill(bot.engine.orders(t)[0].client_order_id, D(2), D(10))
    for _ in range(3):
        bot.engine.step()
    bot.service.state = api.BotState.RUNNING
    bot.command(api.CommandKind.FLATTEN)
    assert bot.run(10)
    (done,) = bot.service.status().trades_today
    assert done.strategy == api.StrategyId("price-action-v1", "3")


def test_flatten_refused_while_a_close_waits_for_the_operator(tmp_path):
    bot = Bot(tmp_path)
    t = opened(bot)
    with bot.store.transaction():
        bot.engine._fence_row(Fence.EXIT_REFUSED, t, "test")
    cmd = bot.command(api.CommandKind.FLATTEN)
    bot.service.process_commands()
    (rec,) = bot.service.status().recent_commands
    assert (rec.command_id, rec.outcome) == (cmd.command_id, api.CommandOutcome.REFUSED)
    assert not bot.engine.trade(t).exit_requested


def test_cancel_own_orders_cancels_entries_never_stops(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = bot.engine.orders(t)[0].client_order_id
    bot.broker.fill(entry, D(1), D(10))  # partial: one share protected
    for _ in range(3):
        bot.engine.step()
    bot.command(api.CommandKind.CANCEL_OWN_ORDERS)
    bot.service.process_commands()
    assert bot.broker.orders[entry].state.terminal
    stops = [o for o in bot.broker.open_orders() if o.order_type == "STOP_LOSS"]
    assert sum(o.quantity for o in stops) == D(1)


def test_status_lists_a_working_entry_until_any_part_fills(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    bot.engine.step()
    (working,) = bot.service.status().working_entries
    assert (working.symbol, working.quantity, working.limit_price, working.stop_price,
            working.planned_loss_usd, working.cancel_requested) == (
        "ABC", D(2), D(10), D("9.80"), D(1), False)
    assert working.entry.sent_at is not None
    assert bot.service.status().positions == ()
    bot.broker.fill(bot.engine.orders(t)[0].client_order_id, D(1), D(10))
    for _ in range(3):
        bot.engine.step()
    status = bot.service.status()
    assert status.working_entries == () and [p.symbol for p in status.positions] == ["ABC"]


def test_cancel_own_orders_for_one_symbol_cancels_only_that_entry(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    wider = replace(CFG, risk=replace(CFG.risk, max_open_positions=2))
    bot.engine.config = bot.service.config = wider
    abc = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    xyz = bot.engine.open_trade("XYZ", D(2), D(20), D("19.80"), D(1))
    bot.engine.step()
    cmd = bot.command(api.CommandKind.CANCEL_OWN_ORDERS, symbol="ABC")
    bot.service.process_commands()
    (rec,) = bot.service.status().recent_commands
    assert (rec.command_id, rec.outcome) == (cmd.command_id, api.CommandOutcome.DONE)
    status = bot.service.status()
    assert {w.symbol: w.cancel_requested for w in status.working_entries} == {
        "ABC": True, "XYZ": False}
    for _ in range(3):
        bot.engine.step()
    assert bot.broker.orders[bot.engine.orders(abc)[0].client_order_id].state.terminal
    assert not bot.broker.orders[bot.engine.orders(xyz)[0].client_order_id].state.terminal
    assert [w.symbol for w in bot.service.status().working_entries] == ["XYZ"]


def test_cancel_own_orders_for_a_symbol_without_a_working_entry_is_refused(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    xyz = bot.engine.open_trade("XYZ", D(2), D(20), D("19.80"), D(1))
    bot.engine.step()
    cmd = bot.command(api.CommandKind.CANCEL_OWN_ORDERS, symbol="ABC")
    bot.service.process_commands()
    (rec,) = bot.service.status().recent_commands
    assert (rec.command_id, rec.outcome, rec.cause) == (
        cmd.command_id, api.CommandOutcome.REFUSED, "no working entry in ABC")
    assert not bot.broker.orders[bot.engine.orders(xyz)[0].client_order_id].state.terminal


def test_session_flatten_time_exits_open_trades(tmp_path):
    times = CALENDAR.day_at(MONDAY_11).session_times(
        CFG.session.last_entry_minutes_before_close, CFG.session.flatten_minutes_before_close)
    bot = Bot(tmp_path, now=times.flatten - timedelta(seconds=10))
    t = opened(bot)
    assert bot.run(12)
    assert bot.engine.trade(t).exit_requested == "session-flatten"
    assert bot.engine.trade(t).state is TradeState.CLOSED


def test_no_calendar_flattens_at_once(tmp_path):
    bot = Bot(tmp_path)
    bot.service.calendar = None
    t = opened(bot)
    assert bot.run(6)
    assert bot.engine.trade(t).exit_requested == "session-flatten"


def test_sigterm_drain_leaves_the_stop_resting_and_cancels_the_entry(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = bot.engine.orders(t)[0].client_order_id
    bot.broker.fill(entry, D(1), D(10))
    assert bot.run(4)
    assert bot.broker.orders[entry].state.terminal
    assert [o.order_type for o in bot.broker.open_orders()] == ["STOP_LOSS"]
    status = bot.status()
    assert status.bot_state is api.BotState.STOPPED
    assert status.position is not None and status.position.quantity == D(1)


def copy_config(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    write_baseline(tmp_path / "config" / "bot.toml")
    calendar_dir = tmp_path / Path(CFG.session.calendar_file).parent
    calendar_dir.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / Path(CFG.session.calendar_file).parent).iterdir():
        (calendar_dir / f.name).write_bytes(f.read_bytes())
    return tmp_path / "config" / "bot.toml"


def test_cli_run_stops_on_sigterm(tmp_path, monkeypatch):
    bot_toml = copy_config(tmp_path)
    broker = FakeBroker()
    market = FakeMarketData()
    started = []
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        broker, market, lambda: started.append("start"), lambda: started.append("stop")))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    sleeps = [0]

    def sleep(seconds):
        sleeps[0] += 1
        if sleeps[0] == 2:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "SLEEP", sleep)
    previous = signal.getsignal(signal.SIGTERM)
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    assert signal.getsignal(signal.SIGTERM) == previous
    assert started == ["start", "stop"]
    cfg = config.load(bot_toml)
    paths = service.ServicePaths.from_config(cfg)
    assert api.read_status(paths.status).bot_state is api.BotState.STOPPED
    database, _ = cli.data_paths(cfg)
    store = Store.open(database, busy_timeout_seconds=0)
    assert store.last_run()["end_cause"] == "stopped (SIGTERM)"
    store.close()


def test_cli_run_marks_its_start_and_stop_in_the_capture(tmp_path, monkeypatch):
    from tickr.data.capture import EVENTS_FILE, CaptureWriter
    bot_toml = copy_config(tmp_path)
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    # On a fake clock the discovery pass runs in place (cli.DISCOVERY_SPAWN). On its own thread
    # it races the SIGTERM: a pass that has not sent its first screener read when the stop
    # lands is cancelled and marks all three kinds `screener-skipped` (DiscoveryCancelled).
    monkeypatch.setattr(cli, "DISCOVERY_SPAWN", cli.run_in_place)
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    cfg = config.load(bot_toml)
    capture = CaptureWriter.from_config(cfg, FakeMarketData().provider)
    path = capture.path(MONDAY_11.astimezone(NEW_YORK).date(), EVENTS_FILE)
    events = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert [e["event"] for e in events] == ["bot-start", "bot-stop"]
    assert events[0]["run_id"] == events[1]["run_id"]
    assert events[1]["cause"] == "stopped (SIGTERM)"


def test_a_bot_start_marker_not_written_never_keeps_the_bot_from_running(tmp_path,
                                                                         monkeypatch):
    from tickr.data.capture import CaptureError, CaptureWriter
    from tickr.runtime.market import CAPTURE_FAILED
    bot_toml = copy_config(tmp_path)
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    passes = []

    def sleep(seconds):
        passes.append(seconds)
        os.kill(os.getpid(), signal.SIGTERM)

    def refuse(self, name, at, **fields):
        raise CaptureError(f"events.jsonl: disk full ({name})")

    monkeypatch.setattr(cli, "SLEEP", sleep)
    monkeypatch.setattr(CaptureWriter, "event", refuse)
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    assert passes  # the loop ran
    database, _ = cli.data_paths(config.load(bot_toml))
    store = Store.open(database, busy_timeout_seconds=0)
    (row,) = store.journal(CAPTURE_FAILED)
    assert row.body["what"] == "bot-start" and row.body["error"] == "CaptureError"
    assert store.last_run()["end_cause"] == "stopped (SIGTERM)"
    store.close()


def test_cli_run_without_a_loadable_strategy_protects_but_never_enters(tmp_path, monkeypatch):
    bot_toml = copy_config(tmp_path)
    strategy_toml = tmp_path / "strategies" / CFG.strategy.active / "strategy.toml"
    strategy_toml.write_text(strategy_toml.read_text().replace(
        "relative_volume_sessions = 5", "relative_volume_sessions = -5"))
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    database, _ = cli.data_paths(config.load(bot_toml))
    store = Store.open(database, busy_timeout_seconds=0)
    (row,) = store.journal(service.STRATEGY_NOT_LOADED)
    assert "relative_volume_sessions" in row.body["cause"]
    store.close()


def test_cli_run_with_too_little_history_protects_but_never_enters(tmp_path, monkeypatch):
    bot_toml = copy_config(tmp_path)
    bot_toml.write_text(bot_toml.read_text().replace("history_sessions = 5",
                                                     "history_sessions = 4", 1))
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    database, _ = cli.data_paths(config.load(bot_toml))
    store = Store.open(database, busy_timeout_seconds=0)
    (row,) = store.journal(service.STRATEGY_NOT_LOADED)
    assert "needs 5 prior sessions, [market_data] history_sessions = 4" in row.body["cause"]
    store.close()


def test_cli_run_with_a_broken_observer_keeps_entries_on(tmp_path, monkeypatch):
    bot_toml = copy_config(tmp_path)
    bot_toml.write_text(bot_toml.read_text().replace(SHIPPED_OBSERVERS,
                                                     'observers = ["missing-folder"]', 1))
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    database, _ = cli.data_paths(config.load(bot_toml))
    store = Store.open(database, busy_timeout_seconds=0)
    (row,) = store.journal(service.OBSERVERS_NOT_LOADED)
    assert "missing-folder" in row.body["cause"]
    assert store.journal(service.STRATEGY_NOT_LOADED) == []
    assert [a.body["kind"] for a in store.journal("alert")] == ["observers-stopped"]
    store.close()


def test_cli_run_alerts_and_journals_a_pending_token(tmp_path, monkeypatch):
    bot_toml = copy_config(tmp_path)

    def adapters(cfg, on_error, on_token_pending):
        on_token_pending()  # what the SDK transport does before it waits for the owner
        return cli.RunAdapters(FakeBroker(), FakeMarketData(), lambda: None, lambda: None)

    monkeypatch.setattr(cli, "RUN_ADAPTERS", adapters)
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    cfg = config.load(bot_toml)
    database, _ = cli.data_paths(cfg)
    store = Store.open(database, busy_timeout_seconds=0)
    (row,) = store.journal("alert")
    assert row.body["kind"] == "token-needs-confirmation"
    assert str(cfg.environment.token_check_duration_seconds) in row.body["text"]
    assert row.run_id == store.last_run()["run_id"]
    store.close()


def test_strategy_not_loaded_is_the_block_reason(tmp_path):
    bot = Bot(tmp_path)
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    bot.service.entries_off = "values refused"
    if config.trading_gate(CFG).open:
        assert bot.service.status().entry_block == api.EntryBlock(
            "strategy_not_loaded", "Active strategy not loaded: values refused")


def test_an_evaluation_exception_is_journaled_and_the_loop_goes_on(tmp_path):
    bot = Bot(tmp_path)

    class Broken(StubEvaluator):
        def tick(self, candidates):
            raise KeyError("bug")

    bot.service.market = StubMarket()  # type: ignore[assignment]
    bot.service.evaluator = Broken()  # type: ignore[assignment]
    assert bot.run(2)
    rows = bot.store.journal(service.EVALUATION_FAILED)
    assert len(rows) == 2 and rows[0].body["error"] == "KeyError"
    assert bot.service.market.watch == {"WAT"}


def test_cancel_own_orders_journaled_but_not_applied_is_finished_on_replay(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = bot.engine.orders(t)[0].client_order_id
    cmd = bot.command(api.CommandKind.CANCEL_OWN_ORDERS)
    with bot.store.transaction():  # crash after the record, before the cancel
        bot.store.append(service.COMMAND, {
            "kind": cmd.kind, "requested_at": api.format_time(cmd.requested_at),
            "source": "test", "outcome": "done", "cause": None}, key=cmd.command_id)
    bot.service.process_commands()
    assert bot.broker.orders[entry].state.terminal
    assert len(bot.store.journal(service.COMMAND)) == 1
    assert bot.store.journal(service.COMMAND_EFFECT, cmd.command_id)


def test_flatten_record_and_exit_request_are_one_write(tmp_path):
    bot = Bot(tmp_path)
    t = opened(bot)
    bot.command(api.CommandKind.FLATTEN)
    real = bot.engine.exit_request_row

    def crash(*args):
        real(*args)
        raise RuntimeError("crash inside the transaction")

    bot.engine.exit_request_row = crash
    with pytest.raises(RuntimeError):
        bot.service.process_commands()
    assert not bot.store.journal(service.COMMAND)  # rolled back together
    assert not bot.engine.trade(t).exit_requested
    bot.engine.exit_request_row = real
    bot.service.process_commands()  # file still in the inbox: acted on now
    assert bot.engine.trade(t).exit_requested == "operator-flatten"


@pytest.mark.parametrize("kind", ["missing", "text", "raises"])
def test_cli_run_with_a_misbehaving_strategy_protects_but_never_enters(tmp_path, monkeypatch,
                                                                       break_strategy, kind):
    bot_toml = copy_config(tmp_path)
    break_strategy(kind)
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    database, _ = cli.data_paths(config.load(bot_toml))
    store = Store.open(database, busy_timeout_seconds=0)
    (row,) = store.journal(service.STRATEGY_NOT_LOADED)
    assert row.body["cause"]
    store.close()


def test_status_blocks_entries_when_the_daily_room_is_below_one_trade(tmp_path):
    if not config.trading_gate(CFG).open:
        pytest.skip("the configured trading gate is closed")
    broker = FakeBroker()
    bot = Bot(tmp_path, broker)
    bot.service.evaluator = object()  # type: ignore[assignment]  # entries wired
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    assert bot.service.status().entry_block is None
    # One losing trade today that leaves less room than min_daily_room_usd.
    t = bot.engine.open_trade("ABC", D(10), D(40), D(30), D(100))
    (entry,) = bot.engine.orders(t)
    broker.fill(entry.client_order_id, D(10), D(40))
    bot.engine.step()  # stop placed
    bot.engine.step()  # stop verified
    lose = (CFG.risk.max_daily_loss_usd - CFG.risk.min_daily_room_usd) / 10 + 1
    bot.engine.request_exit(t, "test")
    for _ in range(8):
        bot.engine.step()
        for order in bot.engine.orders(t):
            if order.order_type == "MARKET" and order.live:
                broker.fill(order.client_order_id, D(10), D(40) - lose)
    assert bot.engine.trades() == []
    block = bot.service.status().entry_block
    assert block is not None and block.cause == "daily_ceiling"
    assert block.params == {"limit_usd": str(CFG.risk.max_daily_loss_usd),
                            "min_room_usd": str(CFG.risk.min_daily_room_usd)}


# ---------------------------------------------------------------- flatten one symbol


def two_trades(bot: Bot) -> tuple[str, str]:
    from dataclasses import replace

    wider = replace(CFG, risk=replace(CFG.risk, max_open_positions=2))
    bot.engine.config = bot.service.config = wider
    first = opened(bot)
    second = bot.engine.open_trade("XYZ", D(2), D(10), D("9.80"), D(1))
    bot.broker.fill(bot.engine.orders(second)[0].client_order_id, D(2), D(10))
    for _ in range(3):
        bot.engine.step()
    return first, second


def test_flatten_with_a_symbol_closes_only_that_position(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    abc, xyz = two_trades(bot)
    cmd = api.new_command(api.CommandKind.FLATTEN, bot.now, "test", symbol="XYZ")
    api.write_command(bot.paths.inbox, cmd)
    bot.service.process_commands()
    assert bot.engine.trade(xyz).exit_requested == "operator-flatten"
    assert bot.engine.trade(abc).exit_requested is None
    (rec,) = bot.service.status().recent_commands
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.DONE, None)


def test_flatten_of_a_symbol_without_a_position_is_refused(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    t = opened(bot)
    cmd = api.new_command(api.CommandKind.FLATTEN, bot.now, "test", symbol="ZZZ")
    api.write_command(bot.paths.inbox, cmd)
    bot.service.process_commands()
    (rec,) = bot.service.status().recent_commands
    assert (rec.command_id, rec.outcome) == (cmd.command_id, api.CommandOutcome.REFUSED)
    assert rec.cause == "no open position in ZZZ"
    assert bot.engine.trade(t).exit_requested is None
    assert list(bot.paths.inbox.iterdir()) == []


# ---------------------------------------------------------------- stop-bot


@pytest.mark.parametrize("fill", [D(0), D(1)])  # an entry working with no fill; partly filled
def test_stop_bot_with_a_trade_open_is_refused_and_changes_nothing(tmp_path, fill):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = bot.engine.orders(t)[0].client_order_id
    if fill:
        bot.broker.fill(entry, fill, D(10))  # the rest of the entry still works
    for _ in range(3):
        bot.engine.step()
    before = sorted((o.client_order_id, o.state) for o in bot.broker.open_orders())
    cmd = bot.command(api.CommandKind.STOP_BOT)
    bot.service.process_commands()
    assert bot.service.stopped_by is None
    assert stopmarker.describe(bot.service.config) is None
    assert sorted((o.client_order_id, o.state) for o in bot.broker.open_orders()) == before
    assert not bot.broker.orders[entry].state.terminal
    assert bot.engine.trade(t).exit_requested is None
    assert str(Fence.ENTRY_STOP) not in bot.engine.fences()
    (rec,) = bot.service.status().recent_commands
    assert (rec.command_id, rec.outcome) == (cmd.command_id, api.CommandOutcome.REFUSED)
    assert rec.cause == ("1 trade(s) open or entering; flatten first (or send emergency), "
                         "then stop-bot")
    assert list(bot.paths.inbox.iterdir()) == []
    later = bot.command(api.CommandKind.ENTRY_STOP_ON)  # the bot keeps obeying commands
    bot.service.process_commands()
    records = {r.command_id: r for r in bot.service.status().recent_commands}
    assert records[later.command_id].outcome is api.CommandOutcome.DONE


def test_stop_bot_after_emergency_cancelled_an_unfilled_entry_is_obeyed(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = opened(bot, fill=False)  # an entry working, nothing bought
    entry = bot.engine.orders(t)[0].client_order_id
    bot.command(api.CommandKind.EMERGENCY)
    bot.service.process_commands()
    for _ in range(3):
        bot.engine.step()
    assert bot.broker.orders[entry].state.terminal
    assert bot.engine.trade(t).state is TradeState.CLOSED
    bot.now += timedelta(seconds=1)
    cmd = bot.command(api.CommandKind.STOP_BOT)
    assert bot.run(1000)  # ends on the command, long before the iterations run out
    assert bot.service.stopped_by == cmd.command_id
    assert bot.broker.open_orders() == [] and bot.broker.positions() == []
    assert bot.store.journal("shutdown-clean")
    status = bot.status()
    assert status.bot_state is api.BotState.STOPPED
    records = {r.command_id: r for r in status.recent_commands}
    assert (records[cmd.command_id].outcome, records[cmd.command_id].cause) == (
        api.CommandOutcome.DONE, None)
    stopped = stopmarker.describe(bot.service.config)
    assert stopped is not None and cmd.command_id in stopped


def test_stop_bot_ends_the_pass_before_the_strategy_runs(tmp_path):
    bot = Bot(tmp_path)
    bot.engine.config = bot.service.config = config.load(ROOT / "config" / "bot.toml",
                                                          root=tmp_path)
    evaluator = StubEvaluator()
    bot.service.market, bot.service.evaluator = StubMarket(), evaluator  # type: ignore[assignment]
    bot.command(api.CommandKind.STOP_BOT)
    assert bot.run(5)
    assert evaluator.calls == []


def test_cli_run_obeys_stop_bot_then_refuses_until_cleared(tmp_path, monkeypatch, capsys):
    bot_toml = copy_config(tmp_path)
    cfg = config.load(bot_toml)
    paths = service.ServicePaths.from_config(cfg)
    cmd = api.new_command(api.CommandKind.STOP_BOT, MONDAY_11, "test")
    api.write_command(paths.inbox, cmd)
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: None)
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    database, _ = cli.data_paths(cfg)
    store = Store.open(database, busy_timeout_seconds=0)
    assert store.last_run()["end_cause"] == f"stopped (operator command {cmd.command_id})"
    runs = store.run_count()
    store.close()
    assert api.read_status(paths.status).bot_state is api.BotState.STOPPED
    assert cli.main(["--config", str(bot_toml), "run"]) == 3
    assert "tickr clear-stop" in capsys.readouterr().err
    store = Store.open(database, busy_timeout_seconds=0)
    assert store.run_count() == runs  # refused before any run was recorded
    store.close()
    assert cli.main(["--config", str(bot_toml), "status"]) == 0
    assert "starts refused" in capsys.readouterr().out
    assert cli.main(["--config", str(bot_toml), "clear-stop"]) == 0
    assert stopmarker.describe(cfg) is None
    assert cli.main(["--config", str(bot_toml), "clear-stop"]) == 1


# ---------------------------------------------------------------- emergency


class Recorder(FakeBroker):
    """Records every place and cancel, in order; nothing fills on its own."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def place(self, request):
        self.calls.append(("place", request.order_type))
        return super().place(request)

    def cancel(self, client_order_id):
        self.calls.append(("cancel", self.orders[client_order_id].order_type))
        return super().cancel(client_order_id)


def test_emergency_steps_run_in_order_and_never_leave_the_position_unprotected(tmp_path):
    broker = Recorder()
    bot = Bot(tmp_path, broker=broker)
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = bot.engine.orders(t)[0].client_order_id
    broker.fill(entry, D(1), D(10))  # partial fill: a stop rests, the entry still works
    for _ in range(3):
        bot.engine.step()
    broker.calls.clear()
    cmd = bot.command(api.CommandKind.EMERGENCY)
    bot.service.process_commands()
    steps = [r.body["step"] for r in bot.store.journal(service.COMMAND_STEP, cmd.command_id)]
    assert steps == ["entry-stop-on", "cancel-own-orders", "flatten"]
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()
    assert broker.calls == [("cancel", "LIMIT")]  # the entry only: the stop still protects
    assert [o.order_type for o in broker.open_orders()] == ["STOP_LOSS"]
    assert bot.engine.trade(t).exit_requested == "operator-emergency"
    (rec,) = bot.service.status().recent_commands
    assert (rec.kind, rec.outcome) == (api.CommandKind.EMERGENCY, api.CommandOutcome.DONE)
    assert rec.cause == ("entry-stop-on: done; cancel-own-orders: 1 entry cancel(s) "
                         "requested; flatten: done")
    for _ in range(6):  # the exit path: the stop goes only right before the closing sale
        bot.engine.step()
        for order in broker.open_orders():
            if order.order_type == "MARKET":
                broker.fill(order.client_order_id, order.quantity, D(10))
    assert broker.calls[1:] == [("cancel", "STOP_LOSS"), ("place", "MARKET")]
    assert bot.engine.trade(t).state is TradeState.CLOSED
    assert broker.positions() == []


def test_emergency_refused_flatten_does_not_hide_the_other_steps(tmp_path):
    broker = Recorder()
    bot = Bot(tmp_path, broker=broker)
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = bot.engine.orders(t)[0].client_order_id
    broker.fill(entry, D(1), D(10))
    for _ in range(3):
        bot.engine.step()
    with bot.store.transaction():
        bot.engine._fence_row(Fence.EXIT_REFUSED, t, "test")
    bot.command(api.CommandKind.EMERGENCY)
    bot.service.process_commands()
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()
    assert broker.orders[entry].state.terminal
    assert not bot.engine.trade(t).exit_requested
    (rec,) = bot.service.status().recent_commands
    assert rec.outcome is api.CommandOutcome.REFUSED
    assert rec.cause.startswith("flatten: closing order refused before for " + t)


def test_emergency_after_a_crash_finishes_the_missing_steps_only(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    t = opened(bot)
    cmd = bot.command(api.CommandKind.EMERGENCY)
    real = bot.service._flatten_rows

    def crash(*args):
        raise RuntimeError("crash before the flatten step")

    bot.service._flatten_rows = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        bot.service.process_commands()
    assert not bot.store.journal(service.COMMAND)
    bot.service._flatten_rows = real  # type: ignore[method-assign]
    bot.service.process_commands()  # the file is still in the inbox
    steps = [r.body["step"] for r in bot.store.journal(service.COMMAND_STEP, cmd.command_id)]
    assert steps == ["entry-stop-on", "cancel-own-orders", "flatten"]
    assert len(bot.store.journal(service.COMMAND)) == 1
    assert bot.engine.trade(t).exit_requested == "operator-emergency"
    bot.service.process_commands()
    assert len(bot.store.journal(service.COMMAND_STEP, cmd.command_id)) == 3


# ---------------------------------------------------------------- stop-bot with closes


def tmp_config(bot: Bot, tmp_path: Path) -> None:
    """Stop markers go under tmp_path, never into the repository's data folder."""
    bot.engine.config = bot.service.config = config.load(ROOT / "config" / "bot.toml",
                                                          root=tmp_path)


@pytest.mark.parametrize("first", [api.CommandKind.EMERGENCY, api.CommandKind.FLATTEN])
def test_stop_bot_after_a_close_request_is_refused(tmp_path, first):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = opened(bot)
    bot.command(first, offset_seconds=-1)
    stop = bot.command(api.CommandKind.STOP_BOT)
    bot.service.process_commands()  # both in one pass
    assert bot.service.stopped_by is None
    assert stopmarker.describe(bot.service.config) is None
    assert bot.engine.trade(t).exit_requested is not None
    records = {r.command_id: r for r in bot.service.status().recent_commands}
    assert records[stop.command_id].outcome is api.CommandOutcome.REFUSED
    assert records[stop.command_id].cause == service.CLOSE_IN_PROGRESS
    for _ in range(6):  # the close goes on; a later stop-bot is obeyed
        bot.engine.step()
        for order in bot.broker.open_orders():
            if order.order_type == "MARKET":
                bot.broker.fill(order.client_order_id, order.quantity, D(10))
    assert bot.engine.trade(t).state is TradeState.CLOSED
    again = bot.command(api.CommandKind.STOP_BOT)
    bot.service.process_commands()
    assert bot.service.stopped_by == again.command_id


def test_stop_bot_refused_then_emergency_then_stop_bot_obeyed_once_flat(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = opened(bot)
    first = bot.command(api.CommandKind.STOP_BOT)
    bot.service.process_commands()
    assert bot.service.stopped_by is None
    bot.now += timedelta(seconds=1)
    bot.command(api.CommandKind.EMERGENCY)
    bot.service.process_commands()
    for _ in range(6):
        bot.engine.step()
        for order in bot.broker.open_orders():
            if order.order_type == "MARKET":
                bot.broker.fill(order.client_order_id, order.quantity, D(10))
    assert bot.engine.trade(t).state is TradeState.CLOSED
    bot.now += timedelta(seconds=1)
    again = bot.command(api.CommandKind.STOP_BOT)
    assert bot.run(1000)
    assert bot.service.stopped_by == again.command_id
    assert bot.broker.positions() == [] and bot.broker.open_orders() == []
    records = {r.command_id: r for r in bot.status().recent_commands}
    assert records[first.command_id].outcome is api.CommandOutcome.REFUSED
    assert records[again.command_id].outcome is api.CommandOutcome.DONE
    assert stopmarker.describe(bot.service.config) is not None


def test_commands_after_an_obeyed_stop_bot_are_refused_in_the_same_pass(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    stop = bot.command(api.CommandKind.STOP_BOT, offset_seconds=-1)
    entry_stop = bot.command(api.CommandKind.ENTRY_STOP_ON)
    bot.service.process_commands()
    assert bot.service.stopped_by == stop.command_id
    assert str(Fence.ENTRY_STOP) not in bot.engine.fences()
    records = {r.command_id: r for r in bot.service.status().recent_commands}
    assert (records[stop.command_id].outcome, records[stop.command_id].cause) == (
        api.CommandOutcome.DONE, None)
    assert records[entry_stop.command_id].outcome is api.CommandOutcome.REFUSED
    assert records[entry_stop.command_id].cause == (
        f"read after stop-bot {stop.command_id}: the bot is stopping")
    assert list(bot.paths.inbox.iterdir()) == []


def test_interrupted_emergency_read_after_its_lifetime_is_recorded_refused(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    t = opened(bot)
    cmd = bot.command(api.CommandKind.EMERGENCY)

    def crash(*args):
        raise RuntimeError("crash before the flatten step")

    bot.service._flatten_rows = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        bot.service.process_commands()
    # The restart comes after the command's lifetime.
    bot.now += timedelta(seconds=int(CFG.api.command_max_age_seconds) + 1)
    restarted = service.Service(bot.engine, now=lambda: bot.now, calendar=CALENDAR,
                                strategy=service.strategy_id(CFG), paths=bot.paths)
    restarted.process_commands()
    assert bot.engine.trade(t).exit_requested is None  # never executed late
    (rec,) = restarted.status().recent_commands
    assert (rec.command_id, rec.outcome) == (cmd.command_id, api.CommandOutcome.REFUSED)
    assert rec.cause.startswith("expired: ")
    assert rec.cause.endswith("; steps done: entry-stop-on, cancel-own-orders; not run: flatten")
    assert list(bot.paths.inbox.iterdir()) == []


def test_a_bot_started_after_the_flatten_time_flattens_the_previous_runs_trade(tmp_path):
    """The watchdog's restart (runtime/watchdog.py): a run died with a protected trade; a new
    run started after the flatten time recovers it and closes it through the normal path."""
    first = Bot(tmp_path)
    t = opened(first)
    assert first.engine.trade(t).state is TradeState.OPEN
    first.store.close()  # the crash: no drain, the stop rests at the broker
    times = CALENDAR.day_at(MONDAY_11).session_times(
        CFG.session.last_entry_minutes_before_close, CFG.session.flatten_minutes_before_close)
    late = Bot(tmp_path, broker=first.broker, now=times.flatten + timedelta(seconds=60))
    late.engine = Engine(late.store, late.broker, CFG, "svc00002",
                         lambda: GateResult(True, "t"), clock=lambda: late.clock[0])
    late.service = service.Service(late.engine, now=lambda: late.now, calendar=CALENDAR,
                                   strategy=service.strategy_id(CFG), paths=late.paths)
    assert late.run(12)
    trade = late.engine.trade(t)
    assert (trade.state, trade.exit_requested) == (TradeState.CLOSED, "session-flatten")
    assert late.broker.positions() == [] and late.broker.open_orders() == []


def test_a_failed_open_orders_read_keeps_the_broker_read_time_old(tmp_path):
    """Admission reads this time: entries stay refused until a reconcile succeeds, not only
    on the one pass in which the open-orders read failed."""
    bot = Bot(tmp_path)
    assert bot.run(1)
    before = bot.service.last_broker_read_at
    assert before is not None

    def fail():
        raise RateLimited("x")

    bot.broker.open_orders = fail
    bot.now += timedelta(seconds=30)
    bot.service._observe(reconcile=True)
    for _ in range(3):
        bot.now += timedelta(seconds=3)
        bot.service._observe(reconcile=False)
    assert bot.service.last_broker_read_at == before
    del bot.broker.open_orders
    bot.service._observe(reconcile=True)
    assert bot.service.last_broker_read_at == bot.now


def test_cli_run_refuses_a_database_of_another_environment(tmp_path, monkeypatch, capsys):
    bot_toml = copy_config(tmp_path)
    cfg = config.load(bot_toml)
    database, _ = cli.data_paths(cfg)
    database.parent.mkdir(parents=True, exist_ok=True)
    store = Store.open(database, busy_timeout_seconds=0)
    store.start_run(environment="another", config_digest="d", strategy="s",
                    strategy_version="1", code_version="x")
    store.close()
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: None)
    assert cli.main(["--config", str(bot_toml), "run"]) == 3
    assert "environment 'another'" in capsys.readouterr().err
    store = Store.open(database, busy_timeout_seconds=0)
    assert store.run_count() == 1  # refused before any run was recorded
    store.close()


SESSION = {"open": "09:30", "close": "16:00"}


@pytest.mark.parametrize("now, cause, params", [
    (MONDAY_11, None, None),
    (datetime(2026, 11, 16, 8, 0, tzinfo=NEW_YORK), "outside_regular_session", SESSION),
    (datetime(2026, 11, 16, 15, 59, tzinfo=NEW_YORK), "after_last_entry", None),
    (datetime(2026, 11, 16, 20, 0, tzinfo=NEW_YORK), "outside_regular_session", SESSION),
    (datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK), "no_session_today",   # Saturday
     {"day": "weekend"}),
    (datetime(2026, 11, 26, 11, 0, tzinfo=NEW_YORK), "no_session_today",   # Thanksgiving
     {"day": "holiday"}),
])
def test_status_names_the_ordinary_reasons_admission_refuses_entries(tmp_path, now, cause,
                                                                     params):
    """The apps read `entry_block`: without these they say "entries allowed" at night."""
    bot = Bot(tmp_path, now=now)
    bot.service.market, bot.service.evaluator = StubMarket(), StubEvaluator()  # type: ignore[assignment]
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    block = bot.service._ordinary_block()
    assert (None if block is None else (block.cause, block.attention)) == (
        None if cause is None else (cause, False))
    if params is not None:
        assert block is not None and block.params == params
    if cause == "after_last_entry":
        assert block is not None and set(block.params) == {"time"}
    if config.trading_gate(CFG).open:
        assert bot.service.status().entry_block == block


def test_status_says_calmly_that_the_position_limit_is_used(tmp_path):
    from dataclasses import replace

    bot = Bot(tmp_path, broker=FakeBroker())
    bot.engine.config = bot.service.config = replace(  # one slot, taken by the trade below
        CFG, risk=replace(CFG.risk, max_open_positions=1))
    bot.service.market, bot.service.evaluator = StubMarket(), StubEvaluator()  # type: ignore[assignment]
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    bot.engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D(1))
    block = bot.service._ordinary_block()
    assert block is not None and (block.cause, block.attention) == ("own_trade_open", False)


def test_status_counts_position_slots_like_admission(tmp_path):
    """With several slots, one open trade leaves entries open (admission refuses only its own
    symbol); the status says "own trade open" once the open trades take every slot."""
    from dataclasses import replace

    bot = Bot(tmp_path, broker=FakeBroker())
    bot.engine.config = bot.service.config = replace(
        CFG, risk=replace(CFG.risk, max_open_positions=2))
    bot.engine.recover()
    assert bot.service._ordinary_block() is None
    bot.engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D(1))  # entry working, no fill
    assert bot.service._ordinary_block() is None
    bot.engine.open_trade("XYZ", D(2), D("10.00"), D("9.80"), D(1))
    block = bot.service._ordinary_block()
    assert block is not None and (block.cause, block.attention) == ("own_trade_open", False)


def test_a_missing_calendar_is_a_real_block(tmp_path):
    bot = Bot(tmp_path)
    bot.service.calendar = None
    block = bot.service._ordinary_block()
    assert block is not None and (block.cause, block.attention) == ("calendar_unknown", True)


def test_positions_and_watch_are_evaluated_before_discovery_starts(tmp_path):
    """The real market runtime and evaluator in the service loop: the WATCH symbol's decision
    comes before discovery's first screener read of the minute."""
    from tickr.data.base import ScreenerKind, ScreenerSession
    from tickr.data.health import StreamHealth
    from tickr.data.history import HistoryReader, HistorySettings
    from tickr.data.todaybars import TodayBars
    from tickr.observers.log import DecisionLog
    from tickr.runtime.entries import EntryDesk
    from tickr.runtime.evaluation import Evaluator
    from tickr.runtime.market import DISCOVERY, MarketRuntime, StreamInbox
    from tickr.strategy.base import Check, Decision, Verdict
    from tickr.types import Bar

    events: list[str] = []

    class Source(FakeMarketData):
        def screener(self, kind: ScreenerKind, session=ScreenerSession.REGULAR):
            events.append(f"screener {kind}")
            return super().screener(kind, session)

    class Watching:
        name, version = CFG.strategy.active, "1"

        def evaluate(self, context):
            events.append(f"evaluate {context.symbol}")
            return Decision(self.name, self.version, context.symbol, Verdict.WATCH,
                            (Check("rule", D(1), ">=", D(1), True),))

        def quote_liquidity(self, quote):
            return ()

        def session_liquidity(self, bars):
            return ()

    bot = Bot(tmp_path)
    source, inbox, today_bars = Source(), StreamInbox(), TodayBars()
    one = D(10)
    for day in (9, 10, 11, 12, 13):
        start = datetime(2026, 11, day, 9, 30, tzinfo=NEW_YORK)
        source.add_bars([Bar("WAT", start + timedelta(minutes=i), one, one, one, one, one)
                         for i in range(390)])
    source.add_bars([Bar("WAT", MONDAY_11 - timedelta(minutes=2), one, one, one, one, one)])
    market = MarketRuntime(bot.engine, CFG, source, health=StreamHealth.from_config(CFG),
                           inbox=inbox, capture=None, calendar=CALENDAR, today_bars=today_bars,
                           now=lambda: bot.now, monotonic=lambda: bot.clock[0],
                           spawn=lambda run: run())  # the pass in place: one loop pass
    strategy = Watching()
    market.screen = strategy
    desk = EntryDesk(bot.engine, CFG, calendar=CALENDAR, last_broker_read_at=lambda: bot.now,
                     log=DecisionLog(tmp_path / "decisions"))
    evaluator = Evaluator(bot.engine, CFG, source, strategy=strategy, desk=desk,
                          history=HistoryReader(source, CALENDAR,
                                                HistorySettings.from_config(CFG), None),
                          inbox=inbox, calendar=CALENDAR, today_bars=today_bars,
                          now=lambda: bot.now)
    evaluator.watch = {"WAT"}
    bot.service.market, bot.service.evaluator = market, evaluator
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    bot.run(1)
    assert events[0] == "evaluate WAT" and events[1].startswith("screener")
    assert len(bot.store.journal(DISCOVERY)) == 1


def test_cli_run_records_the_code_version_on_the_run_and_status(tmp_path, monkeypatch,
                                                                 code_version_in_environment):
    TEST_CODE_VERSION = code_version_in_environment  # noqa: N806 - the fixture's value

    bot_toml = copy_config(tmp_path)
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: cli.RunAdapters(
        FakeBroker(), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    cfg = config.load(bot_toml)
    assert api.read_status(service.ServicePaths.from_config(cfg).status).code_version == \
        TEST_CODE_VERSION
    database, _ = cli.data_paths(cfg)
    store = Store.open(database, busy_timeout_seconds=0)
    run_id = store.last_run()["run_id"]
    assert store.run_code_version(run_id) == TEST_CODE_VERSION
    (started,) = store.journal("run-started")
    assert started.body["code_version"] == TEST_CODE_VERSION
    store.close()


def test_cli_run_refuses_without_a_code_version(tmp_path, monkeypatch, capsys):
    bot_toml = copy_config(tmp_path)
    monkeypatch.delenv("TICKR_CODE_VERSION")
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda cfg, on_error, on_token: pytest.fail(
        "the run must be refused before any adapter is built"))
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    assert cli.main(["--config", str(bot_toml), "run"]) == 3
    err = capsys.readouterr().err
    assert "code version unknown" in err and "TICKR_CODE_VERSION" in err
    database, _ = cli.data_paths(config.load(bot_toml))
    assert not database.exists()  # refused before any run was recorded


def test_status_code_version_is_null_for_a_run_recorded_without_one(tmp_path):
    bot = Bot(tmp_path)
    assert bot.service.status().code_version is None  # no runs row: never guessed
    run_id = bot.store.start_run(environment="sandbox", config_digest="d", strategy="s",
                                 strategy_version="1", code_version="-")
    bot.engine.run_id = run_id
    assert bot.service.status().code_version is None


def test_a_trade_carries_the_code_version_of_the_run_that_opened_it(tmp_path):
    sha = "6c4eec8b0f3a91d2e5b7c6a8d9e0f1a2b3c4d5e6-dirty"
    bot = Bot(tmp_path)
    bot.engine.run_id = bot.store.start_run(environment="sandbox", config_digest="d",
                                            strategy="price-action-v1", strategy_version="3",
                                            code_version=sha)
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1),
                              strategy=("price-action-v1", "3"))
    (row,) = bot.store.journal("trade-opened", t)
    assert (row.body["strategy_version"], row.body["code_version"]) == ("3", sha)
    bot.broker.fill(bot.engine.orders(t)[0].client_order_id, D(2), D(10))
    for _ in range(3):
        bot.engine.step()
    bot.service.state = api.BotState.RUNNING
    bot.command(api.CommandKind.FLATTEN)
    assert bot.run(10)
    status = bot.service.status()
    (done,) = status.trades_today
    assert done.code_version == sha and status.code_version == sha


def _resting(bot: Bot) -> str:
    return bot.engine.open_trade("ABC", D(10), D("9.90"), D("9.60"), D("3"),
                                 entry_low_price=D("9.80"),
                                 rest_until=bot.now + timedelta(minutes=6))


def test_a_restart_never_resumes_a_working_entry(tmp_path):
    first = Bot(tmp_path, broker=FakeBroker())
    first.engine.recover()
    t = _resting(first)
    first.store.close()  # the crash: the entry still rests at the broker
    bot = Bot(tmp_path, broker=first.broker)
    bot.run(0)
    (row,) = bot.store.journal("entry-cancel")
    assert row.body["cause"] == service.RESTART_ENTRY_CANCEL
    (entry,) = bot.engine.orders(t)
    assert entry.cancel_state == "sent"


def test_without_a_market_every_working_entry_is_cancelled(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    bot.engine.recover()
    t = _resting(bot)
    assert bot.service.market is None
    bot.service._guard_entries()
    (row,) = bot.store.journal("entry-cancel")
    assert row.body["cause"] == "no quote"
    assert bot.engine.orders(t)[0].cancel_state == "sent"


def _early(tmp_path: Path, record: str | None) -> Bot:
    """A bot whose paths name a watchdog early-restart record holding `record`."""
    path = tmp_path / "watchdog-entry-restart.json"
    if record is not None:
        path.write_text(record, "utf-8")
    bot = Bot(tmp_path, broker=FakeBroker())
    bot.service.paths = replace(bot.paths, watchdog_entry_restart=path)
    return bot


def _today_record(at: datetime = MONDAY_11) -> str:
    return json.dumps({"ny_date": at.date().isoformat(), "at": at.isoformat(),
                       "early": True}) + "\n"


def test_a_run_the_watchdog_started_early_takes_no_new_entry_until_cleared(tmp_path):
    bot = _early(tmp_path, _today_record())
    bot.run(0)
    fences = bot.engine.fences()
    assert str(Fence.WATCHDOG_STARTED) in fences
    assert "no new entry" in fences[str(Fence.WATCHDOG_STARTED)]
    assert bot.engine.clear_operator_fence(str(Fence.WATCHDOG_STARTED))
    again = _early(tmp_path, _today_record())  # a later start the same day, same record
    again.run(0)
    assert str(Fence.WATCHDOG_STARTED) not in again.engine.fences()


@pytest.mark.parametrize("record", [None, _today_record(MONDAY_11 - timedelta(days=3))])
def test_no_fence_without_a_record_of_today(tmp_path, record):
    bot = _early(tmp_path, record)
    bot.run(0)
    assert str(Fence.WATCHDOG_STARTED) not in bot.engine.fences()


def test_an_unreadable_early_restart_record_fences_too(tmp_path):
    bot = _early(tmp_path, "{not json")
    bot.run(0)
    assert "unreadable" in bot.engine.fences()[str(Fence.WATCHDOG_STARTED)]


def test_a_nearly_full_disk_fences_entries_until_there_is_room(tmp_path):
    bot = Bot(tmp_path)
    floor = CFG.runtime.min_free_disk_mb
    free = {"mb": floor - 1}

    def disk_free(path: Path) -> int:
        assert path == tmp_path
        if free["mb"] is None:
            raise PermissionError("no")
        return free["mb"] * BYTES_PER_MB

    bot.service = service.Service(
        bot.engine, now=lambda: bot.now, calendar=CALENDAR, strategy=service.strategy_id(CFG),
        paths=replace(bot.paths, data=tmp_path), disk_free=disk_free)
    bot.service.check_disk()
    fences = bot.engine.fences()
    assert fences[str(Fence.DISK_LOW)] == (
        f"less than {floor} MB free on the data disk: no new entry until there is room")
    bot.service.check_disk()  # set once, not again every pass
    assert len(bot.store.journal("fence-set", key=str(Fence.DISK_LOW))) == 1
    free["mb"] = floor  # back at the floor: kept until the clear level, no flapping
    bot.service.check_disk()
    assert str(Fence.DISK_LOW) in bot.engine.fences()
    free["mb"] = CFG.runtime.disk_clear_free_mb - 1
    bot.service.check_disk()
    assert str(Fence.DISK_LOW) in bot.engine.fences()
    free["mb"] = CFG.runtime.disk_clear_free_mb
    bot.service.check_disk()
    assert str(Fence.DISK_LOW) not in bot.engine.fences()
    free["mb"] = floor  # at the floor with no fence set: no fence
    bot.service.check_disk()
    assert str(Fence.DISK_LOW) not in bot.engine.fences()
    free["mb"] = None  # unknown is never "enough"
    bot.service.check_disk()
    assert "unreadable (PermissionError)" in bot.engine.fences()[str(Fence.DISK_LOW)]


def test_the_loop_checks_the_disk_every_pass(tmp_path):
    bot = Bot(tmp_path)
    seen = []
    bot.service = service.Service(
        bot.engine, now=lambda: bot.now, calendar=CALENDAR, strategy=service.strategy_id(CFG),
        paths=replace(bot.paths, data=tmp_path),
        disk_free=lambda path: seen.append(path) or 0)
    assert bot.run(2)
    assert len(seen) == 2
    assert bot.store.journal("fence-set", key=str(Fence.DISK_LOW))


# ---------------------------------------------------------------- stop-bot with a mode (V4)


def steps(bot: Bot, cmd: api.CommandV1) -> list[str]:
    return [r.body["step"] for r in bot.store.journal(service.STOP_PROGRESS, cmd.command_id)]


def record(bot: Bot, cmd: api.CommandV1) -> api.CommandRecord:
    return {r.command_id: r for r in bot.service.status().recent_commands}[cmd.command_id]


def markets(broker: FakeBroker) -> list[str]:
    return [r.client_order_id for r in broker.sent if r.order_type == "MARKET"]


def test_close_and_stop_closes_every_position_then_stops(tmp_path):
    bot = Bot(tmp_path)
    tmp_config(bot, tmp_path)
    t = opened(bot)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()
    bot.service.state = api.BotState.RUNNING
    progress = bot.service.status().stop_progress
    assert progress == api.StopProgress(cmd.command_id, api.StopMode.CLOSE_AND_STOP,
                                        api.StopStep.CLOSING_POSITIONS, bot.now, 1)
    assert bot.service.stopped_by is None and stopmarker.describe(bot.service.config) is None
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()  # no new trade while closing
    assert bot.run(1000)  # ends on the stop, long before the iterations run out
    assert bot.service.stopped_by == cmd.command_id
    trade = bot.engine.trade(t)
    assert (trade.state, trade.exit_requested) == (TradeState.CLOSED, "operator-flatten")
    assert bot.broker.positions() == [] and bot.broker.open_orders() == []
    assert len(markets(bot.broker)) == 1  # one closing order, sent once
    assert steps(bot, cmd) == ["closing_positions", "positions_closed", "stopping", "stopped"]
    status = bot.status()
    assert status.bot_state is api.BotState.STOPPED and status.positions == ()
    assert status.stop_progress is not None
    assert (status.stop_progress.step, status.stop_progress.open_trades) == (
        api.StopStep.STOPPED, 0)
    rec = {r.command_id: r for r in status.recent_commands}[cmd.command_id]
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.DONE, None)
    stopped = stopmarker.describe(bot.service.config)
    assert stopped is not None and cmd.command_id in stopped


@pytest.mark.parametrize("mode", list(api.StopMode))
def test_a_stop_with_a_mode_and_nothing_open_stops_at_once(tmp_path, mode):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=mode)
    assert bot.run(1000)
    assert bot.service.stopped_by == cmd.command_id
    assert steps(bot, cmd) == ["stopping", "stopped"]
    assert str(Fence.ENTRY_STOP) not in bot.engine.fences()
    assert bot.broker.sent == []
    rec = {r.command_id: r for r in bot.status().recent_commands}[cmd.command_id]
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.DONE, None)


def test_close_and_stop_with_a_failed_close_sends_once_and_never_stops(tmp_path):
    broker = Recorder()
    bot = Bot(tmp_path, broker=broker)
    tmp_config(bot, tmp_path)
    t = opened(bot)
    broker.calls.clear()
    broker.script("refuse")  # the closing MARKET order is refused by the broker
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()
    for _ in range(12):
        bot.engine.step()
        bot.service.carry_out_stop()
    assert len(markets(broker)) == 1  # never resent
    assert broker.calls[:2] == [("cancel", "STOP_LOSS"), ("place", "MARKET")]
    assert f"{Fence.EXIT_REFUSED}:{t}" in bot.engine.fences()
    rec = record(bot, cmd)
    assert rec.outcome is api.CommandOutcome.REFUSED
    assert rec.cause == (f"closing order refused for {t}: the bot places their stop again; "
                         "look in the Webull app; the bot keeps running and entry-stop stays on")
    codes = api.to_jsonable(rec)["cause_codes"]
    assert [c["code"] for c in codes] == ["stop_close_refused", "stop_close_kept"]
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()  # as the cause says
    assert bot.service.stopped_by is None and stopmarker.describe(bot.service.config) is None
    assert bot.service.status().stop_progress is None
    assert bot.engine.trade(t).state is TradeState.OPEN
    assert [o.order_type for o in broker.open_orders()] == ["STOP_LOSS"]  # protection back
    assert steps(bot, cmd) == ["closing_positions"]
    bot.now += timedelta(seconds=1)
    later = bot.command(api.CommandKind.ENTRY_STOP_OFF)  # the bot keeps obeying commands
    bot.service.process_commands()
    assert record(bot, later).outcome is api.CommandOutcome.DONE
    for _ in range(6):
        bot.engine.step()
        bot.service.carry_out_stop()
    assert len(markets(broker)) == 1


def test_close_and_stop_refused_by_the_flatten_changes_nothing(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = opened(bot)
    with bot.store.transaction():
        bot.engine._fence_row(Fence.EXIT_REFUSED, t, "test")
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()
    bot.service.carry_out_stop()
    rec = record(bot, cmd)
    assert rec.outcome is api.CommandOutcome.REFUSED
    assert rec.cause.startswith(f"flatten: closing order refused before for {t}")
    assert str(Fence.ENTRY_STOP) not in bot.engine.fences()
    assert bot.engine.trade(t).exit_requested is None
    assert steps(bot, cmd) == [] and bot.service.stopped_by is None


def test_close_and_stop_waits_for_a_close_that_does_not_fill_until_its_fence(tmp_path):
    """No time-out of its own: a sent close that does not fill ends the flow through the
    engine's exit-not-filled fence ([orders] max_exit_seconds), never sooner, never resent."""
    bot = Bot(tmp_path, broker=FakeBroker())  # nothing fills: the close stays working
    tmp_config(bot, tmp_path)
    t = opened(bot)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()
    for _ in range(6):  # the stop's cancel, then the close goes out
        bot.engine.step()
        bot.service.carry_out_stop()
    assert len(markets(bot.broker)) == 1
    assert bot.service._closing is not None  # still waiting: no fence yet
    bot.now += timedelta(seconds=float(CFG.orders.max_exit_seconds))
    bot.engine.step()
    assert f"{Fence.EXIT_NOT_FILLED}:{t}" in bot.engine.fences()
    bot.service.carry_out_stop()
    rec = record(bot, cmd)
    assert rec.outcome is api.CommandOutcome.REFUSED
    # The truth at the broker: the close still works and its shares have no stop.
    assert rec.cause == (f"closing order not filled for {t}: it is still working at the broker "
                         "and its shares are not under a stop; look in the Webull app; the bot "
                         "keeps running and entry-stop stays on")
    assert [o.order_type for o in bot.broker.open_orders()] == ["MARKET"]
    codes = api.to_jsonable(rec)["cause_codes"]
    assert [c["code"] for c in codes] == ["stop_close_not_filled", "stop_close_kept"]
    assert bot.service.stopped_by is None and stopmarker.describe(bot.service.config) is None
    assert bot.engine.trade(t).exit_requested == "operator-flatten"  # the close goes on
    assert len(markets(bot.broker)) == 1


def test_close_and_stop_ends_on_an_operator_fence_on_one_of_its_orders(tmp_path):
    """A stop still working after every cancel (cancel-failed, keyed by the order) holds its
    shares: the engine closes them no further without the operator, so the flow ends."""
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = opened(bot)
    stop = next(o for o in bot.engine.orders(t) if o.leg is Leg.STOP)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()
    bot.service.carry_out_stop()
    assert bot.service._closing is not None
    with bot.store.transaction():
        bot.engine._fence_row(Fence.CANCEL_FAILED, stop.client_order_id, "test")
    bot.service.carry_out_stop()
    rec = record(bot, cmd)
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.REFUSED, (
        f"close held for {t} (cancel-failed): look in the Webull app; the bot keeps running "
        "and entry-stop stays on"))
    codes = api.to_jsonable(rec)["cause_codes"]
    assert [(c["code"], c["params"]) for c in codes] == [
        ("stop_close_failed", {"trades": t, "fences": "cancel-failed"}), ("stop_close_kept", {})]
    assert bot.service.stopped_by is None and bot.service.status().stop_progress is None


def test_a_close_and_stop_cause_says_per_fence_what_is_true_at_the_broker():
    cause = service.stop_close_failed([("t2", Fence.MARKET_CLOSED), ("t1", Fence.EXIT_NOT_FILLED),
                                       ("t3", Fence.REPLACE_UNRESOLVED),
                                       ("t1", Fence.UNPROTECTED_TIMEOUT)])
    assert cause == (
        "closing order not filled for t1: it is still working at the broker and its shares are "
        "not under a stop; look in the Webull app; close not sent for t2 outside the regular "
        "session: nothing was sent and the stop stays; close held for t1, t3 "
        "(replace-unresolved, unprotected-timeout): look in the Webull app; the bot keeps "
        "running and entry-stop stays on")
    assert [r.code for r in reasons.prose_reasons(cause)] == [
        "stop_close_not_filled", "stop_close_not_sent", "stop_close_failed", "stop_close_kept"]


def test_close_and_stop_ends_when_a_trade_it_did_not_begin_with_opens(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    opened(bot)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()
    bot.service.carry_out_stop()
    assert bot.service._closing is not None
    with bot.store.transaction():  # the entry-stop lifted some other way than the API
        bot.engine.entry_stop_row(False, "test")
    late = bot.engine.open_trade("XYZ", D(1), D(10), D("9.80"), D(1))
    bot.service.carry_out_stop()
    rec = record(bot, cmd)
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.REFUSED,
                                        service.STOP_NEW_TRADE.format(trades=late))
    codes = api.to_jsonable(rec)["cause_codes"]
    assert [c["code"] for c in codes] == ["stop_new_trade"]
    assert bot.service.stopped_by is None and stopmarker.describe(bot.service.config) is None


def test_entry_stop_off_while_close_and_stop_runs_is_refused(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    opened(bot)
    cmd = bot.command(api.CommandKind.STOP_BOT, offset_seconds=-1,
                      mode=api.StopMode.CLOSE_AND_STOP)
    off = bot.command(api.CommandKind.ENTRY_STOP_OFF)
    bot.service.process_commands()  # both in one pass
    rec = record(bot, off)
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.REFUSED,
                                        service.STOP_RUNNING.format(command=cmd.command_id))
    codes = api.to_jsonable(rec)["cause_codes"]
    assert [c["code"] for c in codes] == ["stop_already_running"]
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()  # no new trade while closing
    assert bot.service._closing is not None


def test_close_and_stop_goes_on_in_the_bot_when_the_app_disconnects(tmp_path):
    """Nothing after the command depends on the app or the API: the inbox may vanish."""
    bot = Bot(tmp_path)
    tmp_config(bot, tmp_path)
    opened(bot)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()
    shutil.rmtree(bot.paths.inbox.parent)  # the app and the API are gone
    assert bot.run(1000)
    assert bot.service.stopped_by == cmd.command_id
    assert steps(bot, cmd) == ["closing_positions", "positions_closed", "stopping", "stopped"]


def test_a_second_stop_while_close_and_stop_runs_is_refused(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    opened(bot)
    first = bot.command(api.CommandKind.STOP_BOT, offset_seconds=-2,
                        mode=api.StopMode.CLOSE_AND_STOP)
    again = bot.command(api.CommandKind.STOP_BOT, offset_seconds=-1,
                        mode=api.StopMode.CLOSE_AND_STOP)
    leave = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.LEAVE_OPEN)
    bot.service.process_commands()
    for cmd in (again, leave):
        rec = record(bot, cmd)
        assert (rec.outcome, rec.cause) == (api.CommandOutcome.REFUSED,
                                            service.STOP_RUNNING.format(command=first.command_id))
    assert first.command_id not in {r.command_id for r in bot.service.status().recent_commands}
    assert bot.service.stopped_by is None


def test_close_and_stop_interrupted_by_a_restart_is_refused_never_resumed(tmp_path):
    bot = Bot(tmp_path)
    tmp_config(bot, tmp_path)
    t = opened(bot)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.CLOSE_AND_STOP)
    bot.service.process_commands()  # then the process dies
    restarted = service.Service(bot.engine, now=lambda: bot.now, calendar=CALENDAR,
                                strategy=service.strategy_id(CFG), paths=bot.paths)
    bot.service = restarted
    assert bot.run(20)
    assert restarted.stopped_by is None  # ended by the test's SIGTERM, not by the command
    assert stopmarker.describe(bot.service.config) is None
    rec = {r.command_id: r for r in bot.status().recent_commands}[cmd.command_id]
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.REFUSED, service.STOP_INTERRUPTED)
    # The exit request it made stays and closes the trade through the normal path.
    trade = bot.engine.trade(t)
    assert (trade.state, trade.exit_requested) == (TradeState.CLOSED, "operator-flatten")
    assert len(markets(bot.broker)) == 1


def test_leave_open_stops_at_once_and_closes_nothing(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = opened(bot)
    stop = next(o for o in bot.broker.open_orders() if o.order_type == "STOP_LOSS")
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.LEAVE_OPEN)
    assert bot.run(1000)
    assert bot.service.stopped_by == cmd.command_id
    assert markets(bot.broker) == []
    assert [o.client_order_id for o in bot.broker.open_orders()] == [stop.client_order_id]
    assert [(p.symbol, p.quantity) for p in bot.broker.positions()] == [("ABC", D(2))]
    trade = bot.engine.trade(t)
    assert (trade.state, trade.exit_requested) == (TradeState.OPEN, None)
    assert str(Fence.ENTRY_STOP) not in bot.engine.fences()
    assert steps(bot, cmd) == ["stopping", "stopped"]
    status = bot.status()
    assert status.bot_state is api.BotState.STOPPED
    # What the apps show as "bot stopped, 1 position open": the last status, with its
    # positions as of the last broker read.
    assert [(p.symbol, p.quantity) for p in status.positions] == [("ABC", D(2))]
    assert status.freshness.last_broker_read_at is not None
    assert status.stop_progress == api.StopProgress(
        cmd.command_id, api.StopMode.LEAVE_OPEN, api.StopStep.STOPPED, bot.now, 1)
    rec = {r.command_id: r for r in status.recent_commands}[cmd.command_id]
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.DONE,
                                        service.TRADES_LEFT_OPEN.format(count=1))
    assert [c["code"] for c in api.to_jsonable(rec)["cause_codes"]] == ["trades_left_open"]
    assert stopmarker.describe(bot.service.config) is not None


def test_leave_open_with_an_entry_working_cancels_it_and_keeps_the_fill_protected(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    t = bot.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
    entry = bot.engine.orders(t)[0].client_order_id
    bot.broker.fill(entry, D(1), D(10))  # partly filled; the rest still works
    for _ in range(3):
        bot.engine.step()
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.LEAVE_OPEN)
    assert bot.run(1000)
    assert bot.service.stopped_by == cmd.command_id
    assert bot.broker.orders[entry].state.terminal  # the drain cancelled the entry
    stops = [o for o in bot.broker.open_orders() if o.order_type == "STOP_LOSS"]
    assert sum(o.quantity for o in stops) == D(1)
    assert bot.store.journal("shutdown-clean")
    assert markets(bot.broker) == []


def test_leave_open_is_refused_while_a_close_is_in_progress(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    tmp_config(bot, tmp_path)
    opened(bot)
    bot.command(api.CommandKind.FLATTEN, offset_seconds=-1)
    cmd = bot.command(api.CommandKind.STOP_BOT, mode=api.StopMode.LEAVE_OPEN)
    bot.service.process_commands()
    rec = record(bot, cmd)
    assert (rec.outcome, rec.cause) == (api.CommandOutcome.REFUSED, service.CLOSE_IN_PROGRESS)
    assert bot.service.stopped_by is None and stopmarker.describe(bot.service.config) is None
