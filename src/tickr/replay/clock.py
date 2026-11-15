"""Per-session clock of a replay: regular open, last entry and flatten for one NY date.

The CLI builds it from `config/bot.toml [session]` plus the session calendar, exactly as the live
bot does, so half days and closures are handled from the same source. A date the calendar
refuses (weekend, holiday, outside coverage) refuses the replay: bars on such a day are bad data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from tickr.calendar import CalendarRefused, SessionCalendar
from tickr.config import SessionConfig
from tickr.replay.errors import ReplayError


@dataclass(frozen=True)
class SessionClock:
    """Aware deadlines of one session. `entries_refused` names why no entry may be taken."""

    regular_open: datetime
    last_entry: datetime
    flatten: datetime
    entries_refused: str | None = None

    def __post_init__(self) -> None:
        if not self.regular_open < self.last_entry <= self.flatten:
            raise ReplayError(
                "session clock must satisfy regular_open < last_entry <= flatten: "
                f"{self.regular_open.isoformat()} / {self.last_entry.isoformat()} / "
                f"{self.flatten.isoformat()}"
            )


ClockFor = Callable[[date], SessionClock]


def calendar_clock(calendar: SessionCalendar, session: SessionConfig) -> ClockFor:
    """Clock per NY date from the calendar and the `[session]` offsets of bot.toml."""

    def clock_for(ny_date: date) -> SessionClock:
        try:
            day = calendar.day(ny_date)
            times = day.session_times(
                session.last_entry_minutes_before_close, session.flatten_minutes_before_close
            )
        except CalendarRefused as exc:
            raise ReplayError(f"calendar refuses {ny_date.isoformat()}: {exc}") from exc
        refused = None
        if day.half_day and not session.half_days_enabled:
            refused = "half day: trading disabled"
        return SessionClock(day.regular_open, times.last_entry, times.flatten, refused)

    return clock_for


def fixed_clock(zone: ZoneInfo, regular_open: time, last_entry: time, flatten: time) -> ClockFor:
    """The same wall-clock times every day (tests and ad-hoc runs without a calendar)."""

    def clock_for(ny_date: date) -> SessionClock:
        def at(wall: time) -> datetime:
            return datetime.combine(ny_date, wall, zone)

        return SessionClock(at(regular_open), at(last_entry), at(flatten))

    return clock_for
