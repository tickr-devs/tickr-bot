from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import cli, config
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.config import GateResult
from aperture.engine.core import Engine, Fence, Leg, TradeState
from aperture.engine.manual import ManualRefused, check_window, plan_probe, run_until_closed
from aperture.store import Store

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CAL = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY_11 = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)


def test_plan_defaults():
    plan = plan_probe(CFG, "abc", D("20.00"))
    assert plan.symbol == "ABC" and plan.quantity == D(CFG.probe.quantity)
    assert plan.stop_price == D("19.60")
    assert plan.planned_loss_usd == D("0.40")


@pytest.mark.parametrize("kwargs, match", [
    ({"limit_price": D("50.01")}, "at most"),
    ({"limit_price": D("10.001")}, "cents"),
    ({"limit_price": D("10"), "stop_price": D("10")}, "below the limit"),
    ({"limit_price": D("49"), "stop_price": D("40")}, "planned loss"),
    ({"limit_price": D("10"), "quantity": D(CFG.probe.quantity + 1)}, "whole number"),
])
def test_plan_refusals(kwargs, match):
    with pytest.raises(ManualRefused, match=match):
        plan_probe(CFG, "ABC", **kwargs)


def test_window():
    flatten = check_window(CFG, CAL, MONDAY_11)
    assert flatten.hour == 15 and flatten.minute == 60 - CFG.session.flatten_minutes_before_close
    with pytest.raises(ManualRefused, match="probe window"):
        check_window(CFG, CAL, MONDAY_11.replace(hour=9, minute=45))
    with pytest.raises(ManualRefused, match="session"):
        check_window(CFG, CAL, datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK))  # Saturday


class Market(FakeBroker):
    """Entry and exit orders fill one poll after they are placed (unless `fill_entries` off)."""

    fill_entries = True

    def tick(self) -> None:
        for cid, order in list(self.orders.items()):
            if order.state.terminal or order.order_type == "STOP_LOSS":
                continue
            if order.side == "BUY" and not self.fill_entries:
                continue
            self.fill(cid, order.quantity - order.filled_quantity, D(10))


def drive(engine, market, clock, **kwargs):
    def sleep(seconds):
        clock[0] += seconds
        market.tick()

    run_until_closed(engine, sleep=sleep, monotonic=lambda: clock[0],
                     now=lambda: MONDAY_11, interrupted=lambda: False, **kwargs)


def make(tmp_path, market):
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    return Engine(store, market, CFG, "manual01", lambda: GateResult(True, "t"))


def test_hold_then_exit(tmp_path):
    market = Market()
    engine = make(tmp_path, market)
    t = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    clock = [0.0]
    drive(engine, market, clock, flatten_at=None, hold_seconds=30)
    trade = engine.trade(t)
    assert trade.state is TradeState.CLOSED and trade.exit_requested == "hold-elapsed"
    assert [o.leg for o in engine.orders(t)] == [Leg.ENTRY, Leg.STOP, Leg.EXIT]
    assert market.positions() == []
    assert clock[0] >= 30


def test_unfilled_entry_is_cancelled(tmp_path):
    market = Market()
    market.fill_entries = False
    engine = make(tmp_path, market)
    t = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    clock = [0.0]
    drive(engine, market, clock, flatten_at=None, hold_seconds=30)
    trade = engine.trade(t)
    assert trade.state is TradeState.CLOSED
    assert trade.exit_requested == "entry-not-filled-in-time"
    assert [o.leg for o in engine.orders(t)] == [Leg.ENTRY]
    assert clock[0] >= CFG.probe.entry_wait_seconds


def test_flatten_deadline(tmp_path):
    market = Market()
    engine = make(tmp_path, market)
    t = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    drive(engine, market, [0.0], flatten_at=MONDAY_11, hold_seconds=None)
    assert engine.trade(t).exit_requested == "session-flatten"


def copy_config(tmp_path) -> Path:
    (tmp_path / "config").mkdir()
    bot = tmp_path / "config" / "bot.toml"
    bot.write_text((ROOT / "config" / "bot.toml").read_text("utf-8"), "utf-8")
    calendar_dir = tmp_path / Path(CFG.session.calendar_file).parent
    calendar_dir.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / Path(CFG.session.calendar_file).parent).iterdir():
        (calendar_dir / f.name).write_bytes(f.read_bytes())
    return bot


def test_cli_trade_end_to_end(tmp_path, monkeypatch, capsys):
    bot = copy_config(tmp_path)
    market = Market()
    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: market)
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "SLEEP", lambda s: market.tick())
    monkeypatch.setattr(cli.time, "monotonic", iter(range(0, 10**6, 5)).__next__)

    assert cli.main(["--config", str(bot), "trade", "ABC", "--limit", "10.00"]) == 0
    out = capsys.readouterr().out
    assert "closed (hold-elapsed)" in out
    assert market.positions() == []

    market.fill_entries = False
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11.replace(hour=16, minute=30))
    assert cli.main(["--config", str(bot), "trade", "ABC", "--limit", "10.00"]) == 3


def test_cli_exit_closes_open_trade_after_restart(tmp_path, monkeypatch, capsys):
    bot = copy_config(tmp_path)
    cfg = config.load(bot)
    database, _ = cli.data_paths(cfg)
    market = Market()
    store = Store.open(database, busy_timeout_seconds=0)
    engine = Engine(store, market, cfg, "crashed1", lambda: GateResult(True, "t"))
    t = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    market.tick()
    engine.step()  # filled and stop placed, then the process "dies"
    store.close()

    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: market)
    monkeypatch.setattr(cli, "SLEEP", lambda s: market.tick())
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    assert cli.main(["--config", str(bot), "exit"]) == 0
    assert f"trade {t} ABC: closed (operator-exit)" in capsys.readouterr().out
    assert market.positions() == []
    assert cli.main(["--config", str(bot), "clear-fence", "unprotected:t1"]) == 1


def test_resume_after_close_flattens_at_once(tmp_path, monkeypatch, capsys):
    bot = copy_config(tmp_path)
    cfg = config.load(bot)
    database, _ = cli.data_paths(cfg)
    market = Market()
    store = Store.open(database, busy_timeout_seconds=0)
    engine = Engine(store, market, cfg, "crashed2", lambda: GateResult(True, "t"))
    t = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    market.tick()
    for _ in range(3):
        engine.step()
    store.close()
    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: market)
    monkeypatch.setattr(cli, "SLEEP", lambda s: market.tick())
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11.replace(hour=17))  # after the close
    assert cli.main(["--config", str(bot), "resume"]) == 0
    assert f"trade {t} ABC: closed (session-flatten)" in capsys.readouterr().out
    assert market.positions() == []


def test_flatten_deadline_from_calendar():
    from aperture.engine.manual import flatten_deadline
    assert flatten_deadline(CFG, CAL, MONDAY_11) > MONDAY_11
    late = MONDAY_11.replace(hour=15, minute=59)
    assert flatten_deadline(CFG, CAL, late) == late
    saturday = datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK)
    assert flatten_deadline(CFG, CAL, saturday) == saturday


def test_refused_close_never_loops(tmp_path):
    market = Market()
    engine = make(tmp_path, market)
    t = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    clock = [0.0]
    sends = []
    real_place = market.place

    def place(request):
        sends.append(request.order_type)
        if request.order_type == "MARKET":
            from aperture.broker.base import Refused
            raise Refused("CLOSED", "refused at the session edge")
        return real_place(request)

    market.place = place

    def sleep(seconds):
        clock[0] += seconds
        market.tick()

    waiting = run_until_closed(engine, sleep=sleep, monotonic=lambda: clock[0],
                               now=lambda: MONDAY_11, interrupted=lambda: True,
                               flatten_at=None, hold_seconds=None)
    assert waiting == [t]
    assert sends.count("MARKET") == 1
    stops = [o for o in market.open_orders() if o.order_type == "STOP_LOSS"]
    assert sum(o.quantity for o in stops) == D(1)


def test_a_close_that_did_not_fill_waits_for_the_operator_never_loops(tmp_path):
    """A halt: the close never fills; once the operator cancels it in the app the stop is back
    and the manual run returns, with one close sent."""
    clock = [0.0]

    class Halted(Market):
        engine: Engine

        def tick(self) -> None:
            for cid, order in list(self.orders.items()):
                if order.state.terminal or order.order_type == "STOP_LOSS":
                    continue
                if order.side == "BUY":
                    self.fill(cid, order.quantity, D(10))
                elif f"{Fence.EXIT_NOT_FILLED}:t1" in self.engine.fences():
                    self.cancel(cid)  # the operator cancels the stuck close in the app

    market = Halted()
    start = MONDAY_11.astimezone(UTC)
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                       now=lambda: start + timedelta(seconds=clock[0]))
    engine = Engine(store, market, CFG, "manual01", lambda: GateResult(True, "t"),
                    clock=lambda: clock[0])
    market.engine = engine
    t = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))

    def sleep(seconds):
        clock[0] += seconds
        market.tick()

    waiting = run_until_closed(engine, sleep=sleep, monotonic=lambda: clock[0],
                               now=lambda: MONDAY_11, interrupted=lambda: True,
                               flatten_at=None, hold_seconds=None)
    assert waiting == [t]
    assert [r.order_type for r in market.sent].count("MARKET") == 1
    stops = [o for o in market.open_orders() if o.order_type == "STOP_LOSS"]
    assert sum(o.quantity for o in stops) == D(1)


def test_default_stop_is_derived_from_the_fill(tmp_path):
    market = Market()
    engine = make(tmp_path, market)
    plan = plan_probe(CFG, "ABC", D("10.50"))
    t = engine.open_trade(plan.symbol, plan.quantity, plan.limit_price, plan.stop_price,
                          plan.planned_loss_usd, CFG.probe.stop_percent_below_fill)
    market.fill(engine.orders(t)[0].client_order_id, D(1), D("10.00"))  # filled below limit
    for _ in range(2):
        engine.step()
    (stop,) = [o for o in engine.orders(t) if o.leg is Leg.STOP]
    assert stop.stop_price == D("9.80")  # 2 % below the 10.00 fill, not below the 10.50 limit
    assert plan.stop_price == D("10.29")


def test_operator_fences_are_reported_once(tmp_path):
    from aperture.broker.base import Refused

    market = Market()
    clock = [0.0]
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    engine = Engine(store, market, CFG, "manual01", lambda: GateResult(True, "t"),
                    clock=lambda: clock[0])
    engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    real_place = market.place

    def place(request):
        if request.order_type in ("MARKET", "STOP_LOSS"):
            raise Refused("CLOSED", "refused")
        return real_place(request)

    market.place = place
    lines = []

    class Done(Exception):
        pass

    def sleep(seconds):
        clock[0] += seconds
        market.tick()
        if clock[0] > 600:
            raise Done

    with pytest.raises(Done):  # unprotected and unclosable: never "waiting", never closed
        run_until_closed(engine, sleep=sleep, monotonic=lambda: clock[0],
                         now=lambda: MONDAY_11, interrupted=lambda: False, flatten_at=None,
                         hold_seconds=None, notify=lines.append)
    names = [line.split(": ")[1] for line in lines]
    assert any(n.startswith("stop-rejected:") for n in names)
    assert any(n.startswith("exit-refused:") for n in names)
    assert len(names) == len(set(names))
