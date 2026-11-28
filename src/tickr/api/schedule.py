"""The bot's run schedule for the apps (`schedule` of GET /status): whether the bot is meant to
be running now, when its next run starts and ends, and the last day it traded.

The apps show a calm "Bot is off until Mon 04:00 ET" outside the schedule and an alarm only when
the bot should run and does not; they never work the schedule out themselves. Sources, the same
the bot's own start reads, none of them written here:

- the timer: `[runtime] bot_start_local` on `[runtime] launch_weekdays` (validated by
  runtime.launchd), read as wall-clock time in `[session] timezone`. On the server that is the
  zone `render-systemd` writes into the timers (its `--timezone` defaults to it, and the deploy
  passes none). On a Mac, launchd fires in the Mac's own zone, so this holds there only while the
  Mac is on New York time;
- the session calendar: a run is a day the calendar opens (no weekend, no closure) and, on a half
  day, only with `[session] half_days_enabled`, as preflight decides; it lasts from the start to
  that day's extended close (the bot runs through the post-market, docs/operations.md).

Refuse, never guess: a calendar that cannot answer a day it must look at (not covered, invalid),
or no run within `[api] next_session_search_days` days either way, is a null schedule with one
cause.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tickr.api import contract as api
from tickr.api.causes import safe_cause
from tickr.calendar import CalendarCause, CalendarRefused, MarketDay, SessionCalendar
from tickr.config import Config
from tickr.runtime.launchd import schedule

# launchd numbers Sunday both 0 and 7 (runtime/launchd.py); ISO numbers it 7.
_LAUNCHD_SUNDAY = 0
_ISO_SUNDAY = 7
# A day the calendar answers with no session: the search goes on past it.
_NO_SESSION = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})


@dataclass(frozen=True)
class ScheduleSettings:
    calendar: Path  # the session calendar's folder
    timezone: str  # [session] timezone: the zone of bot_start_local
    bot_start: time  # [runtime] bot_start_local
    weekdays: frozenset[int]  # [runtime] launch_weekdays as ISO weekdays (Monday = 1)
    half_days_enabled: bool
    search_days: int  # [api] next_session_search_days: how far either way a run is looked for

    @classmethod
    def from_config(cls, config: Config) -> ScheduleSettings:
        plan = schedule(config.runtime)
        return cls(
            calendar=(config.root / config.session.calendar_file).parent,
            timezone=config.session.timezone,
            bot_start=time(plan.bot.hour, plan.bot.minute),
            weekdays=frozenset(_ISO_SUNDAY if d == _LAUNCHD_SUNDAY else d for d in plan.weekdays),
            half_days_enabled=config.session.half_days_enabled,
            search_days=config.api.next_session_search_days)


@dataclass(frozen=True)
class _Run:
    start: datetime
    end: datetime


def no_run_within(settings: ScheduleSettings, way: str, today: date) -> str:
    return f"no bot run within {settings.search_days} days {way} {today.isoformat()}"


def bot_schedule(settings: ScheduleSettings,
                 now: datetime) -> tuple[api.BotScheduleV1 | None, str | None]:
    """The schedule at `now`, or None with the cause when it cannot be known."""
    zone = ZoneInfo(settings.timezone)
    today = now.astimezone(zone).date()
    try:
        calendar = SessionCalendar.load(settings.calendar)
        last = _first(settings, calendar, zone, today, -1, lambda run: run.start <= now)
        upcoming = _first(settings, calendar, zone, today, 1, lambda run: run.start > now)
    except CalendarRefused as exc:
        return None, safe_cause(exc)
    if last is None:
        return None, no_run_within(settings, "before", today)
    if upcoming is None:
        return None, no_run_within(settings, "after", today)
    running = now < last.end
    return api.BotScheduleV1(
        timezone=settings.timezone,
        expected_running=running,
        run_ends_at=last.end if running else None,
        next_start_at=upcoming.start,
        next_end_at=upcoming.end,
        last_trading_day=last.start.astimezone(zone).date(),
    ), None


def _first(settings: ScheduleSettings, calendar: SessionCalendar, zone: ZoneInfo, today: date,
           step: int, wanted: Callable[[_Run], bool]) -> _Run | None:
    """The first run from `today` going `step` days at a time that is `wanted`, looked for over
    `search_days` days; only the days walked are asked of the calendar."""
    for days in range(settings.search_days + 1):
        run = _run(settings, calendar, zone, today + timedelta(days=step * days))
        if run is not None and wanted(run):
            return run
    return None


def _run(settings: ScheduleSettings, calendar: SessionCalendar, zone: ZoneInfo,
         day: date) -> _Run | None:
    """The bot's run on `day`, None when it has none; refuses what the calendar cannot answer."""
    if day.isoweekday() not in settings.weekdays:
        return None
    try:
        market: MarketDay = calendar.day(day)
    except CalendarRefused as refused:
        if refused.cause in _NO_SESSION:
            return None
        raise
    if market.half_day and not settings.half_days_enabled:
        return None
    start = datetime.combine(day, settings.bot_start, tzinfo=zone)
    return _Run(start, market.extended_close)
