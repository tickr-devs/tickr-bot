"""Shared market and order types used across modules.

Prices and quantities are Decimal. Times are timezone-aware datetimes (UTC unless stated).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


@dataclass(frozen=True)
class Bar:
    """One completed bar. `start` is the bar's opening time (aware datetime)."""

    symbol: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Quote:
    """One L1 quote with provider time and our receipt time."""

    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    provider_time: datetime
    receipt_time: datetime


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"


class TimeInForce(StrEnum):
    DAY = "DAY"


class TradingSession(StrEnum):
    CORE = "CORE"
    ALL = "ALL"


class OrderState(StrEnum):
    """Internal order state derived only from broker Detail."""

    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
)


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    time_in_force: TimeInForce
    session: TradingSession
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class OrderDetail:
    """Broker's view of one order (cumulative aggregates only). `stop_price`, `limit_price`
    and `broker_order_id` are what the broker row shows, when it shows them (all optional: no
    working stop has been recorded yet, and only LIMIT rows carry a limit price)."""

    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    state: OrderState
    raw_status: str
    stop_price: Decimal | None = None
    broker_order_id: str | None = None
    limit_price: Decimal | None = None
    # The sum of the row's `fees[].actual_value`; None when the row has no readable fees.
    # Recorded once the order is finished (the daily loss counts it).
    fees: Decimal | None = None


@dataclass(frozen=True)
class Position:
    """A broker position. The fields after `average_cost` are display only (recorded
    2026-11-16): nothing decides on them, and None means not shown or unreadable."""

    symbol: str
    quantity: Decimal
    average_cost: Decimal | None
    last_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_profit_loss: Decimal | None = None
    day_profit_loss: Decimal | None = None
