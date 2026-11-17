"""GET /events: the day's readable events from the bot's journal, read-only, with each order's
symbol and leg from its own tables. Rows are written by the real engine on the fake broker."""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from tickr import config
from tickr.api import contract
from tickr.api.contract import DayEventKind as K
from tickr.api.journal import NO_DATABASE, day_events
from tickr.api.server import make_server, settings_from_config
from tickr.broker.fake import FakeBroker
from tickr.calendar import ny_date_of
from tickr.config import GateResult
from tickr.engine.core import Engine, Leg
from tickr.store import Store

from .samples import T0
from .schema_check import errors
from .test_server import Clock as ServerClock
from .test_server import doc, request

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
POLL = float(CFG.orders.detail_poll_seconds)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def make(store: Store, broker: FakeBroker) -> Engine:
    run_id = store.start_run(environment="test", config_digest="d", strategy="s",
                             strategy_version="1", code_version="x")
    return Engine(store, broker, CFG, run_id, lambda: GateResult(True, "test"), clock=Clock())


def run(engine: Engine, steps: int) -> None:
    for _ in range(steps):
        engine.clock.now += POLL
        engine.step()


def leg(engine: Engine, trade_id: str, which: Leg) -> str:
    return next(o.client_order_id for o in engine.orders(trade_id) if o.leg is which)


def _day(store: Store):
    return ny_date_of(datetime.fromisoformat(store.journal()[0].at_utc))


def test_a_trade_reads_as_entry_fills_stop_and_close(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store.open(path, busy_timeout_seconds=0)
    try:
        broker = FakeBroker()
        engine = make(store, broker)
        trade = engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D("1"))
        broker.fill(leg(engine, trade, Leg.ENTRY), D(2), D("10.01"))
        run(engine, 3)
        broker.fill(leg(engine, trade, Leg.STOP), D(2), D("9.79"))
        run(engine, 2)
        day = _day(store)
        store.end_run(engine.run_id, "finished")
    finally:
        store.close()

    document = day_events(path, day)
    assert document.cause is None
    shown = [(e.kind, e.symbol, e.leg, e.quantity, e.price, e.stop_price, e.cause)
             for e in document.events]
    assert shown == [
        (K.BOT_STARTED, None, None, None, None, None, None),
        (K.ENTRY_SENT, "ABC", None, D(2), D("10.00"), D("9.80"), None),
        (K.FILLED, "ABC", "entry", D(2), D("10.01"), None, None),
        (K.STOP_PLACED, "ABC", None, D(2), None, D("9.80"), None),
        (K.FILLED, "ABC", "stop", D(2), D("9.79"), None, None),
        (K.TRADE_CLOSED, "ABC", None, None, None, None, "stopped-out"),
        (K.BOT_STOPPED, None, None, None, None, None, "finished")]
    assert document.events[0].subject == "s@1"
    seqs = [e.seq for e in document.events]
    assert seqs == sorted(seqs)
    body = contract.to_jsonable(document)
    assert errors(contract.json_schema(contract.DayEventsV1), body) == []


def test_another_day_is_empty_and_no_database_says_so(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store.open(path, busy_timeout_seconds=0)
    try:
        make(store, FakeBroker())
        day = _day(store)
    finally:
        store.close()
    assert day_events(path, day - timedelta(days=1)).events == ()
    assert [e.kind for e in day_events(path, day).events] == [K.BOT_STARTED]
    missing = day_events(tmp_path / "none.sqlite3", day)
    assert missing.events == () and missing.cause == NO_DATABASE
    path.write_bytes(b"not a database")
    assert day_events(path, day).cause.startswith("database unreadable")


def test_an_exit_and_the_operator_entry_stop(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store.open(path, busy_timeout_seconds=0)
    try:
        broker = FakeBroker()
        engine = make(store, broker)
        trade = engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D("1"))
        broker.fill(leg(engine, trade, Leg.ENTRY), D(2), D("10.01"))
        run(engine, 3)
        engine.request_exit(trade, "operator-flatten")
        run(engine, 3)
        broker.fill(leg(engine, trade, Leg.EXIT), D(2), D("10.10"))
        run(engine, 2)
        with store.transaction():
            engine.entry_stop_row(True, "operator")
        day = _day(store)
    finally:
        store.close()
    shown = [(e.kind, e.symbol, e.leg, e.subject, e.cause)
             for e in day_events(path, day).events][4:]
    assert shown == [
        (K.EXIT_REQUESTED, "ABC", None, None, "operator-flatten"),
        (K.FILLED, "ABC", "exit", None, None),
        (K.TRADE_CLOSED, "ABC", None, None, "operator-flatten"),
        (K.FENCE_SET, None, None, "entry-stop", "operator")]


def test_the_route_reads_today_or_the_date_asked(tmp_path):
    root = tmp_path
    shutil.copytree(ROOT / "config" / "calendar", root / "config" / "calendar")
    settings = replace(settings_from_config(config.load(ROOT / "config" / "bot.toml", root=root)),
                       port=0)
    server = make_server(settings, ServerClock(T0))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        resp, payload = request(server, "GET", "/events")
        today = doc(payload)
        resp_day, payload_day = request(server, "GET", "/events?date=2026-01-02")
        bad = request(server, "GET", "/events?date=yesterday")[0]
        post = request(server, "POST", "/events", {})[0]
    finally:
        server.shutdown()
        server.server_close()
    assert resp.status == 200 and today["day"] == ny_date_of(T0).isoformat()
    assert today["cause"] == NO_DATABASE
    assert errors(contract.json_schema(contract.DayEventsV1), today) == []
    assert resp_day.status == 200 and doc(payload_day)["day"] == "2026-01-02"
    assert bad.status == 400 and post.status == 405
