from __future__ import annotations

import os
import signal
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import cli, config
from aperture.api import contract as api
from aperture.broker.base import RateLimited
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.config import GateResult
from aperture.data.base import FakeMarketData
from aperture.engine.core import Engine, Fence, TradeState
from aperture.runtime import service, stopmarker
from aperture.store import Store

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
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

    def command(self, kind: api.CommandKind, offset_seconds: int = 0) -> api.CommandV1:
        cmd = api.new_command(kind, self.now + timedelta(seconds=offset_seconds), "test")
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
                                         f"Trading gate closed: {gate.cause}."))


class StubEvaluator:
    def __init__(self) -> None:
        self.watch = {"WAT"}
        self.calls: list[list[str]] = []

    def tick(self, candidates):
        self.calls.append(list(candidates))


class StubMarket:
    def __init__(self) -> None:
        from aperture.runtime.market import StreamInbox

        self.inbox = StreamInbox()
        self.watch: set[str] = set()
        self.discovery = None
        self.ticks = 0
        self.discovers: list[bool] = []

    def tick(self, *, discover: bool = True) -> None:
        self.ticks += 1
        self.discovers.append(discover)

    def freshness(self, last_broker_read_at):
        return api.Freshness(last_broker_read_at, None, api.StreamHealth.OFF)


def test_with_an_evaluator_the_loop_runs_the_strategy_and_entries_are_open(tmp_path):
    from aperture.runtime.market import DiscoveryState

    bot = Bot(tmp_path)
    market, evaluator = StubMarket(), StubEvaluator()
    bot.service.market, bot.service.evaluator = market, evaluator  # type: ignore[assignment]
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    if config.trading_gate(CFG).open:
        assert bot.service.status().entry_block is None
    bot.run(1)
    assert evaluator.calls == [[]] and market.watch == {"WAT"}

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
    for rel in ("config/bot.toml", f"strategies/{CFG.strategy.active}/strategy.toml"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes())
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
    bot_toml.write_text(bot_toml.read_text().replace("observers = []",
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
    assert "aperture clear-stop" in capsys.readouterr().err
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


@pytest.mark.parametrize("now, cause", [
    (MONDAY_11, None),
    (datetime(2026, 11, 16, 8, 0, tzinfo=NEW_YORK), "outside_regular_session"),
    (datetime(2026, 11, 16, 15, 59, tzinfo=NEW_YORK), "after_last_entry"),
    (datetime(2026, 11, 16, 20, 0, tzinfo=NEW_YORK), "outside_regular_session"),
    (datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK), "no_session_today"),   # Saturday
    (datetime(2026, 11, 26, 11, 0, tzinfo=NEW_YORK), "no_session_today"),   # Thanksgiving
])
def test_status_names_the_ordinary_reasons_admission_refuses_entries(tmp_path, now, cause):
    """The apps read `entry_block`: without these they say "entries allowed" at night."""
    bot = Bot(tmp_path, now=now)
    bot.service.market, bot.service.evaluator = StubMarket(), StubEvaluator()  # type: ignore[assignment]
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    block = bot.service._ordinary_block()
    assert (None if block is None else (block.cause, block.attention)) == (
        None if cause is None else (cause, False))
    if config.trading_gate(CFG).open:
        assert bot.service.status().entry_block == block


def test_status_says_calmly_that_the_position_limit_is_used(tmp_path):
    bot = Bot(tmp_path, broker=FakeBroker())
    bot.service.market, bot.service.evaluator = StubMarket(), StubEvaluator()  # type: ignore[assignment]
    bot.engine.recover()
    bot.service.state = api.BotState.RUNNING
    bot.engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D(1))
    block = bot.service._ordinary_block()
    assert block is not None and (block.cause, block.attention) == ("own_trade_open", False)


def test_a_missing_calendar_is_a_real_block(tmp_path):
    bot = Bot(tmp_path)
    bot.service.calendar = None
    block = bot.service._ordinary_block()
    assert block is not None and (block.cause, block.attention) == ("calendar_unknown", True)
