"""Drive an Engine: step every detail_poll_seconds, reconcile every reconcile_interval_seconds,
and on a stop request drain within shutdown_deadline_seconds."""

from __future__ import annotations

import time
from collections.abc import Callable

from aperture.engine.core import Engine


def run(engine: Engine, *, should_stop: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic) -> bool:
    """Recover, then loop until `should_stop()` is true, then `drain`. Never opens trades on
    its own: it only observes, protects and tears down what the engine already owns.
    Returns False if the drain hit its deadline (the caller exits non-zero and alerts)."""
    orders = engine.config.orders
    engine.recover()
    last_reconcile = monotonic()
    while not should_stop():
        sleep(float(orders.detail_poll_seconds))
        if monotonic() - last_reconcile >= float(orders.reconcile_interval_seconds):
            engine.reconcile()  # includes a step
            last_reconcile = monotonic()
        else:
            engine.step()
    return drain(engine, sleep=sleep, monotonic=monotonic)


def drain(engine: Engine, *, sleep: Callable[[float], None],
          monotonic: Callable[[], float]) -> bool:
    """SIGTERM teardown (spec 11): block entries, cancel working entries, confirm, protect any
    partial fill, leave stops resting. Bounded by shutdown_deadline_seconds."""
    orders = engine.config.orders
    deadline = monotonic() + float(orders.shutdown_deadline_seconds)
    while True:
        pending = engine.shutdown_pending()
        if not pending:
            with engine.store.transaction():
                engine.store.append("shutdown-clean", {}, run_id=engine.run_id)
            return True
        if monotonic() >= deadline:
            with engine.store.transaction():
                engine.store.append("shutdown-deadline", {"pending": pending},
                                    run_id=engine.run_id)
            return False
        sleep(float(orders.detail_poll_seconds))
