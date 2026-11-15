"""`order-held`: the loop journals, from Detail reads it already makes, that a stop is still
working with nothing filled, at most once per `[orders] stop_held_proof_seconds`."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from tickr import config
from tickr.broker.fake import FakeBroker
from tickr.config import Config, GateResult
from tickr.engine.core import ORDER_HELD, ORDER_OBSERVED, Engine, Fence, Leg, TradeState
from tickr.store import Store
from tickr.types import OrderState

RUN_ID = re.compile(r"[0-9a-f]{8}(?=-t)|^[0-9a-f-]{32,36}$")
ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
OPEN = GateResult(True, "test")
POLL = float(CFG.orders.detail_poll_seconds)
INTERVAL = float(CFG.orders.stop_held_proof_seconds)
EVERY_POLL = replace(CFG, orders=replace(CFG.orders,
                                         stop_held_proof_seconds=CFG.orders.detail_poll_seconds))
NEVER = replace(CFG, orders=replace(CFG.orders, stop_held_proof_seconds=D(10) ** 9))


class Clock:
    """One clock for the engine and the store's journal stamps, as in the running bot."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def utc(self) -> datetime:
        return datetime.fromtimestamp(self.now, UTC)


@dataclass
class CountingBroker(FakeBroker):
    """The fake broker, counting every broker call by method."""

    calls: Counter[str] = field(default_factory=Counter)

    def positions(self):
        self.calls["positions"] += 1
        return super().positions()

    def open_orders(self):
        self.calls["open_orders"] += 1
        return super().open_orders()

    def order_detail(self, client_order_id):
        self.calls["order_detail"] += 1
        return super().order_detail(client_order_id)

    def place(self, request):
        self.calls["place"] += 1
        return super().place(request)

    def cancel(self, client_order_id):
        self.calls["cancel"] += 1
        return super().cancel(client_order_id)

    def replace_stop_price(self, client_order_id, quantity, stop_price):
        self.calls["replace"] += 1
        return super().replace_stop_price(client_order_id, quantity, stop_price)


def opened(tmp_path: Path, cfg: Config = CFG, name: str = "db.sqlite3"):
    """A trade with 2 filled and its stop verified working; returns (store, broker, engine,
    trade id, stop id)."""
    clock = Clock()
    store = Store.open(tmp_path / name, busy_timeout_seconds=0, now=clock.utc)
    broker = CountingBroker()
    run_id = store.start_run(environment="test", config_digest="d", strategy="s",
                             strategy_version="1", code_version="x")
    engine = Engine(store, broker, cfg, run_id, lambda: OPEN, clock=clock)
    trade = engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D("1"))
    entry = next(o for o in engine.orders(trade) if o.leg is Leg.ENTRY)
    broker.fill(entry.client_order_id, D(2), D(10))
    steps(engine, 3)
    (stop,) = [o for o in engine.orders(trade) if o.leg is Leg.STOP]
    assert engine.order(stop.client_order_id).verified
    return store, broker, engine, trade, stop.client_order_id


def steps(engine: Engine, n: int) -> None:
    for _ in range(n):
        engine.clock.now += POLL
        engine.step()


def seconds(rows) -> list[float]:
    return [datetime.fromisoformat(r.at_utc).timestamp() for r in rows]


def proofs(store: Store, cid: str) -> list:
    return sorted(store.journal(ORDER_OBSERVED, cid) + store.journal(ORDER_HELD, cid),
                  key=lambda r: r.seq)


def test_held_rows_come_at_the_configured_interval_and_no_more_often(tmp_path):
    store, _, engine, _, stop = opened(tmp_path)
    assert store.journal(ORDER_HELD, stop) == []
    span = 5 * INTERVAL
    steps(engine, int(span / POLL))
    held = store.journal(ORDER_HELD, stop)
    assert len(held) == int(span // INTERVAL)
    times = seconds(proofs(store, stop))
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    # Never more often than the interval, and late by at most one poll (one read per step).
    assert all(INTERVAL <= gap < INTERVAL + POLL for gap in gaps), gaps
    assert all(r.body == {"state": "ACCEPTED", "raw_status": "SUBMITTED",
                          "filled_quantity": "0"} for r in held)
    assert {r.run_id for r in held} == {engine.run_id}


def test_no_held_row_once_the_stop_shows_a_fill(tmp_path):
    store, broker, engine, _, stop = opened(tmp_path)
    broker.fill(stop, D(1), D("9.80"))  # partial: still working, one share sold
    steps(engine, 2)
    assert engine.order(stop).broker_state is OrderState.PARTIALLY_FILLED
    steps(engine, int(3 * INTERVAL / POLL))
    assert store.journal(ORDER_HELD, stop) == []


def test_no_held_row_while_the_stop_is_not_shown_working(tmp_path):
    # An interval of one poll, so every read could write one (the engine closes a trade whose
    # stop is not verified within max_unprotected_seconds, shorter than the default interval).
    store, broker, engine, _, stop = opened(tmp_path, EVERY_POLL)
    steps(engine, 2)
    held = len(store.journal(ORDER_HELD, stop))
    assert held > 0
    broker.orders[stop] = replace(broker.orders[stop], state=OrderState.UNKNOWN,
                                  raw_status="SOMETHING NEW")
    steps(engine, 4)
    assert engine.order(stop).broker_state is OrderState.UNKNOWN
    assert has_fence(engine, Fence.STATUS_UNKNOWN)
    assert len(store.journal(ORDER_HELD, stop)) == held


def test_held_rows_cost_no_broker_call_and_change_nothing_else(tmp_path):
    """The same session with held rows and with them effectively off: identical broker calls,
    sends, state tables and fences; the journals differ only by `order-held` rows."""
    runs = []
    for cfg, name in ((CFG, "with.sqlite3"), (NEVER, "without.sqlite3")):
        store, broker, engine, trade, stop = opened(tmp_path, cfg, name)
        steps(engine, int(4 * INTERVAL / POLL))
        broker.fill(stop, D(2), D("9.80"))
        steps(engine, 3)
        assert engine.trade(trade).state is TradeState.CLOSED
        tables = [[tuple(r) for r in store.query(f"SELECT * FROM {table} ORDER BY rowid")]
                  for table in ("orders", "trades", "fences")]
        kinds = [r.kind for r in store.journal() if r.kind != ORDER_HELD]
        sent = [replace(r, client_order_id=_same(r.client_order_id)) for r in broker.sent]
        runs.append((broker.calls, sent, tables, kinds, len(store.journal(ORDER_HELD))))
        store.close()
    (calls, sent, tables, kinds, held), (calls0, sent0, tables0, kinds0, held0) = runs
    assert held >= 4 and held0 == 0
    assert calls == calls0 and sent == sent0 and kinds == kinds0
    assert _same(tables) == _same(tables0)


def _same(value):
    """`value` without what differs between two databases by construction: run ids (also
    inside client order ids)."""
    if isinstance(value, list | tuple):
        return [_same(v) for v in value]
    return RUN_ID.sub("<run>", value) if isinstance(value, str) else value


def test_restart_recovery_reads_past_held_rows_and_carries_on(tmp_path):
    store, broker, engine, trade, stop = opened(tmp_path)
    steps(engine, int(2 * INTERVAL / POLL))
    before = len(store.journal(ORDER_HELD, stop))
    assert before >= 2
    run_id = store.start_run(environment="test", config_digest="d", strategy="s",
                             strategy_version="1", code_version="x")
    restarted = Engine(store, broker, CFG, run_id, lambda: OPEN, clock=engine.clock)
    restarted.recover()
    calls = sum(broker.calls.values())
    assert restarted.reconcile()
    assert not restarted.fences()
    assert restarted.trade(trade).state is TradeState.OPEN
    assert restarted.order(stop).verified
    assert broker.sent == broker.sent[:2]  # entry and stop: nothing resent
    # The interval counts from the last proof of the earlier run: no burst after a restart.
    restarted.clock.now += POLL
    restarted.step()
    assert len(store.journal(ORDER_HELD, stop)) == before
    assert sum(broker.calls.values()) > calls
    steps(restarted, int(INTERVAL / POLL) + 1)
    assert store.journal(ORDER_HELD, stop)[-1].run_id == run_id
    store.close()


def has_fence(engine: Engine, fence: Fence) -> bool:
    return any(n.split(":", 1)[0] == fence for n in engine.fences())
