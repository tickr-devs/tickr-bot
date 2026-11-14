"""Webull OpenAPI broker adapter. The only module allowed to import the Webull SDK.

Layers, outermost first:

- `WebullBroker` implements `aperture.broker.base.Broker`. It takes request budget, sends
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
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol

from aperture.broker.base import (
    AccountSnapshot,
    Acknowledgement,
    BrokerError,
    OrderNotFound,
    OutcomeUnknown,
    RateLimited,
    Refused,
)
from aperture.broker.budget import RequestBudget, RouteLimit
from aperture.broker.status import map_status
from aperture.calendar import ny_date_of
from aperture.config import Config, GateResult, trading_gate
from aperture.data.base import (
    DelayedData,
    MarketDataError,
    QuoteListener,
    ScreenerKind,
    ScreenerRow,
)
from aperture.types import (
    Bar,
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
GATE_CLOSED = "GATE_CLOSED"
MESSAGE_ORDER_NOT_PRESENT = "order not present"

# Error codes that prove a place request did NOT create an order (probe evidence only).
PLACE_REFUSAL_CODES = frozenset({CODE_NON_TRADING_HOURS})

# Fixed parts of a US equity order body accepted by the broker (probe 2026-10-12, 2026-11-04).
ORDER_COMBO_TYPE = "NORMAL"
ORDER_ENTRUST_TYPE = "QTY"
ORDER_INSTRUMENT_TYPE = "EQUITY"
ORDER_MARKET = "US"

SDK_API_VERSION = "v3"
SDK_LOGGER_PREFIX = "webull"
LOGGING_OFF = logging.CRITICAL + 1
SECONDS_PER_MINUTE = 60


class Route(Enum):
    """Webull routes this adapter uses: (HTTP method, path, mutates broker state)."""

    ACCOUNTS = ("GET", "/trading/accounts/list", False)
    POSITIONS = ("GET", "/trading/assets/positions/list", False)
    OPEN_ORDERS = ("GET", "/trading/orders/open-orders/list", False)
    ORDER_DETAIL = ("GET", "/trading/orders/get", False)
    PLACE = ("POST", "/trading/orders/place", True)
    CANCEL = ("POST", "/trading/orders/cancel", True)
    GAINERS_LOSERS = ("GET", "/market-data/screeners/gainers-losers/list", False)
    TOP_ACTIVES = ("GET", "/market-data/screeners/top-actives/list", False)
    SNAPSHOT = ("GET", "/market-data/stocks/snapshots/list", False)
    BARS = ("POST", "/market-data/stocks/bars/list", False)
    PROFILES = ("GET", "/trading/instruments/stocks/profiles/list", False)

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
     Route.PROFILES}
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
    """A foreign order row outside our vocabulary (e.g. SHORT, TRAILING_STOP_LOSS)."""

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
    contradictions map to UNKNOWN (broker/status.py). `filled_price` has not been observed
    on a fill yet (no fill recorded so far), so it stays optional."""
    _check_account(item, is_own, where)
    raw_status = str(_require(item, "status", where)).strip()
    quantity = _decimal_field(item, "total_quantity", where)
    filled = _decimal_field(item, "filled_quantity", where)
    return OrderDetail(
        client_order_id=str(_require(item, "client_order_id", where)),
        symbol=str(_require(item, "symbol", where)),
        side=_enum_field(item, "side", Side, where),
        order_type=_enum_field(item, "order_type", OrderType, where),
        quantity=quantity,
        filled_quantity=filled,
        average_fill_price=_optional_decimal(item, "filled_price", where),
        state=map_status(raw_status, quantity, filled),
        raw_status=raw_status,
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
    """Positions: a bare array (recorded empty). Non-empty rows are not recorded yet; the
    old bot read `symbol` and `quantity`. The average-cost field name is unproven, so it is
    optional and read as `cost_price`."""
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
            )
        )
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
    return AccountSnapshot(account_present=present, trading_permitted=present)


# --------------------------------------------------------------------------------------------
# Order body
# --------------------------------------------------------------------------------------------


def _wire_decimal(value: Decimal) -> str:
    return format(value, "f")


def build_order_body(request: OrderRequest, client_order_id_max_length: int) -> dict[str, Any]:
    """OrderRequest -> one `new_orders[]` element. Raises Refused for requests Webull must
    never see; the code names the rule, and nothing is sent."""
    cid = request.client_order_id
    if not cid or len(cid) > client_order_id_max_length:
        raise Refused("LOCAL_CLIENT_ORDER_ID", "empty or longer than the broker limit")
    q = request.quantity
    if not q.is_finite() or q <= 0 or q != q.to_integral_value():
        raise Refused("LOCAL_QUANTITY", "quantity must be a positive whole number")
    needs_limit = request.order_type is OrderType.LIMIT
    needs_stop = request.order_type is OrderType.STOP_LOSS
    if needs_limit != (request.limit_price is not None):
        raise Refused("LOCAL_LIMIT_PRICE", f"limit_price does not fit {request.order_type}")
    if needs_stop != (request.stop_price is not None):
        raise Refused("LOCAL_STOP_PRICE", f"stop_price does not fit {request.order_type}")
    for price in (request.limit_price, request.stop_price):
        if price is not None and (not price.is_finite() or price <= 0):
            raise Refused("LOCAL_PRICE", "prices must be positive")
    body: dict[str, Any] = {
        "client_order_id": cid,
        "combo_type": ORDER_COMBO_TYPE,
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
    return body


# --------------------------------------------------------------------------------------------
# Broker
# --------------------------------------------------------------------------------------------


def send_once(
    transport: Transport,
    budget: RequestBudget,
    route: Route,
    params: Mapping[str, Any],
    clock: Callable[[], float],
) -> WireResponse:
    """Take budget, send once, handle transport failure and 429. Never retries."""
    budget.take(route.path)  # BudgetExhausted (RateLimited): nothing was sent
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
    ) -> None:
        if open_orders_max_pages < 1:
            raise AdapterConfigError("open_orders_max_pages must be at least 1")
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

    def positions(self) -> list[Position]:
        return read_positions(self._read(Route.POSITIONS, {}), self._transport.is_own_account)

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


# --------------------------------------------------------------------------------------------
# Market data over HTTP (aperture.data.base.MarketData). Shapes: tests/fixtures/webull/market_data.
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


@dataclass(frozen=True)
class Profile:
    instrument_id: str
    symbol: str
    security_type: str | None


def read_profiles(body: Any) -> dict[str, Profile]:
    """Instrument profile rows (`{data: [...]}`, probe M1) -> {instrument_id: Profile}.

    Identity is the instrument_id: one symbol may return several rows (OPENW returned a stale
    and a current id), and the caller matches the screener row's id, never the first symbol hit.
    A row without `sub_category` has an unknown type (None)."""
    profiles: dict[str, Profile] = {}
    for index, item in enumerate(_unwrap_list(body, "profiles")):
        where = f"profiles[{index}]"
        profile = Profile(
            instrument_id=str(_require(item, "instrument_id", where)),
            symbol=str(_require(item, "symbol", where)),
            security_type=(None if _optional(item, SECURITY_TYPE_KEY) is None
                           else str(item[SECURITY_TYPE_KEY])),
        )
        previous = profiles.get(profile.instrument_id)
        if previous is not None and previous != profile:
            raise MalformedResponse(f"{where}: instrument {profile.instrument_id} listed twice "
                                    "with different contents")
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


def _require_present(obj: Any, key: str, where: str) -> Any:
    """Like _require, but 0 is a value (delay_minutes = 0 means real time)."""
    if not isinstance(obj, Mapping) or key not in obj or obj[key] is None or obj[key] == "":
        raise MalformedResponse(f"{where}: missing field {key!r}")
    return obj[key]


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
    ) -> None:
        self._stream = stream
        if profile_symbols_per_request < 1:
            raise AdapterConfigError("profile_symbols_per_request must be at least 1")
        self._profile_chunk = profile_symbols_per_request
        self._types: dict[str, tuple[str, str]] = {}  # instrument_id -> (symbol, type)
        self._types_day: date | None = None
        self._transport = transport
        self._budget = budget
        self._category = category
        self._gainers_losers_rank_type = gainers_losers_rank_type
        self._top_actives_rank_type = top_actives_rank_type
        self._utc_now = utc_now
        self._clock = clock

    @property
    def provider(self) -> str:
        return MARKET_DATA_PROVIDER

    def _read(self, route: Route, params: Mapping[str, Any]) -> Any:
        response = send_once(self._transport, self._budget, route, params, self._clock)
        if not response.ok:
            raise BrokerReadFailed(route.name, response.status, response.error_code)
        return response.body

    def screener(self, kind: ScreenerKind) -> list[ScreenerRow]:
        if kind is ScreenerKind.TOP_ACTIVES:
            body = self._read(Route.TOP_ACTIVES, {
                "category": self._category, "rank_type": self._top_actives_rank_type})
        else:
            direction = DIRECTION_GAINERS if kind is ScreenerKind.GAINERS else DIRECTION_LOSERS
            body = self._read(Route.GAINERS_LOSERS, {
                "category": self._category,
                "rank_type": self._gainers_losers_rank_type,
                "direction": direction,
            })
        return self._with_types(read_screener(body, kind))

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

    def snapshot(self, symbols: Sequence[str]) -> list[Quote]:
        body = self._read(Route.SNAPSHOT, {
            "symbols": ",".join(symbols), "category": self._category})
        quotes = read_snapshot(body, self._utc_now())
        stray = sorted({q.symbol for q in quotes} - set(symbols))
        if stray:
            raise MalformedResponse(f"snapshot: symbols not requested: {', '.join(stray)}")
        return quotes

    def bars(self, symbols: Sequence[str], count: int) -> list[Bar]:
        body = self._read(Route.BARS, {
            "symbols": list(symbols),
            "category": self._category,
            "timespan": BARS_TIMESPAN_M1,
            "count": str(count),
        })
        return read_bars(body)

    def bars_before(self, symbol: str, end: datetime, count: int) -> list[Bar]:
        """One history page: `end_time` (epoch ms) with `count` (probe M4: end_time is exclusive,
        the newest bar starts at end - 1 min; order newest first). `start_time` is never sent: it
        returns an extra bar at start - 1 min. The reply is still checked: another symbol is
        refused and a bar starting at or after `end` is dropped."""
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError(f"naive end time {end!r}")
        body = self._read(Route.BARS, {
            "symbols": [symbol],
            "category": self._category,
            "timespan": BARS_TIMESPAN_M1,
            "count": str(count),
            "end_time": int(end.timestamp() * MILLISECONDS_PER_SECOND),
        })
        bars = read_bars(body)
        stray = sorted({b.symbol for b in bars} - {symbol})
        if stray:
            raise MalformedResponse(f"bars: symbols not requested: {', '.join(stray)}")
        return [b for b in bars if b.start < end]

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


class QuoteStream(Protocol):
    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None: ...

    def unsubscribe(self, symbols: Iterable[str]) -> None: ...


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
    """

    def __init__(self, budget: RequestBudget, utc_now: Callable[[], datetime],
                 on_error: Callable[[str, Exception], None]) -> None:
        self._budget = budget
        self._utc_now = utc_now
        self._on_error = on_error
        self._lock = threading.Lock()
        self._sync_lock = threading.RLock()  # one _sync at a time: no duplicate subscribe POSTs
        self._symbols: set[str] = set()
        self._sent: set[str] = set()  # subscribed on the current connection
        self._listener: QuoteListener | None = None
        self._send_subscribe: Callable[[list[str]], None] | None = None
        self._send_unsubscribe: Callable[[list[str]], None] | None = None
        self._reconnect: Callable[[], None] | None = None
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
            if pending:
                self._send(True, pending)
                with self._lock:
                    self._sent.update(pending)
            if stale:
                self._send(False, stale)
                with self._lock:
                    self._sent.difference_update(stale)

    # -- calls from the bot
    def subscribe(self, symbols: Iterable[str], listener: QuoteListener) -> None:
        with self._lock:
            if self._listener is not None and listener is not self._listener:
                raise MarketDataError("one stream listener per session; use FanOut")
            first = self._listener is None
            self._listener = listener
            self._symbols.update(symbols)
            up = self._up
        if up:
            self._sync_or_reconnect(raise_error=True)
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

    def _sync_or_reconnect(self, raise_error: bool) -> bool:
        """Sync; on failure mark the transport down (cause reported), call on_error and ask
        for a reconnect, whose resubscribe sends the full set again. True when in sync."""
        try:
            return self._sync()
        except Exception as exc:  # noqa: BLE001 - budget, HTTP or SDK failure while (un)subscribing
            with self._lock:
                self._connected = self._up = False
            self._notify(False, f"resubscribe failed ({type(exc).__name__}); reconnecting")
            self._on_error("subscribe", exc)
            if self._reconnect is not None:
                self._reconnect()
            if raise_error:
                raise
            return False

    # -- calls from the provider thread
    def on_connected(self) -> None:
        with self._lock:
            self._connected, self._up, self._sent = True, False, set()
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
                elif kind != NOTICE_STATUS:
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
        session.bind(
            lambda symbols: client.subscribe(symbols, category, [STREAM_SUB_TYPE_QUOTE]),
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
        self._thread = threading.Thread(target=self._run, name="aperture-l1-stream", daemon=True)
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


class SdkTransport:
    """`Transport` over webull-openapi-python-sdk 3.0.3."""

    def __init__(self, settings: WebullSettings) -> None:
        if settings.trading_host not in settings.allowed_hosts:
            raise AdapterConfigError("trading_host is not on the host allowlist")
        self._host = settings.trading_host
        self._allowed = settings.allowed_hosts
        app_key = _read_credential(settings.app_key_env)
        app_secret = _read_credential(settings.app_secret_env)
        self._account_id = _read_credential(settings.account_id_env)

        silence_sdk_loggers()  # SDK modules attach handlers at import time
        from webull.core.client import ApiClient
        from webull.core.common.api_type import DEFAULT as API_TYPE_HTTP
        from webull.core.http.initializer.client_initializer import ClientInitializer

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
            ClientInitializer.initializer(client)  # token: may wait for app confirmation
        except Exception as exc:  # noqa: BLE001 - SDK raises bare Exceptions here
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
        return account_id == self._account_id

    def _scrub(self, text: str) -> str:
        return text.replace(self._account_id, "<account>") if text else text

    def send(self, route: Route, params: Mapping[str, Any]) -> WireResponse:
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
    env = config.environment
    pause = float(env.rate_limit_cooldown_seconds)
    default = RouteLimit(env.requests_per_route_per_minute, SECONDS_PER_MINUTE, pause)
    limits = {
        route: RouteLimit(count, float(window), pause)
        for route, (count, window) in env.route_limits.items()
    }
    return RequestBudget(default, limits, clock=clock)


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
    )


def broker_from_config(config: Config, transport: Transport | None = None,
                       budget: RequestBudget | None = None) -> WebullBroker:
    """The adapter for the active environment. `transport` defaults to the SDK transport
    (which reads credentials); tests pass a recorded one."""
    return WebullBroker(
        transport if transport is not None else SdkTransport(settings_from_config(config)),
        budget if budget is not None else budget_from_config(config),
        config.environment.open_orders_max_pages,
        config.orders.client_order_id_max_length,
        (config.orders.client_order_id_prefix, config.orders.probe_client_order_id_prefix),
        gate=lambda: trading_gate(config),
    )


def bot_adapters(
    config: Config, on_stream_error: Callable[[str, Exception], None]
) -> tuple[WebullBroker, WebullMarketData, SdkQuoteStream]:
    """Broker, market data and L1 stream for `aperture run`: one SDK transport (one token)
    and one request budget shared by all of them. The stream is not started."""
    transport = SdkTransport(settings_from_config(config))
    budget = budget_from_config(config)
    broker = broker_from_config(config, transport, budget)
    session = StreamSession(budget, lambda: datetime.now(UTC), on_stream_error)
    stream = SdkQuoteStream(stream_settings_from_config(config), session)
    return broker, market_data_from_config(config, transport, budget, stream), stream
