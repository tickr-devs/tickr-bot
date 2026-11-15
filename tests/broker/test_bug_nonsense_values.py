"""BUG reproduction (fails until fixed): the Webull readers pass values that cannot be true
(hard rule 7: refuse, never guess).

- `read_order` (src/tickr/broker/webull.py) returns a FILLED order whose `filled_price` is 0
  or negative: the fill price feeds realised P&L and the stop distance.
- `_require` treats only None and "" as missing, so a blank `symbol` (" ") is returned as the
  symbol " " by `read_order`, `read_positions` and `read_snapshot`.

The fix is a MalformedResponse; tests/broker/test_webull_fuzz.py already accepts it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tickr.broker.webull import (
    MalformedResponse,
    read_order_detail,
    read_positions,
    read_snapshot,
)

from .test_webull_broker import OWN

CID = "ap-1"
ORDER = {"client_order_id": CID, "symbol": "AAPL", "side": "BUY", "order_type": "LIMIT",
         "status": "FILLED", "total_quantity": "1", "filled_quantity": "1",
         "filled_price": "99.98", "limit_price": "100.00", "order_id": "X9"}
QUOTE = {"symbol": "AAPL", "bid": "10.00", "ask": "10.02", "bid_size": "300", "ask_size": "200",
         "quote_time": 1794431115431}


def own_account(account_id: str) -> bool:
    return account_id == OWN


def test_reference_bodies_are_read():
    """The bodies below differ from these readable ones in one field only."""
    assert read_order_detail({"orders": [ORDER]}, CID, own_account).symbol == "AAPL"
    assert read_positions([{"symbol": "AAPL", "quantity": "3"}], own_account)[0].symbol == "AAPL"
    assert read_snapshot([QUOTE], datetime(2026, 11, 13, tzinfo=UTC))[0].symbol == "AAPL"


@pytest.mark.xfail(strict=True, reason="bug: a FILLED order with a fill price <= 0 is returned")
@pytest.mark.parametrize("price", ["0", "-0", "-1", "-99.98"])
def test_filled_order_without_a_positive_fill_price_is_refused(price):
    with pytest.raises(MalformedResponse):
        read_order_detail({"orders": [{**ORDER, "filled_price": price}]}, CID, own_account)


@pytest.mark.xfail(strict=True, reason="bug: a blank symbol is returned as a symbol")
@pytest.mark.parametrize("read", [
    lambda: read_order_detail({"orders": [{**ORDER, "symbol": " "}]}, CID, own_account),
    lambda: read_positions([{"symbol": " ", "quantity": "3"}], own_account),
    lambda: read_snapshot([{**QUOTE, "symbol": "  "}], datetime(2026, 11, 13, tzinfo=UTC)),
], ids=["order", "position", "quote"])
def test_blank_symbol_is_refused(read):
    with pytest.raises(MalformedResponse):
        read()
