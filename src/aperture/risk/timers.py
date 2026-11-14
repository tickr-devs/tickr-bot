"""Last-entry and flatten instants of a trading day, from the calendar and `[session]` offsets."""

from __future__ import annotations

from datetime import date

from aperture.calendar import MarketDay, SessionCalendar, SessionTimes
from aperture.config import SessionConfig


def deadlines(day: MarketDay, last_entry_minutes_before_close: int,
              flatten_minutes_before_close: int) -> SessionTimes:
    """Last-entry and flatten times of `day`; raises CalendarRefused on invalid offsets."""
    return day.session_times(last_entry_minutes_before_close, flatten_minutes_before_close)


def deadlines_for(calendar: SessionCalendar, ny_date: date, session: SessionConfig) -> SessionTimes:
    """Deadlines for a New York date; raises CalendarRefused for unknown or closed days."""
    return deadlines(calendar.day(ny_date), session.last_entry_minutes_before_close,
                     session.flatten_minutes_before_close)
