"""From an active strategy Decision to the engine, through admission (spec 5, 7, 9).

ENTER: build the admission inputs from what the bot knows (engine fences and trades, calendar,
last broker read, the quote the strategy saw, today's money, request budget), run
`risk.admit`, and only on success call `Engine.open_trade`. EXIT: ask the symbol's open trade to
exit through the normal path. Every decision is recorded, admitted or not, in the same per-day
decision file as the observers (`observers.log.DecisionLog`), so skipped signals can be studied.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from tickr.api.history import close_window, closed_trades, orders_of, total_net
from tickr.broker.base import BrokerError
from tickr.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
)
from tickr.config import Config, trading_gate
from tickr.data.base import MarketDataError
from tickr.engine.core import (
    STOP_PLACES_PER_FILL,
    Engine,
    EntryRefused,
    Fence,
    Leg,
    OrderRow,
    TradeRow,
)
from tickr.observers.log import DecisionLog
from tickr.research.records import Admission as RecordAdmission
from tickr.research.records import DecisionRecord
from tickr.risk.admission import (
    AdmissionInputs,
    AdmissionLimits,
    BuyingPower,
    Reconciliation,
    admit,
    refusal_before_buying_power,
)
from tickr.risk.causes import ENGINE_REFUSED
from tickr.risk.ticks import round_to_tick
from tickr.strategy.base import Decision, Role, Verdict
from tickr.types import Quote

ZERO = Decimal(0)
# A strategy's partial EXIT the engine did not take (trade not open, entry still working, an
# earlier reduce, a refused close, or not below the held quantity); one row per trade.
REDUCE_REFUSED = "reduce-refused"
# A REST snapshot read for a working entry whose stream quote was too old (`EntryQuotes`).
ENTRY_QUOTE_SNAPSHOT = "entry-quote-snapshot"
M1 = timedelta(minutes=1)
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


class EntryQuotes:
    """The quote a working entry is judged by (`guard_entries`): the stream's newest, or, while
    that is older than `[risk] max_quote_age_seconds`, a REST snapshot, whichever has the
    newer provider time. `refresh` reads the snapshots of every such symbol in one request, at
    most once every `[risk] entry_snapshot_interval_seconds` for all symbols together. A thin
    stock's L1 stream can stay silent for seconds while its book moves (owner 2026-11-17,
    SHAZ: snapshots 1-2 s old during 40-230 s of stream silence). The snapshot quote passes
    the same age and window checks, so a stuck one (OMH: its snapshot time stood still for
    minutes) still cancels the entry. A failed or refused read (a broker error, or a
    market-data refusal such as an INVALID_SYMBOL 417, which refuses the whole batched
    request) leaves the stream quote of every symbol in that read. Each read is journaled
    (`entry-quote-snapshot`, one row per symbol, with the read's duration on the loop clock)."""

    def __init__(self, engine: Engine, config: Config, stream: Callable[[str], Quote | None],
                 snapshot: Callable[[list[str]], list[Quote]] | None,
                 clock: Callable[[], float]) -> None:
        self._engine = engine
        self._stream = stream
        self._snapshot = snapshot
        self._clock = clock
        self._max_age = timedelta(seconds=float(config.risk.max_quote_age_seconds))
        self._interval = float(config.risk.entry_snapshot_interval_seconds)
        self._last_read: float | None = None  # loop clock of the last request
        self._read: dict[str, Quote] = {}

    def __call__(self, symbol: str, now: datetime) -> Quote | None:
        quote = self._stream(symbol)
        if quote is not None and now - quote.provider_time <= self._max_age:
            return quote
        held = self._read.get(symbol)
        if held is None or (quote is not None and quote.provider_time >= held.provider_time):
            return quote
        return held

    def refresh(self, symbols: set[str], now: datetime) -> None:
        """Read in one request the snapshot of every symbol in `symbols` (those with a working
        entry) whose stream quote is too old, at most once per interval for all symbols
        together (so at most 60 / interval requests a minute, whatever the number of entries);
        drop what is held for any other symbol."""
        for symbol in set(self._read) - symbols:
            self._read.pop(symbol, None)
        read = self._snapshot
        if read is None:
            return
        if self._last_read is not None and self._clock() - self._last_read < self._interval:
            return
        streams = {s: self._stream(s) for s in sorted(symbols)}
        due = [s for s, q in streams.items()
               if q is None or now - q.provider_time > self._max_age]
        if not due:
            return
        self._last_read = self._clock()
        error = None
        found: dict[str, Quote] = {}
        started = self._clock()
        try:
            found = {q.symbol: q for q in read(due) if q.symbol in due}
        except (MarketDataError, BrokerError) as exc:
            error = type(exc).__name__
        seconds = self._clock() - started
        self._read.update(found)
        with self._engine.store.transaction():
            for symbol in due:
                stream, snap = streams[symbol], found.get(symbol)
                body: dict[str, object] = {"stream_age": None if stream is None else (
                    now - stream.provider_time).total_seconds(), "read_seconds": seconds}
                if error is not None:
                    body["error"] = error
                if snap is not None:
                    body.update(snapshot_age=(now - snap.provider_time).total_seconds(),
                                bid=str(snap.bid), ask=str(snap.ask))
                self._engine.store.append(ENTRY_QUOTE_SNAPSHOT, body, key=symbol,
                                          run_id=self._engine.run_id)


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
                              "WHERE closed_at >= ? AND closed_at < ? ORDER BY rowid",
                              close_window(today))
    kept = [row for row in rows
            if datetime.fromisoformat(row["closed_at"]).astimezone(NEW_YORK).date() == today
            and (symbol is None or row["symbol"] == symbol)]
    orders = orders_of(engine.store, [row["trade_id"] for row in kept])
    return {row["trade_id"]: (datetime.fromisoformat(row["closed_at"]),
                              orders.get(row["trade_id"], [])) for row in kept}


def _prices_known(orders: list[OrderRow]) -> bool:
    """Every fill's price is recorded, and no order was resolved by the operator (its
    broker rows never agreed with it, so what it filled is not known)."""
    if any(o.operator_resolved_at is not None for o in orders):
        return False
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
    reserved = sum((t.reservation for t in engine.trades()), ZERO)
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


def own_exposure(engine: Engine, symbol: str | None = None) -> tuple[int, int]:
    """Our own open positions and our own working orders other than stops, in `symbol` (as
    admission counts them for a decision in it) or in every symbol (None)."""
    own_positions = 0
    working = 0
    for trade in engine.trades():
        if symbol is not None and trade.symbol != symbol:
            continue
        orders = engine.orders(trade.trade_id)
        if engine.expected_quantity(orders) > 0:
            own_positions += 1
        working += sum(1 for o in orders if o.live and o.leg is not Leg.STOP)
    return own_positions, working


def working_entries(engine: Engine) -> int:
    """Our entries working in any symbol, against `[risk] max_working_entries`."""
    return sum(1 for o in engine.live_orders() if o.leg is Leg.ENTRY)


def stop_requests_owed(engine: Engine) -> int:
    """Place requests the stops may need if every working entry of ours, and the one about to
    be placed, filled in the same poll: each fill observation places up to
    `STOP_PLACES_PER_FILL` stops at once (its own, a split at the scale-out boundary, and one
    piece told apart by quantity on a cheap stock). An entry is admitted only while the place
    budget holds that many after its own request, so entries filling together are never left
    waiting on our own budget for their stops (with 30 places per 60 s: at most 7 entries
    working at once). Not counted: a later piecemeal fill of an entry already observed, closes,
    stop retries and forced exits; they draw on the rest of the window."""
    return STOP_PLACES_PER_FILL * (working_entries(engine) + 1)


def slots_in_use(engine: Engine) -> int:
    """Open own trades in any symbol (a position, a working entry or an exit in progress):
    each takes one of `[risk] max_open_positions`, as `Engine.open_trade` counts them."""
    return len(engine.trades())


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
        # The latest successful balance read: (day buying power, when).
        self._balance: tuple[Decimal, datetime] | None = None
        # When the desk last tried to read the balance, whatever the outcome.
        self._balance_tried_at: datetime | None = None

    # --- money -----------------------------------------------------------------------------

    def _reservations(self) -> Decimal:
        return sum((t.reservation for t in self.engine.trades()), ZERO)

    def _read_balance(self, now: datetime) -> None:
        """Read the broker's balance unless a read was tried less than
        `[risk] buying_power_max_age_seconds` ago, whatever its outcome: a failing read costs
        one request per that age, never one per decision (no retry on the loop thread). A
        failed read keeps the old figure, which admission then finds too old."""
        max_age = timedelta(seconds=float(self.config.risk.buying_power_max_age_seconds))
        tried = self._balance_tried_at
        if tried is not None and timedelta(0) <= now - tried < max_age:
            return
        self._balance_tried_at = now
        try:
            self._balance = (self.engine.broker.balance().day_buying_power, now)
        except BrokerError:
            pass

    def _buying_power(self) -> BuyingPower:
        """The latest balance read's day buying power (no read here), less the entry value of
        our open trades."""
        committed = sum((t.entry_quantity * t.entry_limit_price for t in self.engine.trades()),
                        ZERO)
        if self._balance is None:
            return BuyingPower(None, None, committed)
        return BuyingPower(self._balance[0], self._balance[1], committed)

    # --- admission -------------------------------------------------------------------------

    def _inputs(self, decision: Decision, quote: Quote | None, now: datetime) -> AdmissionInputs:
        engine = self.engine
        fences = [name.split(":", 1)[0] for name in engine.fences()]
        own_positions, working = own_exposure(engine, decision.symbol)
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
            open_positions=slots_in_use(engine),
            working_entries=working_entries(engine),
            symbol_last_loss_at=last_loss_at(engine, decision.symbol, now),
            decision=decision,
            limit_price=limit,
            stop_price=stop,
            setup_valid=True,  # the decision was made from the latest bar and this quote
            quote=quote,
            realized_loss_today_usd=realized_loss_today(engine, now),
            open_reservations_usd=self._reservations(),
            buying_power=self._buying_power(),
            entry_budget_available=engine.broker.can_place(1),
            stop_budget_available=engine.broker.can_place(1 + stop_requests_owed(engine)),
        )

    # --- the one entry point ---------------------------------------------------------------

    def on_decision(self, decision: Decision, *, bar_start: datetime, quote: Quote | None,
                    now: datetime) -> DecisionRecord:
        """Act on one decision of the active strategy and record it."""
        admission = RecordAdmission.NOT_APPLICABLE
        cause: str | None = None
        if decision.verdict is Verdict.ENTER:
            inputs = self._inputs(decision, quote, now)
            # The balance is read only for an entry every other check admits.
            refused = refusal_before_buying_power(inputs, self.limits)
            if refused is None:
                self._read_balance(now)
                inputs = replace(inputs, buying_power=self._buying_power())
            result = refused or admit(inputs, self.limits)
            if not result.ok:
                admission, cause = RecordAdmission.REFUSED, f"{result.cause}: {result.detail}"
            else:
                assert decision.quantity is not None and result.planned_loss_usd is not None
                assert result.window is not None
                assert inputs.limit_price is not None and inputs.stop_price is not None
                try:
                    self.engine.open_trade(decision.symbol, decision.quantity,
                                           inputs.limit_price, inputs.stop_price,
                                           result.planned_loss_usd,
                                           entry_low_price=decision.entry_low,
                                           strategy=(decision.strategy, decision.version),
                                           rest_until=rest_until(decision, bar_start),
                                           scale_out_quantity=decision.scale_out,
                                           window=result.window,
                                           reserved_usd=result.reserved_usd)
                    admission = RecordAdmission.TAKEN
                except EntryRefused as exc:
                    admission, cause = RecordAdmission.REFUSED, f"{ENGINE_REFUSED}: {exc.cause}"
        elif decision.verdict is Verdict.EXIT:
            for trade in self.engine.trades():
                if trade.symbol != decision.symbol or trade.exit_requested:
                    continue
                if decision.quantity is None:
                    self.engine.request_exit(trade.trade_id, "strategy-exit")
                elif (not self.engine.request_reduce(trade.trade_id, decision.quantity,
                                                     "strategy-scale-out")
                      and trade.reduce_state is None
                      and not self.engine.store.journal(REDUCE_REFUSED, trade.trade_id)):
                    # Journaled once per trade: the strategy asks again every bar.
                    with self.engine.store.transaction():
                        self.engine.store.append(REDUCE_REFUSED, {
                            "quantity": decision.quantity, "state": trade.state,
                            "cause": self.engine.reduce_refusal(trade.trade_id,
                                                                decision.quantity)},
                            key=trade.trade_id, run_id=self.engine.run_id)
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

