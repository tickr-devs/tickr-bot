"""Probe F5 (replace a stop's price) against the fake broker: outcomes, refusals, ordering."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import cli, config
from aperture.broker.fake import FakeBroker
from aperture.calendar import NEW_YORK
from aperture.config import GateResult
from aperture.data.base import FakeMarketData
from aperture.engine.core import Engine, Fence, Leg, TradeState
from aperture.engine.manual import ManualRefused
from aperture.engine.replace_probe import Verdict, observe, plan_replace, precheck, send
from aperture.store import Store
from aperture.types import OrderState, Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
NOW = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)


class Market(FakeBroker):
    """Entry and exit orders fill at 10 on `tick`; stops rest."""

    def tick(self) -> None:
        for cid, order in list(self.orders.items()):
            if not order.state.terminal and order.order_type != "STOP_LOSS":
                self.fill(cid, order.quantity - order.filled_quantity, D(10))


def quotes(bid="10.00", age=0) -> FakeMarketData:
    market = FakeMarketData()
    at = NOW - timedelta(seconds=age)
    market.set_quote(Quote("ABC", D(bid), D(bid) + D("0.02"), D(100), D(100), at, NOW))
    return market


def protected(store: Store, broker: Market, run_id="run00001") -> tuple[Engine, str]:
    """One open trade, 1 ABC filled at 10, a verified stop at 9.80."""
    engine = Engine(store, broker, CFG, run_id, lambda: GateResult(True, "t"))
    trade = engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    broker.tick()
    for _ in range(3):
        engine.step()
    return engine, trade


@pytest.fixture
def setup(tmp_path):
    broker = Market()
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    engine, trade = protected(store, broker)
    (stop,) = [o for o in engine.orders(trade) if o.leg is Leg.STOP]
    assert stop.verified and engine.fences() == {}
    return engine, broker, trade, stop


def run(engine, new="9.90", market=None):
    plan = plan_replace(engine, market or quotes(), D(new), NOW)
    lines: list[str] = []
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    if not send(engine, plan, lines.append):
        return Verdict.NOT_SENT, lines
    return observe(engine, plan, sleep=sleep, monotonic=lambda: clock[0], out=lines.append), lines


def fence(stop) -> str:
    return f"{Fence.REPLACE_UNRESOLVED}:{stop.client_order_id}"


def kinds(engine, cid) -> list[str]:
    return [r.kind for r in engine.store.journal(key=cid) if r.kind.startswith("replace-")]


def test_accepted_same_id(setup):
    engine, broker, trade, stop = setup
    verdict, lines = run(engine)
    assert verdict is Verdict.CONFIRMED
    assert broker.replaced == [(stop.client_order_id, D(1), D("9.90"))]
    assert engine.order(stop.client_order_id).stop_price == D("9.90")
    assert engine.fences() == {}
    assert kinds(engine, stop.client_order_id) == [
        "replace-intent", "replace-attempted", "replace-acknowledged", "replace-confirmed"]
    assert any("stop_price=9.90" in line for line in lines)
    engine.step()
    assert engine.fences() == {} and len(broker.sent) == 2  # entry and stop only


def test_new_id_is_adopted_and_exit_tears_it_down(setup):
    engine, broker, trade, stop = setup
    broker.replace_outcomes.append("new-id")
    verdict, _ = run(engine)
    assert verdict is Verdict.ADOPTED
    assert fence(stop) in engine.fences()
    stops = [o for o in engine.orders(trade) if o.leg is Leg.STOP]
    assert [s.client_order_id for s in stops] == [stop.client_order_id,
                                                  f"{stop.client_order_id}-r"]
    assert stops[0].broker_state is OrderState.CANCELLED and stops[1].verified
    engine.reconcile()  # no foreign order, no second stop
    assert not any(n.startswith(Fence.FOREIGN_ORDER) for n in engine.fences())
    assert [o.order_type for o in broker.sent] == ["LIMIT", "STOP_LOSS"]
    engine.request_exit(trade, "operator-exit")
    for _ in range(6):
        engine.step()
        broker.tick()
    assert engine.trade(trade).state is TradeState.CLOSED
    assert broker.positions() == [] and broker.open_orders() == []


@pytest.mark.parametrize("outcome", ["refuse", "rate-limited"])
def test_refused_changes_nothing(setup, outcome):
    engine, broker, trade, stop = setup
    broker.replace_outcomes.append(outcome)
    verdict, _ = run(engine)
    assert verdict is Verdict.NOT_SENT
    assert engine.fences() == {}
    assert engine.order(stop.client_order_id).stop_price == D("9.80")
    assert kinds(engine, stop.client_order_id) == [
        "replace-intent", "replace-attempted", "replace-not-sent"]


def test_unknown_outcome_reads_detail_and_never_resends(setup):
    engine, broker, trade, stop = setup
    broker.replace_outcomes.append("unknown")  # changed at the broker, answer lost
    reads = []
    real_detail = broker.order_detail
    broker.order_detail = lambda cid: reads.append(cid) or real_detail(cid)
    verdict, lines = run(engine)
    assert len(broker.replaced) == 1
    assert reads and set(reads) == {stop.client_order_id}
    assert verdict is Verdict.CONFIRMED  # Detail resolved it
    assert "replace-unknown" in kinds(engine, stop.client_order_id)
    assert any("NOT resending" in line for line in lines)


@pytest.mark.parametrize("outcome", ["unknown-lost", "not-found"])
def test_unknown_outcome_not_changed_stays_fenced(setup, outcome):
    engine, broker, trade, stop = setup
    broker.replace_outcomes.append(outcome)
    verdict, _ = run(engine)
    assert verdict is Verdict.UNRESOLVED and len(broker.replaced) == 1
    assert fence(stop) in engine.fences()
    assert engine.order(stop.client_order_id).stop_price == D("9.80")
    engine.step()  # protection unchanged; the operator fence blocks entries only
    assert engine.order(stop.client_order_id).verified and len(broker.sent) == 2
    assert engine.clear_operator_fence(fence(stop))


def test_journal_and_fence_are_written_before_the_request(setup):
    engine, broker, trade, stop = setup
    seen = {}
    real = broker.replace_stop_price

    def replace_stop_price(cid, quantity, price):
        seen["kinds"] = kinds(engine, cid)
        seen["fences"] = set(engine.fences())
        return real(cid, quantity, price)

    broker.replace_stop_price = replace_stop_price
    run(engine)
    assert seen["kinds"] == ["replace-intent", "replace-attempted"]
    assert seen["fences"] == {fence(stop)}


def test_crash_after_attempt_leaves_entries_fenced(tmp_path, setup):
    engine, broker, trade, stop = setup

    def crash(*_args):
        raise SystemExit("killed mid-request")

    broker.replace_stop_price = crash
    plan = plan_replace(engine, quotes(), D("9.90"), NOW)
    with pytest.raises(SystemExit):
        send(engine, plan, print)
    restarted = Engine(engine.store, broker, CFG, "run00002", lambda: GateResult(True, "t"))
    restarted.recover()
    assert fence(stop) in restarted.fences()


@pytest.mark.parametrize("new, market, match", [
    ("9.80", None, "only tightening"),
    ("9.70", None, "only tightening"),
    ("10.40", quotes("11.00"), "exceeds"),
    ("9.905", None, "cents"),
    ("9.95", quotes("9.95"), "below the bid"),
    ("9.90", quotes("10.00", age=60), "old"),
    ("9.90", FakeMarketData(), "no quote"),
])
def test_price_refusals(setup, new, market, match):
    engine, broker, _, _ = setup
    with pytest.raises(ManualRefused, match=match):
        plan_replace(engine, market or quotes(), D(new), NOW)
    assert broker.replaced == []


def test_refuses_without_a_position(tmp_path):
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    engine = Engine(store, Market(), CFG, "run00001", lambda: GateResult(True, "t"))
    with pytest.raises(ManualRefused, match="exactly one open trade"):
        precheck(engine)


def test_refuses_an_unverified_stop(tmp_path):
    broker = Market()
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    engine = Engine(store, broker, CFG, "run00001", lambda: GateResult(True, "t"))
    engine.open_trade("ABC", D(1), D(10), D("9.80"), D("0.2"))
    broker.tick()
    engine.step()  # stop placed and acknowledged, not yet seen working by Detail
    with store.transaction():  # fences block first; drop them to reach the stop check
        store.execute("DELETE FROM fences")
    with pytest.raises(ManualRefused, match="verified working stop"):
        plan_replace(engine, quotes(), D("9.90"), NOW)


def test_refuses_while_fenced_or_exit_requested(setup):
    engine, broker, trade, _ = setup
    broker.add_foreign_position("XYZ", D(1), D(5))
    engine.step()
    with pytest.raises(ManualRefused, match="fences set"):
        plan_replace(engine, quotes(), D("9.90"), NOW)
    del broker.holdings["XYZ"]
    engine.step()
    engine.request_exit(trade, "operator-exit")
    with pytest.raises(ManualRefused, match="exit requested"):
        precheck(engine)


def test_refuses_when_broker_stop_differs(setup):
    engine, broker, _, stop = setup
    from dataclasses import replace
    order = broker.orders[stop.client_order_id]
    broker.orders[stop.client_order_id] = replace(order, stop_price=D("9.70"))
    with pytest.raises(ManualRefused, match="Detail shows stop 9.70"):
        plan_replace(engine, quotes(), D("9.90"), NOW)


# -- command line -------------------------------------------------------------------------


def copy_config(tmp_path, text_change=None) -> Path:
    (tmp_path / "config").mkdir()
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    bot = tmp_path / "config" / "bot.toml"
    bot.write_text(text if text_change is None else text_change(text), "utf-8")
    calendar_dir = tmp_path / Path(CFG.session.calendar_file).parent
    calendar_dir.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / Path(CFG.session.calendar_file).parent).iterdir():
        (calendar_dir / f.name).write_bytes(f.read_bytes())
    return bot


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    monkeypatch.setattr(cli.time, "monotonic", iter(range(0, 10**6, 5)).__next__)


def crashed_trade(bot: Path) -> Market:
    cfg = config.load(bot)
    database, _ = cli.data_paths(cfg)
    broker = Market()
    store = Store.open(database, busy_timeout_seconds=0)
    protected(store, broker)
    store.close()
    return broker


def test_cli_probe_then_exit(tmp_path, monkeypatch, capsys):
    bot = copy_config(tmp_path)
    broker = crashed_trade(bot)
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: (broker, quotes()))
    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: broker)
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    monkeypatch.setattr(cli, "SLEEP", lambda s: broker.tick())
    assert cli.main(["--config", str(bot), "probe-replace-stop", "--to", "9.90"]) == 0
    out = capsys.readouterr().out
    assert "F5 result: confirmed" in out and "stop_price=9.90" in out
    assert cli.main(["--config", str(bot), "exit"]) == 0
    assert broker.positions() == [] and broker.open_orders() == []


def test_cli_refusals(tmp_path, monkeypatch, capsys):
    bot = copy_config(tmp_path)
    broker = crashed_trade(bot)
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: (broker, quotes()))
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    assert cli.main(["--config", str(bot), "probe-replace-stop", "--to", "9.70"]) == 3
    assert "only tightening" in capsys.readouterr().err
    monkeypatch.setattr(cli, "NOW", lambda: NOW.replace(hour=9, minute=45))
    assert cli.main(["--config", str(bot), "probe-replace-stop", "--to", "9.90"]) == 3
    assert "probe window" in capsys.readouterr().err
    assert broker.replaced == []


def test_cli_refuses_with_the_gate_closed(tmp_path, monkeypatch, capsys):
    bot = copy_config(tmp_path, lambda t: t.replace("trading_enabled = true",
                                                    "trading_enabled = false"))
    assert not config.trading_gate(config.load(bot)).open
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: pytest.fail("no broker contact"))
    assert cli.main(["--config", str(bot), "probe-replace-stop", "--to", "9.90"]) == 3
    assert "trading gate closed" in capsys.readouterr().err


def test_cli_unknown_outcome_needs_the_operator(tmp_path, monkeypatch, capsys):
    bot = copy_config(tmp_path)
    broker = crashed_trade(bot)
    broker.replace_outcomes.append("unknown-lost")
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: (broker, quotes()))
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    monkeypatch.setattr(cli, "SLEEP", lambda s: None)
    assert cli.main(["--config", str(bot), "probe-replace-stop", "--to", "9.90"]) == 4
    out = capsys.readouterr().out
    assert "F5 result: unresolved" in out and "fence replace-unresolved:" in out
    assert len(broker.replaced) == 1
