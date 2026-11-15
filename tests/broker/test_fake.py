from __future__ import annotations

from decimal import Decimal as D

import pytest

from tickr.broker.base import Broker, OrderNotFound, OutcomeUnknown, RateLimited, Refused
from tickr.broker.fake import FakeBroker
from tickr.types import OrderRequest, OrderState, OrderType, Side, TimeInForce, TradingSession


def req(cid: str, side=Side.BUY, qty="2", kind=OrderType.LIMIT) -> OrderRequest:
    return OrderRequest(cid, "ABC", side, kind, D(qty), TimeInForce.DAY, TradingSession.CORE,
                        limit_price=D("10.01") if kind is OrderType.LIMIT else None)


def test_satisfies_protocol():
    broker: Broker = FakeBroker()
    assert broker.account().account_present


def test_place_fill_and_close():
    b = FakeBroker()
    b.place(req("ap-1"))
    assert b.order_detail("ap-1").state is OrderState.ACCEPTED
    b.fill("ap-1", D(1), D(10))
    assert b.order_detail("ap-1").state is OrderState.PARTIALLY_FILLED
    assert b.order_detail("ap-1").raw_status == "PARTIAL FILLED"
    b.fill("ap-1", D(1), D(11))
    d = b.order_detail("ap-1")
    assert d.state is OrderState.FILLED and d.average_fill_price == D("10.5")
    assert b.positions()[0].quantity == D(2)
    b.place(req("ap-2", side=Side.SELL, kind=OrderType.MARKET))
    b.fill("ap-2", D(2), D(12))
    assert b.positions() == []
    assert b.open_orders() == []


def test_scripted_failures():
    b = FakeBroker()
    b.script("refuse", "unknown", "unknown-lost", "rate-limited")
    with pytest.raises(Refused):
        b.place(req("ap-1"))
    with pytest.raises(OutcomeUnknown):
        b.place(req("ap-2"))
    assert b.order_detail("ap-2").state is OrderState.ACCEPTED  # reached the broker
    with pytest.raises(OutcomeUnknown):
        b.place(req("ap-3"))
    with pytest.raises(OrderNotFound):
        b.order_detail("ap-3")  # never arrived
    with pytest.raises(RateLimited):
        b.place(req("ap-4"))
    assert len(b.sent) == 4


def test_duplicate_client_order_id_refused():
    b = FakeBroker()
    b.place(req("ap-1"))
    with pytest.raises(Refused, match="DUPLICATE"):
        b.place(req("ap-1"))


def test_cancel_and_foreign_position():
    b = FakeBroker()
    b.place(req("ap-1"))
    b.cancel("ap-1")
    assert b.order_detail("ap-1").state is OrderState.CANCELLED
    with pytest.raises(Refused):
        b.cancel("ap-1")
    b.add_foreign_position("XYZ", D(5), D(3))
    assert [p.symbol for p in b.positions()] == ["XYZ"]
