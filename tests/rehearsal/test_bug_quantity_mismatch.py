"""Suspected bug (spec conflict, needs an owner decision): foreign shares in the symbol the bot
holds.

Spec 9, Reconciliation: "Quantity mismatch: broker wins, entries blocked until explained, the
stop is made to cover the broker quantity." Spec 9, Session flatten: "position closed by the
flatten time"; spec 1: the bot "ends every day flat".

The engine (src/tickr/engine/core.py, `_advance` sets QUANTITY_MISMATCH, `_protect` sizes
on Detail only, `_exit_step` sends no close while `held != expected`) keeps the stop at the
Detail quantity and, at the flatten time, sends no closing order at all while the broker holds
more than Detail explains. So after someone buys 10 AAA by hand while the bot holds 26, the
bot's own 26 shares are not sold at 15:55 and stay overnight after the DAY stop expires (only
the quantity-mismatch and not-flat alerts fire). The engine follows the other sentence of spec
9 ("a positions read alone never sizes a stop or a sell"), hence a conflict to decide.

These tests state what spec 9 says and fail today.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from rehearsal.harness import Rehearsal
from rehearsal.kit import add, breakout, scripted, without_fsync
from tickr.types import OrderType


@pytest.fixture(autouse=True)
def _fast_files(monkeypatch):
    without_fsync(monkeypatch)


def held_with_foreign_shares(tmp_path, monkeypatch) -> Rehearsal:
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    r.at("09:50", lambda: ex.add_foreign_position("AAA", D(10), D("24.50")))  # a manual buy
    return r


@pytest.mark.xfail(strict=True, reason=(
    "spec 9 conflict, owner decision pending: broker quantity vs Detail-only sizing"))
def test_the_stop_is_made_to_cover_the_broker_quantity(tmp_path, monkeypatch):
    r = held_with_foreign_shares(tmp_path, monkeypatch)
    assert r.run_until("09:53") == 0
    ex = r.world.exchange
    assert ex.holdings["AAA"].quantity == D(36)
    covered = sum(o.quantity - o.filled_quantity for o in ex.open_orders()
                  if o.order_type is OrderType.STOP_LOSS)
    assert covered == D(36)  # today: 26


@pytest.mark.xfail(strict=True, reason=(
    "spec 9 conflict, owner decision pending: broker quantity vs Detail-only sizing"))
def test_the_bots_own_shares_are_closed_by_the_flatten_time(tmp_path, monkeypatch):
    r = held_with_foreign_shares(tmp_path, monkeypatch)
    assert r.run_until("09:53") == 0
    r.bot_down_until("15:50")
    assert r.run_until("15:58") == 0
    sold = sum(o.quantity for o in r.world.exchange.of("AAA", OrderType.MARKET))
    assert sold == D(26)  # today: nothing is sold; 36 AAA are held into the close
