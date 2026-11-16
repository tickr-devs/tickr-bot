"""Readers and error mapping against recorded Webull responses (tests/fixtures/webull)."""

from __future__ import annotations

import copy
import json
from datetime import date
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


# --- balance (recorded 2026-11-16) -------------------------------------------------------------


def test_recorded_balance():
    b, t, _ = broker(load("balance.json"))
    got = b.balance()
    assert [(r, p) for r, p in t.sent] == [(Route.BALANCE, {"total_asset_currency": "USD"})]
    assert got.net_liquidation == D("1000000.00") and isinstance(got.net_liquidation, D)
    assert (got.cash, got.market_value) == (D("1000000.00"), D("0.00"))
    assert (got.unrealized_profit_loss, got.day_profit_loss) == (D("0.00"), D("0.00"))
    assert (got.day_buying_power, got.overnight_buying_power) == (D("4000000"), D("2000000.00"))
    assert (got.maintenance_margin, got.margin_calls) == (D("0.00"), 0)
    assert got.day_trades_left is None  # "UNLIMITED"


def balance_with(**changes) -> WireResponse:
    body = copy.deepcopy(load("balance.json").body)
    for key, value in changes.items():
        if value is None:
            body.pop(key)
        else:
            body[key] = value
    return WireResponse(200, body)


def test_balance_day_trades_left_number():
    b, _, _ = broker(balance_with(day_trades_left="3"))
    assert b.balance().day_trades_left == 3


@pytest.mark.parametrize("changes", [
    {"total_asset_currency": "HKD"},
    {"total_net_liquidation_value": None},
    {"total_cash_balance": "n/a"},
    {"day_trades_left": "some"},
    {"day_trades_left": None},
    {"open_margin_calls": None},
    {"account_currency_assets": []},
    {"account_currency_assets": "USD"},
])
def test_balance_refuses_unexpected_shapes(changes):
    b, _, _ = broker(balance_with(**changes))
    with pytest.raises(MalformedResponse):
        b.balance()


def test_balance_refuses_a_missing_buying_power():
    body = copy.deepcopy(load("balance.json").body)
    del body["account_currency_assets"][0]["day_buying_power"]
    b, _, _ = broker(WireResponse(200, body))
    with pytest.raises(MalformedResponse):
        b.balance()


def test_balance_read_failure_is_typed():
    b, _, _ = broker(WireResponse(500, {"error_code": "X"}))
    with pytest.raises(BrokerReadFailed):
        b.balance()


def test_open_order_row_carries_its_limit_price():
    rows, _ = read_order_rows(load("historical_orders_cancelled_limit.json").body, own,
                              lambda c: True, "h")
    assert rows[0].limit_price == D("100.00")


def test_an_unreadable_limit_price_is_unknown_never_a_refusal():
    body = load("historical_orders_cancelled_limit.json").body
    body["data"][0]["orders"][0]["limit_price"] = "--"
    rows, bad = read_order_rows(body, own, lambda c: True, "h")
    assert bad == [] and rows[0].limit_price is None


def test_balance_refuses_non_ascii_digits():
    b, _, _ = broker(balance_with(day_trades_left="\u00b2"))
    with pytest.raises(MalformedResponse):
        b.balance()


# --- a position, its working stop and the filled entry (recorded 2026-11-16) ------------------

TRADE_DAY = date(2026, 11, 16)


def test_recorded_long_position():
    b, _, _ = broker(load("positions_one_long.json"))
    (p,) = b.positions()
    assert (p.symbol, p.quantity, p.average_cost) == ("TSCO", D(64), D("38.44"))
    assert (p.last_price, p.market_value) == (D("38.54"), D("2466.56"))
    assert (p.unrealized_profit_loss, p.day_profit_loss) == (D("6.40"), D("6.40"))


def test_unreadable_position_display_fields_are_unknown():
    body = copy.deepcopy(load("positions_one_long.json").body)
    body[0]["last_price"] = "--"
    del body[0]["market_value"]
    b, _, _ = broker(WireResponse(200, body))
    (p,) = b.positions()
    assert (p.quantity, p.last_price, p.market_value) == (D(64), None, None)


def test_recorded_working_stop_is_our_live_stop():
    b, _, _ = broker(load("open_orders_working_stop.json"))
    (stop,) = b.open_orders()
    assert (stop.symbol, stop.side, stop.order_type) == ("TSCO", Side.SELL, OrderType.STOP_LOSS)
    assert stop.state is OrderState.ACCEPTED and stop.raw_status == "SUBMITTED"
    assert (stop.quantity, stop.filled_quantity, stop.stop_price) == (D(64), D(0), D("38.04"))
    assert stop.limit_price is None
    assert stop.fees == D("0.07")  # the broker's estimate already listed: SEC 0.06 + FINRA 0.01


def test_recorded_fill_in_history():
    b, t, _ = broker(load("historical_orders_fill_and_stop.json"))
    (fill,) = b.fills_today(TRADE_DAY)  # the working stop has no fill
    assert [r for r, _ in t.sent] == [Route.HISTORY]
    assert (fill.client_order_id, fill.symbol, fill.side, fill.order_type) == (
        "ap-6e324fb0-t1-e1", "TSCO", "BUY", "LIMIT")
    assert (fill.filled_quantity, fill.filled_price, fill.fees) == (D(64), D("38.44"), D(0))
    assert fill.filled_at is not None and fill.filled_at.isoformat().startswith("2026-11-16T15:17")


def test_recorded_filled_entry_detail():
    body = load("historical_orders_fill_and_stop.json").body
    rows, bad = read_order_rows(body, own, lambda c: c.startswith("ap-"), "h")
    assert bad == []
    entry = next(r for r in rows if r.order_type is OrderType.LIMIT)
    assert entry.state is OrderState.FILLED
    assert (entry.average_fill_price, entry.limit_price) == (D("38.44"), D("38.51"))
    assert entry.fees == D(0)  # an empty `fees` list: nothing charged on the buy


def test_fill_fees_sum_and_unreadable_fee():
    body = copy.deepcopy(load("historical_orders_fill_and_stop.json").body)
    stop = body["data"][0]["orders"][0]
    stop["filled_quantity"] = "64"
    stop["filled_price"] = "38.04"
    stop["filled_time_at"] = "2026-11-16T15:40:00.000Z"
    b, _, _ = broker(WireResponse(200, copy.deepcopy(body)))
    fees = {f.order_type: f.fees for f in b.fills_today(TRADE_DAY)}
    assert fees["STOP_LOSS"] == D("0.07")
    stop["fees"][0]["actual_value"] = "n/a"
    b, _, _ = broker(WireResponse(200, body))
    fees = {f.order_type: f.fees for f in b.fills_today(TRADE_DAY)}
    assert fees["STOP_LOSS"] is None


def test_history_refuses_a_row_without_its_fill():
    body = copy.deepcopy(load("historical_orders_fill_and_stop.json").body)
    del body["data"][1]["orders"][0]["filled_quantity"]
    b, _, _ = broker(WireResponse(200, body))
    with pytest.raises(MalformedResponse):
        b.fills_today(TRADE_DAY)


def test_fills_of_another_new_york_day_are_left_out():
    b, _, _ = broker(load("historical_orders_fill_and_stop.json"))
    assert b.fills_today(date(2026, 11, 17)) == []


def test_a_filled_row_without_its_time_refuses():
    body = copy.deepcopy(load("historical_orders_fill_and_stop.json").body)
    body["data"][1]["orders"][0]["filled_time_at"] = "soon"
    b, _, _ = broker(WireResponse(200, body))
    with pytest.raises(MalformedResponse):
        b.fills_today(TRADE_DAY)


def test_history_pages_until_no_key_and_refuses_a_repeated_key():
    page = copy.deepcopy(load("historical_orders_fill_and_stop.json").body)
    first = {**copy.deepcopy(page), "pagination_key": "k1"}
    b, t, _ = broker(WireResponse(200, first), WireResponse(200, page))
    assert len(b.fills_today(TRADE_DAY)) == 2
    assert [p for _, p in t.sent] == [{}, {"pagination_key": "k1"}]
    b, _, _ = broker(WireResponse(200, first), WireResponse(200, first))
    with pytest.raises(BrokerReadFailed):
        b.fills_today(TRADE_DAY)


# --- cash activities: the exact trade amounts (recorded 2026-11-16) ---------------------------


def test_recorded_cash_trades_give_the_exact_sub_dollar_amounts():
    b, t, _ = broker(load("cash_activities_trades.json"))
    trades = b.cash_trades(TRADE_DAY)
    assert [r for r, _ in t.sent] == [Route.CASH_ACTIVITIES]
    cyab = {c.side: c for c in trades if c.symbol == "CYAB"}
    assert cyab["BUY"].net_amount == D("-208.5696")  # 918 x 0.2272: the order row said 0.23
    assert cyab["SELL"].net_amount == D("166.3352")  # 918 x 0.1814 less 0.19 fees
    assert cyab["SELL"].at.isoformat().startswith("2026-11-16T17:37:58.901")


def test_cash_trades_of_another_day_and_other_activities_are_left_out():
    body = copy.deepcopy(load("cash_activities_trades.json").body)
    body["data"][0]["trade_date"] = "2026-11-13"
    body["data"][1]["activity_type"] = "DIVIDEND"
    b, _, _ = broker(WireResponse(200, body))
    assert len(b.cash_trades(TRADE_DAY)) == 2


def test_a_cash_trade_without_its_amount_refuses():
    body = copy.deepcopy(load("cash_activities_trades.json").body)
    del body["data"][0]["net_amount"]
    b, _, _ = broker(WireResponse(200, body))
    with pytest.raises(MalformedResponse):
        b.cash_trades(TRADE_DAY)
