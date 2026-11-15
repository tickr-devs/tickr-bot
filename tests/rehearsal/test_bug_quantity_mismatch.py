"""Foreign shares in the symbol the bot holds (owner decision 2026-11-15, docs/decisions.md).

Someone buys 10 AAA by hand while the bot holds 26. The broker then holds more than Detail
explains. Decided: the stop stays sized on Detail (26; a positions read never sizes a stop or a
sell), the quantity-mismatch fence blocks entries and alerts, and at the flatten time the bot
sells exactly its own 26 and never touches the 10 foreign shares.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from rehearsal.harness import Rehearsal
from rehearsal.kit import add, breakout, fence_names, scripted, without_fsync
from tickr.types import OrderType, Side


@pytest.fixture(autouse=True)
def _fast_files(monkeypatch):
    without_fsync(monkeypatch)


def held_with_foreign_shares(tmp_path, monkeypatch) -> Rehearsal:
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    r.at("09:50", lambda: ex.add_foreign_position("AAA", D(10), D("24.50")))  # a manual buy
    return r


def test_the_stop_stays_on_the_detail_quantity_and_the_mismatch_is_fenced(tmp_path, monkeypatch):
    r = held_with_foreign_shares(tmp_path, monkeypatch)
    assert r.run_until("09:53") == 0
    ex = r.world.exchange
    assert ex.holdings["AAA"].quantity == D(36)
    covered = sum(o.quantity - o.filled_quantity for o in ex.open_orders()
                  if o.order_type is OrderType.STOP_LOSS)
    assert covered == D(26)  # the bot's own shares only, never the broker quantity
    assert not [o for o in ex.sent if o.side is Side.SELL and o.order_type is not
                OrderType.STOP_LOSS]  # nothing of the foreign shares is sold
    assert "quantity-mismatch:t1" in fence_names(r)  # entries blocked, operator alerted
    texts = [row.body["text"] for row in r.journal("alert")]
    assert "quantity-mismatch:t1: broker holds 36, Detail says 26" in texts


def test_the_bots_own_shares_are_closed_by_the_flatten_time(tmp_path, monkeypatch):
    r = held_with_foreign_shares(tmp_path, monkeypatch)
    assert r.run_until("09:53") == 0
    r.bot_down_until("15:50")
    assert r.run_until("16:00") == 0
    ex = r.world.exchange
    sold = sum(o.quantity for o in ex.of("AAA", OrderType.MARKET))
    assert sold == D(26)  # exactly the bot's own shares
    assert ex.holdings["AAA"].quantity == D(10)  # the foreign shares are never touched
    assert ex.short == []
    # End state at 16:00: the trade stays open (exiting) while the broker is not flat, so
    # entries stay blocked until the operator sells or explains the 10 shares.
    (trade,) = r.trades()
    assert trade["state"] == "exiting" and trade["closed_at"] is None
    store = r.store()
    try:
        fences = {row["name"]: row["cause"] for row in store.query("SELECT * FROM fences")}
    finally:
        store.close()
    # The fence keeps the cause it was set with; it is not rewritten as the quantities change.
    assert fences["quantity-mismatch:t1"] == "broker holds 36, Detail says 26"
    not_flat = [row.body["text"] for row in r.journal("alert")
                if row.body["kind"] == "not-flat-after-flatten"]
    assert not_flat and all("position AAA 10." in text for text in not_flat)
