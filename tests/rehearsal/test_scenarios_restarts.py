"""Rehearsals: the bot dies or is stopped at several points of a trade and starts again
(spec 9, 10, 11). A restart never resends: every client order id reaches the broker at most
once, an order whose send was cut off is resolved by broker reads, and a resting stop is
verified before the strategy runs again. After the flatten time a restart still flattens; after
the close-order cutoff it sends no MARKET order, keeps the stop, fences and alerts.

AAA is bought at 09:45 (26 shares at 24.51, stop 22.65) and, where a scenario needs an exit,
the strategy asks for one from the 09:50 bar on.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal as D

import pytest

from rehearsal.harness import Rehearsal
from rehearsal.kit import (
    Crash,
    add,
    alert_kinds,
    breakout,
    fence_names,
    records_agree,
    run_until_crash,
    scripted,
    strategy_exits,
    without_fsync,
)
from tickr.types import OrderRequest, OrderType


@pytest.fixture(autouse=True)
def _fast_files(monkeypatch):
    without_fsync(monkeypatch)


def once_on_place(kind: OrderType) -> tuple[list[bool], object]:
    armed = [True]

    def hook(request: OrderRequest) -> None:
        if armed[0] and request.order_type is kind:
            armed[0] = False
            raise Crash

    return armed, hook


@pytest.mark.parametrize("where", ["before-stop-sent", "after-stop-sent", "before-stop-cancel",
                                   "after-stop-cancel", "after-close-sent", "before-close-sent"])
def test_a_crash_at_each_step_of_a_trade_then_a_restart(tmp_path, monkeypatch, where):
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    strategy_exits(monkeypatch, {"AAA": "09:50"})
    kind = OrderType.MARKET if "close" in where else OrderType.STOP_LOSS
    armed, hook = once_on_place(kind)
    def die_at_cancel(cid: str) -> None:
        if armed[0] and ex.orders[cid].order_type is OrderType.STOP_LOSS:
            armed[0] = False
            raise Crash

    if where == "before-stop-cancel":
        ex.before_cancel.append(die_at_cancel)  # cancel decided and journaled, never sent
    elif where == "after-stop-cancel":
        ex.after_cancel.append(die_at_cancel)  # Webull cancelled it; we never heard back
    elif where.startswith("before"):
        ex.before_place.append(hook)  # dies after the attempt is journaled, before the HTTP call
    else:
        ex.after_place.append(hook)  # Webull has the order; the reply is lost with the process
    run_until_crash(r, "10:00")
    r.bot_down_until("09:51" if r.world.now.minute < 50 else "09:52")
    restarted = r.world.now
    assert r.run_until("09:55") == 0

    places = [cid for _, call, cid in ex.calls if call == "place"]
    assert all(n == 1 for n in Counter(places).values())  # nothing is ever sent twice
    stops = ex.of("AAA", OrderType.STOP_LOSS)
    closes = ex.of("AAA", OrderType.MARKET)
    (trade,) = r.trades()
    runs = [row.body["cause"] for row in r.journal("run-ended")]
    assert runs == ["crashed", "stopped (SIGTERM)"]

    if where == "before-close-sent":
        # The close never left: proven absent, it is handled like a refused close. Protection
        # is put back and the operator must look (no close is decided again on its own).
        assert closes == []
        assert trade["state"] == "open" and trade["close_cause"] is None
        assert {"exit-refused:t1", "exit-not-filled:t1"} <= set(fence_names(r))
        assert {"exit-refused", "exit-not-filled"} <= set(alert_kinds(r))
        (working,) = ex.open_orders()
        assert working.order_type is OrderType.STOP_LOSS and working.quantity == D(26)
        records_agree(r, ex, flat=False)
        return

    assert trade["close_cause"] == "strategy-exit"
    assert [c.quantity for c in closes] == [D(26)]
    records_agree(r, ex)
    assert alert_kinds(r) == []
    if where == "before-stop-sent":
        # The cut-off stop never reached Webull: proven absent first, then one new stop.
        assert len(stops) == 1
        absent = r.journal("order-absent")
        assert absent and absent[0].key.endswith("-s2")
        first_new = [row for row in r.journal("order-intent")
                     if row.key == stops[0].client_order_id]
        assert absent[0].seq < first_new[0].seq
    else:
        assert len(stops) == 1  # the restart adopted the stop it had; no second one
    if where == "after-stop-sent":
        # The resting stop is verified by Detail before the strategy acts again.
        rows = [row for row in r.journal() if row.at_utc >= restarted.isoformat()]
        verified = next(row.seq for row in rows if row.kind == "order-observed"
                        and row.key == stops[0].client_order_id)
        asked = next(row.seq for row in rows if row.kind == "exit-requested")
        assert verified < asked
    cancels = sum(1 for _, call, _ in ex.calls if call == "cancel")
    if where == "after-stop-cancel":
        assert cancels == 1  # Detail showed it cancelled: not asked again
    if where == "before-stop-cancel":
        assert cancels == 2  # the cut-off cancel is asked again once Detail shows it working


@pytest.mark.parametrize("back", ["15:57", "15:59:30", "16:10"])
def test_a_bot_down_through_the_flatten_time(tmp_path, monkeypatch, back):
    """AAA is held when the bot stops at 09:47 and stays down. Back at 15:57 (after the 15:55
    flatten time, before the close-order cutoff) it flattens with a MARKET order; back after
    the cutoff (15:59) or after the close it sends nothing, keeps the stop, sets
    `market-closed:<trade>` and alerts flatten-after-close, and the day end is not clean."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    assert r.run_until("09:47") == 0
    r.bot_down_until(back)
    assert r.run_until("16:05" if back < "16:00" else "16:15") == 0

    (trade,) = r.trades()
    (day_end,) = r.journal("day-end")
    if back == "15:57":
        assert trade["close_cause"] == "session-flatten"
        assert [c.quantity for c in ex.of("AAA", OrderType.MARKET)] == [D(26)]
        records_agree(r, ex)
        assert alert_kinds(r) == [] and day_end.body["day_clean"]
        return
    assert ex.of("AAA", OrderType.MARKET) == []
    assert trade["state"] == "exiting" and trade["exit_requested"] == "session-flatten"
    (stop,) = ex.open_orders()
    assert stop.order_type is OrderType.STOP_LOSS and stop.quantity == D(26)
    assert sum(1 for _, call, _ in ex.calls if call == "cancel") == 0  # the stop is kept
    assert "market-closed:t1" in fence_names(r)
    assert alert_kinds(r) == ["flatten-after-close", "not-flat-after-flatten"]
    assert not day_end.body["day_clean"] and not day_end.body["broker_flat"]
    records_agree(r, ex, flat=False)
