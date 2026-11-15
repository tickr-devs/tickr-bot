"""Calendar helpers: open sessions between two dates and the sessions before a date."""

from __future__ import annotations

from datetime import date, timedelta

from aperture.backtest.errors import BacktestError
from aperture.calendar import CalendarCause, CalendarRefused, MarketDay, SessionCalendar

NOT_A_SESSION = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})
ONE_DAY = timedelta(days=1)


def session_or_none(calendar: SessionCalendar, day: date) -> MarketDay | None:
    """The day's sessions, None on a weekend or holiday; any other refusal stops the step."""
    try:
        return calendar.day(day)
    except CalendarRefused as refused:
        if refused.cause in NOT_A_SESSION:
            return None
        raise BacktestError(f"calendar refuses {day.isoformat()}: {refused}") from None


def sessions_between(calendar: SessionCalendar, first: date, last: date) -> list[MarketDay]:
    """Open sessions from `first` to `last` inclusive, oldest first."""
    if last < first:
        raise BacktestError(f"last date {last} is before first date {first}")
    days: list[MarketDay] = []
    day = first
    while day <= last:
        found = session_or_none(calendar, day)
        if found is not None:
            days.append(found)
        day += ONE_DAY
    return days


def sessions_before(calendar: SessionCalendar, day: date, count: int,
                    max_lookback_days: int) -> list[MarketDay]:
    """The `count` open sessions strictly before `day`, oldest first."""
    found: list[MarketDay] = []
    for back in range(1, max_lookback_days + 1):
        session = session_or_none(calendar, day - timedelta(days=back))
        if session is not None:
            found.append(session)
        if len(found) == count:
            return list(reversed(found))
    raise BacktestError(f"fewer than {count} sessions within {max_lookback_days} days before "
                        f"{day.isoformat()}")
