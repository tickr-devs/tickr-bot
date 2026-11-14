"""From an active strategy Decision to the engine, through admission (spec 5, 7, 9).

ENTER: build the admission inputs from what the bot knows (engine fences and trades, calendar,
last broker read, the quote the strategy saw, today's money, request budget), run
`risk.admit`, and only on success call `Engine.open_trade`. EXIT: ask the symbol's open trade to
exit through the normal path. Every decision is recorded, admitted or not, in the same per-day
decision file as the observers (`observers.log.DecisionLog`), so skipped signals can be studied.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from aperture.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
)
from aperture.config import Config, trading_gate
from aperture.engine.core import Engine, EntryRefused, Fence, Leg, TradeRow
from aperture.observers.log import DecisionLog
from aperture.research.records import Admission as RecordAdmission
from aperture.research.records import DecisionRecord
from aperture.risk.admission import AdmissionInputs, AdmissionLimits, Reconciliation, admit
from aperture.runtime.service import trades_today
from aperture.strategy.base import Decision, Role, Verdict
from aperture.types import Quote

ZERO = Decimal(0)
# Minimum price increments of US equities (SEC Regulation NMS Rule 612): one cent at or above
# one dollar, 0.0001 below it. Protocol constants, not tunable numbers.
CENT = Decimal("0.01")
SUB_DOLLAR_TICK = Decimal("0.0001")
ONE_DOLLAR = Decimal(1)
STOP_ORDERS_PER_ENTRY = 1
FOREIGN = (Fence.FOREIGN_POSITION, Fence.FOREIGN_ORDER)
# Fences that are reported through their own admission field, not as unresolved exposure.
NOT_EXPOSURE = (Fence.STARTUP_RECONCILE, Fence.ENTRY_STOP, Fence.BROKER_READ, *FOREIGN)


def round_to_tick(price: Decimal) -> Decimal:
    """Down to the valid increment: a limit never above the window, a stop never above the
    strategy's stop (the planned loss is recomputed from the rounded prices)."""
    tick = CENT if price >= ONE_DOLLAR else SUB_DOLLAR_TICK
    return price.quantize(tick, rounding=ROUND_DOWN)


def entry_cancel_cause(trade: TradeRow, quote: Quote | None, now: datetime,
                       last_entry: datetime | None, config: Config) -> str | None:
    """Why a working entry must be cancelled now (spec 9), or None to let it rest. The window
    is the one admission checked: entry_low <= ask <= limit, with the same freshness rules.
    `last_entry` None means the calendar cannot say: cancel."""
    if last_entry is None:
        return "no trading session known"
    if now >= last_entry:
        return f"last entry time {last_entry.isoformat()}"
    if quote is None:
        return "no quote"
    age = now - quote.provider_time
    if age > timedelta(seconds=float(config.risk.max_quote_age_seconds)):
        return f"quote stale ({age.total_seconds()} s)"
    if -age > timedelta(seconds=float(config.risk.max_clock_skew_seconds)):
        return f"quote from the future ({(-age).total_seconds()} s ahead)"
    if quote.ask > trade.entry_limit_price:
        return f"ask {quote.ask} above the limit {trade.entry_limit_price}"
    if trade.entry_low_price is not None and quote.ask < trade.entry_low_price:
        return f"ask {quote.ask} below the window {trade.entry_low_price}"
    return None


def guard_entries(engine: Engine, config: Config, calendar: SessionCalendar | None,
                  latest: Callable[[str], Quote | None], now: datetime) -> None:
    """Once per loop pass: cancel every working entry whose quote left the price window or
    went stale, and all of them at the last-entry time. Never re-priced (spec 9)."""
    last_entry: datetime | None = None
    if calendar is not None:
        try:
            times = calendar.day_at(now).session_times(
                config.session.last_entry_minutes_before_close,
                config.session.flatten_minutes_before_close)
            last_entry = times.last_entry
        except CalendarRefused:
            last_entry = None
    for trade in engine.trades():
        if trade.exit_requested is not None:
            continue  # the exit teardown cancels the entry itself
        orders = engine.orders(trade.trade_id)
        if not any(o.leg is Leg.ENTRY and o.live for o in orders):
            continue
        cause = entry_cancel_cause(trade, latest(trade.symbol), now, last_entry, config)
        if cause is not None:
            engine.cancel_entry(trade.trade_id, cause)


def realized_loss_today(engine: Engine, now: datetime) -> Decimal | None:
    """Today's net realized loss as a non-negative amount: wins offset losses (owner
    decision 2026-11-14); None if a trade closed today has fills whose prices are unknown."""
    today = now.astimezone(NEW_YORK).date()
    for row in engine.store.query(
            "SELECT trade_id, closed_at FROM trades WHERE closed_at IS NOT NULL"):
        if datetime.fromisoformat(row["closed_at"]).astimezone(NEW_YORK).date() != today:
            continue
        for order in engine.orders(row["trade_id"]):
            if order.filled_quantity > 0 and order.average_fill_price is None:
                return None
    net = sum((t.result_gross_usd for t in trades_today(engine, now)), ZERO)
    return max(-net, ZERO)


def daily_loss_room(engine: Engine, config: Config, now: datetime) -> Decimal | None:
    """Room the daily loss ceiling leaves for one new trade's planned loss: ceiling minus
    today's net realized loss minus open reservations (spec 7, check 6); may be negative.
    None when today's money is unknown."""
    loss = realized_loss_today(engine, now)
    if loss is None:
        return None
    reserved = sum((t.planned_loss_usd for t in engine.trades()), ZERO)
    return config.risk.max_daily_loss_usd - loss - reserved


def last_loss_at(engine: Engine, symbol: str, now: datetime) -> datetime | None:
    """Close time of the latest trade in `symbol` closed today (New York date) at a loss; a
    closed trade with fills whose prices are unknown counts as a loss (the stricter answer)."""
    closes = [t.closed_at for t in trades_today(engine, now)
              if t.symbol == symbol and t.result_gross_usd < 0]
    today = now.astimezone(NEW_YORK).date()
    for row in engine.store.query("SELECT trade_id, closed_at FROM trades "
                                  "WHERE closed_at IS NOT NULL AND symbol = ?", (symbol,)):
        closed = datetime.fromisoformat(row["closed_at"])
        if closed.astimezone(NEW_YORK).date() == today and any(
                o.filled_quantity > 0 and o.average_fill_price is None
                for o in engine.orders(row["trade_id"])):
            closes.append(closed)
    return max(closes, default=None)


class EntryDesk:
    def __init__(self, engine: Engine, config: Config, *, calendar: SessionCalendar | None,
                 last_broker_read_at: Callable[[], datetime | None],
                 log: DecisionLog) -> None:
        self.engine = engine
        self.config = config
        self.calendar = calendar
        self.last_broker_read_at = last_broker_read_at
        self.log = log
        self.limits = AdmissionLimits.from_config(config)

    # --- money -----------------------------------------------------------------------------

    def _reservations(self) -> Decimal:
        return sum((t.planned_loss_usd for t in self.engine.trades()), ZERO)

    # --- admission -------------------------------------------------------------------------

    def _inputs(self, decision: Decision, quote: Quote | None, now: datetime) -> AdmissionInputs:
        engine = self.engine
        fences = [name.split(":", 1)[0] for name in engine.fences()]
        trades = engine.trades()
        own_positions = 0
        working = 0
        for trade in trades:
            orders = engine.orders(trade.trade_id)
            if engine.expected_quantity(orders) > 0:
                own_positions += 1
            working += sum(1 for o in orders if o.live and o.leg is not Leg.STOP)
        foreign_positions = sum(1 for f in fences if f == Fence.FOREIGN_POSITION)
        session: MarketDay | CalendarRefused
        if self.calendar is None:
            session = CalendarRefused(CalendarCause.CALENDAR_INVALID, "no calendar loaded")
        else:
            try:
                session = self.calendar.day_at(now)
            except CalendarRefused as exc:
                session = exc
        limit = None if decision.entry_high is None else round_to_tick(decision.entry_high)
        stop = None if decision.stop is None else round_to_tick(decision.stop)
        last_read = self.last_broker_read_at()
        return AdmissionInputs(
            gate=trading_gate(self.config),
            reconciliation=Reconciliation(
                startup_done=Fence.STARTUP_RECONCILE not in fences,
                last_broker_read_at=None if Fence.BROKER_READ in fences else last_read,
                foreign_exposure=any(f in FOREIGN for f in fences),
                unresolved_exposure=any(f not in NOT_EXPOSURE for f in fences)),
            entry_stop=Fence.ENTRY_STOP in fences,
            session=session,
            now=now,
            own_open_positions=own_positions,
            own_working_orders=working,
            open_positions=own_positions + foreign_positions,
            symbol_last_loss_at=last_loss_at(engine, decision.symbol, now),
            decision=decision,
            limit_price=limit,
            stop_price=stop,
            setup_valid=True,  # the decision was made from the latest bar and this quote
            quote=quote,
            realized_loss_today_usd=realized_loss_today(engine, now),
            open_reservations_usd=self._reservations(),
            entry_budget_available=engine.broker.can_place(1),
            stop_budget_available=engine.broker.can_place(1 + STOP_ORDERS_PER_ENTRY),
        )

    # --- the one entry point ---------------------------------------------------------------

    def on_decision(self, decision: Decision, *, bar_start: datetime, quote: Quote | None,
                    now: datetime) -> DecisionRecord:
        """Act on one decision of the active strategy and record it."""
        admission = RecordAdmission.NOT_APPLICABLE
        cause: str | None = None
        if decision.verdict is Verdict.ENTER:
            inputs = self._inputs(decision, quote, now)
            result = admit(inputs, self.limits)
            if not result.ok:
                admission, cause = RecordAdmission.REFUSED, f"{result.cause}: {result.detail}"
            else:
                assert decision.quantity is not None and result.planned_loss_usd is not None
                assert inputs.limit_price is not None and inputs.stop_price is not None
                try:
                    self.engine.open_trade(decision.symbol, decision.quantity,
                                           inputs.limit_price, inputs.stop_price,
                                           result.planned_loss_usd,
                                           entry_low_price=decision.entry_low)
                    admission = RecordAdmission.TAKEN
                except EntryRefused as exc:
                    admission, cause = RecordAdmission.REFUSED, f"ENGINE_REFUSED: {exc.cause}"
        elif decision.verdict is Verdict.EXIT:
            for trade in self.engine.trades():
                if trade.symbol == decision.symbol and not trade.exit_requested:
                    self.engine.request_exit(trade.trade_id, "strategy-exit")
        record = DecisionRecord(now, bar_start, Role.ACTIVE, decision, admission, cause)
        try:
            self.log.append(record)
        except (OSError, ValueError) as exc:
            # Research data only: never let it stop the loop that protects what was just sent.
            with self.engine.store.transaction():
                self.engine.store.append("decision-record-failed", {
                    "symbol": decision.symbol, "verdict": decision.verdict,
                    "admission": admission, "error": type(exc).__name__,
                    "detail": str(exc)}, run_id=self.engine.run_id)
        return record

