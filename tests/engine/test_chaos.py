"""Seeded chaos test of Engine + FakeBroker (spec 9, 10, 13; CLAUDE.md hard rules 1-3, 7).

Each seed opens a trade and then, for a number of ticks, randomly mixes engine passes (a step,
or a reconcile every `reconcile_interval_seconds`, as the loop does) with market events
(partial and full fills of entries, exits and the resting stop, broker-side cancels and
expiries, unmapped statuses, stale Detail), scripted send and cancel outcomes, failing reads
(transient Detail, positions and open-orders failures; a non-empty positions body that stays
unreadable),
exit requests, foreign positions and orders, crashes inside broker calls and restarts (a new
Engine on the same Store with a fresh run, then `recover()`, like the runtime's start).

Invariants are checked against the broker's truth after every tick and at every send:
- every client_order_id reaches `place` at most once (no resend after an unknown outcome);
- the bot never sells more than the broker holds: holdings never go short, and own working
  SELL orders (stops plus closes) never exceed the shares held, also at the moment a stop or a
  closing MARKET order is sent (no double sale against a live stop);
- a trade is CLOSED only when the broker is flat in its symbol and none of its orders works;
- an UNKNOWN send keeps its unknown-order fence; foreign positions and orders are fenced;
  while fences are set `open_trade` refuses; with no fence and no open trade the broker is flat
  and no own order works;
- shares the broker holds are covered by a working broker stop or a working close within a
  few quiet ticks, or an entry-blocking fence on the bot's own trade or orders (or a failed
  broker read) explains why; a fence on something else (a foreign position) does not;
- after the market calms down the run ends closed and flat, or holding under a working broker
  stop with entries fenced; and once an operator also clears operator fences (and cancels by
  hand an order the bot gave up cancelling), it ends closed and flat with no fence.
The broker is a FakeBroker subclass; nothing under src/ is changed.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import BrokerError, OutcomeUnknown, RateLimited, Refused
from tickr.broker.fake import FakeBroker
from tickr.config import GateResult
from tickr.engine.core import (
    OPERATOR_FENCES,
    Engine,
    EntryRefused,
    Fence,
    SendState,
    TradeState,
)
from tickr.store import Store
from tickr.types import OrderDetail, OrderRequest, OrderState, OrderType, Side

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
OPEN = GateResult(True, "test")
POLL = float(CFG.orders.detail_poll_seconds)
RECONCILE = float(CFG.orders.reconcile_interval_seconds)
SYMBOL = "ABC"
FOREIGN_SYMBOL = "ZZZ"
LIMIT = D("10.00")
STOP = D("9.50")
CHAOS_TICKS = 45
CALM_TICKS = 40
QUIET_TICKS = 8  # ticks with no market event before held shares must be covered or fenced
SEEDS = range(240)
# Fences that explain a stop the engine cancelled with neither a close nor a new stop after it.
BARE_EXPLAINED = frozenset({Fence.CANCEL_UNKNOWN, "detail-unreadable", Fence.UNPROTECTED,
                            Fence.QUANTITY_MISMATCH})


class Crash(BaseException):
    """The process dies inside a broker call."""


class ReadFailed(BrokerError):
    """A broker read failed in transport."""


class Clock:
    """One clock for the engine and the store's stamps, as in the running bot."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def utc(self) -> datetime:
        return datetime.fromtimestamp(self.now, UTC)


@dataclass
class ChaosBroker(FakeBroker):
    """FakeBroker with failing reads, scripted cancel outcomes, stale or unmapped Detail,
    crashes, and checks made at each send against the broker's own truth."""

    rng: random.Random = field(default_factory=random.Random)
    fail_positions: int = 0  # reads left that fail
    fail_open_orders: int = 0
    detail_fault: float = 0.0  # chance a Detail read fails (transport or budget)
    positions_dead: bool = False  # a non-empty positions body cannot be read, until calm
    unreadable: set[str] = field(default_factory=set)  # orders whose Detail always fails
    cancelled_stop: bool = False  # a cancel took on an unreadable stop while shares were held
    stale: float = 0.0  # chance a Detail read repeats the last answer for that order
    weird: set[str] = field(default_factory=set)  # orders whose Detail shows an unmapped status
    cancel_outcomes: list[str] = field(default_factory=list)
    crash: tuple[str, bool] | None = None  # (method, after the effect)
    seen: dict[str, OrderDetail] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    sent_ids: Counter[str] = field(default_factory=Counter)
    internal: bool = False

    def fill(self, client_order_id: str, quantity: D, price: D) -> None:
        self.internal = True
        try:
            super().fill(client_order_id, quantity, price)
        finally:
            self.internal = False

    def _maybe_crash(self, method: str, after: bool) -> None:
        if self.crash is not None and self.crash[0] in (method, "any") and self.crash[1] == after:
            self.crash = None
            raise Crash(f"{method} {'after' if after else 'before'}")

    def truth(self) -> list[OrderDetail]:
        """Working orders as the broker really has them (no fault, no staleness)."""
        return FakeBroker.open_orders(self)

    def own_selling(self, symbol: str) -> D:
        return sum((o.quantity - o.filled_quantity for o in self.truth()
                    if o.symbol == symbol and o.side is Side.SELL
                    and o.client_order_id.startswith(CFG.orders.client_order_id_prefix)),
                   D(0))

    def held(self, symbol: str) -> D:
        position = self.holdings.get(symbol)
        return D(0) if position is None else position.quantity

    def positions(self):
        self._maybe_crash("positions", False)
        if self.fail_positions:
            self.fail_positions -= 1
            raise ReadFailed("positions")
        if self.positions_dead and super().positions():
            raise ReadFailed("positions unreadable")
        return super().positions()

    def open_orders(self):
        if self.fail_open_orders:
            self.fail_open_orders -= 1
            raise ReadFailed("open orders")
        return super().open_orders()

    def order_detail(self, client_order_id: str) -> OrderDetail:
        if self.internal:  # FakeBroker's own lookups (fill, cancel): the truth
            return super().order_detail(client_order_id)
        if client_order_id in self.unreadable:
            raise ReadFailed("detail unreadable")
        if self.rng.random() < self.detail_fault:
            raise RateLimited("detail") if self.rng.random() < 0.5 else ReadFailed("detail")
        current = super().order_detail(client_order_id)
        if client_order_id in self.seen and self.rng.random() < self.stale:
            current = self.seen[client_order_id]
        self.seen[client_order_id] = current
        if client_order_id in self.weird:
            return replace(current, state=OrderState.UNKNOWN, raw_status="PENDING_CANCEL")
        return current

    def place(self, request: OrderRequest):
        self._maybe_crash("place", False)
        cid = request.client_order_id
        if self.sent_ids[cid]:
            self.violations.append(f"{cid} sent again")
        self.sent_ids[cid] += 1
        if request.side is Side.SELL:
            selling = self.own_selling(request.symbol) + request.quantity
            if selling > self.held(request.symbol):
                self.violations.append(
                    f"{request.order_type} SELL {request.quantity} {cid}: own sells would be "
                    f"{selling} with {self.held(request.symbol)} held")
        if request.side is Side.SELL:
            self.cancelled_stop = False  # a stop or a close follows: nothing left bare
        if request.symbol != SYMBOL:
            self.violations.append(f"order in a symbol the bot never traded: {request.symbol}")
        try:
            return super().place(request)
        finally:
            self._maybe_crash("place", True)

    def cancel(self, client_order_id: str) -> None:
        self._maybe_crash("cancel", False)
        outcome = self.cancel_outcomes.pop(0) if self.cancel_outcomes else "ok"
        if outcome == "rate-limited":
            raise RateLimited("cancel")
        if outcome == "refused":
            raise Refused("CANCEL_REFUSED", "scripted")
        if outcome == "unknown-lost":
            raise OutcomeUnknown("cancel lost")
        if outcome != "ignored":  # "ignored": answered, but the order keeps working
            self.internal = True
            try:
                order = super().order_detail(client_order_id)
                super().cancel(client_order_id)
            finally:
                self.internal = False
            # Only a stop whose Detail the engine cannot read: an unmapped status after a
            # cancel is spec 9's "unknown cancel outcome: send nothing, alert".
            if (order.order_type is OrderType.STOP_LOSS and self.held(order.symbol) > 0
                    and client_order_id in self.unreadable):
                self.cancelled_stop = True
        self._maybe_crash("cancel", True)
        if outcome == "unknown-took":
            raise OutcomeUnknown("cancel reply lost")


class Chaos:
    """One seeded scenario: the engine, the broker, and the invariant checks."""

    def __init__(self, seed: int, *, full: bool = False) -> None:
        self.seed = seed
        # `full` adds persistent unreadable Detail of single orders and the check that a stop
        # the engine cancelled is followed by a close or a new stop, or explained by a fence
        # on this trade (tests/engine/test_bug_detail_unreadable.py: both find known bugs).
        self.full = full
        self.bare_ticks = 0
        self.rng = random.Random(seed)
        self.clock = Clock()
        self.store = Store.open(":memory:", busy_timeout_seconds=0, now=self.clock.utc)
        self.broker = ChaosBroker(rng=random.Random(seed * 7919 + 1))
        self.trace: list[str] = []
        self.last_reconcile = 0.0
        self.closed: set[str] = set()
        self.unquiet = 0  # ticks since the last market event that changed holdings
        self.uncovered_ticks = 0
        self.foreign_orders: dict[str, float] = {}  # id -> when it appeared
        self.last_reconcile_ok: float | None = None
        self.engine = self._boot()

    # --- engine lifecycle ------------------------------------------------------------------

    def _boot(self) -> Engine:
        """A fresh process: new run, recover (which reconciles), like the runtime start."""
        while True:
            run = self.store.start_run(environment="test", config_digest="d", strategy="s",
                                       strategy_version="1", code_version="x")
            engine = Engine(self.store, self.broker, CFG, run, lambda: OPEN, clock=self.clock)
            self.trace.append(f"{self.clock.now:.0f} boot {run[:8]}")
            try:
                engine.recover()
            except Crash as crash:
                self.trace.append(f"crash during recover: {crash}")
                continue
            self.last_reconcile = self.clock.now
            if engine.broker_view is not None:
                self.last_reconcile_ok = self.clock.now
            return engine

    def crashed(self, crash: Crash) -> None:
        self.trace.append(f"CRASH {crash}")
        self.engine = self._boot()

    def guarded(self, action, *args):
        try:
            return action(*args)
        except Crash as crash:
            self.crashed(crash)
            return None

    def tick(self) -> None:
        self.clock.now += POLL
        try:
            if self.clock.now - self.last_reconcile >= RECONCILE:
                ok = self.engine.reconcile()
                self.last_reconcile = self.clock.now
                if ok:
                    self.last_reconcile_ok = self.clock.now
                self.trace.append(f"{self.clock.now:.0f} reconcile ok={ok}")
            else:
                self.engine.step()
                self.trace.append(f"{self.clock.now:.0f} step")
        except Crash as crash:
            self.crashed(crash)

    def open(self) -> None:
        try:
            trade = self.engine.open_trade(SYMBOL, D(self.rng.randint(1, 5)), LIMIT, STOP, D(5))
            self.trace.append(f"open {trade}")
        except EntryRefused as refused:
            self.trace.append(f"open refused: {refused.cause}")
        except Crash as crash:
            self.crashed(crash)

    # --- market and fault events -----------------------------------------------------------

    def own_working(self, leg: str | None = None) -> list[OrderDetail]:
        letter = {"entry": "e", "stop": "s", "exit": "x"}
        out = []
        for o in self.broker.truth():
            if not o.client_order_id.startswith(CFG.orders.client_order_id_prefix):
                continue
            if leg is None or o.client_order_id.rsplit("-", 1)[1].startswith(letter[leg]):
                out.append(o)
        return out

    def fill_some(self, leg: str, price: D) -> None:
        working = self.own_working(leg)
        if not working:
            return
        order = self.rng.choice(working)
        left = order.quantity - order.filled_quantity
        qty = left if self.rng.random() < 0.5 else D(self.rng.randint(1, int(left)))
        self.broker.fill(order.client_order_id, qty, price)
        self.unquiet = 0
        self.trace.append(f"market: fill {qty} of {order.client_order_id} at {price}")

    def chaos_event(self) -> None:
        b, rng = self.broker, self.rng
        kind = rng.choices(
            ["fill-entry", "fill-stop", "fill-exit", "broker-cancel", "place-outcome",
             "cancel-outcome", "read-fail", "detail-fault", "stale", "weird", "exit",
             "foreign-position", "foreign-order", "crash", "restart", "open",
             "positions-dead", "detail-dead"],
            weights=[14, 5, 10, 4, 6, 6, 4, 3, 3, 2, 6, 2, 2, 4, 2, 6, 1,
                     2 if self.full else 0])[0]
        if kind == "fill-entry":
            self.fill_some("entry", LIMIT - D(rng.randint(0, 20)) / 100)
        elif kind == "fill-stop":
            self.fill_some("stop", STOP - D(rng.randint(0, 30)) / 100)
        elif kind == "fill-exit":
            self.fill_some("exit", D(rng.randint(900, 1100)) / 100)
        elif kind == "broker-cancel":
            working = self.own_working()
            if working:
                order = rng.choice(working)
                state, raw = rng.choice([(OrderState.CANCELLED, "CANCELLED"),
                                         (OrderState.EXPIRED, "EXPIRED")])
                b.orders[order.client_order_id] = replace(order, state=state, raw_status=raw)
                self.trace.append(f"market: {raw} {order.client_order_id}")
        elif kind == "place-outcome":
            outcome = rng.choice(["refuse", "unknown", "unknown-lost", "rate-limited"])
            b.script(outcome)
            self.trace.append(f"script place {outcome}")
        elif kind == "cancel-outcome":
            outcome = rng.choice(["rate-limited", "refused", "unknown-lost", "unknown-took",
                                  "ignored"])
            b.cancel_outcomes.append(outcome)
            self.trace.append(f"script cancel {outcome}")
        elif kind == "read-fail":
            if rng.random() < 0.5:
                b.fail_positions += rng.randint(1, 3)
            else:
                b.fail_open_orders += rng.randint(1, 3)
            self.trace.append("reads fail")
        elif kind == "detail-fault":
            b.detail_fault = rng.choice([0.0, 0.3])
        elif kind == "stale":
            b.stale = rng.choice([0.0, 0.5])
        elif kind == "weird":
            working = self.own_working()
            if b.weird:
                b.weird.clear()
            elif working:
                b.weird.add(rng.choice(working).client_order_id)
        elif kind == "exit":
            for trade in self.engine.trades():
                self.engine.request_exit(trade.trade_id, "chaos")
                self.trace.append(f"exit requested {trade.trade_id}")
        elif kind == "foreign-position":
            if FOREIGN_SYMBOL in b.holdings:
                del b.holdings[FOREIGN_SYMBOL]
            else:
                b.add_foreign_position(FOREIGN_SYMBOL, D(3), D(20))
            self.trace.append("foreign position toggled")
        elif kind == "foreign-order":
            self.toggle_foreign_order()
        elif kind == "crash":
            b.crash = (rng.choice(["place", "cancel", "positions", "any"]), rng.random() < 0.5)
            self.trace.append(f"crash armed {b.crash}")
        elif kind == "restart":
            self.engine = self._boot()
        elif kind == "open":
            self.open()
        elif kind == "positions-dead":
            b.positions_dead = not b.positions_dead
            self.trace.append(f"positions unreadable={b.positions_dead}")
        elif kind == "detail-dead":
            working = self.own_working()
            if working:
                cid = rng.choice(working).client_order_id
                b.unreadable.add(cid)
                self.trace.append(f"Detail of {cid} unreadable")

    def toggle_foreign_order(self) -> None:
        if self.foreign_orders:
            for cid in self.foreign_orders:
                del self.broker.orders[cid]
            self.foreign_orders.clear()
            self.trace.append("foreign order gone")
            return
        cid = "manual-1"
        self.broker.orders[cid] = OrderDetail(
            client_order_id=cid, symbol=self.rng.choice([SYMBOL, FOREIGN_SYMBOL]),
            side=Side.BUY, order_type=OrderType.LIMIT, quantity=D(1),
            filled_quantity=D(0), average_fill_price=None, state=OrderState.ACCEPTED,
            raw_status="SUBMITTED")
        self.foreign_orders[cid] = self.clock.now
        self.trace.append("foreign order appeared")

    def calm(self) -> None:
        """No more faults: reads work, sends and cancels are answered, Detail is current."""
        b = self.broker
        b.outcomes.clear()
        b.cancel_outcomes.clear()
        b.fail_positions = b.fail_open_orders = 0
        b.detail_fault = b.stale = 0.0
        b.weird.clear()
        b.unreadable.clear()
        b.positions_dead = False
        b.crash = None
        b.holdings.pop(FOREIGN_SYMBOL, None)
        if self.foreign_orders:
            self.toggle_foreign_order()

    # --- invariants ------------------------------------------------------------------------

    def fail(self, what: str) -> None:
        tail = "\n  ".join(self.trace[-40:])
        pytest.fail(f"seed {self.seed}: {what}\nfences: {self.engine.fences()}\n"
                    f"holdings: {self.broker.holdings}\nlast events:\n  {tail}")

    def check(self) -> None:
        b, e = self.broker, self.engine
        if b.violations:
            self.fail(b.violations[0])
        for symbol, position in b.holdings.items():
            if position.quantity < 0:
                self.fail(f"short {position.quantity} {symbol}")
        held = b.held(SYMBOL)
        if b.own_selling(SYMBOL) > held:
            self.fail(f"own working sells {b.own_selling(SYMBOL)} exceed {held} held")
        fences = e.fences()
        rows = e.orders()
        for row in rows:
            if row.send_state is SendState.UNKNOWN and f"unknown-order:{row.client_order_id}" \
                    not in fences:
                self.fail(f"unknown send {row.client_order_id} without its fence")
        for trade in e.trades(open_only=False):
            if trade.state is not TradeState.CLOSED or trade.trade_id in self.closed:
                continue
            self.closed.add(trade.trade_id)
            mine = {r.client_order_id for r in rows if r.trade_id == trade.trade_id}
            working = [o.client_order_id for o in b.truth() if o.client_order_id in mine]
            if held != 0 or working:
                self.fail(f"{trade.trade_id} closed with {held} held and {working} working")
        positions_read = e._positions is not None  # this pass read positions
        if positions_read and FOREIGN_SYMBOL in b.holdings and \
                f"foreign-position:{FOREIGN_SYMBOL}" not in fences:
            self.fail("foreign position without its fence")
        for cid, since in self.foreign_orders.items():
            if self.last_reconcile_ok is not None and self.last_reconcile_ok > since and \
                    f"foreign-order:{cid}" not in fences:
                self.fail(f"foreign order {cid} without its fence after a reconcile")
        if fences:
            try:
                e.open_trade(SYMBOL, D(1), LIMIT, STOP, D(5))
                self.fail("open_trade accepted while fenced")
            except EntryRefused:
                pass
        elif not e.trades() and positions_read:
            own = self.own_working()
            if b.holdings or own:
                self.fail(f"no fence and no trade, but broker holds {b.holdings}, "
                          f"own working {[o.client_order_id for o in own]}")
        # Shares held must be under a working stop or a working close, or fenced for a reason
        # about this trade (or a failed broker read).
        own = {t.trade_id for t in e.trades(open_only=False)} | {r.client_order_id for r in rows}
        explained = self.explaining(fences, own)
        stops = sum((o.quantity - o.filled_quantity for o in self.own_working("stop")), D(0))
        covered = stops >= held or bool(self.own_working("exit"))
        self.uncovered_ticks = 0 if covered or explained else self.uncovered_ticks + 1
        if self.unquiet >= QUIET_TICKS and self.uncovered_ticks >= QUIET_TICKS:
            self.fail(f"{held} held, stops cover {stops}, no close, no fence for "
                      f"{self.uncovered_ticks} ticks")
        # The engine cancelled a stop on held shares: a close or a new stop must follow
        # (spec 9 teardown; hard rule 2), unless a fence says why it cannot for this trade:
        # spec 9 "Unknown cancel outcome: keep the stop, send nothing, alert" (cancel-unknown),
        # an order whose state cannot be read, a known gap in protection or quantity, or a
        # failed broker read. Any other fence (a foreign position) explains nothing here.
        bare = b.cancelled_stop and held > 0 and not covered and not self.explaining(
            fences, own, BARE_EXPLAINED)
        self.bare_ticks = self.bare_ticks + 1 if bare else 0
        if self.full and self.bare_ticks >= QUIET_TICKS:
            self.fail(f"engine cancelled the stop on {held} held; no close, no new stop for "
                      f"{self.bare_ticks} ticks")

    @staticmethod
    def explaining(fences: dict[str, str], own: set[str],
                   kinds: frozenset[str] | None = None) -> list[str]:
        """Fences that explain held shares of the bot's own trade: a failed broker read, or a
        fence keyed to one of its trades or orders (of `kinds`, when given)."""
        out = []
        for name in fences:
            kind, _, key = name.partition(":")
            if kind == Fence.BROKER_READ or (key in own and (kinds is None or kind in kinds)):
                out.append(name)
        return out

    def final_check(self, *, strict: bool) -> None:
        b, e = self.broker, self.engine
        held = b.held(SYMBOL)
        trades = e.trades()
        fences = e.fences()
        if not trades:
            if held or self.own_working():
                self.fail(f"all trades closed, broker holds {held}")
            if strict and fences:
                self.fail(f"closed and flat, fences remain after the operator looked: {fences}")
            return
        if strict:
            self.fail(f"open after the operator cleared fences: {[t.trade_id for t in trades]}")
        stops = sum((o.quantity - o.filled_quantity for o in self.own_working("stop")
                     if o.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED)), D(0))
        alerted = any(name.split(":", 1)[0] in OPERATOR_FENCES for name in fences)
        if held and stops < held and not alerted:
            # Without a working stop the operator must have been told (an operator fence).
            self.fail(f"calm market: {held} held, working stops cover only {stops}")
        if not fences:
            self.fail(f"calm market: trades {[t.trade_id for t in trades]} still open, no fence")

    # --- the scenario ----------------------------------------------------------------------

    def run(self) -> None:
        self.open()
        self.check()
        for _ in range(CHAOS_TICKS):
            for _ in range(self.rng.choice([0, 1, 1, 2, 3])):
                self.chaos_event()
            self.unquiet += 1
            self.tick()
            self.check()
        self.calm()
        self.trace.append("--- calm")
        for _ in range(CALM_TICKS):
            self.calm_market()
            self.unquiet += 1
            self.tick()
            self.check()
        self.final_check(strict=False)
        self.trace.append("--- operator")
        for _ in range(CALM_TICKS):
            self.operator()
            self.calm_market()
            self.tick()
            self.check()
        self.final_check(strict=True)

    def calm_market(self) -> None:
        """Closing MARKET orders fill; nothing else trades. Exits are requested."""
        for order in self.own_working("exit"):
            self.broker.fill(order.client_order_id, order.quantity - order.filled_quantity,
                             LIMIT)
            self.unquiet = 0
            self.trace.append(f"market: exit {order.client_order_id} filled")
        for trade in self.engine.trades():
            self.guarded(self.engine.request_exit, trade.trade_id, "calm")

    def operator(self) -> None:
        """Clears every operator fence and cancels by hand, in the Webull app, an own order
        the bot gave up cancelling."""
        e = self.engine
        for name in list(e.fences()):
            kind, _, key = name.partition(":")
            if kind in OPERATOR_FENCES:
                e.clear_operator_fence(name)
                self.trace.append(f"operator cleared {name}")
            if kind == "cancel-failed" and key in self.broker.orders:
                order = self.broker.orders[key]
                if not order.state.terminal:
                    self.broker.orders[key] = replace(order, state=OrderState.CANCELLED,
                                                      raw_status="CANCELLED")
                    self.trace.append(f"operator cancelled {key} in the app")


@pytest.mark.parametrize("seed", SEEDS)
def test_chaos(seed):
    Chaos(seed).run()
