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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
from aperture.config import Config, GateResult, trading_gate
from aperture.types import (
    OrderDetail,
    OrderRequest,
    OrderType,
    Position,
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

    @property
    def method(self) -> str:
        return self.value[0]

    @property
    def path(self) -> str:
        return self.value[1]

    @property
    def mutating(self) -> bool:
        return self.value[2]


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
        """Take budget, send once, handle transport failure and 429. Never retries."""
        self._budget.take(route.path)  # BudgetExhausted (RateLimited): nothing was sent
        started = self._clock()
        try:
            response = self._transport.send(route, params)
        except TransportFailure as failure:
            log.warning("%s %s transport failure %s", route.method, route.path, failure.code)
            if route.mutating:
                raise OutcomeUnknown(f"{route.name}: no response ({failure.code})") from None
            raise BrokerReadFailed(route.name, None, failure.code) from None
        elapsed_ms = (self._clock() - started) * 1000
        log.info(
            "%s %s -> %s %s (%.0f ms)",
            route.method, route.path, response.status, response.error_code or "", elapsed_ms,
        )
        if response.status == HTTP_TOO_MANY_REQUESTS:
            self._budget.pause(route.path)
            if route.mutating:
                # Not proven that a throttled POST created nothing: resolve by Detail.
                raise OutcomeUnknown(f"{route.name}: HTTP 429 on a trading request")
            raise RateLimited(f"{route.name}: HTTP 429 {response.error_code or ''}".strip())
        return response

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
        if route is not Route.ACCOUNTS:
            if route.method == "GET":
                query = {"account_id": self._account_id, **params}
            else:
                body = {"account_id": self._account_id, **params}
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


def broker_from_config(config: Config, transport: Transport | None = None) -> WebullBroker:
    """The adapter for the active environment. `transport` defaults to the SDK transport
    (which reads credentials); tests pass a recorded one."""
    return WebullBroker(
        transport if transport is not None else SdkTransport(settings_from_config(config)),
        budget_from_config(config),
        config.environment.open_orders_max_pages,
        config.orders.client_order_id_max_length,
        (config.orders.client_order_id_prefix, config.orders.probe_client_order_id_prefix),
        gate=lambda: trading_gate(config),
    )
