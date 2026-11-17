"""Probe F7: which orders Webull accepts, fills and triggers outside the regular session.

`tickr probe-extended SYMBOL`, run by the owner on the Mac in the pre-market or the post-market
(docs/manual-trade.md, "Extended-hours probe"), with the bot stopped and the account flat. It
answers the facts the extended-hours design waits for (docs/webull-facts.md F7):

1. Acceptance, no position needed: a BUY STOP_LOSS and a BUY STOP_LOSS_LIMIT with session ALL,
   and a BUY STOP_LOSS with session CORE, each `[probe] extended_far_stop_percent` above the
   ask (so none can trigger), are placed once, read back by Detail and cancelled.
2. Fill: a BUY LIMIT DAY ALL for `[probe] quantity` shares at the ask. Unfilled within
   `[probe] entry_wait_seconds`, it is cancelled and the probe ends (the broker does not fill
   outside the regular session, or not at that price).
3. Trigger, holding the shares (the quantity is the broker's position, read before each
   SELL): each stop type accepted in step 1 is placed as a SELL with session ALL one tick
   below the bid and watched for `[probe] extended_trigger_seconds`,
   with a quote read each poll; still working afterwards, it is cancelled. A bid below the stop
   while the stop kept working says it does not trigger in this session.
4. Close: whatever is still held is sold with a LIMIT DAY ALL `[probe] extended_close_percent`
   below the bid, watched for `[orders] max_exit_seconds`. Not filled: the probe says so loudly
   and exits 4; the owner closes it in the Webull app.

Hard rule 2: between the entry fill and the first stop, and after a trigger test that did not
fill, the probe holds `[probe] quantity` share(s) without a broker stop for at most a few
minutes: proving whether a stop works in this session is what the probe is for, and the extended
sessions stay closed to the bot until it has answered. It is the one sender besides the engine
(tests/test_invariants.py names it).

No SELL is sent while an earlier order of the run is not proven finished: an order still working
after its cancel, or never shown by Detail (and not proven absent), stops the run at once, the
close included, and the probe exits 4 with the order ids for the owner.

Hard rule 3 holds: every order is written to the evidence file (flushed to disk) before its one
request, never resent; an unknown outcome is read back by Detail. The probe holds the bot's lock
for its whole run, so the bot cannot start next to it, and its client order ids carry the probe
prefix, so a leftover order blocks the next bot start. Evidence: one JSON line per step in
`<data_dir>/<[probe] evidence_dir>/<NY date>/extended-<UTC time>/steps.jsonl`, and a
`summary.json` with one verdict per question.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from tickr.broker.base import Broker, BrokerError, OrderNotFound, RateLimited, Refused
from tickr.calendar import CalendarRefused, MarketDay, SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError
from tickr.engine.manual import ManualRefused
from tickr.engine.replace_probe import read_quote, stale_cause
from tickr.types import (
    OrderDetail,
    OrderRequest,
    OrderState,
    OrderType,
    Quote,
    Side,
    TimeInForce,
    TradingSession,
    TrailingType,
)

# A US ticker as the probe accepts it (letters, digits, one class dot); protocol shape.
SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,5}(\.[A-Z])?$")
PERCENT = Decimal(100)
CENT = Decimal("0.01")
SUB_DOLLAR_TICK = Decimal("0.0001")
ONE_DOLLAR = Decimal(1)

PRE = "pre-market"
POST = "post-market"

# Verdicts, one per question in summary.json.
ACCEPTED = "accepted"
REFUSED = "refused"
UNKNOWN = "unknown"
NOT_SENT = "not-sent-request-budget"
NOT_VISIBLE = "acknowledged-but-never-shown-by-detail"
FILLED = "filled"
NOT_FILLED = "not-filled"
TRIGGERED = "triggered"
NOT_TRIGGERED_PRICE_PASSED = "not-triggered-although-bid-passed-stop"
NOT_TRIGGERED = "not-triggered-price-never-reached"
NOT_TESTED = "not-tested"
CLOSED = "closed"
NOT_CLOSED = "NOT-CLOSED-close-it-in-the-webull-app"
NOT_SENT_UNRESOLVED = "NOT-SENT-an-order-is-unresolved-close-it-in-the-webull-app"

EXIT_ANSWERED = 0
EXIT_OPERATOR = 4

Out = Callable[[str], None]
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]

# Step 1 cases: (name, order type, session).
ACCEPTANCE_CASES = (
    ("stop-all", OrderType.STOP_LOSS, TradingSession.ALL),
    ("stop-limit-all", OrderType.STOP_LOSS_LIMIT, TradingSession.ALL),
    ("stop-core", OrderType.STOP_LOSS, TradingSession.CORE),
)


def tick(price: Decimal) -> Decimal:
    return CENT if price >= ONE_DOLLAR else SUB_DOLLAR_TICK


def floor_tick(price: Decimal) -> Decimal:
    return price.quantize(tick(price), rounding=ROUND_DOWN)


def session_now(day: MarketDay, now: datetime) -> tuple[str, datetime]:
    """Which extended session `now` is in, and when it ends; refused in any other time."""
    if day.premarket_open <= now < day.regular_open:
        return PRE, day.regular_open
    if day.regular_close <= now < day.extended_close:
        return POST, day.extended_close
    raise ManualRefused("only in the pre-market or the post-market")


def check_time(config: Config, calendar: SessionCalendar, now: datetime) -> tuple[str, datetime]:
    """The session and its end, refused unless the whole probe fits before that end."""
    try:
        day = calendar.day_at(now)
    except CalendarRefused as exc:
        raise ManualRefused(f"no trading session: {exc}") from exc
    if day.half_day and not config.session.half_days_enabled:
        raise ManualRefused("half trading day: disabled by config")
    session, end = session_now(day, now)
    margin = timedelta(minutes=config.probe.extended_end_margin_minutes)
    if now > end - margin:
        raise ManualRefused(f"the {session} ends at {end.isoformat()}: less than "
                            f"{config.probe.extended_end_margin_minutes} min left")
    return session, end


class Evidence:
    """steps.jsonl, each line flushed to disk before the call it describes."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._path = folder / "steps.jsonl"

    def write(self, row: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, default=str, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def summary(self, summary: dict[str, Any]) -> None:
        (self.folder / "summary.json").write_text(
            json.dumps(summary, default=str, indent=2, sort_keys=True) + "\n", "utf-8")


def detail_row(d: OrderDetail) -> dict[str, Any]:
    return {"client_order_id": d.client_order_id, "order_id": d.broker_order_id,
            "side": d.side, "type": d.order_type, "status": d.raw_status, "state": d.state,
            "quantity": d.quantity, "filled": d.filled_quantity,
            "fill_price": d.average_fill_price, "stop_price": d.stop_price,
            "limit_price": d.limit_price, "trailing_stop_step": d.trailing_stop_step}


def quote_row(q: Quote) -> dict[str, Any]:
    return {"bid": q.bid, "ask": q.ask, "bid_size": q.bid_size, "ask_size": q.ask_size,
            "provider_time": q.provider_time.isoformat()}


class Unresolved(Exception):
    """An order of this run is not proven finished (still working after its cancel, or never
    shown by Detail), or the position cannot be read: nothing more is sent, the close
    included, and the operator gets the order ids."""


@dataclass
class Probe:
    config: Config
    broker: Broker
    market: MarketData
    symbol: str
    evidence: Evidence
    now: Clock
    sleep: Sleep
    out: Out
    run_tag: str
    # The session of every order this run builds unless a step names one.
    order_session: TradingSession = TradingSession.ALL
    # Net shares this run's orders filled, as Detail proved them (buys less sells).
    expected: Decimal = Decimal(0)
    _sequence: int = 0

    # -- plumbing ---------------------------------------------------------------------------

    def _cid(self, name: str) -> str:
        self._sequence += 1
        cid = f"{self.config.orders.probe_client_order_id_prefix}x{self.run_tag}{self._sequence}"
        if len(cid) > self.config.orders.client_order_id_max_length:
            raise ManualRefused(f"client order id {cid} is too long")
        return cid

    def _log(self, step: str, **fields: Any) -> None:
        self.evidence.write({"at": self.now().isoformat(), "step": step, **fields})

    def quote(self) -> Quote:
        quote = read_quote(self.market, self.symbol)
        # Outside the regular session quotes change rarely (minutes apart even for active
        # names, 2026-11-16): the probe has its own age limit, not the entry rule's.
        age, stale = stale_cause(self.config, quote, self.now(),
                                 self.config.probe.extended_max_quote_age_seconds)
        self._log("quote", age_seconds=age, stale=stale, **quote_row(quote))
        if stale is not None:
            raise ManualRefused(f"{stale}: the snapshot quote is not live in this session")
        if not 0 < quote.bid <= quote.ask:
            raise ManualRefused(f"no valid quote: bid {quote.bid} ask {quote.ask}")
        return quote

    def _position(self) -> Decimal:
        try:
            held = sum((p.quantity for p in self.broker.positions() if p.symbol == self.symbol),
                       Decimal(0))
        except BrokerError as exc:
            self._log("position-read-failed", error=str(exc))
            raise Unresolved(f"positions cannot be read ({type(exc).__name__})") from exc
        self._log("position", quantity=held, expected=self.expected)
        return held

    def held(self) -> Decimal:
        """The quantity both authenticated reads agree on: the broker's position in the symbol
        must equal what Detail proved this run filled. The positions read may lag a fill, so it
        is repeated for `[probe] extended_position_agree_seconds`; still different, nothing
        more is sent (Unresolved, with both numbers)."""
        held = self._position()
        polls = int(self.config.probe.extended_position_agree_seconds
                    // float(self.config.orders.detail_poll_seconds))
        for _ in range(polls):
            if held == self.expected:
                break
            self.sleep(float(self.config.orders.detail_poll_seconds))
            held = self._position()
        if held != self.expected:
            raise Unresolved(f"the broker holds {held} {self.symbol}, Detail of this run's "
                             f"orders says {self.expected}")
        return held

    def _count(self, row: OrderDetail | None) -> None:
        if row is not None and row.filled_quantity:
            self.expected += row.filled_quantity if row.side is Side.BUY else -row.filled_quantity

    def place(self, name: str, request: OrderRequest) -> str:
        """Record, send once. Returns ACCEPTED, REFUSED, NOT_SENT or UNKNOWN (resolved by
        Detail). A refusal by our request budget or by the broker proves nothing was created."""
        self._log("intent", case=name, client_order_id=request.client_order_id,
                  side=request.side, type=request.order_type, session=request.session,
                  quantity=request.quantity, limit_price=request.limit_price,
                  stop_price=request.stop_price, trailing_stop_step=request.trailing_stop_step)
        try:
            ack = self.broker.place(request)
        except Refused as exc:
            self._log("refused", case=name, code=exc.code, message=str(exc))
            self.out(f"{name}: refused ({exc.code}) {exc}")
            return REFUSED
        except RateLimited as exc:
            self._log("not-sent", case=name, error=str(exc))
            self.out(f"{name}: not sent, our request budget is spent")
            return NOT_SENT
        except BrokerError as exc:  # OutcomeUnknown or anything else: it may exist
            self._log("unknown", case=name, error=str(exc))
            self.out(f"{name}: answer {exc}; reading Detail, never resending")
            return UNKNOWN
        self._log("acknowledged", case=name, order_id=ack.broker_order_id)
        return ACCEPTED

    def detail(self, name: str, cid: str) -> OrderDetail | None | bool:
        """The row, False when Detail says the order is not present, None when the read
        failed."""
        try:
            d = self.broker.order_detail(cid)
        except OrderNotFound:
            self._log("detail", case=name, client_order_id=cid, found=False)
            return False
        except BrokerError as exc:
            self._log("detail-failed", case=name, client_order_id=cid, error=str(exc))
            return None
        self._log("detail", case=name, found=True, **detail_row(d))
        return d

    def settle(self, name: str, cid: str, seconds: float) -> tuple[OrderDetail | None, bool]:
        """Detail every poll for `seconds`, stopping at a terminal row. Returns the last row
        read and whether the order is proven absent (only "not present" answers, at least
        `[orders] unknown_absent_reads` of them in a row)."""
        last: OrderDetail | None = None
        absent = 0
        polls = max(1, int(seconds // float(self.config.orders.detail_poll_seconds)))
        for _ in range(polls):
            self.sleep(float(self.config.orders.detail_poll_seconds))
            row = self.detail(name, cid)
            if row is False:
                absent += 1
                continue
            absent = 0
            if isinstance(row, OrderDetail):
                last = row
            if last is not None and last.state.terminal:
                break
        return last, last is None and absent >= self.config.orders.unknown_absent_reads

    def finish(self, name: str, cid: str, row: OrderDetail | None,
               absent: bool) -> OrderDetail | None:
        """Make sure the order is over: cancel it unless Detail shows it terminal or proves it
        absent, then require that proof. Raises Unresolved otherwise."""
        if absent or (row is not None and row.state.terminal):
            self._count(row)
            return row
        self._log("cancel-intent", case=name, client_order_id=cid)
        try:
            self.broker.cancel(cid)
        except OrderNotFound:
            self._log("cancel-not-found", case=name)
        except BrokerError as exc:
            self._log("cancel-answer", case=name, error=str(exc))
        after, absent = self.settle(name, cid, float(self.config.orders.max_exit_seconds))
        row = after or row
        if absent or (row is not None and row.state.terminal):
            self._count(row)
            return row
        state = "never shown by Detail" if row is None else f"still {row.raw_status}"
        raise Unresolved(f"{name} order {cid} is {state} after its cancel")

    def request(self, cid: str, side: Side, order_type: OrderType, quantity: Decimal,
                *, session: TradingSession | None = None, limit: Decimal | None = None,
                stop: Decimal | None = None,
                trailing_step: Decimal | None = None,
                trailing_type: TrailingType = TrailingType.PERCENTAGE) -> OrderRequest:
        return OrderRequest(client_order_id=cid, symbol=self.symbol, side=side,
                            order_type=order_type, quantity=quantity,
                            time_in_force=TimeInForce.DAY,
                            session=self.order_session if session is None else session,
                            limit_price=limit, stop_price=stop,
                            trailing_stop_step=trailing_step, trailing_type=trailing_type)

    # -- steps ------------------------------------------------------------------------------

    def acceptance(self, quote: Quote) -> dict[str, str]:
        far = floor_tick(quote.ask * (1 + self.config.probe.extended_far_stop_percent / PERCENT))
        limit = floor_tick(far * (1 + self.config.probe.extended_stop_limit_percent / PERCENT))
        quantity = Decimal(self.config.probe.quantity)
        verdicts: dict[str, str] = {}
        for name, order_type, session in ACCEPTANCE_CASES:
            cid = self._cid(name)
            request = self.request(cid, Side.BUY, order_type, quantity, session=session,
                                   stop=far,
                                   limit=limit if order_type is OrderType.STOP_LOSS_LIMIT else None)
            sent = self.place(name, request)
            if sent in (REFUSED, NOT_SENT):
                verdicts[name] = sent
                self.out(f"{name}: {sent}")
                continue
            row, absent = self.settle(name, cid,
                                      float(self.config.probe.extended_observe_seconds))
            if row is None:
                verdicts[name] = REFUSED if absent and sent == UNKNOWN else (
                    UNKNOWN if sent == UNKNOWN else NOT_VISIBLE)
            elif row.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED,
                               OrderState.FILLED):
                verdicts[name] = f"{ACCEPTED} ({row.raw_status})"
            else:
                verdicts[name] = f"{REFUSED} ({row.raw_status})"
            self.out(f"{name}: {verdicts[name]}")
            self.finish(name, cid, row, absent)
        return verdicts

    def entry(self, quote: Quote) -> str:
        cid = self._cid("entry")
        price = floor_tick(quote.ask) if quote.ask == floor_tick(quote.ask) \
            else floor_tick(quote.ask) + tick(quote.ask)
        if price > self.config.probe.max_price_usd:
            raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
        sent = self.place("entry", self.request(cid, Side.BUY, OrderType.LIMIT,
                                                Decimal(self.config.probe.quantity),
                                                limit=price))
        if sent in (REFUSED, NOT_SENT):
            return sent
        row, absent = self.settle("entry", cid, float(self.config.probe.entry_wait_seconds))
        row = self.finish("entry", cid, row, absent)
        verdict = FILLED if row is not None and row.filled_quantity > 0 else NOT_FILLED
        self.out(f"entry (BUY LIMIT ALL at {price}): {verdict}")
        return verdict

    def trigger(self, name: str, order_type: OrderType) -> str:
        held = self.held()
        if held <= 0:
            return NOT_TESTED
        quote = self.quote()
        stop = floor_tick(quote.bid) - tick(quote.bid)
        if stop <= 0:
            raise ManualRefused(f"bid {quote.bid} leaves no room for a stop below it")
        limit = floor_tick(stop * (1 - self.config.probe.extended_stop_limit_percent / PERCENT))
        cid = self._cid(name)
        request = self.request(cid, Side.SELL, order_type, held, stop=stop,
                               limit=limit if order_type is OrderType.STOP_LOSS_LIMIT else None)
        sent = self.place(name, request)
        if sent in (REFUSED, NOT_SENT):
            return sent
        passed = False
        row: OrderDetail | None = None
        absent = 0
        polls = max(1, int(self.config.probe.extended_trigger_seconds
                           // float(self.config.orders.detail_poll_seconds)))
        for _ in range(polls):
            self.sleep(float(self.config.orders.detail_poll_seconds))
            read = self.detail(name, cid)
            absent = absent + 1 if read is False else 0
            if isinstance(read, OrderDetail):
                row = read
            if row is not None and row.state.terminal:
                break
            try:
                now_quote = read_quote(self.market, self.symbol)
                self._log("quote", case=name, **quote_row(now_quote))
                passed = passed or now_quote.bid < stop
            except (ManualRefused, BrokerError, MarketDataError) as exc:
                self._log("quote-failed", case=name, error=str(exc))
        row = self.finish(name, cid, row,
                          row is None and absent >= self.config.orders.unknown_absent_reads)
        if row is not None and row.filled_quantity:
            verdict = f"{TRIGGERED} (fill {row.average_fill_price}, stop {stop})"
        elif row is None:
            verdict = UNKNOWN
        else:
            verdict = NOT_TRIGGERED_PRICE_PASSED if passed else NOT_TRIGGERED
        self.out(f"{name}: {verdict}")
        return verdict

    def close(self) -> str:
        held = self.held()
        if held <= 0:
            return CLOSED
        try:
            quote = self.quote()
        except (ManualRefused, BrokerError, MarketDataError) as exc:
            self._log("close-not-sent", error=str(exc))
            return NOT_CLOSED
        price = floor_tick(quote.bid * (1 - self.config.probe.extended_close_percent / PERCENT))
        cid = self._cid("close")
        request = self.request(cid, Side.SELL, OrderType.LIMIT, held, limit=price)
        if self.place("close", request) in (REFUSED, NOT_SENT):
            return NOT_CLOSED
        row, absent = self.settle("close", cid, float(self.config.orders.max_exit_seconds))
        self.finish("close", cid, row, absent)  # so the shares are free to sell in the app
        return CLOSED if self.held() <= 0 else NOT_CLOSED

    def leftovers(self) -> dict[str, Any]:
        """Broker truth at the end: the symbol's position (which must agree with what Detail
        proved, see `held`) and every working probe order."""
        prefix = self.config.orders.probe_client_order_id_prefix
        try:
            held = self.held()
            agree = True
        except Unresolved as exc:
            self._log("final-disagrees", error=str(exc))
            held, agree = None, False
        try:
            orders = [o.client_order_id for o in self.broker.open_orders()
                      if o.client_order_id.startswith(prefix) and not o.state.terminal]
        except BrokerError as exc:
            self._log("final-read-failed", error=str(exc))
            return {"readable": False, "position": held, "detail_says": self.expected}
        self._log("final", position=held, working_probe_orders=orders)
        return {"readable": agree, "position": held, "detail_says": self.expected,
                "working_probe_orders": orders}


def require_flat(broker: Broker) -> None:
    """Only on a flat account: nothing of ours or anyone's is at stake if the probe fails."""
    try:
        positions = [p for p in broker.positions() if p.quantity != 0]
        orders = [o for o in broker.open_orders() if not o.state.terminal]
    except BrokerError as exc:
        raise ManualRefused(f"broker read failed: {type(exc).__name__}") from exc
    if positions or orders:
        raise ManualRefused(f"the account is not flat: {len(positions)} position(s), "
                            f"{len(orders)} open order(s)")


def run(config: Config, broker: Broker, market: MarketData, symbol: str, folder: Path, *,
        now: Clock, sleep: Sleep, out: Out, session: str, session_end: datetime) -> int:
    """The four steps; returns the exit code. The caller holds the bot's lock. An order not
    proven finished stops the run before any further order, the close included; the final
    broker read always runs and decides the exit code."""
    started = now()
    probe = Probe(config, broker, market, symbol, Evidence(folder), now, sleep, out,
                  run_tag=f"{started.astimezone(UTC):%H%M%S}")
    summary: dict[str, Any] = {"probe": "extended", "environment": config.environment_name,
                               "symbol": symbol, "session": session,
                               "started_at": started.isoformat(),
                               "session_end": session_end.isoformat()}
    unresolved: str | None = None
    try:
        try:
            require_flat(broker)
            quote = probe.quote()
            if quote.ask > config.probe.max_price_usd:
                raise ManualRefused(f"ask {quote.ask} is above [probe] max_price_usd")
            summary["acceptance"] = acceptance = probe.acceptance(quote)
            summary["entry"] = probe.entry(probe.quote())
            for name, order_type, kind in ACCEPTANCE_CASES:
                if kind is not TradingSession.ALL:
                    continue
                key = f"trigger-{name}"
                summary[key] = (probe.trigger(key, order_type)
                                if acceptance[name].startswith(ACCEPTED) else NOT_TESTED)
        except ManualRefused as exc:
            summary["stopped"] = str(exc)
            out(f"stopped: {exc}")
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
    clean = (after["readable"] and after["position"] == 0 and not after["working_probe_orders"])
    if unresolved is not None or summary.get("close") != CLOSED or not clean:
        first = f"{unresolved}; " if unresolved is not None else ""
        out(f"OPERATOR: {first}the broker shows {after}: cancel the probe orders first, then "
            f"sell what is held in the Webull app now")
        return EXIT_OPERATOR
    return EXIT_ANSWERED


def evidence_folder(config: Config, at: datetime, kind: str = "extended") -> Path:
    folder = (config.root / config.run.data_dir / config.probe.evidence_dir
              / ny_date_of(at).isoformat() / f"{kind}-{at.astimezone(UTC):%H%M%S}")
    folder.mkdir(parents=True, exist_ok=False)
    return folder
