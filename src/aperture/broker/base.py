"""Broker interface. Every broker implementation (fake, Webull) satisfies `Broker`.

No other module may import a broker SDK. Implementations return typed results and raise
`BrokerError` subclasses; they never return raw provider payloads to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aperture.types import OrderDetail, OrderRequest, Position


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
class AccountSnapshot:
    account_present: bool
    trading_permitted: bool


class Broker(Protocol):
    def account(self) -> AccountSnapshot: ...

    def positions(self) -> list[Position]: ...

    def open_orders(self) -> list[OrderDetail]: ...

    def order_detail(self, client_order_id: str) -> OrderDetail: ...

    def place(self, request: OrderRequest) -> Acknowledgement:
        """Send exactly once. Raises Refused (not created) or OutcomeUnknown (maybe created)."""
        ...

    def cancel(self, client_order_id: str) -> None:
        """Request cancellation. State is confirmed only by a later order_detail()."""
        ...

    def can_place(self, count: int) -> bool:
        """True if the request budget would admit `count` place requests now (an entry and
        its stop). Takes nothing and sends nothing."""
        ...
