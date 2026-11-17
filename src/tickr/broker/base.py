"""Broker interface. Every broker implementation (fake, Webull) satisfies `Broker`.

No other module may import a broker SDK. Implementations return typed results and raise
`BrokerError` subclasses; they never return raw provider payloads to callers.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from tickr.types import ComboRole, OrderDetail, OrderRequest, Position


class BrokerError(Exception):
    """Base class for typed broker failures."""


class RateLimited(BrokerError):
    """The provider answered 429 or the local request budget refused the call."""


class OrderNotFound(BrokerError):
    """The broker says this client_order_id does not exist."""


class Refused(BrokerError):
    """The broker refused the request with a known code; the order was NOT created."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class OutcomeUnknown(BrokerError):
    """Transport failure after sending: the order may or may not exist. Never resend."""


@dataclass(frozen=True)
class Acknowledgement:
    client_order_id: str
    broker_order_id: str | None


@dataclass(frozen=True)
class ReplaceAck:
    """The broker answered a replace with success. It proves only that the request arrived:
    whether the order changed, and under which id, is decided by Detail and open orders.
    The ids are what the answer echoed, if anything (shape unproven, probe F5)."""

    client_order_id: str | None
    broker_order_id: str | None


@dataclass(frozen=True)
class AccountSnapshot:
    account_present: bool
    trading_permitted: bool
    label: str | None = None  # the broker's name for the account kind ("Individual Margin")


@dataclass(frozen=True)
class AccountBalance:
    """The account's money as the broker reports it, in USD. Display only: nothing in the bot
    decides on it (limits live in the platform settings)."""

    net_liquidation: Decimal  # cash plus positions at market: the account's equity
    cash: Decimal
    market_value: Decimal  # positions at market
    unrealized_profit_loss: Decimal
    day_profit_loss: Decimal
    day_buying_power: Decimal
    overnight_buying_power: Decimal
    maintenance_margin: Decimal
    margin_calls: int  # open margin calls listed
    day_trades_left: int | None  # None: the broker says unlimited


@dataclass(frozen=True)
class BrokerFill:
    """One of today's orders with a fill, from the broker's order history. Display only.
    `fees` sums the broker's actual fee values; None when a fee is unreadable."""

    client_order_id: str | None
    symbol: str
    side: str
    order_type: str
    filled_quantity: Decimal
    filled_price: Decimal | None
    filled_at: datetime | None
    fees: Decimal | None


@dataclass(frozen=True)
class CashTrade:
    """One trade's settlement in the account's cash activities: the exact signed amount the
    trade moved (fees included; a buy is negative), which the order rows do not give for
    sub-dollar prices (they round to cents, probe 2026-11-16)."""

    symbol: str
    side: str  # the broker's word (BUY, SELL, ...)
    net_amount: Decimal
    at: datetime  # `biz_time`: equal to the fill's `filled_time_at` (probe 2026-11-16)


class Broker(Protocol):
    def account(self) -> AccountSnapshot: ...

    def balance(self) -> AccountBalance:
        """One read of the account's balances. Raises BrokerError when it cannot be read."""
        ...

    def fills_today(self, day: date) -> list[BrokerFill]:
        """The orders filled on New York date `day`, oldest first. Raises BrokerError when
        they cannot be read in full."""
        ...

    def cash_trades(self, day: date) -> list[CashTrade]:
        """The account's TRADE cash activities of New York date `day`. Raises BrokerError
        when they cannot be read in full."""
        ...

    def positions(self) -> list[Position]: ...

    def open_orders(self) -> list[OrderDetail]: ...

    def order_history(self, wanted: Collection[str], max_pages: int) -> list[OrderDetail]:
        """Our own rows of the order history (each shaped like its Detail), page by page until
        every client order id in `wanted` was seen, the last page, or `max_pages` pages. Rows
        it did not reach are simply absent: the caller learns nothing about them. Raises
        BrokerError (RateLimited included) when a page cannot be read."""
        ...

    def order_detail(self, client_order_id: str) -> OrderDetail: ...

    def place(self, request: OrderRequest) -> Acknowledgement:
        """Send exactly once. Raises Refused (not created) or OutcomeUnknown (maybe created)."""
        ...

    def place_combo(self, combo_id: str,
                    legs: Sequence[tuple[ComboRole, OrderRequest]]) -> Acknowledgement:
        """Send one combo (a MASTER entry and its attached legs) exactly once, in one request.
        UNPROVEN at the broker: only the attached-stop probe calls it. Raises Refused (nothing
        created), RateLimited (nothing sent) or OutcomeUnknown (read every leg by Detail)."""
        ...

    def cancel(self, client_order_id: str) -> None:
        """Request cancellation. State is confirmed only by a later order_detail()."""
        ...

    def replace_stop_price(self, client_order_id: str, quantity: Decimal,
                           stop_price: Decimal) -> ReplaceAck:
        """Ask once to move a working STOP_LOSS to `stop_price`, quantity unchanged. Raises
        Refused or RateLimited (nothing sent / not changed), OrderNotFound, or OutcomeUnknown
        (may have changed: read Detail, never resend)."""
        ...

    def can_place(self, count: int) -> bool:
        """True if the request budget would admit `count` place requests now (an entry and
        its stop). Takes nothing and sends nothing."""
        ...
