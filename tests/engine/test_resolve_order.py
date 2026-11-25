"""The operator path for an order no broker read can settle (`Engine.resolve_order`,
`tickr resolve-order`; backlog T19): a hard `order-conflict`, `order-missing` or
`status-unknown` would otherwise block entries forever."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from tickr import cli, config
from tickr.api.history import trade_net
from tickr.broker.fake import FakeBroker
from tickr.engine.core import ORDER_RESOLVED, Engine, Fence, Leg, SendState, TradeState
from tickr.runtime.entries import realized_loss_today
from tickr.store import Store
from tickr.types import OrderState

from .test_engine import CFG, OPEN, Clock, enter, has, legs, make, run
from .test_manual import MONDAY_11, copy_config


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    yield s
    s.close()


def contradicted_entry(store, environment: str = "test") -> tuple[FakeBroker, Engine, str, str]:
    """An entry of 2 that the broker shows as 20 filled, ended: a hard conflict that no read
    will ever clear (the trade can never close by itself)."""
    b = FakeBroker()
    run_id = store.start_run(environment=environment, config_digest="d", strategy="s",
                             strategy_version="1", code_version="x")
    e = Engine(store, b, CFG, run_id, lambda: OPEN, clock=Clock())
    t = enter(e, "2")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.orders[cid] = replace(b.orders[cid], filled_quantity=D(20), average_fill_price=D(10),
                            state=OrderState.FILLED, raw_status="FILLED")
    run(e, 12)
    assert f"{Fence.ORDER_CONFLICT}:{cid}" in e.fences()
    assert e.trade(t).state is not TradeState.CLOSED
    return b, e, t, cid


def test_a_never_ending_conflict_is_resolved_once_the_symbol_is_flat(store):
    b, e, t, cid = contradicted_entry(store)
    b.add_foreign_position("ABC", D(20), D(10))  # what the broker really bought
    refused = e.resolve_order(cid)
    assert refused is not None and "holds 20 ABC" in refused
    assert e.order(cid).live and f"{Fence.ORDER_CONFLICT}:{cid}" in e.fences()

    del b.holdings["ABC"]  # the operator sold it in the Webull app
    assert e.resolve_order(cid) is None
    assert not e.order(cid).live
    assert e.trade(t).state is TradeState.CLOSED
    (cause,) = store.query("SELECT close_cause FROM trades WHERE trade_id = ?", (t,))
    assert cause["close_cause"] == ORDER_RESOLVED
    assert not has(e, Fence.ORDER_CONFLICT)
    (row,) = store.journal(ORDER_RESOLVED, cid)
    assert "20 filled of 2" in row.body["evidence"] and row.body["trade_id"] == t
    sent = len(b.sent)
    run(e, 12)  # later reads and reconciles leave it resolved, and send nothing
    e.reconcile()
    assert e.fences() == {} and len(b.sent) == sent
    assert e.live_orders() == []


def test_resolved_money_is_unknown_so_the_daily_loss_refuses(store):
    b, e, t, cid = contradicted_entry(store)
    assert e.resolve_order(cid) is None
    assert trade_net(e.orders(t), D(0)) is None
    assert realized_loss_today(e, datetime.now(UTC)) is None


def test_refused_while_the_broker_lists_an_open_order_in_the_symbol(store):
    b, e, t, cid = contradicted_entry(store)
    b.orders[cid] = replace(b.orders[cid], state=OrderState.UNKNOWN)
    refused = e.resolve_order(cid)
    assert refused is not None and cid in refused and "cancel them" in refused
    assert e.order(cid).live


def test_refused_when_detail_describes_the_order(store):
    """A working stop the broker agrees with is the bot's to settle, never the operator's."""
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "2")
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(e, 4)
    (stop,) = legs(e, t, Leg.STOP)
    refused = e.resolve_order(stop.client_order_id)
    assert refused is not None and "Detail describes it" in refused
    assert e.order(stop.client_order_id).live
    assert e.trade(t).state is not TradeState.CLOSED


def test_refused_for_an_order_not_ours_or_not_live(store):
    b, e, t, cid = contradicted_entry(store)
    assert "not an order of ours" in (e.resolve_order("nope") or "")
    assert e.resolve_order(cid) is None
    assert "not live" in (e.resolve_order(cid) or "")


def test_an_acknowledged_stop_detail_cannot_find_is_resolved(store):
    """order-missing: the broker acknowledged the stop and Detail now says it does not exist;
    the operator sold the shares in the app."""
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "2")
    b.fill(legs(e, t, Leg.ENTRY)[0].client_order_id, D(2), D(10))
    run(e, 4)
    (stop,) = legs(e, t, Leg.STOP)
    del b.orders[stop.client_order_id]
    run(e, 6)
    assert f"{Fence.ORDER_MISSING}:{stop.client_order_id}" in e.fences()
    assert "holds 2 ABC" in (e.resolve_order(stop.client_order_id) or "")
    del b.holdings["ABC"]
    assert e.resolve_order(stop.client_order_id) is None
    assert e.trade(t).state is TradeState.CLOSED
    assert not has(e, Fence.ORDER_MISSING)


def test_a_resolved_order_working_again_at_the_broker_is_revived(store):
    """Broker is the source of truth: if the order shows up working again, it is ours again
    and the operator is called back (resurrected)."""
    b, e, t, cid = contradicted_entry(store)
    assert e.resolve_order(cid) is None
    b.orders[cid] = replace(b.orders[cid], filled_quantity=D(0), average_fill_price=None,
                            state=OrderState.ACCEPTED)
    e.reconcile()
    assert e.order(cid).live
    assert e.trade(t).state is not TradeState.CLOSED
    assert has(e, Fence.RESURRECTED)


def test_cli_resolve_order_with_the_bot_stopped(tmp_path, monkeypatch, capsys):
    """`tickr resolve-order <id>`: refused (exit 1, the cause printed) while the broker holds
    the shares; done (exit 0) once the operator closed them in the Webull app."""
    bot = copy_config(tmp_path)
    cfg = config.load(bot)
    database, _ = cli.data_paths(cfg)
    s = Store.open(database, busy_timeout_seconds=0)
    b, e, t, cid = contradicted_entry(s, cfg.environment_name)
    s.close()
    b.add_foreign_position("ABC", D(20), D(10))
    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: b)
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    assert cli.main(["--config", str(bot), "resolve-order", cid]) == 1
    assert "holds 20 ABC" in capsys.readouterr().err
    del b.holdings["ABC"]
    assert cli.main(["--config", str(bot), "resolve-order", cid]) == 0
    assert f"trade {t} closed as {ORDER_RESOLVED}" in capsys.readouterr().out


def test_a_resolved_unknown_order_listed_working_is_revived(store):
    """An order whose send outcome stayed unknown, resolved, then listed working by the
    broker: revived like any other (never left working while invisible to the loop)."""
    b = FakeBroker()
    e = make(store, b)
    b.script("unknown")
    t = enter(e, "2")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    real = b.orders.pop(cid)  # Detail cannot find it yet: unknown stays unknown
    assert e.order(cid).send_state is SendState.UNKNOWN
    with store.transaction():  # resolved by the operator (the command's effect)
        store.execute("UPDATE orders SET operator_resolved_at = ? WHERE client_order_id = ?",
                      (store.stamp(), cid))
        store.execute("UPDATE trades SET state = 'closed' WHERE trade_id = ?", (t,))
    assert e.live_orders() == []
    b.orders[cid] = real  # now listed working at the broker
    e.reconcile()
    assert e.order(cid).live and e.order(cid).operator_resolved_at is None
    assert e.trade(t).state is not TradeState.CLOSED
    assert has(e, Fence.RESURRECTED)


def test_cli_resolves_a_trade_with_a_recorded_fill_and_sends_nothing(tmp_path, monkeypatch,
                                                                     capsys):
    """1 of 3 filled and protected by a stop of 1, then Detail says 30 filled (a hard conflict
    forever). The operator sells the share and cancels the stop in the Webull app. The command
    closes the trade with no reconcile or step first, so no stop is placed again for the one
    share the records still expect (Fable review of #751)."""
    bot = copy_config(tmp_path)
    cfg = config.load(bot)
    database, _ = cli.data_paths(cfg)
    s = Store.open(database, busy_timeout_seconds=0)
    b = FakeBroker()
    run_id = s.start_run(environment=cfg.environment_name, config_digest="d", strategy="s",
                         strategy_version="1", code_version="x")
    e = Engine(s, b, CFG, run_id, lambda: OPEN, clock=Clock())
    t = enter(e, "3")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    b.fill(cid, D(1), D(10))
    run(e, 4)
    (stop,) = legs(e, t, Leg.STOP)
    b.orders[cid] = replace(b.orders[cid], filled_quantity=D(30), state=OrderState.FILLED)
    run(e, 8)
    assert f"{Fence.ORDER_CONFLICT}:{cid}" in e.fences()
    # A stop Detail could not read for a while: the command's read of it settles that too.
    e._set_fence(Fence.DETAIL_UNREADABLE, stop.client_order_id, "Detail unreadable")
    s.close()
    b.cancel(stop.client_order_id)  # the operator, in the Webull app
    del b.holdings["ABC"]
    sent = len(b.sent)
    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: b)
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    assert cli.main(["--config", str(bot), "resolve-order", cid]) == 0, capsys.readouterr().err
    assert len(b.sent) == sent and b.open_orders() == []
    s = Store.open(database, busy_timeout_seconds=0)
    (row,) = s.query("SELECT state, close_cause FROM trades WHERE trade_id = ?", (t,))
    assert (row["state"], row["close_cause"]) == (TradeState.CLOSED, ORDER_RESOLVED)
    (stop_row,) = s.query("SELECT broker_state, operator_resolved_at FROM orders "
                          "WHERE client_order_id = ?", (stop.client_order_id,))
    assert stop_row["broker_state"] == OrderState.CANCELLED  # recorded as Detail shows it
    assert stop_row["operator_resolved_at"] is None
    assert s.query("SELECT name FROM fences") == []
    s.close()


def test_recovery_leaves_a_resolved_crashed_send_alone(store):
    """The bot died during a send and the operator resolved that order before any restart:
    the next start's recovery does not turn it back into an unknown send with a fence no read
    could ever clear (Fable review of #751)."""
    b = FakeBroker()
    e = make(store, b)
    t = enter(e, "2")
    cid = legs(e, t, Leg.ENTRY)[0].client_order_id
    del b.orders[cid]  # Detail never finds it
    with store.transaction():  # as a crash between the attempt and the answer leaves it
        store.execute("UPDATE orders SET send_state = ? WHERE client_order_id = ?",
                      (SendState.ATTEMPTED, cid))
    assert e.resolve_order(cid) is None
    again = make(store, b)
    again.recover()
    assert e.order(cid).send_state is SendState.ATTEMPTED
    assert not has(again, Fence.UNKNOWN_ORDER) and again.fences() == {}
