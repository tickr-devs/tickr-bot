"""Entry admission: the 7 checks of spec section 7, in order.

`admit(inputs, limits)` is pure: no clock, no I/O. All checks must hold; the first failure is
the single recorded cause. A refusal never guesses: missing or unknown values refuse.

Size, notional, planned loss and the reservation are computed on the FINAL order prices
(tick-rounded entry limit and stop that will be sent), never on the strategy's raw prices, so
rounding can never push the real planned loss over a limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tickr.calendar import CalendarRefused, MarketDay, ny_date_of
from tickr.config import Config, GateResult, SessionConfig
from tickr.risk.causes import AdmissionCause
from tickr.risk.daily import check_daily
from tickr.risk.sizing import check_size
from tickr.risk.timers import TradingWindow, WindowKind, trading_windows, window_at
from tickr.strategy.base import Decision, Verdict
from tickr.types import Quote

C = AdmissionCause


@dataclass(frozen=True)
class AdmissionLimits:
    """Numbers admission reads from config (`[risk]`, `[session]`, `[orders]`)."""

    planned_loss_usd: Decimal
    position_notional_cap_usd: Decimal
    max_daily_loss_usd: Decimal
    min_daily_room_usd: Decimal
    """Below this room left under the daily ceiling no new entry is admitted."""
    max_open_positions: int
    max_quote_age_seconds: Decimal
    max_clock_skew_seconds: Decimal
    broker_read_max_age_seconds: Decimal
    session: SessionConfig
    """The day's trading windows and the extended windows' own entry limits (`[session]`)."""
    active_strategy: str
    """Only decisions tagged with this strategy name may be admitted."""
    symbol_cooldown_minutes_after_loss: int | None
    """None = cooldown turned off (`symbol_cooldown_after_loss_enabled = false`)."""
    max_entry_rest_bars: int
    """Longest a resting limit entry (`Decision.rest_bars`) may work, in bars."""
    extended_hours_strategies: frozenset[str]
    """Strategies whose ENTER may be admitted outside the regular session."""

    @classmethod
    def from_config(cls, config: Config) -> AdmissionLimits:
        risk, session = config.risk, config.session
        return cls(
            planned_loss_usd=risk.planned_loss_usd,
            position_notional_cap_usd=risk.position_notional_cap_usd,
            max_daily_loss_usd=risk.max_daily_loss_usd,
            min_daily_room_usd=risk.min_daily_room_usd,
            max_open_positions=risk.max_open_positions,
            max_quote_age_seconds=risk.max_quote_age_seconds,
            max_clock_skew_seconds=risk.max_clock_skew_seconds,
            broker_read_max_age_seconds=config.orders.broker_read_max_age_seconds,
            session=session,
            active_strategy=config.strategy.active,
            symbol_cooldown_minutes_after_loss=(
                risk.symbol_cooldown_minutes_after_loss
                if risk.symbol_cooldown_after_loss_enabled else None),
            max_entry_rest_bars=risk.max_entry_rest_bars,
            extended_hours_strategies=frozenset(config.strategy.extended_hours_strategies),
        )


@dataclass(frozen=True)
class Reconciliation:
    """State of reconciliation with the broker, as last established by the engine."""

    startup_done: bool
    last_broker_read_at: datetime | None
    """Time of the latest successful broker read of positions and open orders (aware)."""
    foreign_exposure: bool
    """A position or open order the bot does not own exists at the broker in the decision's
    symbol, or a foreign order's symbol is not known. Foreign exposure in other symbols is
    ignored (owner 2026-11-16)."""
    unresolved_exposure: bool
    """An own order with unknown outcome, a quantity mismatch, or anything not yet explained."""


@dataclass(frozen=True)
class AdmissionInputs:
    gate: GateResult
    reconciliation: Reconciliation
    entry_stop: bool
    """Operator entry-stop flag (blocks entries only)."""
    session: MarketDay | CalendarRefused
    """Calendar answer for the New York date of `now`."""
    now: datetime
    own_open_positions: int
    """The bot's own positions in the decision's symbol (one trade per symbol at a time)."""
    own_working_orders: int
    """The bot's own working orders other than stops (entries, exits) in the decision's
    symbol."""
    open_positions: int
    """Slots in use against `max_open_positions`: the bot's open trades in any symbol (a
    position, a working entry or an exit in progress), as `Engine.open_trade` counts them.
    Manual trades are ignored (owner 2026-11-16)."""
    symbol_last_loss_at: datetime | None
    """When the latest trade in the decision's symbol that closed today (New York date) at a
    loss, or with an unknown result, closed; None = no such trade today."""
    decision: Decision
    limit_price: Decimal | None
    """Final entry LIMIT price that will be sent (tick-rounded); None = not computed."""
    stop_price: Decimal | None
    """Final protective STOP price that will be sent (tick-rounded); None = not computed."""
    setup_valid: bool
    """The setup is still valid at `now` (the strategy's latest evaluation still says ENTER)."""
    quote: Quote | None
    realized_loss_today_usd: Decimal | None
    """Net realized loss today (wins offset losses) as a non-negative amount; None = unknown."""
    open_reservations_usd: Decimal | None
    """Planned loss reserved by open own trades and working entries; None = unknown."""
    entry_budget_available: bool
    stop_budget_available: bool


@dataclass(frozen=True)
class Admission:
    ok: bool
    cause: AdmissionCause | None
    detail: str
    planned_loss_usd: Decimal | None = None
    """On success: the entry's planned loss (quantity x (limit - stop price))."""
    window: WindowKind | None = None
    """On success: the trading window the entry is in."""
    reserved_usd: Decimal | None = None
    """On success, when it is not the planned loss: what the entry reserves against the daily
    loss ceiling (an extended-hours entry: its whole position value, owner decision B2)."""


def _refuse(cause: AdmissionCause, detail: str) -> Admission:
    return Admission(False, cause, detail)


def _aware(instant: datetime | None) -> bool:
    return (isinstance(instant, datetime) and instant.tzinfo is not None
            and instant.utcoffset() is not None)


def _check_gate(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    if not i.gate.open:
        return _refuse(C.GATE_CLOSED, i.gate.cause)
    return None


def _check_reconciliation(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    if not _aware(i.now):
        return _refuse(C.CLOCK_INVALID, f"now {i.now!r} is not an aware datetime")
    r = i.reconciliation
    if not r.startup_done:
        return _refuse(C.RECONCILIATION_NOT_DONE, "startup reconciliation not done")
    if r.last_broker_read_at is None:
        return _refuse(C.BROKER_READ_MISSING, "no broker read recorded")
    if not _aware(r.last_broker_read_at):
        return _refuse(C.CLOCK_INVALID, "last broker read time is not an aware datetime")
    age = Decimal(str((i.now - r.last_broker_read_at).total_seconds()))
    if age < 0:
        return _refuse(C.CLOCK_INVALID, f"last broker read is {-age} s in the future")
    if age > lim.broker_read_max_age_seconds:
        return _refuse(C.BROKER_READ_STALE,
                       f"last broker read {age} s ago > {lim.broker_read_max_age_seconds} s")
    if r.unresolved_exposure:
        return _refuse(C.UNRESOLVED_EXPOSURE, "unresolved exposure")
    if r.foreign_exposure:
        return _refuse(C.FOREIGN_EXPOSURE,
                       "foreign position or order in this symbol, or of unknown symbol")
    if i.entry_stop:
        return _refuse(C.ENTRY_STOP_SET, "operator entry-stop is set")
    return None


def _check_session(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    return session_refusal(i.session, i.now, lim)


def entry_window(day: object, now: datetime,
                 lim: AdmissionLimits) -> TradingWindow | Admission:
    """Check 3: the enabled trading window that admits an entry at `now`, or the refusal.
    The status document asks the same question to say why entries are refused, so it cannot
    drift from admission. With both extended sessions off (as shipped) only the regular
    window exists, so this is the regular session's check unchanged."""
    if isinstance(day, CalendarRefused):
        return _refuse(C.CALENDAR_UNKNOWN, f"{day.cause}: {day.detail}")
    if not isinstance(day, MarketDay):
        return _refuse(C.CALENDAR_UNKNOWN, f"no calendar answer: {day!r}")
    today = ny_date_of(now)
    if day.ny_date != today:
        return _refuse(C.CALENDAR_DAY_MISMATCH,
                       f"calendar day {day.ny_date} is not today {today} in New York")
    if day.half_day and not lim.session.half_days_enabled:
        return _refuse(C.HALF_DAY, f"{day.ny_date} is a half day; half days disabled")
    try:
        windows = trading_windows(day, lim.session)
    except CalendarRefused as exc:
        return _refuse(C.SESSION_TIMES_INVALID, f"{exc.cause}: {exc.detail}")
    window = window_at(windows, now)
    if window is None:
        # The enabled windows touch end to end, so their span is one interval (the API reads
        # this exact shape).
        return _refuse(C.OUTSIDE_REGULAR_SESSION,
                       f"{now.isoformat()} is outside {windows[0].opens.time()}-"
                       f"{windows[-1].closes.time()} New York")
    if now < window.first_entry:
        return _refuse(C.BEFORE_FIRST_ENTRY,
                       f"{window.kind}: first entry is {window.first_entry.isoformat()}")
    if now >= window.last_entry:
        return _refuse(C.AFTER_LAST_ENTRY, f"last entry was {window.last_entry.isoformat()}")
    return window


def session_refusal(day: object, now: datetime, lim: AdmissionLimits) -> Admission | None:
    """Check 3 on its own: the refusal of `entry_window`, or None."""
    found = entry_window(day, now, lim)
    return found if isinstance(found, Admission) else None


def _window(i: AdmissionInputs, lim: AdmissionLimits) -> TradingWindow:
    """The window check 3 admitted (later checks run only after it passed)."""
    found = entry_window(i.session, i.now, lim)
    assert isinstance(found, TradingWindow)
    return found


def window_limits(window: TradingWindow, lim: AdmissionLimits,
                  ) -> tuple[Decimal, Decimal | None, Decimal]:
    """(maximum quote age in seconds, platform spread ceiling or None, size fraction) of a
    window. The regular window keeps `[risk] max_quote_age_seconds`, no platform spread
    ceiling (the strategy's own rule applies) and full size."""
    s = lim.session
    if window.kind is WindowKind.PRE:
        return (s.premarket_max_quote_age_seconds, s.premarket_max_spread_ratio,
                window.size_fraction)
    if window.kind is WindowKind.POST:
        return (s.postmarket_max_quote_age_seconds, s.postmarket_max_spread_ratio,
                window.size_fraction)
    return lim.max_quote_age_seconds, None, window.size_fraction


def _check_own_state(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    counts = {"own_open_positions": i.own_open_positions,
              "own_working_orders": i.own_working_orders,
              "open_positions": i.open_positions}
    for name, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return _refuse(C.COUNT_INVALID, f"{name} {value!r} is not a count")
    # One trade per symbol: a second trade in a held symbol would share its broker position,
    # and the stop, the exit and reconciliation of each trade are sized per symbol.
    if i.own_open_positions:
        return _refuse(C.OWN_POSITION_OPEN, f"{i.own_open_positions} own position(s) open")
    if i.own_working_orders:
        return _refuse(C.OWN_ORDER_WORKING, f"{i.own_working_orders} own order(s) working")
    if i.open_positions >= lim.max_open_positions:
        return _refuse(C.POSITION_LIMIT_REACHED,
                       f"{i.open_positions} open position(s), limit {lim.max_open_positions}")
    return _check_cooldown(i, lim)


def _check_cooldown(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    """Per-symbol pause after a loss (part of check 4); may be turned off in config."""
    loss_at = i.symbol_last_loss_at
    if lim.symbol_cooldown_minutes_after_loss is not None and loss_at is not None:
        if not _aware(loss_at):
            return _refuse(C.CLOCK_INVALID, "losing trade close time is not an aware datetime")
        until = loss_at + timedelta(minutes=lim.symbol_cooldown_minutes_after_loss)
        if i.now < until:
            return _refuse(C.SYMBOL_COOLDOWN,
                           f"{i.decision.symbol} closed at a loss at {loss_at.isoformat()}; "
                           f"no entry before {until.isoformat()}")
    return None


def _check_quote_and_setup(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    d = i.decision
    if d.verdict is not Verdict.ENTER:
        return _refuse(C.DECISION_NOT_ENTER, f"verdict is {d.verdict}")
    if d.strategy != lim.active_strategy:
        return _refuse(C.DECISION_NOT_ACTIVE,
                       f"decision from {d.strategy} {d.version}, active is {lim.active_strategy}")
    window = _window(i, lim)
    max_age, max_spread, _ = window_limits(window, lim)
    if window.kind is not WindowKind.REGULAR and d.strategy not in lim.extended_hours_strategies:
        return _refuse(C.STRATEGY_NOT_EXTENDED,
                       f"{d.strategy} is not allowed to enter in the {window.kind}")
    missing = [name for name in ("entry_low", "entry_high", "stop", "quantity")
               if getattr(d, name) is None]
    if missing:
        return _refuse(C.DECISION_INCOMPLETE, f"ENTER without {', '.join(missing)}")
    numbers = (d.entry_low, d.entry_high, d.stop, d.quantity)
    if not all(isinstance(n, Decimal) and n.is_finite() for n in numbers):
        return _refuse(C.DECISION_INVALID, f"non-finite or non-Decimal value in {numbers}")
    if not (Decimal(0) < d.stop < d.entry_low <= d.entry_high):
        return _refuse(C.DECISION_INVALID,
                       f"need 0 < stop {d.stop} < entry_low {d.entry_low} <= "
                       f"entry_high {d.entry_high}")
    if d.scale_out is not None and not (
            isinstance(d.scale_out, Decimal) and d.scale_out.is_finite()
            and Decimal(0) < d.scale_out < d.quantity
            and d.scale_out == d.scale_out.to_integral_value()):
        return _refuse(C.DECISION_INVALID,
                       f"scale_out {d.scale_out} not a whole number below quantity {d.quantity}")
    rest = d.rest_bars
    if rest is not None and (isinstance(rest, bool) or not isinstance(rest, int)
                             or not 1 <= rest <= lim.max_entry_rest_bars):
        return _refuse(C.DECISION_INVALID,
                       f"rest_bars {rest!r} not a whole number 1..{lim.max_entry_rest_bars}")
    if i.limit_price is None or i.stop_price is None:
        return _refuse(C.ORDER_PRICES_MISSING, "final limit and stop prices not computed")
    prices = (i.limit_price, i.stop_price)
    if not all(isinstance(n, Decimal) and n.is_finite() for n in prices):
        return _refuse(C.ORDER_PRICES_INVALID, f"non-finite or non-Decimal order price in {prices}")
    if not (d.entry_low <= i.limit_price <= d.entry_high):
        return _refuse(C.ORDER_PRICES_INVALID,
                       f"limit {i.limit_price} outside window {d.entry_low}..{d.entry_high}")
    if not (Decimal(0) < i.stop_price < i.limit_price):
        return _refuse(C.ORDER_PRICES_INVALID,
                       f"need 0 < stop price {i.stop_price} < limit {i.limit_price}")
    if not i.setup_valid:
        return _refuse(C.SETUP_INVALIDATED, "setup no longer valid")
    q = i.quote
    if q is None:
        return _refuse(C.QUOTE_MISSING, "no quote")
    if q.symbol != d.symbol:
        return _refuse(C.QUOTE_SYMBOL_MISMATCH, f"quote {q.symbol} for decision {d.symbol}")
    if not all(isinstance(n, Decimal) and n.is_finite() for n in (q.bid, q.ask)):
        return _refuse(C.QUOTE_INVALID, f"non-finite bid {q.bid} / ask {q.ask}")
    if not (Decimal(0) < q.bid <= q.ask):
        return _refuse(C.QUOTE_INVALID, f"bid {q.bid} / ask {q.ask} not executable")
    if not (_aware(q.provider_time) and _aware(q.receipt_time)):
        return _refuse(C.QUOTE_TIME_INVALID, "quote times must be aware datetimes")
    if q.receipt_time > i.now:
        return _refuse(C.QUOTE_TIME_INVALID, "quote receipt time is after now")
    age = Decimal(str((i.now - q.provider_time).total_seconds()))
    if -age > lim.max_clock_skew_seconds:
        return _refuse(C.QUOTE_TIME_INVALID,
                       f"provider time {-age} s ahead of our clock "
                       f"> {lim.max_clock_skew_seconds} s")
    age = max(age, Decimal(0))
    if age > max_age:
        return _refuse(C.QUOTE_STALE, f"quote {age} s old > {max_age} s ({window.kind})")
    if max_spread is not None:
        spread = (q.ask - q.bid) / ((q.ask + q.bid) / 2)
        if spread > max_spread:
            return _refuse(C.SPREAD_TOO_WIDE,
                           f"spread {spread:.4f} > {max_spread} ({window.kind})")
    if rest is not None:
        # A resting buy waits below the ask for a pullback; it fills at its limit or better,
        # so only an ask already under the window refuses it.
        if q.ask < d.entry_low:
            return _refuse(C.PRICE_OUTSIDE_WINDOW,
                           f"ask {q.ask} below the window {d.entry_low}")
        return None
    if not (d.entry_low <= q.ask <= i.limit_price):
        return _refuse(C.PRICE_OUTSIDE_WINDOW,
                       f"ask {q.ask} outside {d.entry_low}..limit {i.limit_price}")
    return None


def _check_money(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    d = i.decision
    assert d.quantity is not None and i.limit_price is not None and i.stop_price is not None
    # Outside the regular session the platform, not the strategy, sizes down (design 2026-11-17).
    _, _, fraction = window_limits(_window(i, lim), lim)
    size = check_size(d.quantity, i.limit_price, i.stop_price, lim.planned_loss_usd * fraction,
                      lim.position_notional_cap_usd * fraction)
    if not size.ok:
        assert size.cause is not None
        return _refuse(size.cause, size.detail)
    assert size.planned_loss_usd is not None
    reserve = _reservation(i, lim) or size.planned_loss_usd
    daily = check_daily(i.realized_loss_today_usd, i.open_reservations_usd,
                        reserve, lim.max_daily_loss_usd, lim.min_daily_room_usd)
    if not daily.ok:
        assert daily.cause is not None
        return _refuse(daily.cause, daily.detail)
    return None


def _reservation(i: AdmissionInputs, lim: AdmissionLimits) -> Decimal | None:
    """An extended-hours entry reserves its whole position value against the daily ceiling:
    nothing at Webull limits its loss there while the bot cannot act (owner decisions B1, B2,
    2026-11-17). None for a regular entry (it reserves its planned loss)."""
    if _window(i, lim).kind is WindowKind.REGULAR:
        return None
    assert i.decision.quantity is not None and i.limit_price is not None
    return i.decision.quantity * i.limit_price


def _check_budget(i: AdmissionInputs, lim: AdmissionLimits) -> Admission | None:
    if not i.entry_budget_available:
        return _refuse(C.ENTRY_BUDGET_UNAVAILABLE, "no request budget for the entry order")
    if not i.stop_budget_available:
        return _refuse(C.STOP_BUDGET_UNAVAILABLE, "no request budget for the protective stops "
                       "of every working entry and this one")
    return None


CHECKS = (
    _check_gate,
    _check_reconciliation,
    _check_session,
    _check_own_state,
    _check_quote_and_setup,
    _check_money,
    _check_budget,
)
"""Spec section 7 checks 1..7, in order."""


def admit(inputs: AdmissionInputs, limits: AdmissionLimits) -> Admission:
    """Evaluate all checks in order; the first failure is the single cause."""
    for check in CHECKS:
        refused = check(inputs, limits)
        if refused is not None:
            return refused
    d = inputs.decision
    assert d.quantity is not None
    assert inputs.limit_price is not None and inputs.stop_price is not None
    planned_loss = d.quantity * (inputs.limit_price - inputs.stop_price)
    return Admission(True, None, f"admitted {d.quantity} {d.symbol}", planned_loss,
                     _window(inputs, limits).kind, _reservation(inputs, limits))

