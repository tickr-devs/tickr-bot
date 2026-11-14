"""WebullBroker over a scripted transport: error mapping, one-send rule, budget."""

from __future__ import annotations

from collections import deque
from decimal import Decimal as D
from typing import Any

import pytest

from aperture.broker.base import (
    Broker,
    BrokerError,
    OrderNotFound,
    OutcomeUnknown,
    RateLimited,
    Refused,
)
from aperture.broker.budget import BudgetExhausted, RequestBudget, RouteLimit
from aperture.broker.webull import (
    GATE_CLOSED,
    BrokerReadFailed,
    MalformedResponse,
    Route,
    TransportFailure,
    WebullBroker,
    WireResponse,
    build_order_body,
    to_decimal,
)
from aperture.config import GateResult
from aperture.types import OrderRequest, OrderType, Side, TimeInForce, TradingSession

MAX_CID = 32
OWN = "ACCOUNT-UNDER-TEST"


class ScriptedTransport:
    def __init__(self, *responses: WireResponse | Exception) -> None:
        self.responses: deque[WireResponse | Exception] = deque(responses)
        self.sent: list[tuple[Route, dict[str, Any]]] = []

    def send(self, route: Route, params: dict[str, Any]) -> WireResponse:
        self.sent.append((route, dict(params)))
        item = self.responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def is_own_account(self, account_id: str) -> bool:
        return account_id == OWN


DEFAULT_LIMIT = RouteLimit(30, 60, 60)


OPEN_GATE = GateResult(True, "test: open")
PREFIXES = ("ap-", "probe-")


def broker(*responses, limit=DEFAULT_LIMIT, max_pages=3, gate=OPEN_GATE):
    transport = ScriptedTransport(*responses)
    budget = RequestBudget(limit, clock=lambda: 0.0)
    b = WebullBroker(transport, budget, max_pages, MAX_CID, PREFIXES, lambda: gate)
    return b, transport, budget


def err(status: int, code: str, message: str = "") -> WireResponse:
    return WireResponse(status, {"error_code": code, "message": message})


def limit_buy(cid="ap-1", qty="1", price="10.01") -> OrderRequest:
    return OrderRequest(cid, "AAPL", Side.BUY, OrderType.LIMIT, D(qty), TimeInForce.DAY,
                        TradingSession.CORE, limit_price=D(price))


NON_TRADING = err(417, "OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS", "non trading hours")
NOT_PRESENT = err(417, "OPENAPI_PARAM_ERR", "Order not present")
THROTTLED = err(429, "TOO_MANY_REQUESTS")


def test_satisfies_protocol():
    b: Broker = broker()[0]
    assert b


# -- place ------------------------------------------------------------------------------------


def test_place_ack():
    b, t, _ = broker(WireResponse(200, {"client_order_id": "ap-1", "order_id": "X9"}))
    ack = b.place(limit_buy())
    assert (ack.client_order_id, ack.broker_order_id) == ("ap-1", "X9")
    route, params = t.sent[0]
    assert route is Route.PLACE and len(t.sent) == 1
    order = params["new_orders"][0]
    assert order["limit_price"] == "10.01" and order["quantity"] == "1"
    assert "account_id" not in params  # only the SDK transport knows it


@pytest.mark.parametrize(
    "outcome",
    [
        TransportFailure("SDK.HttpError"),
        THROTTLED,
        err(500, "INTERNAL_ERROR"),
        err(417, "OPENAPI_PARAM_ERR", "something else"),
        WireResponse(200, {"order_id": "X9"}),  # ack without our id
        WireResponse(200, {"client_order_id": "ap-OTHER"}),
    ],
)
def test_place_doubt_is_outcome_unknown_and_never_resent(outcome):
    b, t, _ = broker(outcome)
    with pytest.raises(OutcomeUnknown):
        b.place(limit_buy())
    assert len(t.sent) == 1


def test_place_non_trading_hours_is_refused():
    b, t, _ = broker(NON_TRADING)
    with pytest.raises(Refused) as exc:
        b.place(limit_buy())
    assert exc.value.code == "OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS"


def test_place_429_pauses_the_route():
    b, t, budget = broker(THROTTLED)
    with pytest.raises(OutcomeUnknown):
        b.place(limit_buy())
    with pytest.raises(BudgetExhausted):
        b.place(limit_buy("ap-2"))
    assert len(t.sent) == 1
    assert budget.usage(Route.PLACE.path).paused_for_seconds > 0


def test_place_refused_by_budget_sends_nothing():
    b, t, _ = broker(limit=RouteLimit(1, 60, 60))
    b._budget.take(Route.PLACE.path)
    with pytest.raises(RateLimited):
        b.place(limit_buy())
    assert t.sent == []


@pytest.mark.parametrize(
    "request_, code",
    [
        (limit_buy(cid="x" * (MAX_CID + 1)), "LOCAL_CLIENT_ORDER_ID"),
        (limit_buy(cid=""), "LOCAL_CLIENT_ORDER_ID"),
        (limit_buy(qty="0"), "LOCAL_QUANTITY"),
        (limit_buy(qty="1.5"), "LOCAL_QUANTITY"),
        (limit_buy(price="0"), "LOCAL_PRICE"),
        (OrderRequest("ap-1", "AAPL", Side.SELL, OrderType.STOP_LOSS, D(1), TimeInForce.DAY,
                      TradingSession.CORE), "LOCAL_STOP_PRICE"),
        (OrderRequest("ap-1", "AAPL", Side.SELL, OrderType.MARKET, D(1), TimeInForce.DAY,
                      TradingSession.CORE, limit_price=D(1)), "LOCAL_LIMIT_PRICE"),
    ],
)
def test_invalid_requests_refused_locally(request_, code):
    b, t, _ = broker()
    with pytest.raises(Refused) as exc:
        b.place(request_)
    assert exc.value.code == code
    assert t.sent == []


def test_stop_body():
    body = build_order_body(
        OrderRequest("ap-s", "AAPL", Side.SELL, OrderType.STOP_LOSS, D("3"), TimeInForce.DAY,
                     TradingSession.CORE, stop_price=D("9.80")),
        MAX_CID,
    )
    assert body["order_type"] == "STOP_LOSS" and body["stop_price"] == "9.80"
    assert "limit_price" not in body
    assert body["combo_type"] == "NORMAL" and body["market"] == "US"


# -- cancel -----------------------------------------------------------------------------------


def test_cancel_ok_and_errors():
    b, t, _ = broker(WireResponse(200, {"client_order_id": "ap-1"}), NOT_PRESENT, NON_TRADING,
                     TransportFailure("SDK.HttpError"), err(500, "INTERNAL_ERROR"))
    b.cancel("ap-1")
    with pytest.raises(OrderNotFound):
        b.cancel("ap-1")
    for _ in range(3):  # no proven cancel refusal: every other answer is resolved by Detail
        with pytest.raises(OutcomeUnknown):
            b.cancel("ap-1")
    assert len(t.sent) == 5


# -- reads ------------------------------------------------------------------------------------


def test_detail_not_present():
    b, _, _ = broker(NOT_PRESENT)
    with pytest.raises(OrderNotFound):
        b.order_detail("ap-404")


@pytest.mark.parametrize(
    "outcome, error",
    [
        (TransportFailure("SDK.HttpError"), BrokerReadFailed),
        (THROTTLED, RateLimited),
        (err(417, "OPENAPI_PARAM_ERR", "bad account"), BrokerReadFailed),
        (err(500, "INTERNAL_ERROR"), BrokerReadFailed),
    ],
)
def test_read_failures_are_typed(outcome, error):
    b, t, _ = broker(outcome)
    with pytest.raises(error):
        b.order_detail("ap-1")
    assert len(t.sent) == 1  # reads are not retried either


def test_open_orders_pages_until_no_key():
    page1 = {"data": [], "pagination_key": "k2"}
    page2 = {"data": []}
    b, t, _ = broker(WireResponse(200, page1), WireResponse(200, page2))
    assert b.open_orders() == []
    assert t.sent[1][1] == {"pagination_key": "k2"}


def test_open_orders_refuses_repeated_cursor():
    page = WireResponse(200, {"data": [], "pagination_key": "k"})
    b, _, _ = broker(page, page, max_pages=3)
    with pytest.raises(BrokerReadFailed) as exc:
        b.open_orders()
    assert exc.value.code == "PAGINATION_REPEATED"


def test_open_orders_refuses_partial_list():
    pages = [WireResponse(200, {"data": [], "pagination_key": f"k{i}"}) for i in range(3)]
    b, _, _ = broker(*pages, max_pages=3)
    with pytest.raises(BrokerReadFailed):
        b.open_orders()


def test_account_present_only_for_own_id():
    b, _, _ = broker(
        WireResponse(200, [{"account_id": "SOMEONE"}, {"account_id": OWN}]),
        WireResponse(200, [{"account_id": "SOMEONE"}]),
    )
    assert b.account().account_present
    assert not b.account().account_present


def test_all_errors_are_broker_errors():
    for cls in (BrokerReadFailed, MalformedResponse, BudgetExhausted):
        assert issubclass(cls, BrokerError)


# -- numbers ----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1.5, True, "abc", "", None, "NaN", "Infinity", [1]])
def test_to_decimal_refuses(value):
    with pytest.raises(MalformedResponse):
        to_decimal(value, "x")


def test_to_decimal_accepts():
    assert to_decimal("10.0100", "x") == D("10.01")
    assert to_decimal(3, "x") == D(3)
    assert to_decimal(D("0.0001"), "x") == D("0.0001")


# -- trading gate -----------------------------------------------------------------------------


def test_closed_gate_refuses_trading_requests_and_sends_nothing():
    closed = GateResult(False, "trading is disabled for this environment")
    b, t, budget = broker(WireResponse(200, {"data": []}), gate=closed)
    for call in (lambda: b.place(limit_buy()), lambda: b.cancel("ap-1")):
        with pytest.raises(Refused) as exc:
            call()
        assert exc.value.code == GATE_CLOSED
    assert t.sent == []
    assert budget.usage(Route.PLACE.path).used == 0
    assert b.open_orders() == []  # reads stay allowed


def test_gate_is_asked_on_every_send():
    answers = [OPEN_GATE, GateResult(False, "authorization removed")]
    transport = ScriptedTransport(WireResponse(200, {"client_order_id": "ap-1", "order_id": "X"}))
    b = WebullBroker(transport, RequestBudget(DEFAULT_LIMIT, clock=lambda: 0.0), 3, MAX_CID,
                     PREFIXES, lambda: answers.pop(0))
    b.place(limit_buy())
    with pytest.raises(Refused):
        b.place(limit_buy("ap-2"))
    assert len(transport.sent) == 1
