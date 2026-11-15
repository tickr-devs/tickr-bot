"""Probe F5 (replace a stop's price) against the fake broker: outcomes, refusals, ordering."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import cli, config
from tickr.broker.base import BrokerError
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK
from tickr.config import GateResult
from tickr.data.base import FakeMarketData
from tickr.engine.core import Engine, Fence, Leg, TradeState
from tickr.engine.manual import ManualRefused
from tickr.engine.replace_probe import Verdict, observe, plan_replace, precheck, send
from tickr.store import Store
from tickr.types import OrderState, Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
NOW = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)


POLL = float(CFG.orders.detail_poll_seconds)
INTERVAL = float(CFG.orders.reconcile_interval_seconds)


class Market(FakeBroker):
    """Entry and exit orders fill at 10 on `tick`; stops rest. `now` is the engine's clock and
    moves one Detail poll per tick."""

    now = 1_000_000.0

    def tick(self) -> None:
        self.now += POLL
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
    engine = Engine(store, broker, CFG, run_id, lambda: GateResult(True, "t"),
                    clock=lambda: broker.now)
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
    assert engine.trade(trade).stop_price == D("9.90")
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
    for _ in range(20):
        engine.step()
        broker.tick()
    assert engine.trade(trade).state is TradeState.CLOSED
    assert broker.positions() == [] and broker.open_orders() == []


def hide_stop_price(broker):
    """Webull rows without `stop_price` (the field name is unproven, docs/webull-facts.md F5)."""
    from dataclasses import replace
    detail, open_orders = broker.order_detail, broker.open_orders
    broker.order_detail = lambda cid: replace(detail(cid), stop_price=None)
    broker.open_orders = lambda: [replace(o, stop_price=None) for o in open_orders()]


def working_sells(broker) -> list[str]:
    return [o.client_order_id for o in broker.open_orders() if o.side == "SELL"]


def test_new_id_without_price_shown_is_adopted_not_doubled(setup):
    engine, broker, trade, stop = setup
    hide_stop_price(broker)
    broker.replace_outcomes.append("new-id")
    verdict, _ = run(engine)
    assert verdict is Verdict.ADOPTED
    new = engine.order(f"{stop.client_order_id}-r")
    assert new.stop_price == D("9.90") and new.verified  # the expectation; shown: none
    engine.reconcile()
    assert working_sells(broker) == [new.client_order_id]
    assert [o.order_type for o in broker.sent] == ["LIMIT", "STOP_LOSS"]
    engine.request_exit(trade, "operator-exit")
    for _ in range(20):
        engine.step()
        broker.tick()
    assert broker.positions() == [] and broker.open_orders() == []


def test_unresolved_replace_never_adds_a_second_stop_or_sells(setup):
    engine, broker, trade, stop = setup
    hide_stop_price(broker)
    broker.replace_outcomes.append("new-id")
    reads = broker.open_orders

    def fail_once():
        from tickr.broker.base import OutcomeUnknown
        broker.open_orders = reads
        raise OutcomeUnknown("read lost")

    broker.open_orders = fail_once  # observe cannot see the new order: not adopted
    verdict, _ = run(engine)
    assert verdict is Verdict.UNRESOLVED
    foreign = f"{stop.client_order_id}-r"
    engine.reconcile()
    engine.request_exit(trade, "operator-exit")
    for _ in range(6):
        engine.step()
        broker.tick()
    assert working_sells(broker) == [foreign]  # Webull's only: no stop, no sale from us
    assert [o.order_type for o in broker.sent] == ["LIMIT", "STOP_LOSS"]
    assert broker.positions()[0].quantity == D(1) and fence(stop) in engine.fences()
    broker.cancel(foreign)  # the operator resolves it in the app
    for _ in range(20):
        engine.reconcile()
        broker.tick()
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


def held(setup):
    """The unresolved case above: Webull's new stop is untracked and our stop is gone."""
    engine, broker, trade, stop = setup
    hide_stop_price(broker)
    broker.replace_outcomes.append("new-id")
    reads = broker.open_orders

    def fail_once():
        from tickr.broker.base import OutcomeUnknown
        broker.open_orders = reads
        raise OutcomeUnknown("read lost")

    broker.open_orders = fail_once
    assert run(engine)[0] is Verdict.UNRESOLVED
    engine.reconcile()
    engine.request_exit(trade, "operator-exit")
    return engine, broker, trade, f"{stop.client_order_id}-r"


def holds(engine, trade) -> list[str]:
    return [r.body["cause"] for r in engine.store.journal("fence-set",
                                                          f"{Fence.STOP_HELD}:{trade}")]


def counting(broker) -> list[int]:
    reads, real = [], broker.open_orders
    broker.open_orders = lambda: reads.append(1) or real()
    return reads


def test_hold_is_shown_once_per_episode(setup):
    engine, broker, trade, foreign = held(setup)
    for _ in range(20):
        engine.step()
        broker.tick()
    engine.reconcile()
    assert holds(engine, trade) == [
        f"stop and close held: untracked SELL {foreign} rests in ABC after an unresolved "
        "replace; resolve it in the Webull app"]
    assert engine.fences()[f"{Fence.STOP_HELD}:{trade}"] == holds(engine, trade)[0]
    broker.cancel(foreign)  # the operator resolves it; the requested exit then sells
    for _ in range(10):
        engine.step()
        broker.tick()
    assert f"{Fence.STOP_HELD}:{trade}" not in engine.fences()
    assert broker.positions() == [] and broker.open_orders() == []
    assert [o.order_type for o in broker.sent] == ["LIMIT", "STOP_LOSS", "MARKET"]
    assert len(holds(engine, trade)) == 1


def test_hold_reads_open_orders_at_most_once_per_interval(setup):
    engine, broker, trade, foreign = held(setup)
    reads = counting(broker)
    passes = 40
    for _ in range(passes):
        engine.step()
        broker.tick()
    assert 0 < len(reads) <= passes * POLL / INTERVAL + 1
    assert working_sells(broker) == [foreign]


def test_a_failed_read_holds(setup):
    engine, broker, trade, foreign = held(setup)
    broker.cancel(foreign)  # gone at Webull, but no read can show it
    real = broker.open_orders
    broker.open_orders = lambda: (_ for _ in ()).throw(BrokerError("read failed"))
    for _ in range(10):
        engine.step()
        broker.tick()
    assert [o.order_type for o in broker.sent] == ["LIMIT", "STOP_LOSS"]
    assert broker.positions()[0].quantity == D(1)
    assert f"{Fence.STOP_HELD}:{trade}" in engine.fences()
    broker.open_orders = real
    for _ in range(10):
        engine.step()
        broker.tick()
    assert broker.positions() == [] and broker.open_orders() == []


def test_a_read_from_before_the_replace_never_releases(setup):
    """Review of #72: a reconcile read in flight during the replace returns the old state."""
    engine, broker, trade, stop = setup
    hide_stop_price(broker)
    broker.replace_outcomes.append("new-id")
    before = broker.open_orders()  # the old stop working, nothing foreign
    plan = plan_replace(engine, quotes(), D("9.90"), NOW)
    assert send(engine, plan, print)
    real = broker.open_orders

    def stale_once():
        broker.open_orders = real
        return before

    broker.open_orders = stale_once
    engine.reconcile()
    for _ in range(3):
        engine.step()
    assert working_sells(broker) == [f"{stop.client_order_id}-r"]
    assert [o.order_type for o in broker.sent] == ["LIMIT", "STOP_LOSS"]
    assert f"{Fence.STOP_HELD}:{trade}" in engine.fences()


def test_new_order_listed_one_read_late(setup):
    engine, broker, trade, stop = setup
    hide_stop_price(broker)
    broker.replace_outcomes.append("new-id")
    plan = plan_replace(engine, quotes(), D("9.90"), NOW)
    assert send(engine, plan, print)
    new = f"{stop.client_order_id}-r"
    real, late = broker.open_orders, [1]

    def listed_late():
        rows = real()
        if late and engine.order(stop.client_order_id).live is False:
            late.pop()  # the first read after we saw the old stop end lacks the new order
            return [o for o in rows if o.client_order_id != new]
        return rows

    broker.open_orders = listed_late
    for _ in range(10):
        engine.step()
        broker.tick()
    assert not late  # the late read happened
    assert working_sells(broker) == [new]
    assert [o.order_type for o in broker.sent] == ["LIMIT", "STOP_LOSS"]
    assert f"untracked SELL {new}" in engine.fences()[f"{Fence.STOP_HELD}:{trade}"]


def test_exit_loop_shows_the_hold_once_then_sells_when_resolved(setup):
    from tickr.engine.manual import run_until_closed
    engine, broker, trade, foreign = held(setup)
    lines = []

    def sleep(seconds):
        broker.tick()
        if broker.now >= engine.clock() and len(lines) and foreign in working_sells(broker):
            broker.cancel(foreign)  # the operator, after reading the line

    started = broker.now
    assert run_until_closed(engine, sleep=sleep, monotonic=lambda: broker.now - started,
                            now=lambda: NOW, flatten_at=None, interrupted=lambda: False,
                            hold_seconds=None, notify=lines.append) == []
    held_lines = [line for line in lines if Fence.STOP_HELD in line]
    assert len(held_lines) == 1 and f"untracked SELL {foreign}" in held_lines[0]
    assert broker.positions() == [] and broker.open_orders() == []


# -- check-quote ---------------------------------------------------------------------------


class NoOrders(Market):
    """Any order method fails the test: check-quote is read-only."""

    def place(self, request):
        pytest.fail("check-quote placed an order")

    def cancel(self, client_order_id):
        pytest.fail("check-quote cancelled an order")

    def replace_stop_price(self, *args):
        pytest.fail("check-quote replaced an order")


class FailingQuotes(FakeMarketData):
    def snapshot(self, symbols):
        from tickr.data.base import MarketDataError
        raise MarketDataError("read failed")


@pytest.mark.parametrize("market, code, verdict", [
    (quotes(age=1), 0, "OK: quote is 1.0 s old (limit 5 s)"),
    (quotes(age=900), 3, "STALE: quote is 900.0 s old (limit 5 s): the probes will refuse; "
                         "the quotes may be delayed"),
    (FailingQuotes(), 3, "refused: quote read failed: MarketDataError"),
    (FakeMarketData(), 3, "refused: no quote for ABC"),
])
def test_cli_check_quote(monkeypatch, capsys, market, code, verdict):
    assert CFG.risk.max_quote_age_seconds == D(5)  # the verdict texts below assume it
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: (NoOrders(), market))
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    bot = str(ROOT / "config" / "bot.toml")
    assert cli.main(["--config", bot, "check-quote", "ABC"]) == code
    captured = capsys.readouterr()
    assert verdict in captured.out + captured.err
    if code == 0 or "STALE" in verdict:
        assert "bid 10.00 ask 10.02" in captured.out and "provider time" in captured.out
