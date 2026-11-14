from __future__ import annotations

import os
import signal
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from aperture import cli, config
from aperture.api import contract as api
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.config import GateResult
from aperture.engine.core import Engine, Fence, TradeState
from aperture.runtime import service
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
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
        self.clock = [0.0]
        self.now = now
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
    bot.now = datetime.now(UTC)  # closed_at is wall-clock time
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
    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: broker)
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
    cfg = config.load(bot_toml)
    paths = service.ServicePaths.from_config(cfg)
    assert api.read_status(paths.status).bot_state is api.BotState.STOPPED
    database, _ = cli.data_paths(cfg)
    store = Store.open(database, busy_timeout_seconds=0)
    assert store.last_run()["end_cause"] == "stopped (SIGTERM)"
    store.close()
