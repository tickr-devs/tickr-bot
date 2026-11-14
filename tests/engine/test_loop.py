from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

from aperture import config
from aperture.broker.fake import FakeBroker
from aperture.config import GateResult
from aperture.engine.core import Engine, Leg
from aperture.engine.loop import run
from aperture.store import Store

CFG = config.load(Path(__file__).resolve().parents[2] / "config" / "bot.toml")


def test_loop_protects_and_reconciles(tmp_path):
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    broker = FakeBroker()
    engine = Engine(store, broker, CFG, "runloop1", lambda: GateResult(True, "t"))
    trade = engine.open_trade("ABC", D(1), D(10), D(9), D(1))
    broker.fill(engine.orders(trade)[0].client_order_id, D(1), D(10))
    clock = [0.0]
    ticks = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        ticks.append(seconds)

    run(engine, should_stop=lambda: len(ticks) >= 5, sleep=sleep, monotonic=lambda: clock[0])
    stops = [o for o in engine.orders(trade) if o.leg is Leg.STOP]
    assert len(stops) == 1 and stops[0].broker_state == "ACCEPTED"
    assert set(engine.fences()) == {"shutting-down"}  # drained on stop; cleared at next start
    assert ticks == [float(CFG.orders.detail_poll_seconds)] * 5
    store.close()


def _engine(tmp_path, broker):
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    return Engine(store, broker, CFG, "rundrain", lambda: GateResult(True, "t"))


def test_drain_cancels_entry_protects_partial_fill_and_leaves_stop(tmp_path):
    from aperture.engine.loop import drain
    broker = FakeBroker()
    engine = _engine(tmp_path, broker)
    trade = engine.open_trade("ABC", D(3), D(10), D(9), D(1))
    entry = engine.orders(trade)[0].client_order_id
    broker.fill(entry, D(1), D(10))
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    assert drain(engine, sleep=sleep, monotonic=lambda: clock[0])
    assert broker.order_detail(entry).state == "CANCELLED"
    stops = [o for o in broker.open_orders() if o.order_type == "STOP_LOSS"]
    assert sum(o.quantity for o in stops) == D(1)  # stop left resting
    kinds = [r.kind for r in engine.store.journal()]
    assert "shutdown-clean" in kinds
    with __import__("pytest").raises(Exception, match="shutting-down"):
        engine.open_trade("XYZ", D(1), D(10), D(9), D(1))


def test_drain_deadline_returns_false(tmp_path):
    from aperture.engine.loop import drain

    class NeverCancels(FakeBroker):
        def cancel(self, client_order_id):
            pass

    broker = NeverCancels()
    engine = _engine(tmp_path, broker)
    engine.open_trade("ABC", D(1), D(10), D(9), D(1))
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    assert not drain(engine, sleep=sleep, monotonic=lambda: clock[0])
    assert clock[0] >= float(CFG.orders.shutdown_deadline_seconds)
    assert "shutdown-deadline" in [r.kind for r in engine.store.journal()]
