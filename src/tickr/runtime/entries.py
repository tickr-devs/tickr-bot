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
from decimal import Decimal

from tickr.api.history import closed_trades, total_net
from tickr.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
)
from tickr.config import Config, trading_gate
from tickr.engine.core import Engine, EntryRefused, Fence, Leg, OrderRow, TradeRow
from tickr.observers.log import DecisionLog
from tickr.research.records import Admission as RecordAdmission
from tickr.research.records import DecisionRecord
from tickr.risk.admission import AdmissionInputs, AdmissionLimits, Reconciliation, admit
from tickr.risk.causes import ENGINE_REFUSED
from tickr.risk.ticks import round_to_tick
from tickr.strategy.base import Decision, Role, Verdict
from tickr.types import Quote

ZERO = Decimal(0)
M1 = timedelta(minutes=1)
STOP_ORDERS_PER_ENTRY = 1
FOREIGN = (Fence.FOREIGN_POSITION, Fence.FOREIGN_ORDER)
# Fences that are reported through their own admission field, not as unresolved exposure.
NOT_EXPOSURE = (Fence.STARTUP_RECONCILE, Fence.ENTRY_STOP, Fence.BROKER_READ, *FOREIGN)


def entry_cancel_cause(trade: TradeRow, quote: Quote | None, now: datetime,
                       last_entry: datetime | None, config: Config) -> str | None:
    """Why a working entry must be cancelled now (spec 9), or None to let it rest. The window
    is the one admission checked: entry_low <= ask <= limit, with the same freshness rules; a
    resting entry (`entry_rest_until` set) may wait with the ask above its limit, until its
    rest time is over. `last_entry` None means the calendar cannot say: cancel."""
    if last_entry is None:
        return "no trading session known"
    if now >= last_entry:
        return f"last entry time {last_entry.isoformat()}"
    resting = trade.entry_rest_until is not None
    if trade.entry_rest_until is not None and now >= trade.entry_rest_until:
        return f"rest time over {trade.entry_rest_until.isoformat()}"
    if quote is None:
        return "no quote"
    age = now - quote.provider_time
    if age > timedelta(seconds=float(config.risk.max_quote_age_seconds)):
        return f"quote stale ({age.total_seconds()} s)"
    if -age > timedelta(seconds=float(config.risk.max_clock_skew_seconds)):
        return f"quote from the future ({(-age).total_seconds()} s ahead)"
    if not resting and quote.ask > trade.entry_limit_price:
        return f"ask {quote.ask} above the limit {trade.entry_limit_price}"
    if trade.entry_low_price is not None and quote.ask < trade.entry_low_price:
        return f"ask {quote.ask} below the window {trade.entry_low_price}"
    return None


def rest_until(decision: Decision, bar_start: datetime) -> datetime | None:
    """End of a resting entry's time: the decision bar closes at bar_start + 1 minute and the
    order then works for `rest_bars` more bars. None for a marketable entry."""
    if decision.rest_bars is None:
        return None
    return bar_start + (decision.rest_bars + 1) * M1


def entry_block_cause(engine: Engine, config: Config, trade: TradeRow,
                      now: datetime) -> str | None:
    """Why a working entry must not stay open for a platform reason: the state in which a new
    entry would be refused (hard rule 1: a conflict blocks new entries, and a fill of an entry
    still working is a new position). The trading gate closed, any fence that blocks an entry
    in this symbol (`Engine.entry_fences`: operator pause, startup reconcile, broker read,
    reconcile disagreement, foreign exposure in this symbol or of unknown symbol, another trade's
    trouble, ...) except the fences about this trade itself or its own orders (the engine
    already acts on those: its stop, its unknown status, its cancel; cancelling the entry for
    them would only add a cancel of an order whose state is the open question), or today's
    money unknown or over the daily loss ceiling with the open reservations. None when
    nothing blocks."""
    gate = engine.gate()
    if not gate.open:
        return f"trading gate closed: {gate.cause}"
    own = {trade.trade_id, *(o.client_order_id for o in engine.orders(trade.trade_id))}
    fences = sorted(name for name in engine.entry_fences(trade.symbol)
                    if name.partition(":")[2] not in own)
    if fences:
        return "entries fenced: " + ", ".join(fences)
    room = daily_loss_room(engine, config, now)
    if room is None:
        return "today's realized result unknown"
    if room < 0:
        return f"daily loss ceiling: {-room} over it with the open reservations"
    return None


def guard_entries(engine: Engine, config: Config, calendar: SessionCalendar | None,
                  latest: Callable[[str], Quote | None], now: datetime) -> None:
    """Once per loop pass: cancel every working entry whose quote left the price window or
    went stale, all of them at the last-entry time, and all of them while a new entry would
    be refused for a platform reason (`entry_block_cause`). Never re-priced (spec 9)."""
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
        cause = (entry_block_cause(engine, config, trade, now)
                 or entry_cancel_cause(trade, latest(trade.symbol), now, last_entry, config))
        if cause is not None:
            engine.cancel_entry(trade.trade_id, cause)


def _closed_today_orders(engine: Engine, now: datetime,
                         symbol: str | None = None) -> dict[str, tuple[datetime, list[OrderRow]]]:
    """Trades closed today (New York date), optionally of one symbol: trade id -> (close
    time, its orders)."""
    today = now.astimezone(NEW_YORK).date()
    rows = engine.store.query("SELECT trade_id, symbol, closed_at FROM trades "
                              "WHERE closed_at IS NOT NULL")
    found = {}
    for row in rows:
        closed = datetime.fromisoformat(row["closed_at"])
        if closed.astimezone(NEW_YORK).date() != today:
            continue
        if symbol is not None and row["symbol"] != symbol:
            continue
        found[row["trade_id"]] = (closed, engine.orders(row["trade_id"]))
    return found


def _prices_known(orders: list[OrderRow]) -> bool:
    return all(o.fill_price is not None for o in orders if o.filled_quantity > 0)


def realized_loss_today(engine: Engine, now: datetime) -> Decimal | None:
    """Today's net realized loss as a non-negative amount: wins offset losses (owner
    decision 2026-11-14), after the broker's fees (owner decision 2026-11-16): each trade's
    `result_net_usd`, the settled cash once proven, else fill prices (exact where recorded)
    less recorded fees, so it matches the broker's day result. None if a trade closed today
    has fills whose prices or fees are unknown."""
    closed = _closed_today_orders(engine, now)
    if not all(_prices_known(orders) for _, orders in closed.values()):
        return None
    net = total_net(closed_trades(engine.store, closed_on=now.astimezone(NEW_YORK).date()))
    return None if net is None else max(-net, ZERO)


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
    """Close time of the latest trade in `symbol` closed today (New York date) at a loss
    after the broker's fees; a closed trade with fills whose prices or fees are unknown
    counts as a loss (the stricter answer)."""
    closed = _closed_today_orders(engine, now, symbol)
    net = {t.trade_id: t.result_net_usd for t in closed_trades(
        engine.store, closed_on=now.astimezone(NEW_YORK).date())}
    closes = []
    for trade_id, (at, orders) in closed.items():
        if not _prices_known(orders):
            closes.append(at)
        elif trade_id in net and (net[trade_id] is None or net[trade_id] < 0):
            closes.append(at)
    return max(closes, default=None)


def own_exposure(engine: Engine) -> tuple[int, int]:
    """Our own open positions and our own working orders other than stops, as admission
    counts them. The status document reads the same counts."""
    own_positions = 0
    working = 0
    for trade in engine.trades():
        orders = engine.orders(trade.trade_id)
        if engine.expected_quantity(orders) > 0:
            own_positions += 1
        working += sum(1 for o in orders if o.live and o.leg is not Leg.STOP)
    return own_positions, working


def session_at(calendar: SessionCalendar | None, now: datetime) -> MarketDay | CalendarRefused:
    """The calendar's answer for `now`, or its refusal, in the form admission takes."""
    if calendar is None:
        return CalendarRefused(CalendarCause.CALENDAR_INVALID, "no calendar loaded")
    try:
        return calendar.day_at(now)
    except CalendarRefused as exc:
        return exc


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
        own_positions, working = own_exposure(engine)
        # Manual trades are ignored (owner 2026-11-16): a foreign position or order blocks only
        # an entry in its own symbol, and does not count toward the position limit. A foreign
        # order whose symbol is not known blocks every entry (refuse, never guess).
        foreign = engine.foreign_symbols()
        session = session_at(self.calendar, now)
        limit = None if decision.entry_high is None else round_to_tick(decision.entry_high)
        stop = None if decision.stop is None else round_to_tick(decision.stop)
        last_read = self.last_broker_read_at()
        return AdmissionInputs(
            gate=trading_gate(self.config),
            reconciliation=Reconciliation(
                startup_done=Fence.STARTUP_RECONCILE not in fences,
                last_broker_read_at=None if Fence.BROKER_READ in fences else last_read,
                foreign_exposure=foreign is None or decision.symbol in foreign,
                unresolved_exposure=any(f not in NOT_EXPOSURE for f in fences)),
            entry_stop=Fence.ENTRY_STOP in fences,
            session=session,
            now=now,
            own_open_positions=own_positions,
            own_working_orders=working,
            open_positions=own_positions,
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
                                           entry_low_price=decision.entry_low,
                                           strategy=(decision.strategy, decision.version),
                                           rest_until=rest_until(decision, bar_start))
                    admission = RecordAdmission.TAKEN
                except EntryRefused as exc:
                    admission, cause = RecordAdmission.REFUSED, f"{ENGINE_REFUSED}: {exc.cause}"
        elif decision.verdict is Verdict.EXIT:
            for trade in self.engine.trades():
                if trade.symbol == decision.symbol and not trade.exit_requested:
                    self.engine.request_exit(trade.trade_id, "strategy-exit")
        return self._write(DecisionRecord(now, bar_start, Role.ACTIVE, decision, admission,
                                          cause))

    def record_late(self, decision: Decision, *, bar_start: datetime,
                    now: datetime) -> DecisionRecord:
        """Record a decision made late on a minute put aside for protection (runtime
        evaluation): never admitted or acted on, so an ENTER is `not_evaluated`."""
        admission = (RecordAdmission.NOT_EVALUATED if decision.verdict is Verdict.ENTER
                     else RecordAdmission.NOT_APPLICABLE)
        return self._write(DecisionRecord(now, bar_start, Role.ACTIVE, decision, admission))

    def _write(self, record: DecisionRecord) -> DecisionRecord:
        decision, admission = record.decision, record.admission
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

