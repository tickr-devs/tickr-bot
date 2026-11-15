"""Test-only hooks for the rehearsals in tests/rehearsal/test_scenarios_*.py.

`ScriptedExchange` is the rehearsal `Exchange` with a few scripted broker behaviours the plain
world does not have: entries that fill in pieces, hooks around place / cancel / Detail (crash,
lost reply, cancel-vs-fill race, unmapped status, "not found"), and a record of every call.
`strategy_exits` makes the active strategy answer EXIT for a held symbol from a given minute
(price-action-v1 has no exit rule of its own; the platform path is what is under test).
Nothing here changes product code: the exchange is swapped in through `world.exchange`, which
the harness reads when `tickr run` builds its adapters, and the strategy through monkeypatch.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

import pytest

from rehearsal.harness import DAY, Rehearsal, history
from rehearsal.world import Exchange, World, build_day, ny
from tickr import cli
from tickr.broker.base import Acknowledgement, OutcomeUnknown
from tickr.strategy.base import Check, Decision, SymbolContext, Verdict
from tickr.strategy.price_action_v1 import PriceActionV1
from tickr.types import OrderDetail, OrderRequest, OrderState, OrderType, Side

HOT = Decimal(30000)
QUIET = Decimal(3000)


class Crash(BaseException):
    """The process dies (kill -9, power): no drain, no orderly shutdown."""


@dataclass
class Piece:
    """One fill of an entry: at most `cap` shares (None: the rest), not before `not_before`."""

    cap: Decimal | None
    not_before: datetime | None = None


class ScriptedExchange(Exchange):
    def __init__(self, world: World) -> None:
        super().__init__(world)
        self.pieces: dict[str, list[Piece]] = {}
        # Hooks get the request (place) or the client order id (cancel).
        self.before_place: list[Callable[[OrderRequest], None]] = []
        self.after_place: list[Callable[[OrderRequest], None]] = []
        self.before_cancel: list[Callable[[str], None]] = []
        self.after_cancel: list[Callable[[str], None]] = []
        # May return a changed detail or raise (OrderNotFound, OutcomeUnknown).
        self.detail_hook: Callable[[str, OrderDetail], OrderDetail] | None = None
        self.calls: list[tuple[datetime, str, str]] = []
        # Cancels of an order that is already terminal are answered like a cancel that
        # arrived too late at Webull: accepted, and Detail then shows the fill.
        self.late_cancel_accepted = False
        self.short: list[tuple[datetime, str, Decimal]] = []  # any negative holding seen
        self._internal = 0  # > 0 while the fake reads its own orders (fill, cancel)

    # --- broker calls --------------------------------------------------------------------

    def place(self, request: OrderRequest) -> Acknowledgement:
        self.calls.append((self.world.now, "place", request.client_order_id))
        for hook in self.before_place:
            hook(request)
        try:
            ack = super().place(request)
        except OutcomeUnknown:
            # "unknown" (created, reply lost): the order rests and may fill like any other.
            if request.client_order_id in self.orders:
                self.requests[request.client_order_id] = request
                self._unverify(request.client_order_id)
                self._match(request.client_order_id)
            raise
        for hook in self.after_place:
            hook(request)
        return ack

    def cancel(self, client_order_id: str) -> None:
        self.calls.append((self.world.now, "cancel", client_order_id))
        for hook in self.before_cancel:
            hook(client_order_id)
        order = self.orders.get(client_order_id)
        if not (self.late_cancel_accepted and order is not None and order.state.terminal):
            self._internal += 1
            try:
                super().cancel(client_order_id)
            finally:
                self._internal -= 1
        for hook in self.after_cancel:
            hook(client_order_id)

    def order_detail(self, client_order_id: str) -> OrderDetail:
        if self._internal:
            return super().order_detail(client_order_id)
        self.calls.append((self.world.now, "detail", client_order_id))
        detail = super().order_detail(client_order_id)
        if self.detail_hook is not None:
            return self.detail_hook(client_order_id, detail)
        return detail

    def fill(self, client_order_id: str, quantity: Decimal, price: Decimal) -> None:
        self._internal += 1
        try:
            super().fill(client_order_id, quantity, price)
        finally:
            self._internal -= 1
        symbol = self.orders[client_order_id].symbol
        held = self.holdings.get(symbol)
        if held is not None and held.quantity < 0:
            self.short.append((self.world.now, symbol, held.quantity))

    # --- matching: entries may fill in pieces ----------------------------------------------

    def _match(self, cid: str) -> None:
        order = self.orders[cid]
        plan = self.pieces.get(order.symbol)
        if not (plan and order.order_type is OrderType.LIMIT and order.side is Side.BUY):
            super()._match(cid)
            return
        if order.state.terminal or order.state is OrderState.UNKNOWN or not self.world.in_session():
            return
        piece = plan[0]
        if piece.not_before is not None and self.world.now < piece.not_before:
            return
        request = self.requests[cid]
        quote = self.world.quote(order.symbol, self.world.exchange_lead)
        if request.limit_price is None or quote.ask > request.limit_price:
            return
        rest = order.quantity - order.filled_quantity
        quantity = rest if piece.cap is None else min(rest, piece.cap)
        plan.pop(0)
        self.fill(cid, quantity, quote.ask)
        self.fills.append((self.world.now, order.symbol, order.side, order.order_type,
                           quantity, quote.ask))

    # --- reading -------------------------------------------------------------------------

    def of(self, symbol: str, order_type: OrderType | None = None) -> list[OrderRequest]:
        return [o for o in self.sent if o.symbol == symbol
                and (order_type is None or o.order_type is order_type)]

    def fill_now(self, cid: str, quantity: Decimal | None = None) -> None:
        """Fill a working order at the current bid/ask (a race the bot cannot see coming)."""
        order = self.orders[cid]
        quote = self.world.quote(order.symbol)
        price = quote.bid if order.side is Side.SELL else quote.ask
        amount = order.quantity - order.filled_quantity if quantity is None else quantity
        self.fill(cid, amount, price)
        self.fills.append((self.world.now, order.symbol, order.side, order.order_type,
                           amount, price))


def without_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip os.fsync of the status and command files (about a fifth of a rehearsal's time).
    Durability is not under test here: a scripted crash is an exception in this process, so
    every write is still there for the restart."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def scripted(r: Rehearsal) -> ScriptedExchange:
    exchange = ScriptedExchange(r.world)
    r.world.exchange = exchange
    return exchange


def breakout(start: str, end: str, top: str) -> tuple[str, str, Decimal, Decimal]:
    """A hot ramp price-action-v1 buys near its end (0.3 USD a minute over 15 minutes)."""
    return (start, end, Decimal(top), HOT)


def add(r: Rehearsal, symbol: str, base: str, *legs: tuple[str, str, Decimal, Decimal]) -> None:
    r.world.add_symbol(symbol, build_day(symbol, DAY, Decimal(base), QUIET, list(legs)),
                       history(symbol, Decimal(base)))


def gap(r: Rehearsal, symbol: str, clock: str) -> None:
    """Turn the leg starting at `clock` (see `gap_leg`) into a gap: its first bar opens at the
    leg's price, so the price jumps there at `clock` and a resting stop fills below itself."""
    start = ny(DAY, clock)
    bars = r.world.today[symbol]
    r.world.today[symbol] = [replace(b, open=b.close, low=min(b.low, b.close))
                             if b.start == start else b for b in bars]


def gap_leg(clock: str, end: str, to: str) -> tuple[str, str, Decimal, Decimal]:
    """The one-minute leg `gap` turns into a gap: the price is `to` from `clock` on."""
    return (clock, end, Decimal(to), HOT)


def strategy_exits(monkeypatch: pytest.MonkeyPatch, rules: dict[str, str]) -> None:
    """price-action-v1 answers EXIT for a held symbol once its last completed bar starts at or
    after the New York time in `rules` (symbol -> "HH:MM"); otherwise its own verdict."""
    original = PriceActionV1.evaluate
    times = {symbol: ny(DAY, clock) for symbol, clock in rules.items()}

    def evaluate(self: PriceActionV1, context: SymbolContext) -> Decision:
        at = times.get(context.symbol)
        if context.position is not None and at is not None and context.bars[-1].start >= at:
            check = Check("rehearsal_exit", None, "is", None, True, "scripted exit")
            return Decision(self.name, self.version, context.symbol, Verdict.EXIT, (check,))
        return original(self, context)

    monkeypatch.setattr(PriceActionV1, "evaluate", evaluate)


def alert_kinds(r: Rehearsal) -> list[str]:
    return [row.body["kind"] for row in r.journal("alert")]


def at(clock: str) -> datetime:
    return ny(DAY, clock)


def fence_names(r: Rehearsal) -> list[str]:
    store = r.store()
    try:
        return [row["name"] for row in store.query("SELECT name FROM fences ORDER BY name")]
    finally:
        store.close()


def clear_fence(r: Rehearsal, monkeypatch: pytest.MonkeyPatch, name: str) -> int:
    """The operator's `tickr clear-fence <name>` between two runs, against the world's
    exchange (the command reconciles before it clears)."""
    monkeypatch.setattr(cli, "BROKER_FACTORY", lambda cfg: r.world.exchange)
    return cli.main(["--config", str(r.bot_toml), "clear-fence", name])


def run_until_crash(r: Rehearsal, clock: str) -> None:
    """`tickr run` until a hook raises Crash (kill -9); the world's pending events are
    dropped (the SIGTERM of `run_until` belongs to the dead process)."""
    try:
        r.run_until(clock)
    except Crash:
        pass
    else:
        raise AssertionError("the scripted crash never happened")
    r.world.events.clear()


def records_agree(r: Rehearsal, exchange: ScriptedExchange, *, flat: bool = True) -> None:
    """Broker and our records agree: every order the broker has is ours with the broker's state
    and filled quantity, the broker was never short, and (`flat`) nothing is open or held."""
    store = r.store()
    try:
        rows = {row["client_order_id"]: row for row in store.query("SELECT * FROM orders")}
    finally:
        store.close()
    for cid, order in exchange.orders.items():
        row = rows[cid]
        assert row["broker_state"] == order.state, cid
        assert Decimal(row["filled_quantity"]) == order.filled_quantity, cid
    assert exchange.short == []
    if flat:
        assert exchange.open_orders() == []
        assert exchange.holdings == {}


def sent(exchange: ScriptedExchange) -> list[tuple[Side, OrderType, Decimal]]:
    return [(o.side, o.order_type, o.quantity) for o in exchange.sent]
