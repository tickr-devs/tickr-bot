"""Webull OpenAPI broker adapter. The only module allowed to import the Webull SDK.

Layers, outermost first:

- `WebullBroker` implements `tickr.broker.base.Broker`. It takes request budget, sends
  through a `Transport`, maps HTTP outcomes to typed `BrokerError`s and parses bodies with
  the tolerant readers below. It never sees credentials or the account id.
- `Transport` is the wire boundary: `send(route, params) -> WireResponse`. Tests use a
  recorded transport; the running bot uses `SdkTransport`.
- `SdkTransport` wraps `webull-openapi-python-sdk` (optional dependency group `webull`). It
  pins every request to the configured host, which must be on the allowlist; turns SDK
  retries off; removes the SDK's log handlers (KD-4); never constructs `TradeEventsClient`
  (KD-3) and never imports the SDK's order enums (KD-5). It holds the account id and adds it
  to each request; nothing else in the process does.

Known SDK defects KD-1 (streaming close) and KD-2 (tick request limits) concern market data,
which this adapter does not read; the market-data adapter must handle them.

Rules this module enforces (CLAUDE.md, spec section 9):
- Trading POSTs (place, cancel) are sent exactly once. Any doubt about the outcome raises
  `OutcomeUnknown`; resolving it is the caller's job (Detail, never a resend).
- Unknown order status maps to `OrderState.UNKNOWN`, never to a guess.
- Missing or malformed fields raise `MalformedResponse` naming the field, never a default.
- Prices and quantities are `Decimal`; JSON numbers are parsed as `Decimal`, never float.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from tickr.broker.base import (
    AccountBalance,
    AccountSnapshot,
    Acknowledgement,
    BrokerError,
    BrokerFill,
    CashTrade,
    OrderNotFound,
    OutcomeUnknown,
    RateLimited,
    Refused,
    ReplaceAck,
)
from tickr.broker.budget import RequestBudget, RouteLimit
from tickr.broker.status import map_status
from tickr.calendar import ny_date_of
from tickr.config import BARS_ROUTE, Config, GateResult, check_chart_slice, trading_gate
from tickr.data.base import (
    BarSpan,
    DelayedData,
    InstrumentProfile,
    MarketDataError,
    QuoteListener,
    ScreenerKind,
    ScreenerRow,
    ScreenerSession,
    SessionBar,
    SourceRead,
    StreamNotice,
    SubscriptionLimit,
    SymbolsRefused,
)
from tickr.types import (
    Bar,
    ComboRole,
    OrderDetail,
    OrderRequest,
    OrderType,
    Position,
    Quote,
    Side,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------------
# Protocol constants (Webull wire vocabulary, docs/webull-facts.md). Not tunables.
# --------------------------------------------------------------------------------------------

HTTP_OK_MIN = 200
HTTP_OK_MAX = 299
HTTP_TOO_MANY_REQUESTS = 429

CODE_NON_TRADING_HOURS = "OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS"
CODE_PARAM_ERROR = "OPENAPI_PARAM_ERR"
CODE_INVALID_SYMBOL = "INVALID_SYMBOL"
GATE_CLOSED = "GATE_CLOSED"
MESSAGE_ORDER_NOT_PRESENT = "order not present"

# Error codes that prove a place request did NOT create an order (probe evidence only).
PLACE_REFUSAL_CODES = frozenset({CODE_NON_TRADING_HOURS})

# Fixed parts of a US equity order body accepted by the broker (probe 2026-10-12, 2026-11-04).
ORDER_COMBO_TYPE = "NORMAL"
ORDER_ENTRUST_TYPE = "QTY"
ORDER_INSTRUMENT_TYPE = "EQUITY"
ORDER_MARKET = "US"
# Trailing stop parts, from the SDK v3 sample only (NOT probed: engine/trailing_probe.py).
ORDER_TRAILING_TYPE = "PERCENTAGE"
ORDER_TRIGGER_PRICE_TYPE = "PRICE"

SDK_API_VERSION = "v3"
SDK_LOGGER_PREFIX = "webull"
LOGGING_OFF = logging.CRITICAL + 1
SECONDS_PER_MINUTE = 60


class Route(Enum):
    """Webull routes this adapter uses: (HTTP method, path, mutates broker state)."""

    ACCOUNTS = ("GET", "/trading/accounts/list", False)
    BALANCE = ("GET", "/trading/assets/balances/get", False)
    HISTORY = ("GET", "/trading/orders/historical-orders/list", False)
    CASH_ACTIVITIES = ("GET", "/trading/activities/cash-activities/list", False)
    POSITIONS = ("GET", "/trading/assets/positions/list", False)
    OPEN_ORDERS = ("GET", "/trading/orders/open-orders/list", False)
    ORDER_DETAIL = ("GET", "/trading/orders/get", False)
    PLACE = ("POST", "/trading/orders/place", True)
    CANCEL = ("POST", "/trading/orders/cancel", True)
    REPLACE = ("POST", "/trading/orders/replace", True)
    GAINERS_LOSERS = ("GET", "/market-data/screeners/gainers-losers/list", False)
    TOP_ACTIVES = ("GET", "/market-data/screeners/top-actives/list", False)
    SNAPSHOT = ("GET", "/market-data/stocks/snapshots/list", False)
    BARS = ("POST", BARS_ROUTE, False)
    PROFILES = ("GET", "/trading/instruments/stocks/profiles/list", False)
    # Read only by `quote_sources` (probe Q1): which read is fresh outside the regular session.
    DEPTHS = ("GET", "/market-data/stocks/depths/list", False)
    TICKS = ("GET", "/market-data/stocks/ticks/list", False)

    @property
    def method(self) -> str:
        return self.value[0]

    @property
    def path(self) -> str:
        return self.value[1]

    @property
    def mutating(self) -> bool:
        return self.value[2]


# Routes that are not account-scoped: the transport never adds the account id to them.
ACCOUNT_FREE_ROUTES = frozenset(
    {Route.ACCOUNTS, Route.GAINERS_LOSERS, Route.TOP_ACTIVES, Route.SNAPSHOT, Route.BARS,
     Route.PROFILES, Route.DEPTHS, Route.TICKS}
)
# The only routes a market-data-only transport (the API's chart reads) may send.
MARKET_DATA_ROUTES = frozenset(
    {Route.GAINERS_LOSERS, Route.TOP_ACTIVES, Route.SNAPSHOT, Route.BARS, Route.PROFILES,
     Route.DEPTHS, Route.TICKS}
)


# --------------------------------------------------------------------------------------------
# Errors specific to this adapter (all are BrokerError, so callers can catch the base).
# --------------------------------------------------------------------------------------------


class MalformedResponse(BrokerError):
    """A broker response lacked a required field or had an unparseable value."""


class BrokerReadFailed(BrokerError):
    """A read did not return usable data (transport failure or non-success HTTP status)."""

    def __init__(self, route: str, http_status: int | None, code: str | None) -> None:
        super().__init__(f"{route}: http_status={http_status} code={code}")
        self.route = route
        self.http_status = http_status
        self.code = code


class AdapterConfigError(BrokerError):
    """The adapter cannot be constructed safely (host not allowed, credential absent...)."""


@dataclass(frozen=True)
class UnreadableOrder:
    """A foreign order row outside our vocabulary (e.g. SHORT, TRAILING_STOP_LOSS_LIMIT)."""

    client_order_id: str
    symbol: str
    reason: str


class ForeignOrdersUnreadable(BrokerError):
    """Open orders were read completely, but some foreign rows could not be parsed.

    `orders` holds every readable row (our own included, so protection stays visible);
    `unreadable` lists the rest. The caller must treat them as foreign exposure."""

    def __init__(self, orders: list[OrderDetail], unreadable: list[UnreadableOrder]) -> None:
        super().__init__(f"{len(unreadable)} foreign open order(s) could not be read")
        self.orders = orders
        self.unreadable = unreadable


# --------------------------------------------------------------------------------------------
# Wire boundary
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WireResponse:
    """HTTP status plus the parsed JSON body (numbers as Decimal). For non-success statuses
    the body is `{"error_code": ..., "message": ...}` as far as the broker supplied them."""

    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return HTTP_OK_MIN <= self.status <= HTTP_OK_MAX

    @property
    def error_code(self) -> str | None:
        if isinstance(self.body, Mapping):
            code = self.body.get("error_code")
            return str(code) if code is not None else None
        return None

    @property
    def message(self) -> str:
        if isinstance(self.body, Mapping):
            return str(self.body.get("message") or "")
        return ""


class TransportFailure(Exception):
    """No HTTP response was received (connect error, timeout, TLS...). The request may or
    may not have reached the broker."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Transport(Protocol):
    def send(self, route: Route, params: Mapping[str, Any]) -> WireResponse:
        """Send one request exactly once. Raise TransportFailure if no response arrived."""
        ...

    def is_own_account(self, account_id: str) -> bool:
        """True if `account_id` is the configured account. The value is never stored."""
        ...


def parse_json(raw: bytes | str) -> Any:
    """Parse a broker body. Non-integer numbers become Decimal, never float."""
    return json.loads(raw, parse_float=Decimal)


# --------------------------------------------------------------------------------------------
# Tolerant readers: ignore unknown fields, refuse on missing or malformed required ones.
# --------------------------------------------------------------------------------------------


def _require(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, Mapping):
        raise MalformedResponse(f"{where}: expected an object, got {type(obj).__name__}")
    value = obj.get(key)
    if value is None or value == "":
        raise MalformedResponse(f"{where}: missing field {key!r}")
    return value


def _optional(obj: Mapping[str, Any], key: str) -> Any:
    value = obj.get(key)
    return None if value == "" else value


def to_decimal(value: Any, where: str) -> Decimal:
    """Strict Decimal conversion: str, int or Decimal; never float or bool."""
    if isinstance(value, bool) or isinstance(value, float):
        raise MalformedResponse(f"{where}: refusing {type(value).__name__} as a number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise MalformedResponse(f"{where}: not a number") from None
    else:
        raise MalformedResponse(f"{where}: unexpected type {type(value).__name__}")
    if not result.is_finite():
        raise MalformedResponse(f"{where}: not a finite number")
    return result


def _decimal_field(obj: Mapping[str, Any], key: str, where: str) -> Decimal:
    return to_decimal(_require(obj, key, where), f"{where}.{key}")


def _optional_decimal(obj: Mapping[str, Any], key: str, where: str) -> Decimal | None:
    value = _optional(obj, key)
    return None if value is None else to_decimal(value, f"{where}.{key}")


def _display_decimal(obj: Mapping[str, Any], key: str, where: str) -> Decimal | None:
    """A number shown and never decided on: None when absent or unreadable."""
    try:
        return _optional_decimal(obj, key, where)
    except MalformedResponse:
        return None


def _enum_field[E: Enum](obj: Mapping[str, Any], key: str, enum: type[E], where: str) -> E:
    raw = str(_require(obj, key, where))
    try:
        return enum(raw)
    except ValueError:
        raise MalformedResponse(f"{where}.{key}: unsupported value {raw!r}") from None


def _unwrap_list(body: Any, where: str) -> list[Any]:
    """Lists arrive either bare or as {"data": [...]}. Anything else is malformed."""
    if isinstance(body, list):
        return body
    if isinstance(body, Mapping) and isinstance(body.get("data"), list):
        return list(body["data"])
    raise MalformedResponse(f"{where}: expected a list or an object with list 'data'")


IsOwnAccount = Callable[[str], bool]


def _check_account(row: Any, is_own: IsOwnAccount, where: str) -> None:
    """Reads are account-scoped; a row that names an account must name ours."""
    if isinstance(row, Mapping) and "account_id" in row and not is_own(str(row["account_id"])):
        raise MalformedResponse(f"{where}: row belongs to another account")


def read_order(item: Any, is_own: IsOwnAccount, where: str) -> OrderDetail:
    """One order row (Detail `orders[]` or open-orders `orders[]`) -> OrderDetail.

    Field names are the recorded ones (tests/fixtures/webull). `filled_quantity` is required:
    an order whose fill we cannot read is one we cannot reason about. Status/quantity
    contradictions map to UNKNOWN (broker/status.py). `filled_price` is recorded on a
    filled row and absent on an unfilled one, so it stays optional; so do `limit_price`
    (recorded on a LIMIT row; read on LIMIT rows only, display only), `stop_price`
    (recorded on the working stop, 2026-11-16) and `order_id`."""
    _check_account(item, is_own, where)
    raw_status = str(_require(item, "status", where)).strip()
    quantity = _decimal_field(item, "total_quantity", where)
    filled = _decimal_field(item, "filled_quantity", where)
    order_type = _enum_field(item, "order_type", OrderType, where)
    # Display only: read on LIMIT rows, the one type it was recorded on (and STOP_LOSS_LIMIT
    # rows, sent only by the F7 probe), and an unreadable value is shown as unknown: nothing
    # decides on it, so it can never make one of our orders unreadable.
    limit_price = _display_decimal(item, "limit_price", where) \
        if order_type in (OrderType.LIMIT, OrderType.STOP_LOSS_LIMIT) else None
    return OrderDetail(
        client_order_id=str(_require(item, "client_order_id", where)),
        symbol=str(_require(item, "symbol", where)),
        side=_enum_field(item, "side", Side, where),
        order_type=order_type,
        quantity=quantity,
        filled_quantity=filled,
        average_fill_price=_optional_decimal(item, "filled_price", where),
        state=map_status(raw_status, quantity, filled),
        raw_status=raw_status,
        stop_price=_optional_decimal(item, "stop_price", where),
        limit_price=limit_price,
        broker_order_id=None if _optional(item, "order_id") is None else str(item["order_id"]),
        fees=_fees(item, where),
        trailing_stop_step=_display_decimal(item, "trailing_stop_step", where)
        if order_type is OrderType.TRAILING_STOP_LOSS else None,
        row_keys=tuple(sorted(str(k) for k in item))
        if order_type is OrderType.TRAILING_STOP_LOSS else None,
    )


DETAIL_NOT_VISIBLE = "DETAIL_NOT_VISIBLE"


def read_order_detail(body: Any, client_order_id: str, is_own: IsOwnAccount) -> OrderDetail:
    """Detail body `{..., "orders": [row]}` -> the one row with our client_order_id.

    An empty `orders` list is not proof that the order was never sent (Detail can lag the
    acknowledgement): it raises BrokerReadFailed(DETAIL_NOT_VISIBLE) so the caller polls."""
    where = "order_detail"
    _check_account(body, is_own, where)
    if not isinstance(body, Mapping) or "orders" not in body:
        raise MalformedResponse(f"{where}: missing field 'orders'")
    orders = body["orders"]
    if orders == []:
        raise BrokerReadFailed(Route.ORDER_DETAIL.name, HTTP_OK_MIN, DETAIL_NOT_VISIBLE)
    if not isinstance(orders, list):
        raise MalformedResponse(f"{where}: 'orders' is not a list")
    matches = [
        o for o in orders if isinstance(o, Mapping) and o.get("client_order_id") == client_order_id
    ]
    if len(matches) != 1:
        raise MalformedResponse(f"{where}: expected one order with our id, found {len(matches)}")
    return read_order(matches[0], is_own, where)


OwnOrder = Callable[[str], bool]


def read_order_rows(
    body: Any, is_own: IsOwnAccount, is_own_order: OwnOrder, where: str
) -> tuple[list[OrderDetail], list[UnreadableOrder]]:
    """Open-orders (and history) page: `{"data": [combo{..., "orders": [row...]}]}`.

    Our own rows (by client_order_id prefix) are strict: one malformed row fails the read.
    A foreign row we cannot parse is returned as `UnreadableOrder` so that one manual order
    of an unsupported side or type never hides our own stop."""
    result: list[OrderDetail] = []
    unreadable: list[UnreadableOrder] = []
    for index, combo in enumerate(_unwrap_list(body, where)):
        here = f"{where}[{index}]"
        _check_account(combo, is_own, here)
        nested = _require(combo, "orders", here)
        if not isinstance(nested, list):
            raise MalformedResponse(f"{here}: 'orders' is not a list")
        for i, row in enumerate(nested):
            row_where = f"{here}.orders[{i}]"
            _check_account(row, is_own, row_where)  # another account's row is never "foreign"
            cid = str(row.get("client_order_id") or "") if isinstance(row, Mapping) else ""
            try:
                result.append(read_order(row, is_own, row_where))
            except MalformedResponse as exc:
                if cid and is_own_order(cid):
                    raise
                symbol = str(row.get("symbol") or "") if isinstance(row, Mapping) else ""
                unreadable.append(UnreadableOrder(cid, symbol, str(exc)))
    return result, unreadable


def read_positions(body: Any, is_own: IsOwnAccount) -> list[Position]:
    """Positions: a bare array (recorded empty and with one long row,
    tests/fixtures/webull/positions_one_long.json). `symbol` and `quantity` are required;
    `cost_price` is the average cost. Last price, market value and P&L are display only:
    absent or unreadable is None, never a refusal."""
    where = "positions"
    result: list[Position] = []
    for index, row in enumerate(_unwrap_list(body, where)):
        here = f"{where}[{index}]"
        _check_account(row, is_own, here)
        result.append(
            Position(
                symbol=str(_require(row, "symbol", here)),
                quantity=_decimal_field(row, "quantity", here),
                average_cost=_optional_decimal(row, "cost_price", here),
                last_price=_display_decimal(row, "last_price", here),
                market_value=_display_decimal(row, "market_value", here),
                unrealized_profit_loss=_display_decimal(row, "unrealized_profit_loss", here),
                day_profit_loss=_display_decimal(row, "day_profit_loss", here),
            )
        )
    return result


def _display_time(row: Mapping[str, Any], key: str) -> datetime | None:
    """An ISO time shown and never decided on (`filled_time_at`): None when unreadable."""
    value = _optional(row, key)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _fees(row: Mapping[str, Any], where: str) -> Decimal | None:
    """Sum of the row's `fees[].actual_value`; None when the list or a value is unreadable."""
    fees = row.get("fees")
    if not isinstance(fees, list):
        return None
    total = Decimal(0)
    for fee in fees:
        if not isinstance(fee, Mapping):
            return None
        value = _display_decimal(fee, "actual_value", where)
        if value is None:
            return None
        total += value
    return total


# `activity_type` of a trade's settlement in the cash activities (probe 2026-11-16).
CASH_ACTIVITY_TRADE = "TRADE"


def read_cash_trades(body: Any, is_own: IsOwnAccount, day: date) -> list[CashTrade]:
    """Cash activities page `{"data": [row...]}` (recorded 2026-11-16,
    tests/fixtures/webull/cash_activities_trades.json) -> the TRADE rows of New York date
    `day` (`trade_date`). Every field used is required: an exact amount we cannot read is
    no amount."""
    where = "cash_activities"
    result: list[CashTrade] = []
    for index, row in enumerate(_unwrap_list(body, where)):
        here = f"{where}[{index}]"
        _check_account(row, is_own, here)
        if _require(row, "activity_type", here) != CASH_ACTIVITY_TRADE:
            continue
        if str(_require(row, "trade_date", here)) != day.isoformat():
            continue
        at = _display_time(row, "biz_time")
        if at is None:
            raise MalformedResponse(f"{here}: unreadable 'biz_time'")
        result.append(CashTrade(
            symbol=str(_require(row, "symbol", here)),
            side=str(_require(row, "activity_sub_type", here)),
            net_amount=_decimal_field(row, "net_amount", here),
            at=at,
        ))
    return result


def read_fills(body: Any, is_own: IsOwnAccount, day: date) -> list[BrokerFill]:
    """Order history page `{"data": [combo{..., "orders": [row...]}]}` (recorded,
    tests/fixtures/webull/historical_orders_fill_and_stop.json) -> the rows filled on New York
    date `day` (the route's default window is several days, SDK). `symbol`,
    `filled_quantity` and, on a filled row, `filled_time_at` are required (the day of a fill
    whose time is unreadable is unknown); price and fees are display only."""
    where = "history"
    result: list[BrokerFill] = []
    for index, combo in enumerate(_unwrap_list(body, where)):
        here = f"{where}[{index}]"
        _check_account(combo, is_own, here)
        nested = _require(combo, "orders", here)
        if not isinstance(nested, list):
            raise MalformedResponse(f"{here}: 'orders' is not a list")
        for i, row in enumerate(nested):
            row_where = f"{here}.orders[{i}]"
            _check_account(row, is_own, row_where)
            filled = _decimal_field(row, "filled_quantity", row_where)
            if filled <= 0:
                continue
            filled_at = _display_time(row, "filled_time_at")
            if filled_at is None:
                raise MalformedResponse(f"{row_where}: filled row without a readable "
                                        "'filled_time_at'")
            if ny_date_of(filled_at) != day:
                continue
            cid = _optional(row, "client_order_id")
            result.append(BrokerFill(
                client_order_id=None if cid is None else str(cid),
                symbol=str(_require(row, "symbol", row_where)),
                side=str(_optional(row, "side") or ""),
                order_type=str(_optional(row, "order_type") or ""),
                filled_quantity=filled,
                filled_price=_display_decimal(row, "filled_price", row_where),
                filled_at=filled_at,
                fees=_fees(row, row_where),
            ))
    return result


def read_acknowledgement(body: Any, client_order_id: str, is_own: IsOwnAccount) -> Acknowledgement:
    """Place response -> Acknowledgement. Must echo our client_order_id and carry the broker
    order id, with no error envelope. Anything else is not an acknowledgement."""
    where = "place"
    if isinstance(body, Mapping) and "error_code" in body:
        raise MalformedResponse(f"{where}: success status with an error envelope")
    _check_account(body, is_own, where)
    echoed = str(_require(body, "client_order_id", where))
    if echoed != client_order_id:
        raise MalformedResponse(f"{where}: acknowledgement is for another client_order_id")
    order_id = str(_require(body, "order_id", where)).strip()
    if not order_id:
        raise MalformedResponse(f"{where}: empty order_id")
    return Acknowledgement(client_order_id=echoed, broker_order_id=order_id)


def read_accounts(body: Any, is_own: Callable[[str], bool]) -> AccountSnapshot:
    where = "accounts"
    rows = _unwrap_list(body, where)
    own = [r for r in rows if isinstance(r, Mapping) and is_own(str(r.get("account_id", "")))]
    if len(own) > 1:
        raise MalformedResponse(f"{where}: configured account listed more than once")
    # Being listed for this App Key is what makes trading requests possible; Webull exposes
    # no separate permission flag in this route.
    present = len(own) == 1
    label = _optional(own[0], "account_label") if present else None
    return AccountSnapshot(account_present=present, trading_permitted=present,
                           label=None if label is None else str(label))


# The currency the balance read asks its totals in (`total_asset_currency`), and the currency
# row it reads the buying powers from: the bot trades US stocks in USD.
BALANCE_CURRENCY = "USD"
# `day_trades_left` when the broker sets no limit (probe 2026-11-16).
DAY_TRADES_UNLIMITED = "UNLIMITED"


def read_balance(body: Any, is_own: IsOwnAccount) -> AccountBalance:
    """Balance body (recorded 2026-11-16, tests/fixtures/webull/balance.json): `total_*` sums in
    `total_asset_currency`, and one `account_currency_assets[]` row per currency holding the
    buying powers. Every field we show is required; anything unexpected refuses the read."""
    where = "balance"
    if not isinstance(body, Mapping):
        raise MalformedResponse(f"{where}: expected an object")
    _check_account(body, is_own, where)
    currency = str(_require(body, "total_asset_currency", where))
    if currency != BALANCE_CURRENCY:
        raise MalformedResponse(f"{where}: totals in {currency!r}, not {BALANCE_CURRENCY}")
    rows = _require(body, "account_currency_assets", where)
    if not isinstance(rows, list):
        raise MalformedResponse(f"{where}: 'account_currency_assets' is not a list")
    usd = [r for r in rows if isinstance(r, Mapping) and r.get("currency") == BALANCE_CURRENCY]
    if len(usd) != 1:
        raise MalformedResponse(f"{where}: expected one {BALANCE_CURRENCY} row, found {len(usd)}")
    row, row_where = usd[0], f"{where}.account_currency_assets[{BALANCE_CURRENCY}]"
    calls = body.get("open_margin_calls")
    if not isinstance(calls, list):
        raise MalformedResponse(f"{where}: 'open_margin_calls' is not a list")
    left = str(_require(body, "day_trades_left", where)).strip()
    if left == DAY_TRADES_UNLIMITED:
        trades_left = None
    elif left.isascii() and left.isdigit():
        trades_left = int(left)
    else:
        raise MalformedResponse(f"{where}.day_trades_left: unsupported value {left!r}")
    return AccountBalance(
        net_liquidation=_decimal_field(body, "total_net_liquidation_value", where),
        cash=_decimal_field(body, "total_cash_balance", where),
        market_value=_decimal_field(body, "total_market_value", where),
        unrealized_profit_loss=_decimal_field(body, "total_unrealized_profit_loss", where),
        day_profit_loss=_decimal_field(body, "total_day_profit_loss", where),
        day_buying_power=_decimal_field(row, "day_buying_power", row_where),
        overnight_buying_power=_decimal_field(row, "overnight_buying_power", row_where),
        maintenance_margin=_decimal_field(body, "maintenance_margin", where),
        margin_calls=len(calls),
        day_trades_left=trades_left,
    )


# --------------------------------------------------------------------------------------------
# Order body
# --------------------------------------------------------------------------------------------


def _wire_decimal(value: Decimal) -> str:
    return format(value, "f")


def build_order_body(request: OrderRequest, client_order_id_max_length: int,
                     combo_type: str = ORDER_COMBO_TYPE) -> dict[str, Any]:
    """OrderRequest -> one `new_orders[]` element (`combo_type` NORMAL, or a combo leg's role).
    Raises Refused for requests Webull must never see; the code names the rule, and nothing is
    sent."""
    cid = request.client_order_id
    if not cid or len(cid) > client_order_id_max_length:
        raise Refused("LOCAL_CLIENT_ORDER_ID", "empty or longer than the broker limit")
    q = request.quantity
    if not q.is_finite() or q <= 0 or q != q.to_integral_value():
        raise Refused("LOCAL_QUANTITY", "quantity must be a positive whole number")
    needs_limit = request.order_type in (OrderType.LIMIT, OrderType.STOP_LOSS_LIMIT)
    needs_stop = request.order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT)
    trailing = request.order_type is OrderType.TRAILING_STOP_LOSS
    if needs_limit != (request.limit_price is not None):
        raise Refused("LOCAL_LIMIT_PRICE", f"limit_price does not fit {request.order_type}")
    if needs_stop != (request.stop_price is not None):
        raise Refused("LOCAL_STOP_PRICE", f"stop_price does not fit {request.order_type}")
    if trailing != (request.trailing_stop_step is not None):
        raise Refused("LOCAL_TRAILING_STEP",
                      f"trailing_stop_step does not fit {request.order_type}")
    for price in (request.limit_price, request.stop_price, request.trailing_stop_step):
        if price is not None and (not price.is_finite() or price <= 0):
            raise Refused("LOCAL_PRICE", "prices must be positive")
    body: dict[str, Any] = {
        "client_order_id": cid,
        "combo_type": combo_type,
        "entrust_type": ORDER_ENTRUST_TYPE,
        "instrument_type": ORDER_INSTRUMENT_TYPE,
        "market": ORDER_MARKET,
        "order_type": request.order_type.value,
        "quantity": _wire_decimal(q.to_integral_value()),
        "side": request.side.value,
        "support_trading_session": request.session.value,
        "symbol": request.symbol,
        "time_in_force": request.time_in_force.value,
    }
    if request.limit_price is not None:
        body["limit_price"] = _wire_decimal(request.limit_price)
    if request.stop_price is not None:
        body["stop_price"] = _wire_decimal(request.stop_price)
    if request.trailing_stop_step is not None:
        # SDK v3 sample (trade_client_v3.py, trailing stop loss): PERCENTAGE step, PRICE trigger.
        body["trailing_type"] = ORDER_TRAILING_TYPE
        body["trailing_stop_step"] = _wire_decimal(request.trailing_stop_step)
        body["trigger_price_type"] = ORDER_TRIGGER_PRICE_TYPE
    return body


def build_replace_stop_body(client_order_id: str, quantity: Decimal, stop_price: Decimal,
                            client_order_id_max_length: int) -> dict[str, Any]:
    """One `modify_orders[]` element (SDK v3 samples: the existing `client_order_id` names the
    order, with `quantity` and the new price). Raises Refused for what must never be sent."""
    if not client_order_id or len(client_order_id) > client_order_id_max_length:
        raise Refused("LOCAL_CLIENT_ORDER_ID", "empty or longer than the broker limit")
    if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
        raise Refused("LOCAL_QUANTITY", "quantity must be a positive whole number")
    if not stop_price.is_finite() or stop_price <= 0:
        raise Refused("LOCAL_PRICE", "prices must be positive")
    return {
        "client_order_id": client_order_id,
        "quantity": _wire_decimal(quantity.to_integral_value()),
        "stop_price": _wire_decimal(stop_price),
    }


def read_replace_ack(body: Any) -> ReplaceAck:
    """Replace success body -> whatever ids it echoes (shape unproven). An error envelope
    under a success status is not an acknowledgement."""
    if isinstance(body, Mapping) and "error_code" in body:
        raise MalformedResponse("replace: success status with an error envelope")
    if not isinstance(body, Mapping):
        return ReplaceAck(None, None)
    cid, oid = _optional(body, "client_order_id"), _optional(body, "order_id")
    return ReplaceAck(None if cid is None else str(cid), None if oid is None else str(oid))


# --------------------------------------------------------------------------------------------
# Broker
# --------------------------------------------------------------------------------------------


def send_once(
    transport: Transport,
    budget: RequestBudget,
    route: Route,
    params: Mapping[str, Any],
    clock: Callable[[], float],
    reserve: int = 0,
) -> WireResponse:
    """Take budget (leaving `reserve` requests of the route free), send once, handle transport
    failure and 429. Never retries."""
    budget.take(route.path, reserve)  # BudgetExhausted (RateLimited): nothing was sent
    started = clock()
    try:
        response = transport.send(route, params)
    except TransportFailure as failure:
        log.warning("%s %s transport failure %s", route.method, route.path, failure.code)
        if route.mutating:
            raise OutcomeUnknown(f"{route.name}: no response ({failure.code})") from None
        raise BrokerReadFailed(route.name, None, failure.code) from None
    elapsed_ms = (clock() - started) * 1000
    log.info(
        "%s %s -> %s %s (%.0f ms)",
        route.method, route.path, response.status, response.error_code or "", elapsed_ms,
    )
    if response.status == HTTP_TOO_MANY_REQUESTS:
        budget.pause(route.path)
        if route.mutating:
            # Not proven that a throttled POST created nothing: resolve by Detail.
            raise OutcomeUnknown(f"{route.name}: HTTP 429 on a trading request")
        raise RateLimited(f"{route.name}: HTTP 429 {response.error_code or ''}".strip())
    return response


class WebullBroker:
    """`Broker` over a `Transport`, with a per-route request budget."""

    def __init__(
        self,
        transport: Transport,
        budget: RequestBudget,
        open_orders_max_pages: int,
        client_order_id_max_length: int,
        own_client_order_id_prefixes: tuple[str, ...],
        gate: Callable[[], GateResult],
        clock: Callable[[], float] = time.monotonic,
        *,
        history_max_pages: int,
        cash_activities_max_pages: int,
    ) -> None:
        if open_orders_max_pages < 1:
            raise AdapterConfigError("open_orders_max_pages must be at least 1")
        if history_max_pages < 1:
            raise AdapterConfigError("history_max_pages must be at least 1")
        self._history_max_pages = history_max_pages
        if cash_activities_max_pages < 1:
            raise AdapterConfigError("cash_activities_max_pages must be at least 1")
        self._cash_activities_max_pages = cash_activities_max_pages
        if not own_client_order_id_prefixes or not all(own_client_order_id_prefixes):
            raise AdapterConfigError("own client order id prefixes must be non-empty")
        self._own_prefixes = own_client_order_id_prefixes
        self._gate = gate
        self._transport = transport
        self._budget = budget
        self._open_orders_max_pages = open_orders_max_pages
        self._client_order_id_max_length = client_order_id_max_length
        self._clock = clock

    # -- plumbing -------------------------------------------------------------------------

    def _send(self, route: Route, params: Mapping[str, Any]) -> WireResponse:
        return send_once(self._transport, self._budget, route, params, self._clock)

    @staticmethod
    def _order_not_present(response: WireResponse) -> bool:
        return (
            response.error_code == CODE_PARAM_ERROR
            and MESSAGE_ORDER_NOT_PRESENT in response.message.lower()
        )

    def _is_own_order(self, client_order_id: str) -> bool:
        return client_order_id.startswith(self._own_prefixes)

    def _require_gate(self) -> None:
        """Hard rule 5: no trading request leaves this adapter unless trading_gate() is open.
        Checked on every place/cancel, before budget is taken or anything is sent."""
        result = self._gate()
        if not result.open:
            raise Refused(GATE_CLOSED, result.cause)

    def _read(self, route: Route, params: Mapping[str, Any]) -> Any:
        response = self._send(route, params)
        if not response.ok:
            raise BrokerReadFailed(route.name, response.status, response.error_code)
        return response.body

    # -- Broker protocol ------------------------------------------------------------------

    def account(self) -> AccountSnapshot:
        return read_accounts(self._read(Route.ACCOUNTS, {}), self._transport.is_own_account)

    def balance(self) -> AccountBalance:
        return read_balance(
            self._read(Route.BALANCE, {"total_asset_currency": BALANCE_CURRENCY}),
            self._transport.is_own_account)

    def positions(self) -> list[Position]:
        return read_positions(self._read(Route.POSITIONS, {}), self._transport.is_own_account)

    def cash_trades(self, day: date) -> list[CashTrade]:
        """The cash activities' default window (no `start_time`; its format and the window
        are unproven), every page by `pagination_key` (SDK `list_activities`) up to
        `cash_activities_max_pages`, kept to the TRADE rows of `day`; a partial list refuses.
        Display reads only, off the loop thread."""
        trades: list[CashTrade] = []
        params: dict[str, Any] = {}
        seen_keys: set[str] = set()
        for _page in range(self._cash_activities_max_pages):
            body = self._read(Route.CASH_ACTIVITIES, params)
            trades.extend(read_cash_trades(body, self._transport.is_own_account, day))
            next_key = _optional(body, "pagination_key") if isinstance(body, Mapping) else None
            if next_key is None:
                return trades
            if str(next_key) in seen_keys:
                raise BrokerReadFailed(Route.CASH_ACTIVITIES.name, None, "PAGINATION_REPEATED")
            seen_keys.add(str(next_key))
            params = {"pagination_key": str(next_key)}
        raise BrokerReadFailed(Route.CASH_ACTIVITIES.name, None, "PAGE_LIMIT_REACHED")

    def fills_today(self, day: date) -> list[BrokerFill]:
        """The history's default window (no `start_time`; its format is unproven), every page
        by `pagination_key` (SDK `list_order_history`) up to `history_max_pages`, kept to the
        fills of `day`; a partial list refuses. Display only, called off the loop thread."""
        fills: list[BrokerFill] = []
        params: dict[str, Any] = {}
        seen_keys: set[str] = set()
        for _page in range(self._history_max_pages):
            body = self._read(Route.HISTORY, params)
            fills.extend(read_fills(body, self._transport.is_own_account, day))
            next_key = _optional(body, "pagination_key") if isinstance(body, Mapping) else None
            if next_key is None:
                return sorted(fills, key=lambda f: f.filled_at or datetime.min.replace(tzinfo=UTC))
            if str(next_key) in seen_keys:
                raise BrokerReadFailed(Route.HISTORY.name, None, "PAGINATION_REPEATED")
            seen_keys.add(str(next_key))
            params = {"pagination_key": str(next_key)}
        raise BrokerReadFailed(Route.HISTORY.name, None, "PAGE_LIMIT_REACHED")

    def open_orders(self) -> list[OrderDetail]:
        orders: list[OrderDetail] = []
        unreadable: list[UnreadableOrder] = []
        params: dict[str, Any] = {}
        seen_keys: set[str] = set()
        for _page in range(self._open_orders_max_pages):
            body = self._read(Route.OPEN_ORDERS, params)
            rows, bad = read_order_rows(
                body, self._transport.is_own_account, self._is_own_order, "open_orders"
            )
            orders.extend(rows)
            unreadable.extend(bad)
            next_key = _optional(body, "pagination_key") if isinstance(body, Mapping) else None
            if next_key is None:
                if unreadable:
                    raise ForeignOrdersUnreadable(orders, unreadable)
                return orders
            if str(next_key) in seen_keys:
                raise BrokerReadFailed(Route.OPEN_ORDERS.name, None, "PAGINATION_REPEATED")
            seen_keys.add(str(next_key))
            params = {"pagination_key": str(next_key)}
        # A partial list of open orders is not the broker's truth: refuse.
        raise BrokerReadFailed(Route.OPEN_ORDERS.name, None, "PAGE_LIMIT_REACHED")

    def order_detail(self, client_order_id: str) -> OrderDetail:
        response = self._send(Route.ORDER_DETAIL, {"client_order_id": client_order_id})
        if response.ok:
            return read_order_detail(
                response.body, client_order_id, self._transport.is_own_account
            )
        if self._order_not_present(response):
            raise OrderNotFound(client_order_id)
        raise BrokerReadFailed(Route.ORDER_DETAIL.name, response.status, response.error_code)

    def place(self, request: OrderRequest) -> Acknowledgement:
        self._require_gate()
        # Refused before any budget is taken or anything is sent.
        body = build_order_body(request, self._client_order_id_max_length)
        response = self._send(Route.PLACE, {"new_orders": [body]})
        if response.ok:
            try:
                return read_acknowledgement(
                    response.body, request.client_order_id, self._transport.is_own_account
                )
            except MalformedResponse as exc:
                # The broker accepted something; we cannot tell what. Detail decides.
                raise OutcomeUnknown(f"PLACE: unreadable acknowledgement ({exc})") from None
        if response.error_code in PLACE_REFUSAL_CODES:
            raise Refused(response.error_code or "", response.message)
        raise OutcomeUnknown(f"PLACE: HTTP {response.status} {response.error_code or ''}".strip())

    def place_combo(self, combo_id: str,
                    legs: Sequence[tuple[ComboRole, OrderRequest]]) -> Acknowledgement:
        """One combo in one place request (Webull docs: `client_combo_order_id` and one
        `new_orders[]` element per leg, each with its own `client_order_id` and its role as
        `combo_type`). UNPROVEN: the attached-stop probe is its only caller. The
        acknowledgement must echo the MASTER leg; any other success body is OutcomeUnknown,
        with its top-level keys named so the probe's evidence records the shape."""
        self._require_gate()
        if not combo_id or len(combo_id) > self._client_order_id_max_length:
            raise Refused("LOCAL_CLIENT_ORDER_ID", "combo id empty or longer than the limit")
        masters = [r for role, r in legs if role is ComboRole.MASTER]
        if len(masters) != 1 or len(legs) < 2:
            raise Refused("LOCAL_COMBO", "a combo is one MASTER and at least one attached leg")
        bodies = [build_order_body(r, self._client_order_id_max_length, role.value)
                  for role, r in legs]
        response = self._send(Route.PLACE, {"client_combo_order_id": combo_id,
                                             "new_orders": bodies})
        if response.ok:
            try:
                return read_acknowledgement(
                    response.body, masters[0].client_order_id, self._transport.is_own_account)
            except MalformedResponse as exc:
                keys = sorted(response.body) if isinstance(response.body, Mapping) else []
                raise OutcomeUnknown(
                    f"PLACE combo: unreadable acknowledgement ({exc}); keys {keys}") from None
        if response.error_code in PLACE_REFUSAL_CODES:
            raise Refused(response.error_code or "", response.message)
        raise OutcomeUnknown(
            f"PLACE combo: HTTP {response.status} {response.error_code or ''}".strip())

    def can_place(self, count: int) -> bool:
        return self._budget.can_take(Route.PLACE.path, count)

    def cancel(self, client_order_id: str) -> None:
        self._require_gate()
        response = self._send(Route.CANCEL, {"client_order_id": client_order_id})
        if response.ok:
            return
        if self._order_not_present(response):
            raise OrderNotFound(client_order_id)
        # No cancel refusal code is proven yet: anything else is resolved by Detail.
        raise OutcomeUnknown(f"CANCEL: HTTP {response.status} {response.error_code or ''}".strip())

    def replace_stop_price(self, client_order_id: str, quantity: Decimal,
                           stop_price: Decimal) -> ReplaceAck:
        self._require_gate()
        body = build_replace_stop_body(client_order_id, quantity, stop_price,
                                       self._client_order_id_max_length)
        response = self._send(Route.REPLACE, {"modify_orders": [body]})
        if response.ok:
            try:
                return read_replace_ack(response.body)
            except MalformedResponse as exc:
                raise OutcomeUnknown(f"REPLACE: unreadable answer ({exc})") from None
        if self._order_not_present(response):
            raise OrderNotFound(client_order_id)
        # No replace refusal code is proven yet (probe F5): Detail decides what happened.
        raise OutcomeUnknown(
            f"REPLACE: HTTP {response.status} {response.error_code or ''}".strip())


# --------------------------------------------------------------------------------------------
# Market data over HTTP (tickr.data.base.MarketData). Shapes: tests/fixtures/webull/market_data.
# --------------------------------------------------------------------------------------------

MARKET_DATA_PROVIDER = "webull"
BARS_TIMESPAN_M1 = "M1"
DIRECTION_GAINERS = "DESC"
DIRECTION_LOSERS = "ASC"
MILLISECONDS_PER_SECOND = 1000
# Security type of an instrument profile row (probe M1, 2026-11-14): COMMON_STOCK, ETF, WARRANT;
# ADRs are COMMON_STOCK. `category` is US_STOCK for all of them and is not the type.
SECURITY_TYPE_KEY = "sub_category"
# Bar `time` wire format (probe M2): "2026-11-13T20:59:00.000+0000", UTC, the bar's START.
BAR_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
# Bars are requested for the regular session only (the default); any other session is refused.
BAR_SESSION_REGULAR = "RTH"
# Chart M1 bars ask for every session probe M4 proved (PRE 04:00-09:29, RTH, ATH 16:00-19:59 ET;
# no overnight bars), sent comma-joined as the SDK does (`set_trading_sessions`).
BAR_SESSIONS_ALL = ("PRE", BAR_SESSION_REGULAR, "ATH")


def epoch_ms_to_utc(value: Any, where: str) -> datetime:
    """Epoch milliseconds (JSON number or digit string) -> aware UTC datetime."""
    millis = to_decimal(value, where)
    if millis != millis.to_integral_value() or millis <= 0:
        raise MalformedResponse(f"{where}: not a positive whole number of milliseconds")
    return datetime.fromtimestamp(int(millis) / MILLISECONDS_PER_SECOND, tz=UTC)


def _bar_time(value: Any, where: str) -> datetime:
    """Bar `time` -> aware UTC bar start, exactly in the probed format; anything else refused."""
    if not isinstance(value, str):
        raise MalformedResponse(f"{where}: expected a string, got {type(value).__name__}")
    try:
        parsed = datetime.strptime(value.strip(), BAR_TIME_FORMAT)
    except ValueError:
        raise MalformedResponse(f"{where}: unsupported time {value!r}") from None
    if parsed.second or parsed.microsecond:
        raise MalformedResponse(f"{where}: not on a whole minute {value!r}")
    return parsed.astimezone(UTC)


def read_screener(body: Any, kind: ScreenerKind) -> list[ScreenerRow]:
    """Screener body (bare array, recorded) -> rows in provider order, rank from 1."""
    rows: list[ScreenerRow] = []
    for index, item in enumerate(_unwrap_list(body, "screener"), start=1):
        where = f"screener[{index}]"
        rows.append(ScreenerRow(
            kind=kind,
            rank=index,
            instrument_id=str(_require(item, "instrument_id", where)),
            symbol=str(_require(item, "symbol", where)),
            exchange_code=str(_require(item, "exchange_code", where)),
            currency=str(_require(item, "currency", where)),
            security_type=None,  # screener rows carry none; filled from the profile
            fields={str(k): str(v) for k, v in item.items() if v is not None},
        ))
    return rows


def _field_text(value: Any) -> str:
    """A provider value as capture text: booleans in their JSON spelling ("true", not
    Python's "True"), everything else as `str` (strings as sent, numbers as written)."""
    if isinstance(value, bool):
        return json.dumps(value)
    return str(value)


def read_profiles(body: Any) -> dict[str, InstrumentProfile]:
    """Instrument profile rows (`{data: [...]}`, probe M1) -> {instrument_id: InstrumentProfile}.

    Identity is the instrument_id: one symbol may return several rows (OPENW returned a stale
    and a current id), and the caller matches the screener row's id, never the first symbol hit.
    A row without `sub_category` has an unknown type (None)."""
    profiles: dict[str, InstrumentProfile] = {}
    for index, item in enumerate(_unwrap_list(body, "profiles")):
        where = f"profiles[{index}]"
        profile = InstrumentProfile(
            instrument_id=str(_require(item, "instrument_id", where)),
            symbol=str(_require(item, "symbol", where)),
            security_type=(None if _optional(item, SECURITY_TYPE_KEY) is None
                           else str(item[SECURITY_TYPE_KEY])),
            fields={str(k): _field_text(v) for k, v in item.items() if v is not None},
        )
        previous = profiles.get(profile.instrument_id)
        if previous is not None:
            # The same instrument twice must agree on what discovery uses; other fields of a
            # repeat are not compared, and the first row is the one captured.
            if (previous.symbol, previous.security_type) != (profile.symbol,
                                                             profile.security_type):
                raise MalformedResponse(f"{where}: instrument {profile.instrument_id} listed "
                                        "twice with different contents")
            continue
        profiles[profile.instrument_id] = profile
    return profiles


def read_snapshot(body: Any, receipt_time: datetime) -> list[Quote]:
    """Snapshot body (bare array, recorded) -> quotes.

    A row without bid, ask, sizes or `quote_time` (for example a halted symbol) yields no quote;
    the caller then records NO_QUOTE for it. A row that is not an object or has no symbol, or a
    present but malformed value, raises MalformedResponse."""
    quotes: list[Quote] = []
    for index, item in enumerate(_unwrap_list(body, "snapshot")):
        where = f"snapshot[{index}]"
        symbol = str(_require(item, "symbol", where))
        needed = ("bid", "ask", "bid_size", "ask_size", "quote_time")
        absent = [k for k in needed if _optional(item, k) is None]
        if absent:
            log.warning("snapshot %s without %s: no quote", symbol, ",".join(absent))
            continue
        quotes.append(Quote(
            symbol=symbol,
            bid=_decimal_field(item, "bid", where),
            ask=_decimal_field(item, "ask", where),
            bid_size=_decimal_field(item, "bid_size", where),
            ask_size=_decimal_field(item, "ask_size", where),
            provider_time=epoch_ms_to_utc(item["quote_time"], f"{where}.quote_time"),
            receipt_time=receipt_time,
        ))
    return quotes


def read_bars(body: Any) -> list[Bar]:
    """Bars body `{result: [{delay_minutes, instrument_id, symbol, result: [bar...]}]}` -> bars,
    oldest first per symbol (the wire order is newest first, probe M2). Any series with
    delay_minutes != 0 raises DelayedData; a bar outside the regular session is refused. The
    last regular bar (15:59 ET) includes the closing auction volume."""
    if not isinstance(body, Mapping):
        raise MalformedResponse("bars: expected an object with 'result'")
    series = body.get("result")
    if not isinstance(series, list):
        raise MalformedResponse("bars: missing list 'result'")
    bars: list[Bar] = []
    delayed: list[str] = []
    delay = Decimal(0)
    for index, entry in enumerate(series):
        where = f"bars.result[{index}]"
        symbol = str(_require(entry, "symbol", where))
        minutes = to_decimal(_require_present(entry, "delay_minutes", where),
                             f"{where}.delay_minutes")
        if minutes != 0:
            delayed.append(symbol)
            delay = max(delay, minutes)
            continue
        rows = entry.get("result")
        if not isinstance(rows, list):
            raise MalformedResponse(f"{where}: missing list 'result'")
        own = []
        for number, row in enumerate(rows):
            at = f"{where}.result[{number}]"
            session = str(_require(row, "trading_session", at))
            if session != BAR_SESSION_REGULAR:
                raise MalformedResponse(f"{at}: trading_session {session!r} not requested")
            own.append(Bar(
                symbol=symbol,
                start=_bar_time(_require(row, "time", at), f"{at}.time"),
                open=_decimal_field(row, "open", at),
                high=_decimal_field(row, "high", at),
                low=_decimal_field(row, "low", at),
                close=_decimal_field(row, "close", at),
                volume=_decimal_field(row, "volume", at),
            ))
        bars.extend(sorted(own, key=lambda b: b.start))
    if delayed:
        raise DelayedData(delayed, delay)
    return bars


def read_history_bars(body: Any, span: BarSpan) -> list[SessionBar]:
    """Chart bars body (same envelope as M1 bars) -> labelled bars, oldest first per symbol.
    An M1 bar must carry a session of BAR_SESSIONS_ALL; a D bar's `time` is the provider's label
    (never interpreted here) and its session, if any, is not read (probe C3)."""
    out: list[SessionBar] = []
    for symbol, rows in _bar_series(body):
        own = []
        for number, row in enumerate(rows):
            at = f"bars[{symbol}][{number}]"
            session = None
            if span is BarSpan.M1:
                session = str(_require(row, "trading_session", at))
                if session not in BAR_SESSIONS_ALL:
                    raise MalformedResponse(f"{at}: trading_session {session!r} not requested")
            own.append(SessionBar(Bar(
                symbol=symbol,
                start=_bar_time(_require(row, "time", at), f"{at}.time"),
                open=_decimal_field(row, "open", at),
                high=_decimal_field(row, "high", at),
                low=_decimal_field(row, "low", at),
                close=_decimal_field(row, "close", at),
                volume=_decimal_field(row, "volume", at),
            ), session))
        out.extend(sorted(own, key=lambda b: b.bar.start))
    return out


def _require_present(obj: Any, key: str, where: str) -> Any:
    """Like _require, but 0 is a value (delay_minutes = 0 means real time)."""
    if not isinstance(obj, Mapping) or key not in obj or obj[key] is None or obj[key] == "":
        raise MalformedResponse(f"{where}: missing field {key!r}")
    return obj[key]


# Probe Q1 (`tickr probe-quote-sources`): names of the reads and their fixed parameters.
SOURCE_SNAPSHOT = "snapshot"
SOURCE_SNAPSHOT_EXTENDED = "snapshot-extended"
SOURCE_DEPTH = "depth"
SOURCE_TICKS = "ticks"
SNAPSHOT_EXTENDED_PARAM = "extend_hour_required"  # SDK get_snapshot: include PRE and ATH
SNAPSHOT_EXTENDED_VALUE = "true"
DEPTH_TOP_LEVEL = "1"  # SDK get_quotes: level 1 = the best bid and ask only
TICKS_COUNT = "5"  # the newest trades
TICKS_SESSIONS = "PRE,RTH,ATH"  # the bars' trading_session labels (probe M4)


class WebullMarketData:
    """`MarketData` over the HTTP routes. Takes request budget per route like the broker.

    Screener rows get their security type from the instrument profile (one lookup per new
    symbol per New York day). L1 quotes come from `stream` (a `QuoteStream`); without one,
    subscribe and unsubscribe raise MarketDataError.
    """

    def __init__(
        self,
        transport: Transport,
        budget: RequestBudget,
        category: str,
        gainers_losers_rank_type: str,
        top_actives_rank_type: str,
        profile_symbols_per_request: int,
        utc_now: Callable[[], datetime],
        clock: Callable[[], float] = time.monotonic,
        stream: QuoteStream | None = None,
        extended_rank_types: Mapping[ScreenerSession, str] | None = None,
    ) -> None:
        self._stream = stream
        # Gainers/losers rank_type per session; REGULAR is gainers_losers_rank_type.
        self._rank_types = {**(extended_rank_types or {}),
                            ScreenerSession.REGULAR: gainers_losers_rank_type}
        if profile_symbols_per_request < 1:
            raise AdapterConfigError("profile_symbols_per_request must be at least 1")
        self._profile_chunk = profile_symbols_per_request
        self._types: dict[str, tuple[str, str]] = {}  # instrument_id -> (symbol, type)
        self._types_day: date | None = None
        self._transport = transport
        self._budget = budget
        self._category = category
        self._top_actives_rank_type = top_actives_rank_type
        self._utc_now = utc_now
        self._clock = clock

    @property
    def provider(self) -> str:
        return MARKET_DATA_PROVIDER

    def _read(self, route: Route, params: Mapping[str, Any], reserve: int = 0) -> Any:
        response = send_once(self._transport, self._budget, route, params, self._clock, reserve)
        if not response.ok:
            if response.error_code == CODE_INVALID_SYMBOL:
                raise SymbolsRefused(route.name, response.status, response.error_code)
            raise BrokerReadFailed(route.name, response.status, response.error_code)
        return response.body

    def screener(self, kind: ScreenerKind,
                 session: ScreenerSession = ScreenerSession.REGULAR) -> list[ScreenerRow]:
        """One list. The gainers and losers rank by `session`'s change (its configured
        rank_type; a session without one is refused); the top actives have one ranking."""
        if kind is ScreenerKind.TOP_ACTIVES:
            body = self._read(Route.TOP_ACTIVES, {
                "category": self._category, "rank_type": self._top_actives_rank_type})
        else:
            direction = DIRECTION_GAINERS if kind is ScreenerKind.GAINERS else DIRECTION_LOSERS
            body = self._read(Route.GAINERS_LOSERS, {
                "category": self._category,
                "rank_type": self._rank_type(session),
                "direction": direction,
            })
        return self._with_types(read_screener(body, kind))

    def _rank_type(self, session: ScreenerSession) -> str:
        rank_type = self._rank_types.get(session)
        if rank_type is None:
            raise AdapterConfigError(f"no gainers/losers rank_type for the {session} session")
        return rank_type

    def _with_types(self, rows: list[ScreenerRow]) -> list[ScreenerRow]:
        """Fill each row's security type from the instrument profile with the SAME instrument_id,
        cached per New York day by instrument_id.

        A budget refusal (RateLimited) propagates so the caller shows it. A row whose id the
        profile did not return, whose profile names another symbol, or that states no type keeps
        None (refused by discovery) and is asked again on the next pass."""
        today = ny_date_of(self._utc_now())
        if self._types_day != today:
            self._types, self._types_day = {}, today
        missing = list(dict.fromkeys(r.symbol for r in rows if r.instrument_id not in self._types))
        for start in range(0, len(missing), self._profile_chunk):
            chunk = missing[start:start + self._profile_chunk]
            found = read_profiles(self._read(Route.PROFILES, {
                "symbols": ",".join(chunk), "category": self._category}))
            for profile in found.values():
                if profile.security_type is not None and profile.symbol in chunk:
                    self._types[profile.instrument_id] = (profile.symbol, profile.security_type)

        def typed(row: ScreenerRow) -> ScreenerRow:
            known = self._types.get(row.instrument_id)
            same = known is not None and known[0] == row.symbol
            return replace(row, security_type=known[1] if same else None)

        return [typed(r) for r in rows]

    def profiles(self, symbols: Sequence[str], *,
                 budget_reserve: int = 0) -> list[InstrumentProfile]:
        """Every profile row of `symbols`, `profile_symbols_per_request` per request. Rows of
        symbols not asked are dropped."""
        found: list[InstrumentProfile] = []
        for start in range(0, len(symbols), self._profile_chunk):
            chunk = list(symbols[start:start + self._profile_chunk])
            rows = read_profiles(self._read(Route.PROFILES, {
                "symbols": ",".join(chunk), "category": self._category}, budget_reserve))
            found.extend(p for p in rows.values() if p.symbol in chunk)
        return found

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]:
        body = self._read(Route.SNAPSHOT, {
            "symbols": ",".join(symbols), "category": self._category})
        quotes = read_snapshot(body, self._utc_now())
        stray = sorted({q.symbol for q in quotes} - set(symbols))
        if stray:
            raise MalformedResponse(f"snapshot: symbols not requested: {', '.join(stray)}")
        return quotes

    def quote_sources(self, symbol: str) -> list[SourceRead]:
        """Probe Q1, read only: the four reads that could price an order outside the regular
        session, each sent once and kept as answered (a refusal is evidence too): the snapshot
        as the bot reads it, the snapshot with `extend_hour_required`, the top of the book
        (depths, depth 1) and the last trades of every session (ticks). Never used to trade."""
        reads = (
            (SOURCE_SNAPSHOT, Route.SNAPSHOT, {"symbols": symbol, "category": self._category}),
            (SOURCE_SNAPSHOT_EXTENDED, Route.SNAPSHOT,
             {"symbols": symbol, "category": self._category,
              SNAPSHOT_EXTENDED_PARAM: SNAPSHOT_EXTENDED_VALUE}),
            (SOURCE_DEPTH, Route.DEPTHS,
             {"symbol": symbol, "category": self._category, "depth": DEPTH_TOP_LEVEL}),
            (SOURCE_TICKS, Route.TICKS,
             {"symbol": symbol, "category": self._category, "count": TICKS_COUNT,
              "trading_sessions": TICKS_SESSIONS}),
        )
        found: list[SourceRead] = []
        for name, route, params in reads:
            try:
                response = send_once(self._transport, self._budget, route, params, self._clock)
            except (RateLimited, BrokerReadFailed) as exc:
                found.append(SourceRead(name, None, type(exc).__name__, None, self._utc_now()))
                continue
            found.append(SourceRead(name, response.status, response.error_code, response.body,
                                    self._utc_now()))
        return found

    def bars(self, symbols: Sequence[str], count: int, *, budget_reserve: int = 0) -> list[Bar]:
        body = self._read(Route.BARS, {
            "symbols": list(symbols),
            "category": self._category,
            "timespan": BARS_TIMESPAN_M1,
            "count": str(count),
        }, budget_reserve)
        return read_bars(body)

    def bars_before(self, symbols: Sequence[str], end: datetime, count: int, *,
                    budget_reserve: int = 0) -> list[Bar]:
        """One history page for up to 20 symbols: `end_time` (epoch ms) with `count` per symbol
        (probe M4: end_time is exclusive, the newest bar starts at end - 1 min; order newest
        first; probe 2026-11-16: count is per symbol, 20 symbols at most). `start_time` is never
        sent: it returns an extra bar at start - 1 min. The reply is still checked: a symbol not
        asked is refused and a bar starting at or after `end` is dropped."""
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError(f"naive end time {end!r}")
        body = self._read(Route.BARS, {
            "symbols": list(symbols),
            "category": self._category,
            "timespan": BARS_TIMESPAN_M1,
            "count": str(count),
            "end_time": int(end.timestamp() * MILLISECONDS_PER_SECOND),
        }, budget_reserve)
        bars = read_bars(body)
        stray = sorted({b.symbol for b in bars} - set(symbols))
        if stray:
            raise MalformedResponse(f"bars: symbols not requested: {', '.join(stray)}")
        return [b for b in bars if b.start < end]

    def history_bars(self, symbol: str, span: BarSpan, end: datetime,
                     count: int) -> list[SessionBar]:
        """One chart page: `end_time` (exclusive, probe M4) and `count`, never `start_time`. M1
        asks for PRE, RTH and ATH. Built only by `chart_market_data_from_config` (the API's own
        transport and budget); the bot never calls it."""
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError(f"naive end time {end!r}")
        params: dict[str, Any] = {
            "symbols": [symbol],
            "category": self._category,
            "timespan": span.value,
            "count": str(count),
            "end_time": int(end.timestamp() * MILLISECONDS_PER_SECOND),
        }
        if span is BarSpan.M1:
            params["trading_sessions"] = ",".join(BAR_SESSIONS_ALL)
        bars = read_history_bars(self._read(Route.BARS, params), span)
        stray = sorted({b.bar.symbol for b in bars} - {symbol})
        if stray:
            raise MalformedResponse(f"bars: symbols not requested: {', '.join(stray)}")
        return [b for b in bars if b.bar.start < end]

    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        """Any failure (budget, HTTP, SDK) is a MarketDataError naming only its type: the
        caller is the trading loop, which must never be ended by a stream problem."""
        if self._stream is None:
            raise MarketDataError("no L1 stream configured for this adapter")
        try:
            self._stream.subscribe(symbols, listener)
        except MarketDataError:
            raise
        except Exception as exc:  # noqa: BLE001 - see docstring
            raise MarketDataError(f"subscribe failed ({type(exc).__name__})") from None

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        if self._stream is None:
            raise MarketDataError("no L1 stream configured for this adapter")
        try:
            self._stream.unsubscribe(symbols)
        except MarketDataError:
            raise
        except Exception as exc:  # noqa: BLE001 - see subscribe
            raise MarketDataError(f"unsubscribe failed ({type(exc).__name__})") from None

    def subscribe_optional(self, symbols: Iterable[str], listener: QuoteListener,
                           budget_reserve: int) -> None:
        if self._stream is None:
            raise MarketDataError("no L1 stream configured for this adapter")
        self._stream.subscribe_optional(symbols, listener, budget_reserve)

    def unsubscribe_optional(self, symbols: Iterable[str], budget_reserve: int) -> None:
        if self._stream is None:
            raise MarketDataError("no L1 stream configured for this adapter")
        self._stream.unsubscribe_optional(symbols, budget_reserve)


# --------------------------------------------------------------------------------------------
# L1 quote stream (MQTT). Session logic is SDK-free and tested; SdkQuoteStream binds it to the SDK.
# --------------------------------------------------------------------------------------------

STREAM_TOPIC_QUOTE = "quote"
STREAM_TOPIC_NOTICE = "notice"
NOTICE_STATUS = "1001"
NOTICE_ENTITLEMENT_PREEMPTED = "1002"
STREAM_SUB_TYPE_QUOTE = "QUOTE"
STREAM_SUBSCRIBE_PATH = "/market-data/streaming/subscribe"
STREAM_UNSUBSCRIBE_PATH = "/market-data/streaming/unsubscribe"
# Subscribe refusal past the per-connection symbol limit (probe M3: HTTP 417, REST ServerException,
# no stream notice; the symbols already subscribed stay).
STREAM_TOO_MANY_SYMBOLS = "TOO_MANY_SYMBOLS_SUBSCRIPTION"


# --------------------------------------------------------------------------------------------
# Offline history for the backtest (python -m tickr.backtest). Never used by the bot.
# --------------------------------------------------------------------------------------------

BARS_TIMESPAN_DAY = "D"
# Top-level keys of an instrument-profile page: rows, and the key of the next page (SDK
# list_instruments docstring: "not returned on the last page"). Anything else is refused.
INSTRUMENT_PAGE_KEYS = frozenset({"data", "pagination_key"})


@dataclass(frozen=True)
class InstrumentRow:
    instrument_id: str
    symbol: str
    exchange_code: str
    currency: str
    security_type: str | None
    status: str | None


@dataclass(frozen=True)
class InstrumentPage:
    rows: tuple[InstrumentRow, ...]
    next_key: str | None


def read_instrument_page(body: Any) -> InstrumentPage:
    """One page of `/trading/instruments/stocks/profiles/list` without symbols (the full list).

    The page shape beyond `data` is unproven: an unknown top-level key is refused so that a
    differently named pagination key can never end the listing early unnoticed."""
    if not isinstance(body, Mapping):
        raise MalformedResponse("instruments: expected an object")
    unknown = sorted(set(body) - INSTRUMENT_PAGE_KEYS)
    if unknown:
        raise MalformedResponse(f"instruments: unknown top-level keys {unknown}")
    rows: list[InstrumentRow] = []
    for index, item in enumerate(_unwrap_list(body, "instruments")):
        where = f"instruments[{index}]"
        rows.append(InstrumentRow(
            instrument_id=str(_require(item, "instrument_id", where)),
            symbol=str(_require(item, "symbol", where)),
            exchange_code=str(_require(item, "exchange_code", where)),
            currency=str(_require(item, "currency", where)),
            security_type=(None if _optional(item, SECURITY_TYPE_KEY) is None
                           else str(item[SECURITY_TYPE_KEY])),
            status=None if _optional(item, "status") is None else str(item["status"]),
        ))
    key = _optional(body, "pagination_key")
    return InstrumentPage(tuple(rows), None if key is None else str(key))


@dataclass(frozen=True)
class DailyBar:
    """One daily bar as Webull labels it. `time` is the provider's label, never interpreted
    here: the backtest maps it to a New York session date and refuses one that is not a session.
    Daily bars are forward-adjusted (SDK docstring); minute bars are not."""

    symbol: str
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _bar_series(body: Any) -> list[tuple[str, list[Any]]]:
    """(symbol, rows) per series of a bars body; DelayedData if any series is delayed."""
    if not isinstance(body, Mapping):
        raise MalformedResponse("bars: expected an object with 'result'")
    series = body.get("result")
    if not isinstance(series, list):
        raise MalformedResponse("bars: missing list 'result'")
    out: list[tuple[str, list[Any]]] = []
    delayed: list[str] = []
    delay = Decimal(0)
    for index, entry in enumerate(series):
        where = f"bars.result[{index}]"
        symbol = str(_require(entry, "symbol", where))
        minutes = to_decimal(_require_present(entry, "delay_minutes", where),
                             f"{where}.delay_minutes")
        if minutes != 0:
            delayed.append(symbol)
            delay = max(delay, minutes)
            continue
        rows = entry.get("result")
        if not isinstance(rows, list):
            raise MalformedResponse(f"{where}: missing list 'result'")
        out.append((symbol, rows))
    if delayed:
        raise DelayedData(delayed, delay)
    return out


def read_daily_bars(body: Any) -> list[DailyBar]:
    """Daily bars body (same envelope as M1 bars) -> bars, oldest first per symbol."""
    bars: list[DailyBar] = []
    for symbol, rows in _bar_series(body):
        own = []
        for number, row in enumerate(rows):
            at = f"daily[{symbol}][{number}]"
            own.append(DailyBar(
                symbol=symbol,
                time=_bar_time(_require(row, "time", at), f"{at}.time"),
                open=_decimal_field(row, "open", at),
                high=_decimal_field(row, "high", at),
                low=_decimal_field(row, "low", at),
                close=_decimal_field(row, "close", at),
                volume=_decimal_field(row, "volume", at),
            ))
        bars.extend(sorted(own, key=lambda b: b.time))
    return bars


class WebullHistory:
    """Read-only history reads for the offline backtest: the full common-stock list, daily bars
    and regular-session M1 bars of several symbols before an end time. Takes request budget per
    route like the bot; `BudgetExhausted`/`RateLimited` propagate (the caller waits).

    With `raw_dir`, the first response of each kind and every failed one are written there as
    JSON before parsing (market data only: these routes carry no account id), so the real shape
    can be inspected and recorded as a fixture."""

    def __init__(self, transport: Transport, budget: RequestBudget, category: str,
                 clock: Callable[[], float] = time.monotonic,
                 raw_dir: Path | None = None) -> None:
        self._transport = transport
        self._budget = budget
        self._category = category
        self._clock = clock
        self._raw_dir = raw_dir
        self._requests = 0
        self._saved: set[str] = set()
        # Raw file names carry the start of this process, so a rerun never overwrites them.
        self._started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    @property
    def requests(self) -> int:
        """Requests sent so far."""
        return self._requests

    def _read(self, route: Route, params: Mapping[str, Any], tag: str) -> Any:
        response = send_once(self._transport, self._budget, route, params, self._clock)
        self._requests += 1
        if self._raw_dir is not None and (tag not in self._saved or not response.ok):
            self._saved.add(tag)
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            name = f"{self._started}-{self._requests:06d}-{tag}.json"
            (self._raw_dir / name).write_text(
                json.dumps({"status": response.status, "body": response.body}), "utf-8")
        if not response.ok:
            if response.error_code == CODE_INVALID_SYMBOL:
                raise SymbolsRefused(route.name, response.status, response.error_code)
            raise BrokerReadFailed(route.name, response.status, response.error_code)
        return response.body

    def instruments(self, sub_category: str, pagination_key: str | None) -> InstrumentPage:
        params: dict[str, Any] = {"category": self._category, "sub_category": sub_category}
        if pagination_key is not None:
            params["pagination_key"] = pagination_key
        return read_instrument_page(self._read(Route.PROFILES, params, "instruments"))

    def daily_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[DailyBar]:
        bars = read_daily_bars(self._read(Route.BARS, self._bars_params(
            symbols, BARS_TIMESPAN_DAY, end, count), "daily"))
        self._only(symbols, {b.symbol for b in bars})
        return [b for b in bars if b.time < end]

    def minute_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[Bar]:
        """Up to `count` regular-session M1 bars per symbol starting before `end` (probe M4:
        end_time is exclusive; probe 2026-11-16: `count` applies per symbol). The caller pages
        until each symbol reaches its window."""
        bars = read_bars(self._read(Route.BARS, self._bars_params(
            symbols, BARS_TIMESPAN_M1, end, count), "minutes"))
        self._only(symbols, {b.symbol for b in bars})
        return [b for b in bars if b.start < end]

    def _bars_params(self, symbols: Sequence[str], timespan: str, end: datetime,
                     count: int) -> dict[str, Any]:
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError(f"naive end time {end!r}")
        return {
            "symbols": list(symbols),
            "category": self._category,
            "timespan": timespan,
            "count": str(count),
            "end_time": int(end.timestamp() * MILLISECONDS_PER_SECOND),
        }

    @staticmethod
    def _only(asked: Sequence[str], got: set[str]) -> None:
        stray = sorted(got - set(asked))
        if stray:
            raise MalformedResponse(f"bars: symbols not requested: {', '.join(stray)}")


def subscribe_error(exc: Exception, symbols: list[str], limit: int) -> Exception:
    """The SDK's subscribe failure -> SubscriptionLimit for the proven limit code, else as is."""
    if getattr(exc, "error_code", None) == STREAM_TOO_MANY_SYMBOLS:
        return SubscriptionLimit(symbols, limit, by_provider=True)
    return exc


class QuoteStream(Protocol):
    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None: ...

    def unsubscribe(self, symbols: Iterable[str]) -> None: ...

    def subscribe_optional(self, symbols: Iterable[str], listener: QuoteListener,
                           budget_reserve: int) -> None: ...

    def unsubscribe_optional(self, symbols: Iterable[str], budget_reserve: int) -> None: ...


def quote_from_stream(result: Any, receipt_time: datetime) -> Quote | None:
    """SDK `QuoteResult` (basic{symbol, timestamp ms}, bids[], asks[] of {price, size}) -> Quote.

    Field names from the SDK 3.0.3 decoder; no frame has been recorded yet. A frame without a
    best bid or ask (empty side) yields None: no quote, never a guessed price."""
    basic = getattr(result, "basic", None)
    symbol = getattr(basic, "symbol", "")
    if not symbol:
        raise MalformedResponse("stream quote: missing symbol")
    bids = getattr(result, "bids", None) or []
    asks = getattr(result, "asks", None) or []
    if not bids or not asks:
        return None
    where = f"stream quote {symbol}"
    best_bid, best_ask = bids[0], asks[0]
    if getattr(best_bid, "price", None) is None or getattr(best_ask, "price", None) is None:
        return None
    return Quote(
        symbol=symbol,
        bid=to_decimal(best_bid.price, f"{where}.bid"),
        ask=to_decimal(best_ask.price, f"{where}.ask"),
        bid_size=to_decimal(getattr(best_bid, "size", None), f"{where}.bid_size"),
        ask_size=to_decimal(getattr(best_ask, "size", None), f"{where}.ask_size"),
        provider_time=epoch_ms_to_utc(getattr(basic, "timestamp", None), f"{where}.timestamp"),
        receipt_time=receipt_time,
    )


def notice_from_stream(payload: Mapping[str, Any], receipt_time: datetime) -> StreamNotice:
    """Notice 1001 -> StreamNotice. Each counter (`drop`, `rtt`, `sent`; probe M3 saw JSON
    integers) must be a whole number >= 0; a missing or other value is MalformedResponse."""
    counters: dict[str, int] = {}
    for name in ("drop", "rtt", "sent"):
        where = f"stream notice {NOTICE_STATUS}.{name}"
        value = to_decimal(payload.get(name), where)
        if value != value.to_integral_value() or value < 0:
            raise MalformedResponse(f"{where}: not a whole number >= 0")
        counters[name] = int(value)
    return StreamNotice(receipt_time, counters["drop"], counters["rtt"], counters["sent"])


class StreamSession:
    """Desired subscriptions and event translation for one MQTT connection, without the SDK.

    - Every (re)connect resubscribes the FULL desired set (the SDK reconnects but never
      resubscribes, docs/webull-facts.md). The transport is reported "up" only when what the
      broker has been sent equals the desired set, decided under the same lock, so a
      subscribe() racing with a connect is never lost.
    - Every subscribe/unsubscribe POST takes request budget first. A failure while connecting
      (budget, HTTP) reports the transport down with its cause, goes to `on_error`, and asks
      for a deliberate reconnect; nothing is retried here and nothing raises into the
      provider thread.
    - Disconnect, entitlement pre-emption (notice 1002) and a stopped loop report "down".
    - Notice 1001 counters go to the listener's `on_notice`; a malformed one goes to
      `on_error` (journaled as a stream error) and the stream carries on.
    - At most `max_symbols` symbols per connection (probe M3: 100, cumulative). A subscribe()
      that would pass it raises SubscriptionLimit before anything is sent, without reconnecting.
      Slots are counted on the current set, as Webull does (probe M3b: an unsubscribe frees its
      slots), so candidates rotate on one connection. A provider refusal of the same kind (HTTP
      417) while under our cap is therefore unexpected and only a fallback is kept for it:
      its symbols are taken back out and it is handled like any failed subscribe (transport
      down, reconnect, so a fresh connection resets the count), then SubscriptionLimit raises.
    """

    def __init__(self, budget: RequestBudget, utc_now: Callable[[], datetime],
                 on_error: Callable[[str, Exception], None], *, max_symbols: int) -> None:
        self._max_symbols = max_symbols
        self._budget = budget
        self._utc_now = utc_now
        self._on_error = on_error
        self._lock = threading.Lock()
        self._sync_lock = threading.RLock()  # one _sync at a time: no duplicate subscribe POSTs
        self._symbols: set[str] = set()
        self._sent: set[str] = set()  # subscribed on the current connection
        self._maybe_held: set[str] = set()  # see subscribe_optional; this connection only
        self._listener: QuoteListener | None = None
        self._send_subscribe: Callable[[list[str]], None] | None = None
        self._send_unsubscribe: Callable[[list[str]], None] | None = None
        self._reconnect: Callable[[], None] | None = None
        self._generation = 0  # counts connects and disconnects: an optional send checks it
        self._connected = False
        self._up = False

    @property
    def symbols(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._symbols)

    @property
    def up(self) -> bool:
        with self._lock:
            return self._up

    def bind(self, send_subscribe: Callable[[list[str]], None],
             send_unsubscribe: Callable[[list[str]], None],
             reconnect: Callable[[], None]) -> None:
        self._send_subscribe = send_subscribe
        self._send_unsubscribe = send_unsubscribe
        self._reconnect = reconnect

    def _notify(self, up: bool, detail: str) -> None:
        with self._lock:
            listener = self._listener
        if listener is not None:
            try:
                listener.on_transport(up, self._utc_now(), detail)
            except Exception as exc:  # noqa: BLE001 - never into the provider thread
                self._on_error("transport", exc)

    def _send(self, subscribe: bool, symbols: list[str]) -> None:
        send = self._send_subscribe if subscribe else self._send_unsubscribe
        if send is None:
            raise MarketDataError("stream session is not bound to a client")
        self._budget.take(STREAM_SUBSCRIBE_PATH if subscribe else STREAM_UNSUBSCRIBE_PATH)
        send(symbols)

    def _sync(self) -> bool:
        """Send until the broker set equals the desired set. True once in sync (then up)."""
        with self._sync_lock:
            return self._sync_locked()

    def _sync_locked(self) -> bool:
        while True:
            with self._lock:
                if not self._connected:
                    return False
                pending = sorted(self._symbols - self._sent)
                stale = sorted(self._sent - self._symbols)
                if not pending and not stale:
                    self._up = True
                    return True
            # Unsubscribe first: the provider cap is on the current set (probe M3b), so freed
            # slots must be free before the new symbols arrive.
            if stale:
                self._send(False, stale)
                with self._lock:
                    self._sent.difference_update(stale)
            if pending:
                self._send(True, pending)
                with self._lock:
                    self._sent.update(pending)
                    self._maybe_held.difference_update(pending)  # held for sure now

    # -- calls from the bot
    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        with self._lock:
            if self._listener is not None and listener is not self._listener:
                raise MarketDataError("one stream listener per session; use FanOut")
            added = set(symbols) - self._symbols
            if len(self._symbols) + len(added) > self._max_symbols:
                raise SubscriptionLimit(added, self._max_symbols, by_provider=False)
            if len(self._symbols | added | self._maybe_held) > self._max_symbols:
                # Maybe-held leftovers would push the provider past its cap (a 417 and a
                # reconnect): this sync unsubscribes them first, as stale.
                self._sent.update(self._maybe_held - self._symbols - added)
                self._maybe_held.clear()
            first = self._listener is None
            self._listener = listener
            self._symbols.update(added)
            up = self._up
        if up:
            # A refused subscribe takes its own symbols back out before the reconnect, so the
            # resubscribe of the full set cannot fail on them again (and again).
            self._sync_or_reconnect(raise_error=True, rollback=added)
        if first:
            # A connect before any listener was bound was reported to nobody: tell the new
            # listener now, so its health never stays "never connected" on a live stream.
            with self._lock:
                up = self._up
            if up:
                self._notify(True, "connected (listener bound after connect)")

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        with self._lock:
            self._symbols.difference_update(symbols)
            up = self._up
        if up:
            self._sync_or_reconnect(raise_error=True)

    def subscribe_optional(self, symbols: Iterable[str], listener: QuoteListener,
                           budget_reserve: int) -> None:
        """Subscribe symbols no position or entry depends on (discovery candidates, observer
        symbols) without ever disturbing the connection: only while up, only within
        `max_symbols` counting every symbol the provider may hold, and only while at least
        `budget_reserve` subscribe requests stay free for the required symbols. A failed send
        is reported to `on_error` and raised as MarketDataError, without a reconnect.

        Symbols the provider may still hold although we no longer want them (the answer to a
        failed optional subscribe was lost, or an optional unsubscribe failed or was refused)
        are kept apart as maybe-held: counted against `max_symbols`, never sent by the
        required sync (whose failure reconnects), and unsubscribed, optionally, before the
        next optional subscribe goes out; if that cleanup fails, the subscribe is refused.
        A new connection forgets them (the provider's count starts again)."""
        with self._sync_lock:  # no _sync runs between the checks and the sends
            with self._lock:
                if self._listener is not listener:
                    raise MarketDataError("optional subscribe needs the bound stream listener")
                if not self._up:
                    raise MarketDataError("optional subscribe needs a live stream in sync")
                generation = self._generation
                leftover = sorted(self._maybe_held - self._symbols)
            if leftover:
                self._send_optional(False, leftover, budget_reserve, generation)
                with self._lock:
                    if self._generation != generation:
                        raise MarketDataError("optional cleanup raced a reconnect")
                    self._maybe_held.difference_update(leftover)
            with self._lock:
                added = set(symbols) - self._symbols
                if not added:
                    return
                held = self._symbols | self._sent | self._maybe_held
                if len(held | added) > self._max_symbols:
                    raise SubscriptionLimit(added, self._max_symbols, by_provider=False)
            self._send_optional(True, sorted(added), budget_reserve, generation)
            with self._lock:
                if self._generation != generation:  # reconnected meanwhile: not on this one
                    raise MarketDataError("optional subscribe raced a reconnect: dropped")
                self._symbols.update(added)
                self._sent.update(added)
                self._maybe_held.difference_update(added)

    def unsubscribe_optional(self, symbols: Iterable[str], budget_reserve: int) -> None:
        """Drop symbols added by `subscribe_optional`, with the same guarantees. They leave
        the desired set and the sent set whatever happens; when the request is refused or
        fails they become maybe-held (see `subscribe_optional`)."""
        with self._sync_lock:
            with self._lock:
                gone = set(symbols) & self._symbols
                self._symbols.difference_update(gone)
                sent = sorted(gone & self._sent)
                self._sent.difference_update(sent)
                up, generation = self._up, self._generation
                if not up:  # no live connection to send on; the next one starts clean
                    return
                self._maybe_held.update(sent)
            if sent:
                self._send_optional(False, sent, budget_reserve, generation)
                with self._lock:
                    if self._generation == generation:
                        self._maybe_held.difference_update(sent)

    def _send_optional(self, subscribe: bool, symbols: list[str], budget_reserve: int,
                       generation: int) -> None:
        route = STREAM_SUBSCRIBE_PATH if subscribe else STREAM_UNSUBSCRIBE_PATH
        if not self._budget.can_take(route, budget_reserve + 1):
            raise MarketDataError(f"optional {route}: request budget kept for required symbols")
        try:
            self._send(subscribe, symbols)
        except Exception as exc:  # noqa: BLE001 - budget, HTTP or SDK: never a reconnect here
            with self._lock:
                if self._generation == generation and subscribe:
                    self._maybe_held.update(symbols)  # the answer may be lost: count them
            self._on_error("subscribe-optional" if subscribe else "unsubscribe-optional", exc)
            raise MarketDataError(f"optional {route} failed ({type(exc).__name__})") from None

    def _sync_or_reconnect(self, raise_error: bool,
                           rollback: Iterable[str] = ()) -> bool:
        """Sync; on failure drop `rollback` from the desired set, mark the transport down
        (cause reported), call on_error and ask for a reconnect, whose resubscribe sends the
        full set again. True when in sync."""
        try:
            return self._sync()
        except Exception as exc:  # noqa: BLE001 - budget, HTTP or SDK failure while (un)subscribing
            self._down_and_reconnect(exc, rollback)
            if raise_error:
                raise
            return False

    def _down_and_reconnect(self, exc: Exception, rollback: Iterable[str]) -> bool:
        """Mark the transport down (cause reported), call on_error, ask for a reconnect whose
        resubscribe sends the full set again. Always False (not in sync)."""
        with self._lock:
            self._symbols.difference_update(rollback)
            self._connected = self._up = False
        self._notify(False, f"resubscribe failed ({type(exc).__name__}); reconnecting")
        self._on_error("subscribe", exc)
        if self._reconnect is not None:
            self._reconnect()
        return False

    # -- calls from the provider thread
    def on_connected(self) -> None:
        with self._lock:
            self._connected, self._up, self._sent = True, False, set()
            self._maybe_held = set()
            self._generation += 1
        if not self._sync_or_reconnect(raise_error=False):
            return
        with self._lock:
            count = len(self._sent)
        self._notify(True, f"connected, {count} symbol(s) subscribed")

    def on_disconnected(self, detail: str) -> None:
        with self._lock:
            was = self._connected or self._up
            self._connected = self._up = False
            self._sent = set()
            self._maybe_held = set()
            self._generation += 1
        if was or detail:
            self._notify(False, detail)

    def on_message(self, topic: str, payload: Any) -> None:
        try:
            if topic == STREAM_TOPIC_QUOTE:
                quote = quote_from_stream(payload, self._utc_now())
                with self._lock:
                    listener = self._listener
                    wanted = quote is not None and quote.symbol in self._symbols
                if wanted and listener is not None:
                    listener.on_quote(quote)  # type: ignore[arg-type]
            elif topic == STREAM_TOPIC_NOTICE and isinstance(payload, Mapping):
                kind = str(payload.get("type", ""))
                if kind == NOTICE_ENTITLEMENT_PREEMPTED:
                    with self._lock:
                        self._up = False
                    self._notify(False, "entitlement taken by another device (notice 1002)")
                elif kind == NOTICE_STATUS:
                    notice = notice_from_stream(payload, self._utc_now())
                    with self._lock:
                        listener = self._listener
                    if listener is not None:
                        listener.on_notice(notice)
                else:
                    log.warning("stream notice of unknown type %r", kind)
        except Exception as exc:  # noqa: BLE001 - never into the provider thread
            self._on_error(topic, exc)


@dataclass(frozen=True)
class StreamSettings:
    """What the SDK stream needs; built from the active environment's config."""

    mqtt_host: str
    mqtt_port: int
    http_host: str
    allowed_hosts: frozenset[str]
    region_id: str
    token_dir: str
    app_key_env: str
    app_secret_env: str
    category: str
    reconnect_delay_seconds: float
    max_symbols: int


class SdkQuoteStream:
    """`QuoteStream` over the SDK's DataStreamingClient (MQTT + subscribe POST), own thread.

    Hosts are pinned and checked against the allowlist; the SDK's own logger setup is skipped
    (`logger_enable=False`, KD-4); the SDK reconnects every 10 s on transport errors and this
    class resubscribes on each connect through `StreamSession`. If the SDK loop ends for good,
    the session reports the transport down. Snapshot/tick topics are not subscribed (KD-1)."""

    def __init__(self, settings: StreamSettings, session: StreamSession) -> None:
        for host in (settings.mqtt_host, settings.http_host):
            if host not in settings.allowed_hosts:
                raise AdapterConfigError("stream host is not on the host allowlist")
        app_key = _read_credential(settings.app_key_env)
        app_secret = _read_credential(settings.app_secret_env)
        silence_sdk_loggers()
        from webull.data.data_streaming_client import DataStreamingClient

        silence_sdk_loggers()
        client = DataStreamingClient(
            app_key, app_secret, settings.region_id, uuid.uuid4().hex,
            http_host=settings.http_host, mqtt_host=settings.mqtt_host,
            mqtt_port=settings.mqtt_port,
        )
        client.set_token_dir(settings.token_dir)
        category = settings.category
        limit = settings.max_symbols

        def send_subscribe(symbols: list[str]) -> None:
            try:
                client.subscribe(symbols, category, [STREAM_SUB_TYPE_QUOTE])
            except Exception as exc:  # noqa: BLE001 - the SDK raises ServerException; mapped
                mapped = subscribe_error(exc, symbols, limit)
                if mapped is exc:
                    raise
                raise mapped from exc

        session.bind(
            send_subscribe,
            lambda symbols: client.unsubscribe(symbols, category, [STREAM_SUB_TYPE_QUOTE]),
            client.disconnect,  # ends this loop; _run starts a new connection after the delay
        )
        client.on_connect_success = lambda _c, _api, _sid: session.on_connected()
        client.on_quotes_message = lambda _c, topic, payload: session.on_message(topic, payload)
        client.on_disconnect = (
            lambda _c, _u, rc: session.on_disconnected(f"disconnected (rc={rc})"))
        silence_sdk_loggers()
        self._client = client
        self._session = session
        self._delay = settings.reconnect_delay_seconds
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        self._session.subscribe(symbols, listener)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        self._session.unsubscribe(symbols)

    def subscribe_optional(self, symbols: Iterable[str], listener: QuoteListener,
                           budget_reserve: int) -> None:
        self._session.subscribe_optional(symbols, listener, budget_reserve)

    def unsubscribe_optional(self, symbols: Iterable[str], budget_reserve: int) -> None:
        self._session.unsubscribe_optional(symbols, budget_reserve)

    def _run(self) -> None:
        """Connect and loop until stop(). Whenever the SDK loop ends (our deliberate
        disconnect after a failed resubscribe, or the SDK giving up), report the transport down
        and connect again after the configured delay."""
        while not self._stopping.is_set():
            try:
                self._client.connect_and_loop_forever(logger_enable=False)
                detail = "stream loop exited"
            except Exception as exc:  # noqa: BLE001 - the SDK raises bare Exceptions
                detail = f"stream loop stopped ({type(exc).__name__})"
            self._session.on_disconnected(detail)
            # SDK 3.0.3 / paho 1.6.1: a disconnect sets this flag; clear it for the next loop.
            self._client._thread_terminate = False
            self._stopping.wait(self._delay)

    def start(self) -> None:
        if self._thread is not None:
            raise MarketDataError("stream already started")
        self._thread = threading.Thread(target=self._run, name="tickr-l1-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        # SDK 3.0.3 (paho 1.6.1): connect_and_loop_forever checks this private flag between
        # reconnect attempts; there is no public way to end its loop.
        self._client._thread_terminate = True
        self._client.disconnect()


# --------------------------------------------------------------------------------------------
# SDK transport (the only code touching the Webull SDK; imported lazily)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WebullSettings:
    """Everything the SDK transport needs. Built by config from the active environment's
    section of config/bot.toml; no field has a default here."""

    trading_host: str
    http_port: int
    allowed_hosts: frozenset[str]
    region_id: str
    connect_timeout_seconds: int
    read_timeout_seconds: int
    token_dir: str
    token_check_duration_seconds: int
    token_check_interval_seconds: int
    app_key_env: str
    app_secret_env: str
    account_id_env: str


def silence_sdk_loggers() -> None:
    """KD-4: the SDK attaches stdout/file handlers and logs request bodies, tokens and
    signatures. Strip every handler from every `webull*` logger and stop propagation."""
    manager = logging.Logger.manager
    names = [n for n in list(manager.loggerDict) if n.split(".")[0] == SDK_LOGGER_PREFIX]
    for name in [SDK_LOGGER_PREFIX, *names]:
        sdk_logger = logging.getLogger(name)
        for handler in list(sdk_logger.handlers):
            sdk_logger.removeHandler(handler)
        sdk_logger.addHandler(logging.NullHandler())
        sdk_logger.propagate = False
        sdk_logger.setLevel(LOGGING_OFF)


def _read_credential(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AdapterConfigError(f"credential variable {name} is not set")
    return value


# The SDK's token state that waits for the owner in the Webull app (docs/webull-facts.md).
TOKEN_PENDING = "PENDING"
# SDK 3.0.3 error codes of token initialisation and what they mean. The SDK's own message is
# never passed on (it carries a masked token).
_TOKEN_CAUSES = {
    "ERROR_CHECK_TOKEN_ENABLE": "token config read failed",
    "ERROR_CREATE_TOKEN": "token create failed",
    "ERROR_CHECK_TOKEN": "token check failed or token INVALID/EXPIRED",
    "ERROR_INIT_TOKEN": "token not confirmed in the Webull app in time",
    "ERROR_LOAD_TOKEN": "token file unreadable",
    "ERROR_SAVE_TOKEN": "token file not writable",
}


def _init_token(client: Any, on_token_pending: Callable[[], None]) -> None:
    """`ClientInitializer.initializer` of SDK 3.0.3 with one addition: when the token the SDK
    creates is PENDING, `on_token_pending()` runs before the SDK waits (up to
    token_check_duration_seconds) for the owner to confirm it in the Webull app."""
    from webull.core.http.initializer.client_initializer import ClientInitializer
    from webull.core.http.initializer.token.token_manager import TokenManager

    class _TokenManager(TokenManager):  # type: ignore[misc]
        def create_token(self, token_operation: Any, local_token: Any) -> Any:
            created = TokenManager.create_token(token_operation, local_token)
            if created.get("status") == TOKEN_PENDING:
                try:
                    on_token_pending()
                except Exception as exc:  # a failed alert never stops token creation
                    print(f"token-pending alert failed ({type(exc).__name__})", file=sys.stderr)
            return created

    silence_sdk_loggers()
    if ClientInitializer._check_token_enable(client):
        _TokenManager(client.get_token_dir()).init_token(client)


def _no_token_hook() -> None:
    return None


class SdkTransport:
    """`Transport` over webull-openapi-python-sdk 3.0.3. `on_token_pending` runs at most once,
    during construction, when the token waits for the owner's confirmation in the Webull app.

    `market_data_only` (the API's chart reads): the account id is never read and only
    MARKET_DATA_ROUTES may be sent; any other route is refused before anything is sent.

    `send` is called from several threads at once (the trading loop, the discovery thread, the
    account display reads) and takes no lock, so no request ever waits behind another
    thread's. Thread safety read from SDK 3.0.3's source: every send builds its own
    `ApiRequest` (its own header, query and body dicts); after construction
    `ApiClient.get_response` only reads the client (signer and credential fixed, a fresh uuid
    nonce per call, the token set once by the token initialisation here and never refreshed
    later); `webull.core.http.response.Response.get_response_object` opens a new
    `requests.Session` for each request, so no connection pool or cookie jar is shared; the
    SDK's per-request context (`RequestContextHolder`, used only by its own order helpers,
    not here) is thread-local. The request budget has its own lock (broker/budget.py)."""

    def __init__(self, settings: WebullSettings,
                 on_token_pending: Callable[[], None] = _no_token_hook,
                 market_data_only: bool = False) -> None:
        if settings.trading_host not in settings.allowed_hosts:
            raise AdapterConfigError("trading_host is not on the host allowlist")
        self._host = settings.trading_host
        self._allowed = settings.allowed_hosts
        self._routes = MARKET_DATA_ROUTES if market_data_only else frozenset(Route)
        app_key = _read_credential(settings.app_key_env)
        app_secret = _read_credential(settings.app_secret_env)
        self._account_id = None if market_data_only else _read_credential(
            settings.account_id_env)

        silence_sdk_loggers()  # SDK modules attach handlers at import time
        from webull.core.client import ApiClient
        from webull.core.common.api_type import DEFAULT as API_TYPE_HTTP
        from webull.core.exception.exceptions import ClientException

        silence_sdk_loggers()
        client = ApiClient(
            app_key,
            app_secret,
            settings.region_id,
            port=settings.http_port,
            connect_timeout=settings.connect_timeout_seconds,
            timeout=settings.read_timeout_seconds,
            auto_retry=False,  # never retry: one send per decision, and budget accounting
            token_check_duration_seconds=settings.token_check_duration_seconds,
            token_check_interval_seconds=settings.token_check_interval_seconds,
        )
        # Keep TradeClient/DataClient-style initialisers from attaching handlers (KD-4).
        client._stream_logger_set = True
        client._file_logger_set = True
        client.set_logger(logging.getLogger(f"{SDK_LOGGER_PREFIX}.core.client"))
        # Token and config calls made by the SDK itself resolve the endpoint by region.
        client.add_endpoint(settings.region_id, self._host, API_TYPE_HTTP)
        client.set_token_dir(settings.token_dir)
        silence_sdk_loggers()
        self._client = client
        self._check_resolved_host()
        try:
            _init_token(client, on_token_pending)  # may wait for app confirmation
        except ClientException as exc:
            code = str(exc.get_error_code())
            cause = _TOKEN_CAUSES.get(code, "SDK client error")
            raise AdapterConfigError(f"token initialisation failed: {cause} ({code})") \
                from None
        except Exception as exc:  # noqa: BLE001 - anything else: no usable token
            raise AdapterConfigError(f"token initialisation failed ({type(exc).__name__})") \
                from None
        silence_sdk_loggers()

    def _check_resolved_host(self) -> None:
        from webull.core.endpoint.resolver_endpoint_request import ResolveEndpointRequest

        resolved = self._client._endpoint_resolver.resolve(
            ResolveEndpointRequest(self._client.get_region_id())
        )
        if resolved not in self._allowed:
            raise AdapterConfigError("SDK resolves to a host outside the allowlist")

    def is_own_account(self, account_id: str) -> bool:
        return self._account_id is not None and account_id == self._account_id

    def _scrub(self, text: str) -> str:
        if not text or self._account_id is None:
            return text
        return text.replace(self._account_id, "<account>")

    def send(self, route: Route, params: Mapping[str, Any]) -> WireResponse:
        if route not in self._routes:  # checked before anything is built or sent
            raise AdapterConfigError(f"route {route.name} is not allowed on this transport")
        from webull.core.exception.exceptions import ClientException, ServerException
        from webull.core.request import ApiRequest

        query: dict[str, Any] = {}
        body: dict[str, Any] | None = None
        scoped = {} if route in ACCOUNT_FREE_ROUTES else {"account_id": self._account_id}
        if route.method == "GET":
            query = {**scoped, **params}
        else:
            body = {**scoped, **params}
        request = ApiRequest(
            route.path,
            version=SDK_API_VERSION,
            method=route.method,
            query_params=query,
            body_params=body,
        )
        if route is Route.PLACE:
            request.add_header("category", f"{ORDER_MARKET}_{ORDER_INSTRUMENT_TYPE}")
        if self._host not in self._allowed:  # invariant, checked on every send
            raise AdapterConfigError("trading_host is not on the host allowlist")
        request.set_endpoint(self._host)
        try:
            raw = self._client.get_response(request)
        except ServerException as exc:
            return WireResponse(
                int(exc.get_http_status() or 0),
                {
                    "error_code": exc.get_error_code(),
                    "message": self._scrub(str(exc.get_error_msg() or "")),
                },
            )
        except ClientException as exc:
            raise TransportFailure(self._scrub(str(exc.get_error_code()))) from None
        except Exception as exc:  # noqa: BLE001 - anything else: no usable response
            raise TransportFailure(type(exc).__name__) from None
        try:
            parsed = parse_json(raw.content) if raw.content else None
        except ValueError:
            return WireResponse(raw.status_code, {"error_code": "UNPARSEABLE_BODY"})
        return WireResponse(raw.status_code, parsed)


# --------------------------------------------------------------------------------------------
# Construction from config (the only place adapter numbers come from)
# --------------------------------------------------------------------------------------------


def settings_from_config(config: Config) -> WebullSettings:
    env = config.environment
    return WebullSettings(
        trading_host=env.http_host,
        http_port=env.http_port,
        allowed_hosts=env.hosts,
        region_id=env.region_id,
        connect_timeout_seconds=env.http_connect_timeout_seconds,
        read_timeout_seconds=env.http_read_timeout_seconds,
        token_dir=str(config.root / config.run.data_dir / env.token_dir),
        token_check_duration_seconds=env.token_check_duration_seconds,
        token_check_interval_seconds=env.token_check_interval_seconds,
        app_key_env=env.app_key_variable,
        app_secret_env=env.app_secret_variable,
        account_id_env=env.account_variable,
    )


def budget_from_config(
    config: Config, clock: Callable[[], float] = time.monotonic
) -> RequestBudget:
    """The bot's (and every bot-side tool's) request budget. With `[chart] webull_enabled` the
    bars route keeps `[chart] requests_per_minute` fewer requests per 60 s: that slice belongs to
    the API's chart budget (`chart_budget_from_config`), so both processes together never pass
    the route limit even if Webull counts it per App Key. The slice's window and size are refused
    when the config is loaded (`config.check_chart_slice`); checked again here for a config built
    another way."""
    env = config.environment
    pause = float(env.rate_limit_cooldown_seconds)
    default = RouteLimit(env.requests_per_route_per_minute, SECONDS_PER_MINUTE, pause)
    limits = {
        route: RouteLimit(count, float(window), pause)
        for route, (count, window) in env.route_limits.items()
    }
    chart = config.chart
    if chart.webull_enabled:
        check_chart_slice(chart, config.environment_name, env)
        bars = limits.get(Route.BARS.path, default)
        limits[Route.BARS.path] = RouteLimit(bars.max_requests - chart.requests_per_minute,
                                             SECONDS_PER_MINUTE, pause)
    return RequestBudget(default, limits, clock=clock)


def chart_budget_from_config(
    config: Config, clock: Callable[[], float] = time.monotonic
) -> RequestBudget:
    """The API's chart budget: `[chart] requests_per_minute` per 60 s on every route (only the
    bars route is ever used), its own instance, never shared with the bot's."""
    limit = RouteLimit(config.chart.requests_per_minute, SECONDS_PER_MINUTE,
                       float(config.environment.rate_limit_cooldown_seconds))
    return RequestBudget(limit, clock=clock)


def stream_settings_from_config(config: Config) -> StreamSettings:
    env = config.environment
    return StreamSettings(
        mqtt_host=env.mqtt_host,
        mqtt_port=env.mqtt_port,
        http_host=env.http_host,
        allowed_hosts=env.hosts,
        region_id=env.region_id,
        token_dir=str(config.root / config.run.data_dir / env.token_dir),
        app_key_env=env.app_key_variable,
        app_secret_env=env.app_secret_variable,
        category=config.market_data.screener_category,
        reconnect_delay_seconds=float(config.market_data.stream_reconnect_delay_seconds),
        max_symbols=config.market_data.stream_max_symbols,
    )


def market_data_from_config(
    config: Config,
    transport: Transport | None = None,
    budget: RequestBudget | None = None,
    stream: QuoteStream | None = None,
) -> WebullMarketData:
    """Market data for the active environment. Pass the broker's transport (one token, one SDK
    client) and, to share route accounting, its budget. `stream` adds L1 quotes (for example
    an `SdkQuoteStream` over a `StreamSession` with the same budget)."""
    md = config.market_data
    return WebullMarketData(
        transport if transport is not None else SdkTransport(settings_from_config(config)),
        budget if budget is not None else budget_from_config(config),
        md.screener_category,
        md.gainers_losers_rank_type,
        md.top_actives_rank_type,
        md.profile_symbols_per_request,
        utc_now=lambda: datetime.now(UTC),
        stream=stream,
        extended_rank_types={ScreenerSession.PRE: md.premarket_gainers_losers_rank_type,
                             ScreenerSession.POST: md.postmarket_gainers_losers_rank_type},
    )


def chart_market_data_from_config(
    config: Config, transport: Transport | None = None,
    budget: RequestBudget | None = None,
) -> WebullMarketData:
    """Market data for GET /chart in the API process: its own market-data-only SDK transport (no
    account id; the same token folder as the bot, docs/webull-facts.md C1) and its own chart
    budget. Refused unless `[chart] webull_enabled`."""
    if not config.chart.webull_enabled:
        raise AdapterConfigError("[chart] webull_enabled is false")
    return market_data_from_config(
        config,
        transport if transport is not None
        else SdkTransport(settings_from_config(config), market_data_only=True),
        budget if budget is not None else chart_budget_from_config(config),
    )


def chart_probe_market_data_from_config(config: Config) -> WebullMarketData:
    """Market data for `python -m tickr.probes chart-limit` (docs/webull-facts.md C2): its
    own market-data-only SDK transport (no account id) and the environment's own route limits,
    as one more process of the same App Key would have. The probe refuses while a bot runs."""
    return market_data_from_config(
        config, SdkTransport(settings_from_config(config), market_data_only=True),
        budget_from_config(config))


def check_token(config: Config, on_token_pending: Callable[[], None]) -> None:
    """Preflight: make the active environment's Webull token usable (may wait for the owner's
    confirmation in the app). Raises AdapterConfigError with the cause when it is not."""
    SdkTransport(settings_from_config(config), on_token_pending)


def broker_from_config(config: Config, transport: Transport | None = None,
                       budget: RequestBudget | None = None,
                       on_token_pending: Callable[[], None] = _no_token_hook) -> WebullBroker:
    """The adapter for the active environment. `transport` defaults to the SDK transport
    (which reads credentials); tests pass a recorded one."""
    return WebullBroker(
        transport if transport is not None
        else SdkTransport(settings_from_config(config), on_token_pending),
        budget if budget is not None else budget_from_config(config),
        config.environment.open_orders_max_pages,
        config.orders.client_order_id_max_length,
        (config.orders.client_order_id_prefix, config.orders.probe_client_order_id_prefix),
        gate=lambda: trading_gate(config),
        history_max_pages=config.environment.history_max_pages,
        cash_activities_max_pages=config.environment.cash_activities_max_pages,
    )


def probe_adapters(config: Config, on_token_pending: Callable[[], None] = _no_token_hook,
                   ) -> tuple[WebullBroker, WebullMarketData]:
    """Broker and snapshot market data for the manual probe commands: one SDK transport (one
    token) and one request budget."""
    transport = SdkTransport(settings_from_config(config), on_token_pending)
    budget = budget_from_config(config)
    return (broker_from_config(config, transport, budget),
            market_data_from_config(config, transport, budget))


def bot_adapters(
    config: Config, on_stream_error: Callable[[str, Exception], None],
    on_token_pending: Callable[[], None] = _no_token_hook,
) -> tuple[WebullBroker, WebullMarketData, SdkQuoteStream]:
    """Broker, market data and L1 stream for `tickr run`: one SDK transport (one token)
    and one request budget shared by all of them. The stream is not started."""
    transport = SdkTransport(settings_from_config(config), on_token_pending)
    budget = budget_from_config(config)
    broker = broker_from_config(config, transport, budget)
    settings = stream_settings_from_config(config)
    session = StreamSession(budget, lambda: datetime.now(UTC), on_stream_error,
                            max_symbols=settings.max_symbols)
    stream = SdkQuoteStream(settings, session)
    return broker, market_data_from_config(config, transport, budget, stream), stream


def history_from_config(config: Config, raw_dir: Path | None = None) -> WebullHistory:
    """History reads for the active environment's market data (the backtest tool only)."""
    return WebullHistory(SdkTransport(settings_from_config(config)), budget_from_config(config),
                         config.market_data.screener_category, raw_dir=raw_dir)
