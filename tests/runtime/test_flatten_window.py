"""No closing MARKET order outside the regular session (review of #129): a bot that starts
after the close refuses the session flatten, keeps the broker stop and alerts the owner."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract as api
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.engine.core import Engine, Fence, TradeState
from tickr.engine.manual import close_refusal
from tickr.runtime import service
from tickr.runtime.alerts import Alerter, AlertKind
from tickr.runtime.watch import ALERT, Watch
from tickr.store import Store

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)
HALF_DAY = datetime(2026, 11, 27, 11, 0, tzinfo=NEW_YORK)  # regular close 13:00 ET
CUTOFF = timedelta(seconds=float(CFG.session.close_order_cutoff_seconds))
MONDAY_CLOSE = CALENDAR.day_at(MONDAY).regular_close
LATE = f"at or after {MONDAY_CLOSE - CUTOFF:%H:%M:%S} ET"  # the cause on a normal day


class Market(FakeBroker):
    """Entries and exits fill on every tick; stops rest."""

    def tick(self) -> None:
        for cid, order in list(self.orders.items()):
            if order.state.terminal or order.order_type == "STOP_LOSS":
                continue
            self.fill(cid, order.quantity - order.filled_quantity, D(10))


class Shown:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def notify(self, title: str, text: str) -> None:
        self.texts.append(text)


class Bot:
    """One bot process on a database and broker that outlive it."""

    def __init__(self, tmp_path: Path, now: datetime) -> None:
        self.tmp_path = tmp_path
        self.broker = Market()
        self.now = now
        self.clock = [0.0]
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                now=lambda: self.now)
        self.start()

    def start(self) -> None:
        self.engine = Engine(self.store, self.broker, CFG, "win00001",
                             lambda: GateResult(True, "t"), clock=lambda: self.clock[0],
                             close_refusal=lambda: close_refusal(CFG, CALENDAR, self.now))
        watch = Watch(self.engine, CFG, Alerter(CFG.alerts, "t", Shown(), lambda: self.now),
                      lambda: self.now)
        self.paths = service.ServicePaths(self.tmp_path / "status.json", self.tmp_path / "in",
                                          self.tmp_path / "done")
        self.service = service.Service(self.engine, now=lambda: self.now, calendar=CALENDAR,
                                       strategy=service.strategy_id(CFG), paths=self.paths,
                                       watch=watch)

    def open(self) -> str:
        t = self.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
        self.broker.fill(self.engine.orders(t)[0].client_order_id, D(2), D(10))
        for _ in range(3):
            self.engine.step()
        assert self.stops() and not self.markets()
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

    def stops(self) -> list:
        return [o for o in self.broker.open_orders() if o.order_type == "STOP_LOSS"]

    def markets(self) -> list:
        return [o for o in self.broker.orders.values() if o.order_type == "MARKET"]

    def alerts(self) -> list[dict]:
        return [r.body for r in self.store.journal(ALERT)
                if r.body["kind"] == AlertKind.FLATTEN_AFTER_CLOSE]

    def command(self, kind: api.CommandKind) -> api.CommandV1:
        cmd = api.new_command(kind, self.now, "test")
        api.write_command(self.paths.inbox, cmd)
        return cmd


def late_start(tmp_path: Path, opened_at: datetime, restart_at: datetime) -> tuple[Bot, str]:
    """A trade opened and protected during the session, then a new bot process at
    `restart_at` (the watchdog of #129 may restart a down bot)."""
    bot = Bot(tmp_path, opened_at)
    t = bot.open()
    bot.now = restart_at
    bot.start()
    return bot, t


def test_late_start_after_the_close_sends_nothing_keeps_the_stop_and_alerts(tmp_path):
    bot, t = late_start(tmp_path, MONDAY, MONDAY_CLOSE + timedelta(minutes=5))
    stop = bot.stops()[0].client_order_id
    bot.run(10)
    assert bot.markets() == []
    assert [o.client_order_id for o in bot.stops()] == [stop]  # resting, never cancelled
    trade = bot.engine.trade(t)
    assert trade.state is TradeState.EXITING and trade.exit_requested == "session-flatten"
    name = f"{Fence.MARKET_CLOSED}:{t}"
    (row,) = bot.store.journal("fence-set", name)  # one recorded cause
    assert row.body["cause"].startswith(f"session-flatten of 2 ABC not sent: {LATE}")
    assert "Webull app" in row.body["cause"]
    (alert,) = bot.alerts()
    assert alert["delivery"] == "shown" and alert["text"].startswith(name)


def test_the_refusal_sticks_into_the_next_session_until_the_operator_clears_it(tmp_path):
    bot, t = late_start(tmp_path, MONDAY, MONDAY_CLOSE + timedelta(minutes=5))
    bot.run(3)
    bot.now = datetime(2026, 11, 17, 9, 31, tzinfo=NEW_YORK)  # next open, same process
    bot.run(5)
    assert bot.markets() == [] and len(bot.stops()) == 1  # nobody decided: nothing sold
    assert bot.engine.clear_operator_fence(f"{Fence.MARKET_CLOSED}:{t}")
    bot.run(8)
    assert len(bot.markets()) == 1
    assert bot.engine.trade(t).state is TradeState.CLOSED


def test_a_start_between_the_flatten_time_and_the_close_still_flattens(tmp_path):
    day = CALENDAR.day_at(MONDAY)
    flatten = day.before_close(CFG.session.flatten_minutes_before_close)
    bot, t = late_start(tmp_path, MONDAY, flatten + (day.regular_close - CUTOFF - flatten) / 2)
    bot.run(8)
    assert len(bot.markets()) == 1
    assert bot.engine.trade(t).state is TradeState.CLOSED
    assert bot.alerts() == [] and f"{Fence.MARKET_CLOSED}:{t}" not in bot.engine.fences()


@pytest.mark.parametrize(("minutes_after_close", "sent"), [(-3, True), (5, False)])
def test_a_half_day_uses_its_early_close(tmp_path, minutes_after_close, sent):
    day = CALENDAR.day_at(HALF_DAY)
    assert day.half_day and day.regular_close.hour == 13
    bot, t = late_start(tmp_path, HALF_DAY,
                        day.regular_close + timedelta(minutes=minutes_after_close))
    bot.run(8)
    assert (len(bot.markets()) == 1) is sent
    assert (bot.engine.trade(t).state is TradeState.CLOSED) is sent
    assert (len(bot.alerts()) == 1) is not sent
    assert len(bot.stops()) == (0 if sent else 1)


def test_operator_flatten_after_the_close_is_refused_and_sends_nothing(tmp_path):
    bot = Bot(tmp_path, MONDAY)
    t = bot.open()
    bot.now = MONDAY_CLOSE + timedelta(minutes=1)
    cmd = bot.command(api.CommandKind.FLATTEN)
    bot.service.process_commands()
    (rec,) = bot.store.journal(service.COMMAND, cmd.command_id)
    assert rec.body["outcome"] == api.CommandOutcome.REFUSED
    assert rec.body["cause"].startswith(f"no closing order is sent now: {LATE}")
    assert not bot.engine.trade(t).exit_requested
    assert bot.markets() == [] and len(bot.stops()) == 1


def test_emergency_after_the_close_stops_entries_but_refuses_the_flatten(tmp_path):
    bot = Bot(tmp_path, MONDAY)
    t = bot.open()
    bot.now = MONDAY_CLOSE + timedelta(minutes=1)
    cmd = bot.command(api.CommandKind.EMERGENCY)
    bot.service.process_commands()
    for _ in range(3):
        bot.engine.step()
    assert str(Fence.ENTRY_STOP) in bot.engine.fences()
    (rec,) = bot.store.journal(service.COMMAND, cmd.command_id)
    assert rec.body["outcome"] == api.CommandOutcome.REFUSED
    assert "flatten: no closing order is sent now" in rec.body["cause"]
    assert not bot.engine.trade(t).exit_requested
    assert bot.markets() == [] and len(bot.stops()) == 1


HALF_CLOSE = CALENDAR.day_at(HALF_DAY).regular_close


@pytest.mark.parametrize(("at", "cause"), [
    (datetime(2026, 11, 16, 9, 29, tzinfo=NEW_YORK), "before the regular open 09:30 ET"),
    (MONDAY_CLOSE - CUTOFF, LATE),
    (HALF_CLOSE - CUTOFF, f"at or after {HALF_CLOSE - CUTOFF:%H:%M:%S} ET"),
    (datetime(2026, 11, 21, 11, 0, tzinfo=NEW_YORK), "no regular session now (WEEKEND"),
])
def test_close_refusal_outside_the_regular_session(at, cause):
    refusal = close_refusal(CFG, CALENDAR, at)
    assert refusal is not None and refusal.startswith(cause)


def test_close_allowed_inside_the_session_and_without_a_calendar():
    assert close_refusal(CFG, CALENDAR, MONDAY) is None
    assert close_refusal(CFG, CALENDAR, MONDAY_CLOSE - CUTOFF - timedelta(seconds=1)) is None
    assert close_refusal(CFG, None, MONDAY_CLOSE + timedelta(hours=1)) is None


def test_flatten_while_market_closed_is_set_names_that_cause(tmp_path):
    bot, t = late_start(tmp_path, MONDAY, MONDAY_CLOSE + timedelta(minutes=5))
    bot.run(3)
    assert f"{Fence.MARKET_CLOSED}:{t}" in bot.engine.fences()
    bot.now = datetime(2026, 11, 17, 10, 0, tzinfo=NEW_YORK)  # in session: only the fence
    cmd = bot.command(api.CommandKind.FLATTEN)
    bot.service.process_commands()
    (rec,) = bot.store.journal(service.COMMAND, cmd.command_id)
    assert rec.body["outcome"] == api.CommandOutcome.REFUSED
    assert rec.body["cause"].startswith(f"a close of {t} was due outside the regular session")
    assert "refused before" not in rec.body["cause"]
    assert bot.markets() == [] and len(bot.stops()) == 1
