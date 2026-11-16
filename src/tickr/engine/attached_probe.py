"""Probe F8: does Webull hold an entry's stop at the broker from the moment the entry fills?

`tickr probe-attached-stop SYMBOL`, run by the owner on the Mac in the regular session inside
the `[probe]` window (docs/manual-trade.md, "Attached-stop probe"), with the bot stopped and the
account flat. A resting LIMIT BUY can fill while the bot is down; a stop sent with the entry,
as one combo order, would protect that fill without the bot. Webull documents combo orders (a
MASTER with attached STOP_LOSS and/or STOP_PROFIT legs, one `client_combo_order_id`) but no
expiring limit: time in force is DAY or GTC only, so a GTD fallback does not exist
(docs/webull-facts.md F8). The probe answers:

1. Shape, nothing can fill: a MASTER LIMIT BUY `[probe] attached_far_percent` below the bid with
   a STOP_LOSS SELL leg below it ("stop-only"), then the documented bracket ("bracket": the same
   plus a STOP_PROFIT LIMIT SELL that far above the ask). Each leg is read by Detail for
   `[probe] attached_observe_seconds` (does an attached leg show before its MASTER fills, with
   which status), then only the MASTER is cancelled: do its legs end with it?
2. Fill, with the first shape the broker took: a MASTER LIMIT BUY at the ask with a STOP_LOSS
   leg `[probe] stop_percent_below_fill` below it (planned loss within `[probe] max_loss_usd`).
   Filled within `[probe] entry_wait_seconds`: the stop leg is read until it is working for the
   filled quantity and listed in the open orders, for at most `[probe] attached_stop_wait_seconds`.
   That time is the answer: the gap in which a fill is unprotected.
3. Close: every leg still working is cancelled and proven finished, then what is held is sold
   with a LIMIT DAY `[probe] extended_close_percent` below the bid (the F7 probe's close).

Hard rule 2: the probe holds `[probe] quantity` share(s) without a broker stop only when the
attached stop never shows working (the answer is then "no") and between the stop's cancel and
the close, for at most a few seconds. Whether an attached SELL leg works before its MASTER fills
is one of the questions: if it did and triggered, the account would be short a share; the final
read then exits 4 and the owner buys it back in the Webull app. It is the one sender of combo
orders (tests/ test_invariants.py names it). Hard rule 3 and the unresolved-order rule are the
F7 probe's (engine/session_probe.py, whose plumbing this reuses): every order is in the evidence
file before its one request, never resent; an order not proven finished stops the run before any
further order, the close included, and the probe exits 4 with the order ids.
Evidence:
`<data_dir>/<[probe] evidence_dir>/<NY date>/attached-<UTC time>/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.broker.base import Broker, BrokerError, RateLimited, Refused
from tickr.config import Config
from tickr.data.base import MarketData
from tickr.engine.manual import ManualRefused
from tickr.engine.replace_probe import read_quote, stale_cause
from tickr.engine.session_probe import (
    ACCEPTED,
    CLOSED,
    EXIT_ANSWERED,
    EXIT_OPERATOR,
    FILLED,
    NOT_CLOSED,
    NOT_FILLED,
    NOT_SENT,
    NOT_SENT_UNRESOLVED,
    NOT_TESTED,
    NOT_VISIBLE,
    PERCENT,
    REFUSED,
    UNKNOWN,
    Clock,
    Evidence,
    Out,
    Probe,
    Sleep,
    Unresolved,
    detail_row,
    floor_tick,
    quote_row,
    require_flat,
    tick,
)
from tickr.types import (
    ComboRole,
    OrderDetail,
    OrderRequest,
    OrderState,
    OrderType,
    Quote,
    Side,
    TradingSession,
)

# Step 1 shapes: (name, attached leg roles). The first the broker takes is used for step 2.
SHAPES = (
    ("stop-only", (ComboRole.STOP_LOSS,)),
    ("bracket", (ComboRole.STOP_PROFIT, ComboRole.STOP_LOSS)),
)

# Verdicts beyond the F7 probe's.
LEG_ENDED_WITH_MASTER = "ended-with-master"
LEG_OUTLIVED_MASTER = "still-working-after-master-cancel"
LEG_FILLED = "filled"
LEG_WORKING_AFTER_FILL = "working-after-the-fill (cancelled before the close)"
STOP_WORKING = "working"
STOP_NOT_WORKING = "NOT-working-after-the-fill"
LISTED = "listed-in-open-orders"
NOT_LISTED = "not-listed-in-open-orders"

Legs = list[tuple[ComboRole, OrderRequest]]


def ceil_tick(price: Decimal) -> Decimal:
    floor = floor_tick(price)
    return floor if floor == price else floor + tick(price)


def leg_verdict(row: OrderDetail | None, absent: bool) -> str:
    if row is None:
        return "absent" if absent else NOT_VISIBLE
    return f"{row.state.value} ({row.raw_status}, quantity {row.quantity})"


@dataclass
class AttachedProbe(Probe):
    order_session: TradingSession = TradingSession.CORE

    def legs(self, roles: tuple[ComboRole, ...], entry: Decimal, stop: Decimal,
             profit: Decimal) -> Legs:
        quantity = Decimal(self.config.probe.quantity)
        legs: Legs = [(ComboRole.MASTER, self.request(self._cid("master"), Side.BUY,
                                                      OrderType.LIMIT, quantity, limit=entry))]
        for role in roles:
            if role is ComboRole.STOP_LOSS:
                legs.append((role, self.request(self._cid("stop"), Side.SELL,
                                                OrderType.STOP_LOSS, quantity, stop=stop)))
            else:
                legs.append((role, self.request(self._cid("profit"), Side.SELL,
                                                OrderType.LIMIT, quantity, limit=profit)))
        return legs

    def place_combo(self, name: str, legs: Legs) -> str:
        """Record every leg, send the combo once. ACCEPTED, REFUSED, NOT_SENT or UNKNOWN."""
        combo_id = self._cid("combo")
        self._log("combo-intent", case=name, combo_id=combo_id,
                  legs=[{"role": role, "client_order_id": r.client_order_id, "side": r.side,
                         "type": r.order_type, "session": r.session, "quantity": r.quantity,
                         "limit_price": r.limit_price, "stop_price": r.stop_price}
                        for role, r in legs])
        try:
            ack = self.broker.place_combo(combo_id, legs)
        except Refused as exc:
            self._log("refused", case=name, code=exc.code, message=str(exc))
            self.out(f"{name}: refused ({exc.code}) {exc}")
            return REFUSED
        except RateLimited as exc:
            self._log("not-sent", case=name, error=str(exc))
            return NOT_SENT
        except BrokerError as exc:  # OutcomeUnknown or anything else: legs may exist
            self._log("unknown", case=name, error=str(exc))
            self.out(f"{name}: answer {exc}; reading Detail, never resending")
            return UNKNOWN
        self._log("acknowledged", case=name, order_id=ack.broker_order_id,
                  client_order_id=ack.client_order_id)
        return ACCEPTED

    def leg_name(self, name: str, role: ComboRole) -> str:
        return f"{name}-{role.value.lower()}"

    def end_attached(self, name: str, legs: Legs,
                     alive: str = LEG_OUTLIVED_MASTER) -> dict[str, str]:
        """After the MASTER is finished: each attached leg either ended with it or is
        cancelled here and proven finished (Unresolved otherwise); `alive` names a leg that
        was still working. Absence by a leg's own id proves nothing while the broker may keep
        legs under ids of its own, so the open orders must then list nothing working
        (`require_no_open_orders`) before any verdict stands."""
        verdicts: dict[str, str] = {}
        for role, request in legs[1:]:
            leg = self.leg_name(name, role)
            row, absent = self.settle(leg, request.client_order_id,
                                      float(self.config.probe.attached_observe_seconds))
            if row is not None and row.state is OrderState.FILLED:
                verdicts[leg] = LEG_FILLED
            elif absent or (row is not None and row.state.terminal):
                verdicts[leg] = LEG_ENDED_WITH_MASTER
            else:
                verdicts[leg] = alive
            self.finish(leg, request.client_order_id, row, absent)
        self.require_no_open_orders(f"{name}-after-legs")
        return verdicts

    def open_orders_read(self, case: str) -> list[OrderDetail]:
        """Every open order the broker lists that is not terminal, whatever its id (the
        account was proven flat with none at the start), recorded with its row. Unresolved
        when the read fails: nothing is proven then."""
        try:
            orders = self.broker.open_orders()
        except BrokerError as exc:
            self._log("open-orders-failed", case=case, error=str(exc))
            raise Unresolved(f"open orders cannot be read ({type(exc).__name__})") from exc
        working = [o for o in orders if not o.state.terminal]
        self._log("open-orders", case=case,
                  orders=[{"symbol": o.symbol, **detail_row(o)} for o in working])
        return working

    def require_no_open_orders(self, case: str) -> None:
        working = self.open_orders_read(case)
        if working:
            raise Unresolved(f"the broker still lists working orders after {case}: "
                             + ", ".join(f"{o.client_order_id} {o.side} {o.order_type} "
                                         f"{o.raw_status}" for o in working))

    def quote(self) -> Quote:
        """The regular session's rule (`[risk] max_quote_age_seconds`), not the F7 probe's
        extended-hours limit: in the regular session a quote minutes old means a fault."""
        quote = read_quote(self.market, self.symbol)
        age, stale = stale_cause(self.config, quote, self.now())
        self._log("quote", age_seconds=age, stale=stale, **quote_row(quote))
        if stale is not None:
            raise ManualRefused(f"{stale}: the snapshot quote is not fresh")
        if not 0 < quote.bid <= quote.ask:
            raise ManualRefused(f"no valid quote: bid {quote.bid} ask {quote.ask}")
        return quote

    def leftovers(self) -> dict[str, Any]:
        """The F7 probe's final read, with every working open order counted whatever its id
        (combo legs may be kept under broker ids)."""
        after = super().leftovers()
        try:
            working = self.open_orders_read("final-any-id")
        except Unresolved:
            return {**after, "readable": False}
        return {**after, "working_probe_orders": [
            f"{o.client_order_id} {o.symbol} {o.side} {o.order_type} {o.raw_status}"
            for o in working]}

    def shape(self, name: str, roles: tuple[ComboRole, ...], quote: Quote) -> dict[str, str]:
        far = self.config.probe.attached_far_percent / PERCENT
        entry = floor_tick(quote.bid * (1 - far))
        stop = floor_tick(entry * (1 - self.config.probe.stop_percent_below_fill / PERCENT))
        legs = self.legs(roles, entry, stop, ceil_tick(quote.ask * (1 + far)))
        sent = self.place_combo(name, legs)
        verdicts = {"combo": sent}
        if sent in (REFUSED, NOT_SENT):
            return verdicts
        seen: dict[str, tuple[OrderDetail | None, bool]] = {}
        for role, request in legs:
            leg = self.leg_name(name, role)
            seen[leg] = self.settle(leg, request.client_order_id,
                                    float(self.config.probe.attached_observe_seconds))
            verdicts[leg] = leg_verdict(*seen[leg])
        master = self.leg_name(name, ComboRole.MASTER)
        row, absent = seen[master]
        if row is None:
            verdicts["combo"] = REFUSED if absent and sent == UNKNOWN else NOT_VISIBLE
        elif row.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED, OrderState.FILLED):
            verdicts["combo"] = ACCEPTED
        else:
            verdicts["combo"] = f"{REFUSED} ({row.raw_status})"
        self.finish(master, legs[0][1].client_order_id, row, absent)
        verdicts.update(self.end_attached(name, legs))
        self.out(f"{name}: {verdicts}")
        return verdicts

    def stop_after_fill(self, stop: OrderRequest, filled: Decimal) -> dict[str, Any]:
        """Read the stop leg once per poll until it is working for `filled` shares and listed
        in the open orders; the seconds it took, or the verdict that it never was."""
        started = self.now()
        polls = max(1, int(self.config.probe.attached_stop_wait_seconds
                           // float(self.config.orders.detail_poll_seconds)))
        row: OrderDetail | None = None
        listed = False
        for _ in range(polls + 1):
            read = self.detail("fill-stop", stop.client_order_id)
            if isinstance(read, OrderDetail):
                row = read
            try:
                listed = any(o.client_order_id == stop.client_order_id
                             for o in self.open_orders_read("fill-stop"))
            except Unresolved:
                listed = False
            working = (row is not None and row.state is OrderState.ACCEPTED
                       and row.quantity - row.filled_quantity >= filled)
            if working and listed:
                return {"stop": STOP_WORKING, "open_orders": LISTED,
                        "seconds_after_fill_seen": (self.now() - started).total_seconds(),
                        "detail": leg_verdict(row, False)}
            self.sleep(float(self.config.orders.detail_poll_seconds))
        return {"stop": STOP_NOT_WORKING, "open_orders": LISTED if listed else NOT_LISTED,
                "waited_seconds": (self.now() - started).total_seconds(),
                "detail": leg_verdict(row, False)}

    def fill(self, name: str, roles: tuple[ComboRole, ...]) -> dict[str, Any]:
        quote = self.quote()
        entry = ceil_tick(quote.ask)
        stop = floor_tick(entry * (1 - self.config.probe.stop_percent_below_fill / PERCENT))
        quantity = Decimal(self.config.probe.quantity)
        if entry > self.config.probe.max_price_usd:
            raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
        if (entry - stop) * quantity > self.config.probe.max_loss_usd:
            raise ManualRefused("the planned loss is above [probe] max_loss_usd")
        far = self.config.probe.attached_far_percent / PERCENT
        legs = self.legs(roles, entry, stop, ceil_tick(quote.ask * (1 + far)))
        result: dict[str, Any] = {"shape": name, "combo": self.place_combo(f"fill-{name}", legs)}
        if result["combo"] in (REFUSED, NOT_SENT):
            return result
        master = legs[0][1].client_order_id
        row, absent = self.settle("fill-master", master,
                                  float(self.config.probe.entry_wait_seconds))
        if row is not None and row.filled_quantity > 0:
            result["entry"] = FILLED
            result["after_fill"] = self.stop_after_fill(
                next(r for role, r in legs if role is ComboRole.STOP_LOSS), row.filled_quantity)
        else:
            result["entry"] = NOT_FILLED
        row = self.finish("fill-master", master, row, absent)
        result["entry_detail"] = leg_verdict(row, absent)
        result["legs_after_master"] = self.end_attached(
            "fill", legs, LEG_WORKING_AFTER_FILL if result["entry"] == FILLED
            else LEG_OUTLIVED_MASTER)
        self.out(f"fill ({name}, BUY LIMIT {entry}, stop {stop}): {result}")
        return result


def run(config: Config, broker: Broker, market: MarketData, symbol: str, folder: Path, *,
        now: Clock, sleep: Sleep, out: Out) -> int:
    """The three steps; returns the exit code. The caller holds the bot's lock and checked the
    window. An order not proven finished stops the run before any further order, the close
    included; the final broker read always runs and decides the exit code."""
    started = now()
    probe = AttachedProbe(config, broker, market, symbol, Evidence(folder), now, sleep, out,
                          run_tag=f"{started.astimezone(UTC):%H%M%S}")
    summary: dict[str, Any] = {"probe": "attached-stop", "environment": config.environment_name,
                               "symbol": symbol, "started_at": started.isoformat()}
    unresolved: str | None = None
    try:
        try:
            require_flat(broker)
            quote = probe.quote()
            if quote.ask > config.probe.max_price_usd:
                raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
            shapes: dict[str, dict[str, str]] = {}
            summary["shapes"] = shapes
            for name, roles in SHAPES:
                shapes[name] = probe.shape(name, roles, probe.quote())
            taken = [(name, roles) for name, roles in SHAPES if shapes[name]["combo"] == ACCEPTED]
            summary["fill"] = probe.fill(*taken[0]) if taken else NOT_TESTED
        except ManualRefused as exc:
            summary["stopped"] = str(exc)
            out(f"stopped: {exc}")
        probe.require_no_open_orders("before-close")  # an unseen stop would hold the share
        summary["close"] = probe.close()
    except Unresolved as exc:
        unresolved = str(exc)
        summary["unresolved"] = unresolved
        summary.setdefault("close", NOT_SENT_UNRESOLVED)
        out(f"stopped, nothing more sent: {exc}")
    except BrokerError as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary.setdefault("close", NOT_CLOSED)
    finally:
        summary["broker_after"] = after = probe.leftovers()
        summary["finished_at"] = now().isoformat()
        probe.evidence.summary(summary)
    clean = after["readable"] and after["position"] == 0 and not after["working_probe_orders"]
    if unresolved is not None or summary.get("close") != CLOSED or not clean:
        first = f"{unresolved}; " if unresolved is not None else ""
        out(f"OPERATOR: {first}the broker shows {after}: cancel the probe orders first, then "
            f"sell what is held in the Webull app now")
        return EXIT_OPERATOR
    return EXIT_ANSWERED
