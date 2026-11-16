"""Rehearsals: odd broker answers during a day, through the real `tickr run` (spec 9, 13).

A lost reply to the entry (order created, or never created), a 429 or a local budget refusal on
the stop, an unmapped order status, Detail that cannot find an order for a while, and a
foreign position appearing mid-day. Unknown outcomes are resolved by broker reads and never
resent; entries stay blocked meanwhile; foreign exposure is never touched.

AAA is bought at 09:45 (26 shares at 24.51, stop 22.65) unless a scenario says otherwise.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D

import pytest

from rehearsal.harness import Rehearsal
from rehearsal.kit import (
    add,
    alert_kinds,
    at,
    breakout,
    fence_names,
    records_agree,
    scripted,
    without_fsync,
)
from tickr.broker.base import OrderNotFound
from tickr.types import OrderDetail, OrderState, OrderType


@pytest.fixture(autouse=True)
def _fast_files(monkeypatch):
    without_fsync(monkeypatch)


def once_each(r: Rehearsal) -> None:
    places = [cid for _, call, cid in r.world.exchange.calls if call == "place"]
    assert all(n == 1 for n in Counter(places).values())


def seconds(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()


def test_entry_reply_lost_but_the_order_was_created(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    ex.script("unknown")
    assert r.run_until("09:48") == 0
    (trade,) = r.trades()
    assert trade["state"] == "open"
    assert len(ex.of("AAA", OrderType.LIMIT)) == 1  # never resent
    (stop,) = ex.of("AAA", OrderType.STOP_LOSS)
    assert stop.quantity == D(26)
    assert [row.key for row in r.journal("order-unknown")] == [ex.sent[0].client_order_id]
    assert [row.body for row in r.journal("order-acknowledged")][0] == {"resolved_by": "detail"}
    assert fence_names(r) == ["shutting-down"] and alert_kinds(r) == []
    records_agree(r, ex, flat=False)


def test_entry_reply_lost_and_the_order_never_created(tmp_path, monkeypatch):
    """Detail, open orders and positions all show no such order: it is given up on as absent
    (never resent) and the next signal opens a new trade with a new order."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "10:00", "29"))  # signals every minute
    ex.script("unknown-lost")
    assert r.run_until("09:48") == 0
    first, second = r.trades()
    assert first["close_cause"] == "entry-not-filled"
    lost, new = ex.of("AAA", OrderType.LIMIT)
    assert lost.client_order_id != new.client_order_id
    assert [row.key for row in r.journal("order-absent")] == [lost.client_order_id]
    absent_at = r.journal("order-absent")[0].at_utc
    assert second["opened_at"] > absent_at
    once_each(r)
    assert second["state"] == "open" and len(ex.of("AAA", OrderType.STOP_LOSS)) == 1
    assert not any(n.startswith(("unknown-order", "cancel-unknown")) for n in fence_names(r))
    records_agree(r, ex, flat=False)


def test_a_429_on_the_stop_is_resolved_then_a_new_stop_is_placed(tmp_path, monkeypatch):
    """A 429 on a trading request is an unknown outcome (broker/webull.py); here the stop was
    not created. It is proven absent by reads, then one new stop goes out, well inside
    `max_unprotected_seconds`, so the position is not exited."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    ex.script("ack", "unknown-lost")
    assert r.run_until("09:48") == 0
    lost, stop = ex.of("AAA", OrderType.STOP_LOSS)
    assert [row.key for row in r.journal("order-absent")] == [lost.client_order_id]
    (fill_seen,) = [row for row in r.journal("order-observed")
                    if row.key == ex.sent[0].client_order_id]
    verified = [row for row in r.journal("order-observed") if row.key == stop.client_order_id]
    took = seconds(fill_seen.at_utc, verified[0].at_utc)
    assert took < float(r.config.orders.max_unprotected_seconds)
    (trade,) = r.trades()
    assert trade["state"] == "open" and trade["exit_requested"] is None
    assert ex.of("AAA", OrderType.MARKET) == []
    once_each(r)
    records_agree(r, ex, flat=False)


def test_a_stop_refused_by_our_request_budget_is_placed_next_pass(tmp_path, monkeypatch):
    """The local budget refusing the stop is not a broker rejection: nothing was sent, it does
    not count against the stop retries, and the next pass places it."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    ex.script("ack", "rate-limited")
    assert r.run_until("09:48") == 0
    refused, stop = ex.of("AAA", OrderType.STOP_LOSS)
    assert refused.client_order_id not in ex.orders and stop.client_order_id in ex.orders
    (trade,) = r.trades()
    assert trade["stop_failures"] == 0 and trade["exit_requested"] is None
    assert alert_kinds(r) == []
    records_agree(r, ex, flat=False)


def test_an_unmapped_entry_status_blocks_entries_but_the_fill_is_protected(tmp_path, monkeypatch):
    """For two minutes Detail shows the filled entry with a status the adapter cannot map
    (UNKNOWN). The filled quantity is still protected; entries are blocked and the operator
    alerted while it lasts; it clears when the status maps again."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))

    def odd(cid: str, detail: OrderDetail) -> OrderDetail:
        if detail.order_type is OrderType.LIMIT and r.world.now < at("09:47"):
            return replace(detail, state=OrderState.UNKNOWN, raw_status="PENDING_REVIEW")
        return detail

    ex.detail_hook = odd
    seen = {}
    r.at("09:46", lambda: seen.setdefault("odd", (r.status(), fence_names(r))))
    assert r.run_until("09:48") == 0
    status, fences = seen["odd"]
    assert any(n.startswith("status-unknown:") for n in fences)
    assert status.position is not None and status.position.stop.state.value == "verified"
    (stop,) = ex.of("AAA", OrderType.STOP_LOSS)
    assert stop.quantity == D(26)
    assert alert_kinds(r) == ["unknown-order-status"]
    assert not any(n.startswith("status-unknown:") for n in fence_names(r))
    records_agree(r, ex, flat=False)


def test_detail_cannot_find_the_working_stop_for_a_while(tmp_path, monkeypatch):
    """Detail answers "not found" for the acknowledged stop from 09:45:10 to 09:47. The stop is
    neither replaced nor doubled and the position is not exited; the operator is alerted and
    the fence clears when Detail finds it again."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))

    def missing(cid: str, detail: OrderDetail) -> OrderDetail:
        if (detail.order_type is OrderType.STOP_LOSS
                and at("09:45:10") <= r.world.now < at("09:47")):
            raise OrderNotFound(cid)
        return detail

    ex.detail_hook = missing
    assert r.run_until("09:48") == 0
    assert len(ex.of("AAA", OrderType.STOP_LOSS)) == 1
    assert ex.of("AAA", OrderType.MARKET) == []
    assert alert_kinds(r) == ["unknown-order-status"]
    assert [row.key for row in r.journal("fence-set") if row.key.startswith("order-missing")]
    assert fence_names(r) == ["shutting-down"]
    records_agree(r, ex, flat=False)


def test_detail_cannot_find_the_filled_entry_for_a_while(tmp_path, monkeypatch):
    """The entry fills on arrival, but Detail answers "not found" for it for a minute. Stops are
    sized on Detail only (spec 9), so the stop goes out once Detail shows the fill, for
    exactly that quantity; the operator is alerted meanwhile."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))

    def missing(cid: str, detail: OrderDetail) -> OrderDetail:
        if detail.order_type is OrderType.LIMIT and r.world.now < at("09:46"):
            raise OrderNotFound(cid)
        return detail

    ex.detail_hook = missing
    assert r.run_until("09:48") == 0
    (stop,) = ex.of("AAA", OrderType.STOP_LOSS)
    assert stop.quantity == D(26)
    (stop_sent,) = [row for row in r.journal("order-attempted") if row.key == stop.client_order_id]
    assert stop_sent.at_utc >= at("09:46").isoformat()
    assert alert_kinds(r) == ["unknown-order-status"]
    assert fence_names(r) == ["shutting-down"]
    records_agree(r, ex, flat=False)


def test_a_foreign_position_mid_day_is_ignored_and_never_touched(tmp_path, monkeypatch):
    """Someone buys 10 ZZZ by hand at 09:50 and sells them at 10:02. Manual trades are
    ignored (owner 2026-11-16): AAA's stop keeps working, BBB is bought on its own signal
    while ZZZ is held, the operator is alerted, and nothing is ever sent for ZZZ."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"), ("09:52", "09:53", D(21), D(30000)))
    add(r, "BBB", "20", breakout("09:48", "10:10", "26.60"))
    r.at("09:50", lambda: ex.add_foreign_position("ZZZ", D(10), D(5)))
    r.at("10:02", lambda: ex.holdings.pop("ZZZ"))
    seen = {}
    r.at("10:01", lambda: seen.setdefault("foreign", r.status()))
    assert r.run_until("10:05") == 0

    aaa, bbb = r.trades()
    assert aaa["close_cause"] == "stopped-out"  # protected by its own stop meanwhile
    assert bbb["symbol"] == "BBB" and bbb["opened_at"] < at("10:02").isoformat()
    assert ex.of("ZZZ") == []
    assert "foreign-exposure" in alert_kinds(r)
    block = seen["foreign"].entry_block
    assert block is None or block.cause != "foreign_position"
    assert not any(d["refusal_cause"] and d["refusal_cause"].startswith("FOREIGN_EXPOSURE")
                   for d in r.decisions())
    assert not any(n.startswith("foreign-position") for n in fence_names(r))
    records_agree(r, ex, flat=False)
