"""Readers and error mapping against recorded Webull responses (tests/fixtures/webull)."""

from __future__ import annotations

import copy
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr.broker.base import OrderNotFound, RateLimited, Refused
from tickr.broker.webull import (
    DETAIL_NOT_VISIBLE,
    BrokerReadFailed,
    ForeignOrdersUnreadable,
    MalformedResponse,
    Route,
    WireResponse,
    parse_json,
    read_order_detail,
    read_order_rows,
)
from tickr.types import OrderState, OrderType, Side

from .test_webull_broker import OWN, broker, limit_buy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webull"
CID = "probe-d13d7316437b4405b9a3a655c0"


def own(account_id: str) -> bool:
    return account_id == OWN


def load(name: str) -> WireResponse:
    raw = json.loads((FIXTURES / name).read_text("utf-8"))
    # Round-trip the body through the adapter's parser: numbers become Decimal.
    return WireResponse(raw["status"], parse_json(json.dumps(raw["body"])))


def recorded_combo() -> dict:
    """The recorded combo row; Detail returns the same shape (combo with `orders[]`)."""
    return copy.deepcopy(load("historical_orders_cancelled_limit.json").body["data"][0])


def test_fixtures_carry_no_account_identity():
    for path in [*FIXTURES.rglob("*.json"), *FIXTURES.rglob("*.md")]:
        text = path.read_text("utf-8")
        assert "account_id" not in text, path.name
        assert "TS" + "JE" not in text, path.name  # prefix of the real Sandbox account


def test_empty_open_orders_and_positions():
    b, t, _ = broker(load("open_orders_empty.json"), load("positions_empty.json"))
    assert b.open_orders() == []
    assert b.positions() == []
    assert [r for r, _ in t.sent] == [Route.OPEN_ORDERS, Route.POSITIONS]


def test_recorded_order_row():
    body = load("historical_orders_cancelled_limit.json").body
    rows, bad = read_order_rows(body, own, lambda c: c.startswith("probe-"), "h")
    assert bad == []
    (order,) = rows
    assert order.client_order_id == CID
    assert (order.symbol, order.side, order.order_type) == ("AAPL", Side.BUY, OrderType.LIMIT)
    assert order.quantity == D(1) and isinstance(order.quantity, D)
    assert order.filled_quantity == D(0)
    assert order.state is OrderState.CANCELLED and order.raw_status == "CANCELLED"
    assert order.average_fill_price is None


def test_detail_from_recorded_shape():
    b, _, _ = broker(WireResponse(200, recorded_combo()))
    detail = b.order_detail(CID)
    assert detail.state is OrderState.CANCELLED


def with_row(**changes) -> dict:
    combo = recorded_combo()
    row = combo["orders"][0]
    for key, value in changes.items():
        if value is None:
            row.pop(key)
        else:
            row[key] = value
    return combo


@pytest.mark.parametrize(
    "changes, state",
    [
        ({"status": "SUBMITTED"}, OrderState.ACCEPTED),
        ({"status": "PARTIAL FILLED", "total_quantity": "2", "filled_quantity": "1",
          "filled_price": "100.00"}, OrderState.PARTIALLY_FILLED),
        ({"status": "FILLED", "filled_quantity": "1", "filled_price": "99.98"}, OrderState.FILLED),
        ({"status": "FAILED"}, OrderState.REJECTED),
        ({"status": "PENDING_CANCEL"}, OrderState.UNKNOWN),  # may still lose to a fill
        ({"status": "FILLED"}, OrderState.UNKNOWN),  # FILLED with 0 filled contradicts
    ],
)
def test_detail_status_variants(changes, state):
    detail = read_order_detail(with_row(**changes), CID, own)
    assert detail.state is state


def test_fill_price_is_decimal():
    detail = read_order_detail(
        with_row(status="FILLED", filled_quantity="1", filled_price="99.9800"), CID, own
    )
    assert detail.average_fill_price == D("99.98")


@pytest.mark.parametrize(
    "changes",
    [
        {"filled_quantity": None},
        {"total_quantity": None},
        {"status": None},
        {"side": "SHORT"},
        {"order_type": "TRAILING_STOP_LOSS"},
        {"filled_quantity": 0.0},
        {"account_id": "SOMEONE-ELSE"},
    ],
)
def test_detail_refuses_missing_or_foreign(changes):
    with pytest.raises(MalformedResponse):
        read_order_detail(with_row(**changes), CID, own)


def test_detail_refuses_another_order():
    with pytest.raises(MalformedResponse):
        read_order_detail(recorded_combo(), "ap-other", own)


def test_detail_empty_orders_is_not_absence():
    combo = recorded_combo()
    combo["orders"] = []
    with pytest.raises(BrokerReadFailed) as exc:
        read_order_detail(combo, CID, own)
    assert exc.value.code == DETAIL_NOT_VISIBLE


def test_recorded_errors_map_to_typed_errors():
    b, t, budget = broker(
        load("error_non_trading_hours.json"),
        load("error_order_not_present.json"),
        load("error_order_not_present.json"),
        load("error_too_many_requests.json"),
    )
    with pytest.raises(Refused) as refused:
        b.place(limit_buy())
    assert refused.value.code == "OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS"
    with pytest.raises(OrderNotFound):
        b.order_detail("ap-404")
    with pytest.raises(OrderNotFound):
        b.cancel("ap-404")
    with pytest.raises(RateLimited):
        b.order_detail("ap-1")
    assert budget.usage(Route.ORDER_DETAIL.path).paused_for_seconds > 0
    assert len(t.sent) == 4


def open_orders_page(*rows: dict) -> WireResponse:
    combo = recorded_combo()
    combo["orders"] = list(rows)
    return WireResponse(200, {"data": [combo]})


def row(**changes) -> dict:
    return with_row(**changes)["orders"][0]


def test_foreign_unsupported_order_does_not_hide_our_stop():
    our_stop = row(client_order_id="ap-stop-1", side="SELL", order_type="STOP_LOSS",
                   status="SUBMITTED", stop_price="95.00", limit_price=None)
    manual_trailing = row(client_order_id="app-manual", order_type="TRAILING_STOP_LOSS",
                          side="SELL", status="SUBMITTED")
    manual_short = row(client_order_id="", side="SHORT", status="SUBMITTED")
    b, _, _ = broker(open_orders_page(our_stop, manual_trailing, manual_short))
    with pytest.raises(ForeignOrdersUnreadable) as exc:
        b.open_orders()
    (ours,) = exc.value.orders
    assert ours.client_order_id == "ap-stop-1" and ours.state is OrderState.ACCEPTED
    assert [u.client_order_id for u in exc.value.unreadable] == ["app-manual", ""]


def test_readable_foreign_order_is_returned():
    manual = row(client_order_id="app-manual", status="SUBMITTED")
    b, _, _ = broker(open_orders_page(manual))
    (only,) = b.open_orders()
    assert only.client_order_id == "app-manual"


def test_own_malformed_row_still_fails_the_read():
    broken = row(client_order_id="ap-1", order_type="TRAILING_STOP_LOSS")
    b, _, _ = broker(open_orders_page(broken))
    with pytest.raises(MalformedResponse):
        b.open_orders()


def test_another_accounts_row_fails_the_read():
    other = row(client_order_id="app-manual", account_id="SOMEONE-ELSE")
    b, _, _ = broker(open_orders_page(other))
    with pytest.raises(MalformedResponse):
        b.open_orders()


def test_detail_reads_order_id_and_optional_stop_price():
    """The recorded row has `order_id` and no stop; a stop row's `stop_price` (named like the
    recorded `limit_price`; no working stop recorded yet) is read when present."""
    detail = read_order_detail(recorded_combo(), CID, own)
    assert detail.broker_order_id == "ORDERID000000000000000001" and detail.stop_price is None
    stop = read_order_detail(with_row(order_type="STOP_LOSS", side="SELL", status="SUBMITTED",
                                      limit_price=None, stop_price="98.50"), CID, own)
    assert stop.stop_price == D("98.50") and stop.order_type is OrderType.STOP_LOSS
