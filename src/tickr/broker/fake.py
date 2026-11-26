"""In-memory broker for tests and dry runs. Behaviour is scripted per call."""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

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
from tickr.types import (
    ComboRole,
    OrderDetail,
    OrderRequest,
    OrderState,
    OrderType,
    Position,
    Side,
    Tradability,
)

# Webull's refusal code for an order identical to a working one (docs/webull-facts.md).
DUPLICATE_ORDER = "OPENAPI_ORDER_RISK_RULE_DUPLICATE_ORDER_CHECK"


@dataclass
class FakeBroker:
    """Scripted outcomes for `place`: "ack" (default), "refuse", "unknown", "unknown-lost",
    "rate-limited" (one outcome per `place_combo` too). "unknown" means the order reached the
    broker but the reply was lost; "unknown-lost" means it never arrived.

    Scripted outcomes for `replace_stop_price` (`replace_outcomes`): "ack" (default: same id,
    new stop price), "new-id" (the old order is CANCELLED and a new one with id
    `<old>-r` carries the new price), "refuse", "unknown" (changed, reply lost),
    "unknown-lost" (not changed), "rate-limited", "not-found"."""

    account_present: bool = True
    trading_permitted: bool = True
    outcomes: deque[str] = field(default_factory=deque)
    orders: dict[str, OrderDetail] = field(default_factory=dict)
    holdings: dict[str, Position] = field(default_factory=dict)
    sent: list[OrderRequest] = field(default_factory=list)
    replace_outcomes: deque[str] = field(default_factory=deque)
    replaced: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)
    # Combo legs by MASTER client order id (place_combo). Cancelling a MASTER cancels its
    # working legs while `combo_cancels_legs` holds (what the broker does is UNPROVEN).
    combos: dict[str, list[str]] = field(default_factory=dict)
    combo_cancels_legs: bool = True
    _next_id: int = 1
    place_budget: int | None = None  # None = unlimited; else place requests can_place admits
    funds: AccountBalance | None = None  # what balance() returns; None = the read fails
    cash: list[CashTrade] = field(default_factory=list)  # what cash_trades() returns
    fill_fees: Decimal | None = Decimal(0)  # the fees a fill shows (None: unreadable)
    account_label: str | None = None
    balance_reads: int = 0
    history_reads: int = 0
    history_down: bool = False  # order_history() raises
    history_hidden: set[str] = field(default_factory=set)  # ids order_history() leaves out
    # What tradability(symbol) returns per symbol; a symbol not listed raises BrokerError.
    tradable: dict[str, Tradability] = field(default_factory=dict)
    tradability_reads: int = 0
    tradability_budget: bool = True  # what can_read_tradability() answers

    def script(self, *outcomes: str) -> None:
        self.outcomes.extend(outcomes)

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(self.account_present, self.trading_permitted, self.account_label)

    def fills_today(self, day: date) -> list[BrokerFill]:  # noqa: ARG002 - one fake day
        return [BrokerFill(o.client_order_id, o.symbol, str(o.side), str(o.order_type),
                           o.filled_quantity, o.average_fill_price, None, o.fees)
                for o in self.orders.values() if o.filled_quantity > 0]

    def cash_trades(self, day: date) -> list[CashTrade]:  # noqa: ARG002 - one fake day
        return list(self.cash)

    def balance(self) -> AccountBalance:
        self.balance_reads += 1
        if self.funds is None:
            raise BrokerError("no balance scripted")
        return self.funds

    def tradability(self, symbol: str) -> Tradability:
        self.tradability_reads += 1
        found = self.tradable.get(symbol)
        if found is None:
            raise BrokerError(f"no tradability scripted for {symbol}")
        return found

    def can_read_tradability(self) -> bool:
        return self.tradability_budget

    def positions(self) -> list[Position]:
        return [p for p in self.holdings.values() if p.quantity != 0]

    def open_orders(self) -> list[OrderDetail]:
        return [o for o in self.orders.values() if not o.state.terminal]

    def order_history(self, wanted: Collection[str], max_pages: int) -> list[OrderDetail]:  # noqa: ARG002 - one page
        """Every order, as Detail shows it, but those in `history_hidden` (not listed yet);
        raises BrokerError while `history_down`."""
        self.history_reads += 1
        if self.history_down:
            raise BrokerError("history down")
        return [o for cid, o in self.orders.items() if cid not in self.history_hidden]

    def order_detail(self, client_order_id: str) -> OrderDetail:
        try:
            return self.orders[client_order_id]
        except KeyError as exc:
            raise OrderNotFound(client_order_id) from exc

    def place(self, request: OrderRequest) -> Acknowledgement:
        self.sent.append(request)
        outcome = self.outcomes.popleft() if self.outcomes else "ack"
        if outcome == "rate-limited":
            raise RateLimited("429")
        if outcome == "refuse":
            raise Refused("REFUSED", "scripted refusal")
        if outcome == "unknown-lost":
            raise OutcomeUnknown("scripted: request lost")
        self._book(request)
        if outcome == "unknown":
            raise OutcomeUnknown("scripted: reply lost after acceptance")
        broker_id = f"fake-{self._next_id}"
        self._next_id += 1
        return Acknowledgement(request.client_order_id, broker_id)

    def _book(self, request: OrderRequest) -> None:
        if request.client_order_id in self.orders:
            raise Refused("DUPLICATE", "client_order_id already used")
        # Webull refuses a stop identical to a working one (probe T D1, docs/webull-facts.md;
        # proven for STOP_LOSS orders only).
        identity = (request.symbol, request.side, request.order_type, request.quantity,
                    request.stop_price, request.limit_price)
        if request.order_type is OrderType.STOP_LOSS and any(
                not o.state.terminal and identity == (o.symbol, o.side, o.order_type, o.quantity,
                                                      o.stop_price, o.limit_price)
                for o in self.orders.values()):
            raise Refused(DUPLICATE_ORDER, "identical to a working order")
        self.orders[request.client_order_id] = OrderDetail(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            filled_quantity=Decimal(0),
            average_fill_price=None,
            state=OrderState.ACCEPTED,
            raw_status="SUBMITTED",
            stop_price=request.stop_price,
            limit_price=request.limit_price,
        )

    def place_combo(self, combo_id: str,
                    legs: Sequence[tuple[ComboRole, OrderRequest]]) -> Acknowledgement:
        """Every leg becomes a working order at once; each leg is recorded in `sent`."""
        outcome = self.outcomes.popleft() if self.outcomes else "ack"
        self.sent.extend(r for _, r in legs)
        if outcome == "rate-limited":
            raise RateLimited("429")
        if outcome == "refuse":
            raise Refused("REFUSED", "scripted refusal")
        if outcome == "unknown-lost":
            raise OutcomeUnknown("scripted: request lost")
        master = next(r for role, r in legs if role is ComboRole.MASTER)
        for _, request in legs:
            self._book(request)
        self.combos[master.client_order_id] = [
            r.client_order_id for role, r in legs if role is not ComboRole.MASTER]
        if outcome == "unknown":
            raise OutcomeUnknown("scripted: reply lost after acceptance")
        return Acknowledgement(master.client_order_id, f"fake-combo-{combo_id}")

    def replace_stop_price(self, client_order_id: str, quantity: Decimal,
                           stop_price: Decimal) -> ReplaceAck:
        self.replaced.append((client_order_id, quantity, stop_price))
        outcome = self.replace_outcomes.popleft() if self.replace_outcomes else "ack"
        if outcome == "rate-limited":
            raise RateLimited("429")
        if outcome == "refuse":
            raise Refused("REFUSED", "scripted refusal")
        if outcome == "not-found":
            raise OrderNotFound(client_order_id)
        if outcome == "unknown-lost":
            raise OutcomeUnknown("scripted: request lost")
        order = self.order_detail(client_order_id)
        if outcome == "new-id":
            self.orders[client_order_id] = replace(order, state=OrderState.CANCELLED,
                                                   raw_status="CANCELLED")
            new_id = f"{client_order_id}-r"
            self.orders[new_id] = replace(order, client_order_id=new_id, stop_price=stop_price)
        else:
            new_id = client_order_id
            self.orders[client_order_id] = replace(order, stop_price=stop_price)
        if outcome == "unknown":
            raise OutcomeUnknown("scripted: reply lost after the replace")
        return ReplaceAck(new_id, None)

    def can_place(self, count: int) -> bool:
        return self.place_budget is None or count <= self.place_budget

    def cancel(self, client_order_id: str) -> None:
        order = self.order_detail(client_order_id)
        if order.state.terminal:
            raise Refused("TERMINAL", "order already terminal")
        self.orders[client_order_id] = replace(order, state=OrderState.CANCELLED,
                                               raw_status="CANCELLED")
        for leg in self.combos.get(client_order_id, []) if self.combo_cancels_legs else []:
            if not self.orders[leg].state.terminal:
                self.orders[leg] = replace(self.orders[leg], state=OrderState.CANCELLED,
                                           raw_status="CANCELLED")

    # --- test controls -------------------------------------------------------------------

    def fill(self, client_order_id: str, quantity: Decimal, price: Decimal) -> None:
        """Fill `quantity` more at `price`, updating the position."""
        order = self.order_detail(client_order_id)
        if order.state.terminal:
            raise ValueError("cannot fill a terminal order")
        filled = order.filled_quantity + quantity
        if filled > order.quantity:
            raise ValueError("overfill")
        previous_value = (order.average_fill_price or Decimal(0)) * order.filled_quantity
        average = (previous_value + price * quantity) / filled
        state = OrderState.FILLED if filled == order.quantity else OrderState.PARTIALLY_FILLED
        raw = "FILLED" if state is OrderState.FILLED else "PARTIAL FILLED"
        self.orders[client_order_id] = replace(order, filled_quantity=filled,
                                               average_fill_price=average,
                                               state=state, raw_status=raw,
                                               fees=self.fill_fees)
        signed = quantity if order.side is Side.BUY else -quantity
        self._apply(order.symbol, signed, price)

    def add_foreign_position(self, symbol: str, quantity: Decimal, cost: Decimal) -> None:
        """A position the bot did not open (for example a manual trade)."""
        self._apply(symbol, quantity, cost)

    def _apply(self, symbol: str, signed: Decimal, price: Decimal) -> None:
        current = self.holdings.get(symbol)
        if current is None or current.quantity == 0:
            self.holdings[symbol] = Position(symbol, signed, price)
            return
        quantity = current.quantity + signed
        if quantity == 0:
            del self.holdings[symbol]
            return
        if (current.quantity > 0) == (signed > 0):
            cost = (current.average_cost * current.quantity + price * signed) / quantity
        else:
            cost = current.average_cost
        self.holdings[symbol] = Position(symbol, quantity, cost)
