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


@dataclass(frozen=True)
class ExtendedQuote:
    """A quote read with the pre-market and post-market included (Webull snapshot with
    `extend_hour_required`, probe Q1): the book plus the newest extended-session trade, or None
    for both when the session has had none."""

    quote: Quote
    last_price: Decimal | None
    last_trade_time: datetime | None


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    # Sent only by the extended-hours probe (engine/session_probe.py) until F7 is answered.
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"
    # Sent only by the trailing-stop probe (engine/trailing_probe.py) until T1-T5 are answered.
    TRAILING_STOP_LOSS = "TRAILING_STOP_LOSS"


class TimeInForce(StrEnum):
    DAY = "DAY"


class TradingSession(StrEnum):
    CORE = "CORE"
    ALL = "ALL"


class ComboRole(StrEnum):
    """A leg's `combo_type` in a combo place request (Webull docs: MASTER with attached
    STOP_PROFIT and/or STOP_LOSS legs). Sent only by the attached-stop probe
    (engine/attached_probe.py) until it is answered."""

    MASTER = "MASTER"
    STOP_LOSS = "STOP_LOSS"
    STOP_PROFIT = "STOP_PROFIT"


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


class TrailingType(StrEnum):
    """How Webull reads a trailing stop's `trailing_stop_step` (SDK `TrailingType`):
    PERCENTAGE as a ratio of the price (0.2 = 20 %, probe T2, 2026-11-17), AMOUNT in USD."""

    PERCENTAGE = "PERCENTAGE"
    AMOUNT = "AMOUNT"


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
    # TRAILING_STOP_LOSS only: Webull's `trailing_stop_step`, sent as given, read in the unit
    # `trailing_type` names.
    trailing_stop_step: Decimal | None = None
    trailing_type: TrailingType = TrailingType.PERCENTAGE


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
    # Display only: a trailing stop's `trailing_stop_step` as the row shows it (unproven).
    trailing_stop_step: Decimal | None = None
    # Trailing rows only: the row's field names (never values), so the trailing probe can
    # record where Webull shows the current stop.
    row_keys: tuple[str, ...] | None = None


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
