"""SUSPECTED BUG reproduction (fails until fixed): two position rows for one symbol are returned
as they are, and the engine then keeps only the last one (hard rules 1 and 7).

`read_positions` (src/tickr/broker/webull.py:413) accepts any number of rows per symbol.
`Engine.step` (src/tickr/engine/core.py:753) builds `{p.symbol: p.quantity ...}`, so with
rows AAPL 100 and AAPL 40 the bot believes it holds 40: the broker's quantity is silently
replaced, not reconciled. The non-empty positions shape is unproven (webull-facts F9), so this
may never happen; the fix may equally be a refusal here or a sum/refusal in the engine. The test
accepts either a refusal or a list with one row per symbol.
"""

from __future__ import annotations

import pytest

from tickr.broker.webull import MalformedResponse, read_positions

from .test_webull_broker import OWN


@pytest.mark.xfail(strict=True, reason=(
    "bug: one symbol listed twice in positions, last row wins in the engine"))
def test_one_symbol_listed_twice_is_not_returned_ambiguously():
    body = [{"symbol": "AAPL", "quantity": "100"}, {"symbol": "AAPL", "quantity": "40"}]
    try:
        positions = read_positions(body, lambda account_id: account_id == OWN)
    except MalformedResponse:
        return  # refusing is a fix
    symbols = [p.symbol for p in positions]
    assert len(symbols) == len(set(symbols)), positions
