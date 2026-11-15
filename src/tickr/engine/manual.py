"""Manual (stage 3) trades: one protected trade on command, with the [probe] limits.

`plan_probe` and `check_window` refuse instead of guessing. `run_until_closed` drives an
engine until every open trade is closed, asking for an exit when the entry does not fill in
time, when the protected position has been held long enough, at the flatten deadline, or
when the operator interrupts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal

from tickr.calendar import NEW_YORK, CalendarRefused, SessionCalendar
from tickr.config import Config
from tickr.engine.core import OPERATOR_FENCES, Engine, Fence, Leg, TradeState

CENT = Decimal("0.01")
PERCENT = Decimal(100)


class ManualRefused(Exception):
    """The manual trade is not allowed; nothing was sent."""


@dataclass(frozen=True)
class ManualPlan:
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal
    planned_loss_usd: Decimal


def plan_probe(config: Config, symbol: str, limit_price: Decimal,
               stop_price: Decimal | None = None, quantity: Decimal | None = None) -> ManualPlan:
    """Check a manual entry against [probe]. Without --stop the planned stop is
    `stop_percent_below_fill` below the LIMIT (the worst case, used for the planned-loss
    check); the engine then places the stop that percentage below the actual FILL (owner
    decision 2026-11-14), which is never lower than this bound."""
    probe = config.probe
    qty = Decimal(probe.quantity) if quantity is None else quantity
    if qty <= 0 or qty != qty.to_integral_value() or qty > probe.quantity:
        raise ManualRefused(f"quantity must be a whole number from 1 to {probe.quantity}")
    if limit_price <= 0 or limit_price > probe.max_price_usd:
        raise ManualRefused(f"limit price must be above 0 and at most {probe.max_price_usd} USD")
    if limit_price != limit_price.quantize(CENT):
        raise ManualRefused("limit price must be in whole cents")
    if stop_price is None:
        factor = 1 - probe.stop_percent_below_fill / PERCENT
        stop_price = (limit_price * factor).quantize(CENT, rounding=ROUND_DOWN)
    if not 0 < stop_price < limit_price:
        raise ManualRefused("stop must be above 0 and below the limit price")
    planned = (limit_price - stop_price) * qty
    if planned > probe.max_loss_usd:
        raise ManualRefused(f"planned loss {planned} USD exceeds {probe.max_loss_usd} USD")
    return ManualPlan(symbol.upper(), qty, limit_price, stop_price, planned)


def _wall(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def check_window(config: Config, calendar: SessionCalendar, now: datetime) -> datetime:
    """Refuse outside the probe window; return the flatten deadline for today."""
    try:
        day = calendar.day_at(now)
    except CalendarRefused as exc:
        raise ManualRefused(f"no trading session: {exc}") from exc
    if day.half_day and not config.session.half_days_enabled:
        raise ManualRefused("half trading day: entries disabled by config")
    if not day.in_regular_session(now):
        raise ManualRefused("outside the regular session")
    local = now.astimezone(NEW_YORK).time()
    if not _wall(config.probe.window_start) <= local <= _wall(config.probe.window_end):
        raise ManualRefused(
            f"outside the probe window {config.probe.window_start}-{config.probe.window_end} ET")
    times = day.session_times(config.session.last_entry_minutes_before_close,
                              config.session.flatten_minutes_before_close)
    if now >= times.last_entry:
        raise ManualRefused("after the last entry time")
    return times.flatten


def flatten_deadline(config: Config, calendar: SessionCalendar, now: datetime) -> datetime:
    """Today's flatten time; `now` (exit at once) when today has no session or the regular
    session is already over. Used by resume/exit so a recovered trade never stays overnight."""
    try:
        day = calendar.day_at(now)
        times = day.session_times(config.session.last_entry_minutes_before_close,
                                  config.session.flatten_minutes_before_close)
    except CalendarRefused:
        return now
    return now if now >= times.flatten else times.flatten


def close_refusal(config: Config, calendar: SessionCalendar | None,
                  now: datetime) -> str | None:
    """Why a closing MARKET order (DAY, session CORE) may not be sent at `now`, or None.
    It is sent only from the regular open until `[session] close_order_cutoff_seconds`
    before the regular close of that day (the calendar's early close on a half day): what
    Webull does with one sent outside that window is unproven (docs/webull-facts.md), and a
    MARKET order queued for the next open would sell with nobody deciding. No calendar:
    None, as `flatten_deadline` (the caller had no way to know the session either)."""
    if calendar is None:
        return None
    try:
        day = calendar.day_at(now)
    except CalendarRefused as exc:
        return f"no regular session now ({exc})"
    cutoff_seconds = config.session.close_order_cutoff_seconds
    cutoff = day.regular_close - timedelta(seconds=float(cutoff_seconds))
    if now < day.regular_open:
        return f"before the regular open {day.regular_open:%H:%M} ET"
    if now >= cutoff:
        return (f"at or after {cutoff:%H:%M:%S} ET, {cutoff_seconds} s before the regular "
                f"close {day.regular_close:%H:%M} ET of {day.ny_date.isoformat()}")
    return None


# A close the engine will not send again until the operator clears the fence.
CLOSE_WAITS = (Fence.EXIT_REFUSED, Fence.EXIT_NOT_FILLED, Fence.MARKET_CLOSED)


def run_until_closed(engine: Engine, *, sleep: Callable[[float], None],
                     monotonic: Callable[[], float], now: Callable[[], datetime],
                     flatten_at: datetime | None, interrupted: Callable[[], bool],
                     hold_seconds: float | None,
                     notify: Callable[[str], None] = lambda _line: None) -> list[str]:
    """Step (and reconcile every reconcile_interval_seconds) until no trade is open, or every
    open trade is waiting for the operator (its closing order was refused, or did not fill in
    `max_exit_seconds` and ended: the engine has put the stop back). Returns the ids of
    trades left open for the operator.
    `hold_seconds=None` never asks for an exit on its own (except entry timeout, flatten and
    interrupt). Each newly set operator fence, and each new `stop-held` cause, is passed to
    `notify` once, so a trade the engine can neither protect nor close never waits silently."""
    config = engine.config
    poll = float(config.orders.detail_poll_seconds)
    started = monotonic()
    last_reconcile = started
    protected_since: dict[str, float] = {}
    notified: set[tuple[str, str]] = set()
    while True:
        trades = engine.trades()
        if not trades:
            return []
        fences = engine.fences()
        for name, cause in sorted(fences.items()):
            shown = name.partition(":")[0] in OPERATOR_FENCES or name.startswith(
                (f"{Fence.STOP_HELD}:", f"{Fence.DETAIL_UNREADABLE}:"))
            if shown and (name, cause) not in notified:
                notified.add((name, cause))
                notify(f"needs the operator: {name}: {cause}")
        waiting = [t.trade_id for t in trades
                   if any(f"{f}:{t.trade_id}" in fences for f in CLOSE_WAITS)
                   and f"{Fence.UNPROTECTED}:{t.trade_id}" not in fences]
        if len(waiting) == len(trades):
            return waiting
        for trade in trades:
            if trade.exit_requested:
                continue
            entry = next(o for o in engine.orders(trade.trade_id) if o.leg is Leg.ENTRY)
            if interrupted():
                engine.request_exit(trade.trade_id, "operator-interrupt")
            elif flatten_at is not None and now() >= flatten_at:
                engine.request_exit(trade.trade_id, "session-flatten")
            elif (trade.state is TradeState.OPENING and entry.filled_quantity == 0
                  and monotonic() - started >= config.probe.entry_wait_seconds):
                engine.request_exit(trade.trade_id, "entry-not-filled-in-time")
            elif (trade.state is TradeState.OPEN
                  and f"{Fence.UNPROTECTED}:{trade.trade_id}" not in fences
                  and not entry.live):
                since = protected_since.setdefault(trade.trade_id, monotonic())
                if hold_seconds is not None and monotonic() - since >= hold_seconds:
                    engine.request_exit(trade.trade_id, "hold-elapsed")
        sleep(poll)
        if monotonic() - last_reconcile >= float(config.orders.reconcile_interval_seconds):
            engine.reconcile()  # includes a step
            last_reconcile = monotonic()
        else:
            engine.step()
