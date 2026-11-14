"""Probe F5: move the price of the bot's own resting STOP_LOSS once, and report what Webull shows.

For the manual probe only (docs/manual-trade.md). Tightening only, strictly below a
fresh bid, at most `[probe] replace_max_move_usd`. The intent is journaled and a
`replace-unresolved` operator fence is set BEFORE the request; the request is sent once and
never resent. Afterwards Detail of the stop is polled and open orders are read, and only an
unambiguous result changes our records:

- the same client_order_id working, unfilled, at the new price, and no other working order in
  the symbol: the stop row takes the new price and the fence is cleared;
- the old order terminal and unfilled, and exactly one other working unfilled SELL STOP_LOSS
  in the symbol for the same quantity at the new price, or with no price shown (whether rows
  carry `stop_price` is unproven): that order is adopted as the trade's stop (protection and
  `aperture exit` then track it); the fence stays for the operator;
- anything else: the fence stays and nothing else changes. While it stays, the engine places
  no stop and sends no close for the trade if a working SELL that is not ours rests in the
  symbol (`Engine._replace_blocks`), so the bot never adds a second stop to Webull's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from aperture.broker.base import BrokerError, OrderNotFound, RateLimited, Refused
from aperture.config import Config
from aperture.data.base import MarketData, MarketDataError
from aperture.engine.core import Engine, Leg, OrderRow, TradeState
from aperture.engine.manual import CENT, ManualRefused
from aperture.types import OrderDetail, OrderState, OrderType, Side

Out = Callable[[str], None]


class Verdict(StrEnum):
    NOT_SENT = "not-sent"  # proven unchanged: refused locally, by the budget or the broker
    CONFIRMED = "confirmed"  # same order, working at the new price; fence cleared
    ADOPTED = "adopted"  # a new order carries the new price and is now tracked; fence stays
    UNRESOLVED = "unresolved"  # anything else; fence stays


@dataclass(frozen=True)
class ReplacePlan:
    trade_id: str
    symbol: str
    stop: OrderRow
    current: Decimal
    new: Decimal
    bid: Decimal


def precheck(engine: Engine) -> None:
    """Before recovery: refuse unless one trade is open and no exit is requested, so this
    command never starts a teardown that nothing would finish."""
    trades = engine.trades()
    if len(trades) != 1:
        raise ManualRefused(f"needs exactly one open trade, found {len(trades)}")
    trade = trades[0]
    if trade.state is not TradeState.OPEN or trade.exit_requested:
        raise ManualRefused(f"trade {trade.trade_id} is {trade.state}, exit requested: "
                            f"{trade.exit_requested or 'no'}")


def _detail_line(d: OrderDetail) -> str:
    stop = "not shown" if d.stop_price is None else str(d.stop_price)
    return (f"{d.client_order_id} order_id={d.broker_order_id or 'not shown'} {d.side} "
            f"{d.order_type} status={d.raw_status} filled={d.filled_quantity}/{d.quantity} "
            f"stop_price={stop}")


def _fresh_bid(config: Config, market: MarketData, symbol: str, now: datetime) -> Decimal:
    try:
        quotes = [q for q in market.snapshot([symbol]) if q.symbol == symbol]
    except (BrokerError, MarketDataError) as exc:
        raise ManualRefused(f"quote read failed: {type(exc).__name__}") from exc
    if len(quotes) != 1:
        raise ManualRefused(f"no quote for {symbol}")
    quote = quotes[0]
    age = Decimal(str((now - quote.provider_time).total_seconds()))
    risk = config.risk
    if age > risk.max_quote_age_seconds or age < -risk.max_clock_skew_seconds:
        raise ManualRefused(f"quote is {age} s old (limit {risk.max_quote_age_seconds} s)")
    if quote.bid <= 0:
        raise ManualRefused("no bid")
    return quote.bid


def plan_replace(engine: Engine, market: MarketData, new: Decimal, now: datetime) -> ReplacePlan:
    """Refuse unless the one open trade is held exactly as recorded, protected by one verified
    working stop, and `new` tightens it within the limit and below a fresh bid."""
    fences = engine.fences()
    if fences:
        raise ManualRefused("fences set: " + ", ".join(sorted(fences)))
    precheck(engine)
    trade = engine.trades()[0]
    orders = engine.orders(trade.trade_id)
    if any(o.live for o in orders if o.leg is not Leg.STOP):
        raise ManualRefused("an entry or exit order is still working")
    expected = engine.expected_quantity(orders)
    stops = [o for o in orders if o.leg is Leg.STOP and o.live]
    if len(stops) != 1 or not stops[0].verified or stops[0].cancel_state is not None:
        raise ManualRefused("needs exactly one verified working stop that is not being cancelled")
    stop = stops[0]
    current = stop.stop_price
    if current is None or expected <= 0 or stop.filled_quantity != 0 \
            or stop.quantity != expected:
        raise ManualRefused(f"stop covers {stop.open_quantity}, records hold {expected}")
    if new != new.quantize(CENT):
        raise ManualRefused("new stop price must be in whole cents")
    if new <= current:
        raise ManualRefused(f"only tightening: new stop must be above the current {current}")
    limit = engine.config.probe.replace_max_move_usd
    if new - current > limit:
        raise ManualRefused(f"move {new - current} USD exceeds {limit} USD")

    broker = engine.broker
    try:
        held = {p.symbol: p.quantity for p in broker.positions() if p.quantity != 0}
        detail = broker.order_detail(stop.client_order_id)
    except BrokerError as exc:
        raise ManualRefused(f"broker read failed: {type(exc).__name__}") from exc
    if held != {trade.symbol: expected}:
        raise ManualRefused(f"broker positions {held} differ from records {trade.symbol} "
                            f"{expected}")
    if (detail.state is not OrderState.ACCEPTED or detail.filled_quantity != 0
            or detail.quantity != stop.quantity or detail.order_type is not OrderType.STOP_LOSS
            or detail.side is not Side.SELL):
        raise ManualRefused(f"stop is not working and unfilled at the broker: "
                            f"{_detail_line(detail)}")
    if detail.stop_price is not None and detail.stop_price != current:
        raise ManualRefused(f"Detail shows stop {detail.stop_price}, records say {current}")
    bid = _fresh_bid(engine.config, market, trade.symbol, now)
    if new >= bid:
        raise ManualRefused(f"new stop {new} must be below the bid {bid}")
    return ReplacePlan(trade.trade_id, trade.symbol, stop, current, new, bid)


def send(engine: Engine, plan: ReplacePlan, out: Out) -> bool:
    """Journal, then one request. False when it is proven nothing changed."""
    cid = plan.stop.client_order_id
    engine.replace_intent(plan.stop, plan.new, {"bid": plan.bid})
    try:
        ack = engine.broker.replace_stop_price(cid, plan.stop.quantity, plan.new)
    except (RateLimited, Refused) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        engine.replace_outcome(cid, "not-sent", {"code": code}, unchanged=True)
        out(f"replace not done ({code}): nothing changed")
        return False
    except OrderNotFound:
        engine.replace_outcome(cid, "not-found", {})
        out("replace answer: order not present; reading Detail")
        return True
    except Exception as exc:  # noqa: BLE001 - OutcomeUnknown or anything else: may have changed
        engine.replace_outcome(cid, "unknown", {"error": type(exc).__name__})
        out(f"replace outcome unknown ({type(exc).__name__}): NOT resending; reading Detail")
        return True
    engine.replace_outcome(cid, "acknowledged", {"client_order_id": ack.client_order_id,
                                                 "broker_order_id": ack.broker_order_id})
    out(f"replace answer: success, echoed client_order_id={ack.client_order_id or 'none'} "
        f"order_id={ack.broker_order_id or 'none'}")
    return True


def observe(engine: Engine, plan: ReplacePlan, *, sleep: Callable[[float], None],
            monotonic: Callable[[], float], out: Out) -> Verdict:
    """Poll Detail of the stop for `replace_observe_seconds`, read open orders once, print
    everything, then settle our records only on an unambiguous result."""
    cid = plan.stop.client_order_id
    poll = float(engine.config.orders.detail_poll_seconds)
    started = monotonic()
    last: OrderDetail | None = None
    while True:
        sleep(poll)
        try:
            last = engine.broker.order_detail(cid)
            out(f"Detail: {_detail_line(last)}")
        except OrderNotFound:
            last = None
            out(f"Detail: {cid} not present")
        except BrokerError as exc:
            last = None
            out(f"Detail: read failed ({type(exc).__name__})")
        if monotonic() - started >= engine.config.probe.replace_observe_seconds:
            break
    try:
        rows: list[OrderDetail] | None = [o for o in engine.broker.open_orders()
                                          if o.symbol == plan.symbol]
    except BrokerError as exc:
        rows = None
        out(f"open orders: read failed ({type(exc).__name__})")
    for row in rows or []:
        out(f"open order: {_detail_line(row)}")
    return _settle(engine, plan, last, rows)


def _settle(engine: Engine, plan: ReplacePlan, last: OrderDetail | None,
            rows: list[OrderDetail] | None) -> Verdict:
    stop = plan.stop
    cid = stop.client_order_id
    if last is None or rows is None:
        return Verdict.UNRESOLVED
    others = [o for o in rows if o.client_order_id != cid and not o.state.terminal]
    if (last.state is OrderState.ACCEPTED and last.filled_quantity == 0
            and last.quantity == stop.quantity and last.stop_price == plan.new and not others
            and any(o.client_order_id == cid for o in rows)):
        engine.replace_confirmed(cid, plan.new, {"broker_order_id": last.broker_order_id})
        return Verdict.CONFIRMED
    known = {o.client_order_id for o in engine.orders()}
    if last.state.terminal and last.filled_quantity == 0 and len(others) == 1:
        new = others[0]
        if (new.side is Side.SELL and new.order_type is OrderType.STOP_LOSS
                and new.state is OrderState.ACCEPTED and new.filled_quantity == 0
                and new.quantity == stop.quantity and new.stop_price in (plan.new, None)
                and new.client_order_id not in known):
            engine.adopt_replacement(stop, last, new, plan.new)
            return Verdict.ADOPTED
    return Verdict.UNRESOLVED
