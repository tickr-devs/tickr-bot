"""Last-entry and flatten instants of a trading day, from the calendar and `[session]` offsets,
and the day's trading windows: the regular session plus the pre-market and post-market when
`[session]` enables them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from tickr.calendar import CalendarCause, CalendarRefused, MarketDay, SessionCalendar, SessionTimes
from tickr.config import SessionConfig
from tickr.types import TradingSession


def deadlines(day: MarketDay, last_entry_minutes_before_close: int,
              flatten_minutes_before_close: int) -> SessionTimes:
    """Last-entry and flatten times of `day`; raises CalendarRefused on invalid offsets."""
    return day.session_times(last_entry_minutes_before_close, flatten_minutes_before_close)


def deadlines_for(calendar: SessionCalendar, ny_date: date, session: SessionConfig) -> SessionTimes:
    """Deadlines for a New York date; raises CalendarRefused for unknown or closed days."""
    return deadlines(calendar.day(ny_date), session.last_entry_minutes_before_close,
                     session.flatten_minutes_before_close)


class WindowKind(StrEnum):
    PRE = "pre-market"
    REGULAR = "regular"
    POST = "post-market"


@dataclass(frozen=True)
class TradingWindow:
    """One session the bot trades in. Entries only in [first_entry, last_entry); everything of
    ours closed from `flatten`; nothing may be left at `closes` (each window flattens itself:
    no position is carried into the next session or overnight). `order_session` is what our
    orders carry (Webull `support_trading_session`); `size_fraction` scales the planned loss
    and the notional cap of an entry in this window."""

    kind: WindowKind
    opens: datetime
    first_entry: datetime
    last_entry: datetime
    flatten: datetime
    closes: datetime
    order_session: TradingSession
    size_fraction: Decimal

    def contains(self, instant: datetime) -> bool:
        return self.opens <= instant < self.closes


def _window(kind: WindowKind, opens: datetime, closes: datetime, first_after: int,
            last_before: int, flatten_before: int, order_session: TradingSession,
            size_fraction: Decimal) -> TradingWindow:
    for name, minutes in (("first entry", first_after), ("last entry", last_before),
                          ("flatten", flatten_before)):
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
            raise CalendarRefused(CalendarCause.INVALID_OFFSET,
                                  f"{kind} {name}: minutes must be a non-negative int, "
                                  f"got {minutes!r}")
    if not 0 < size_fraction <= 1:
        raise CalendarRefused(CalendarCause.INVALID_OFFSET,
                              f"{kind} size fraction must be in (0, 1], got {size_fraction}")
    first = opens + timedelta(minutes=first_after)
    last = closes - timedelta(minutes=last_before)
    flatten = closes - timedelta(minutes=flatten_before)
    if not opens <= first < last < flatten < closes:
        raise CalendarRefused(CalendarCause.INVALID_OFFSET,
                              f"{kind}: needs open {opens.isoformat()} <= first entry "
                              f"{first.isoformat()} < last entry {last.isoformat()} < flatten "
                              f"{flatten.isoformat()} < close {closes.isoformat()}")
    return TradingWindow(kind, opens, first, last, flatten, closes, order_session,
                         size_fraction)


def trading_windows(day: MarketDay, session: SessionConfig) -> tuple[TradingWindow, ...]:
    """The day's enabled windows in time order; raises CalendarRefused on invalid offsets.
    The regular window is always there and keeps its existing settings."""
    windows = []
    if session.premarket_enabled:
        windows.append(_window(
            WindowKind.PRE, day.premarket_open, day.regular_open,
            session.premarket_first_entry_minutes_after_open,
            session.premarket_last_entry_minutes_before_close,
            session.premarket_flatten_minutes_before_close,
            TradingSession.ALL, session.premarket_size_fraction))
    windows.append(_window(
        WindowKind.REGULAR, day.regular_open, day.regular_close, 0,
        session.last_entry_minutes_before_close, session.flatten_minutes_before_close,
        TradingSession.CORE, Decimal(1)))
    if session.postmarket_enabled:
        windows.append(_window(
            WindowKind.POST, day.regular_close, day.extended_close,
            session.postmarket_first_entry_minutes_after_open,
            session.postmarket_last_entry_minutes_before_close,
            session.postmarket_flatten_minutes_before_close,
            TradingSession.ALL, session.postmarket_size_fraction))
    return tuple(windows)


def window_at(windows: tuple[TradingWindow, ...], instant: datetime) -> TradingWindow | None:
    """The enabled window `instant` falls in, or None (outside every enabled session)."""
    return next((w for w in windows if w.contains(instant)), None)
