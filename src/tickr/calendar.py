"""US equity session calendar and session clock.

Reads `config/calendar/us-equity-<year>.json` (standard hours plus dated exceptions) and answers,
for a New York date, what the trading sessions are, or refuses with one typed cause.

Rules:
- Every time of day comes from the JSON; every offset (last entry, flatten) is an argument.
- A day outside the loaded coverage, a weekend, a full closure, or a malformed file is a refusal,
  never a guess.
- Times are timezone-aware. Offsets are applied on the absolute (UTC) time line, so the result is
  correct across daylight-saving changes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Protocol constants of the calendar file format (not tunable values).
SCHEMA = "webull-us-equity-session-calendar-v1"
MARKET = "US"
TIMEZONE_NAME = "America/New_York"
NEW_YORK = ZoneInfo(TIMEZONE_NAME)
FILE_PATTERN = re.compile(r"^us-equity-(\d{4})\.json$")
# ISO weekday numbers (Monday=1) of Saturday and Sunday.
WEEKEND_ISO_DAYS = frozenset({6, 7})

_FILE_KEYS = frozenset(
    {
        "schema",
        "market",
        "timezone",
        "coverage_start",
        "coverage_end",
        "standard_hours",
        "exceptions",
    }
)
_HOURS_KEYS = frozenset({"premarket_open", "regular_open", "regular_close", "extended_close"})
_SOURCE_KEYS = frozenset({"source_url", "verified_on"})
_EXCEPTION_BASE_KEYS = frozenset({"day", "market", "day_kind"}) | _SOURCE_KEYS
_HALF_DAY_TIME_KEYS = frozenset({"regular_close", "extended_close"})


class DayKind(StrEnum):
    """Kind of an exception day in the calendar file."""

    HALF_DAY = "HALF_DAY"
    CLOSED = "CLOSED"


class CalendarCause(StrEnum):
    """The single recorded cause of a calendar refusal."""

    CALENDAR_INVALID = "CALENDAR_INVALID"
    OUT_OF_COVERAGE = "OUT_OF_COVERAGE"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    NAIVE_TIME = "NAIVE_TIME"
    INVALID_OFFSET = "INVALID_OFFSET"


class CalendarRefused(Exception):
    """Typed refusal: the calendar cannot answer, so nobody may trade on the answer."""

    def __init__(self, cause: CalendarCause, detail: str) -> None:
        super().__init__(f"{cause}: {detail}")
        self.cause = cause
        self.detail = detail


@dataclass(frozen=True)
class SessionTimes:
    """Entry and flatten deadlines of one trading day (aware, New York time zone)."""

    last_entry: datetime
    flatten: datetime


@dataclass(frozen=True)
class MarketDay:
    """Sessions of one open New York trading day. All times are aware, New York time zone."""

    ny_date: date
    premarket_open: datetime
    regular_open: datetime
    regular_close: datetime
    extended_close: datetime
    half_day: bool
    source_url: str

    def in_regular_session(self, instant: datetime) -> bool:
        """True when `instant` is in [regular_open, regular_close)."""
        _require_aware(instant)
        return self.regular_open <= instant < self.regular_close

    def before_close(self, minutes: int) -> datetime:
        """Regular close minus `minutes`; refused unless strictly after the regular open."""
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
            raise CalendarRefused(
                CalendarCause.INVALID_OFFSET, f"minutes must be a non-negative int, got {minutes!r}"
            )
        instant = (self.regular_close.astimezone(UTC) - timedelta(minutes=minutes)).astimezone(
            NEW_YORK
        )
        if instant <= self.regular_open:
            raise CalendarRefused(
                CalendarCause.INVALID_OFFSET,
                f"{minutes} min before close {self.regular_close.isoformat()} is not after "
                f"the regular open {self.regular_open.isoformat()}",
            )
        return instant

    def session_times(
        self, last_entry_minutes_before_close: int, flatten_minutes_before_close: int
    ) -> SessionTimes:
        """Last-entry and flatten times from offsets; last entry must precede flatten."""
        last_entry = self.before_close(last_entry_minutes_before_close)
        flatten = self.before_close(flatten_minutes_before_close)
        if not last_entry < flatten:
            raise CalendarRefused(
                CalendarCause.INVALID_OFFSET,
                f"last entry {last_entry.isoformat()} is not before flatten {flatten.isoformat()}",
            )
        return SessionTimes(last_entry=last_entry, flatten=flatten)


@dataclass(frozen=True)
class _Hours:
    premarket_open: time
    regular_open: time
    regular_close: time
    extended_close: time


@dataclass(frozen=True)
class _Exception:
    kind: DayKind
    source_url: str
    regular_close: time | None
    extended_close: time | None


@dataclass(frozen=True)
class _YearFile:
    path: Path
    coverage_start: date
    coverage_end: date
    hours: _Hours
    source_url: str
    exceptions: dict[date, _Exception]


class SessionCalendar:
    """All loaded calendar files. Build with `SessionCalendar.load(directory)`."""

    def __init__(self, files: list[_YearFile]) -> None:
        ordered = sorted(files, key=lambda f: f.coverage_start)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if later.coverage_start <= earlier.coverage_end:
                raise CalendarRefused(
                    CalendarCause.CALENDAR_INVALID,
                    f"coverage of {later.path.name} overlaps {earlier.path.name}",
                )
        self._files = tuple(ordered)

    @classmethod
    def load(cls, directory: Path) -> SessionCalendar:
        """Load and validate every `us-equity-<year>.json` in `directory`."""
        if not directory.is_dir():
            raise CalendarRefused(CalendarCause.CALENDAR_INVALID,
                                  "the calendar directory does not exist")
        files = []
        for path in sorted(directory.iterdir()):
            match = FILE_PATTERN.match(path.name)
            if match:
                files.append(_parse_file(path, int(match.group(1))))
        if not files:
            raise CalendarRefused(
                CalendarCause.CALENDAR_INVALID, "no calendar files in the calendar directory"
            )
        return cls(files)

    @property
    def coverage(self) -> tuple[tuple[date, date], ...]:
        """Covered date ranges (inclusive), in order."""
        return tuple((f.coverage_start, f.coverage_end) for f in self._files)

    def day(self, ny_date: date) -> MarketDay:
        """Sessions of a New York date, or a typed refusal."""
        if isinstance(ny_date, datetime) or not isinstance(ny_date, date):
            raise TypeError(f"expected a date, got {type(ny_date).__name__}")
        year_file = self._file_for(ny_date)
        if ny_date.isoweekday() in WEEKEND_ISO_DAYS:
            raise CalendarRefused(CalendarCause.WEEKEND, f"{ny_date.isoformat()} is a weekend")
        hours = year_file.hours
        regular_close = hours.regular_close
        extended_close = hours.extended_close
        source_url = year_file.source_url
        half_day = False
        exception = year_file.exceptions.get(ny_date)
        if exception is not None:
            if exception.kind is DayKind.CLOSED:
                raise CalendarRefused(
                    CalendarCause.HOLIDAY,
                    f"{ny_date.isoformat()} is a market closure ({exception.source_url})",
                )
            if exception.regular_close is None or exception.extended_close is None:
                raise CalendarRefused(
                    CalendarCause.CALENDAR_INVALID, f"half day {ny_date} has no closing times"
                )
            regular_close = exception.regular_close
            extended_close = exception.extended_close
            source_url = exception.source_url
            half_day = True
        return MarketDay(
            ny_date=ny_date,
            premarket_open=_at(ny_date, hours.premarket_open),
            regular_open=_at(ny_date, hours.regular_open),
            regular_close=_at(ny_date, regular_close),
            extended_close=_at(ny_date, extended_close),
            half_day=half_day,
            source_url=source_url,
        )

    def day_at(self, instant: datetime) -> MarketDay:
        """Sessions of the New York date containing an aware `instant`."""
        return self.day(ny_date_of(instant))

    def _file_for(self, ny_date: date) -> _YearFile:
        for year_file in self._files:
            if year_file.coverage_start <= ny_date <= year_file.coverage_end:
                return year_file
        raise CalendarRefused(
            CalendarCause.OUT_OF_COVERAGE, f"{ny_date.isoformat()} is not covered by any file"
        )


def market_quiet(calendar: SessionCalendar, instant: datetime) -> bool:
    """True when no session of any kind runs at `instant`: outside [pre-market open, extended
    close) of its New York day, or on a weekend or holiday. A day the calendar cannot answer
    (outside its coverage, an invalid file) is never quiet."""
    try:
        day = calendar.day_at(instant)
    except CalendarRefused as refused:
        return refused.cause in (CalendarCause.WEEKEND, CalendarCause.HOLIDAY)
    return not day.premarket_open <= instant < day.extended_close


def ny_date_of(instant: datetime) -> date:
    """New York calendar date of an aware instant."""
    _require_aware(instant)
    return instant.astimezone(NEW_YORK).date()


def _require_aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise CalendarRefused(CalendarCause.NAIVE_TIME, f"naive datetime {instant!r}")


def _at(ny_date: date, wall: time) -> datetime:
    """Aware New York datetime for a wall-clock time on a date (regular hours never hit a gap)."""
    return datetime.combine(ny_date, wall, tzinfo=NEW_YORK)


# ---------------------------------------------------------------------------- parsing


def _invalid(path: Path, detail: str) -> CalendarRefused:
    return CalendarRefused(CalendarCause.CALENDAR_INVALID, f"{path.name}: {detail}")


def _parse_file(path: Path, year: int) -> _YearFile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid(path, f"unreadable: {exc}") from exc
    _expect_keys(path, raw, _FILE_KEYS, "file")
    if raw["schema"] != SCHEMA:
        raise _invalid(path, f"schema {raw['schema']!r} is not {SCHEMA!r}")
    if raw["market"] != MARKET:
        raise _invalid(path, f"market {raw['market']!r} is not {MARKET!r}")
    if raw["timezone"] != TIMEZONE_NAME:
        raise _invalid(path, f"timezone {raw['timezone']!r} is not {TIMEZONE_NAME!r}")
    start = _date(path, raw["coverage_start"], "coverage_start")
    end = _date(path, raw["coverage_end"], "coverage_end")
    if start > end or start.year != year or end.year != year:
        raise _invalid(path, f"coverage {start}..{end} does not lie inside year {year}")

    hours_raw = raw["standard_hours"]
    _expect_keys(path, hours_raw, _HOURS_KEYS | _SOURCE_KEYS, "standard_hours")
    hours = _Hours(
        premarket_open=_time(path, hours_raw["premarket_open"], "premarket_open"),
        regular_open=_time(path, hours_raw["regular_open"], "regular_open"),
        regular_close=_time(path, hours_raw["regular_close"], "regular_close"),
        extended_close=_time(path, hours_raw["extended_close"], "extended_close"),
    )
    _check_order(path, "standard_hours", hours)
    _date(path, hours_raw["verified_on"], "standard_hours.verified_on")
    source_url = _text(path, hours_raw["source_url"], "standard_hours.source_url")

    if not isinstance(raw["exceptions"], list):
        raise _invalid(path, "exceptions must be a list")
    exceptions: dict[date, _Exception] = {}
    for item in raw["exceptions"]:
        day, exception = _parse_exception(path, item, hours)
        if not start <= day <= end:
            raise _invalid(path, f"exception {day} is outside coverage")
        if day.isoweekday() in WEEKEND_ISO_DAYS:
            raise _invalid(path, f"exception {day} falls on a weekend")
        if day in exceptions:
            raise _invalid(path, f"exception {day} is listed twice")
        exceptions[day] = exception
    return _YearFile(path, start, end, hours, source_url, exceptions)


def _parse_exception(path: Path, item: Any, hours: _Hours) -> tuple[date, _Exception]:
    if not isinstance(item, dict) or "day_kind" not in item:
        raise _invalid(path, f"exception without day_kind: {item!r}")
    try:
        kind = DayKind(item["day_kind"])
    except ValueError as exc:
        raise _invalid(path, f"unknown day_kind {item['day_kind']!r}") from exc
    allowed = _EXCEPTION_BASE_KEYS | (_HALF_DAY_TIME_KEYS if kind is DayKind.HALF_DAY else set())
    _expect_keys(path, item, allowed, f"exception {item.get('day')!r}")
    day = _date(path, item["day"], "exception.day")
    if item["market"] != MARKET:
        raise _invalid(path, f"exception {day} market {item['market']!r} is not {MARKET!r}")
    _date(path, item["verified_on"], f"exception {day} verified_on")
    source_url = _text(path, item["source_url"], f"exception {day} source_url")
    if kind is DayKind.CLOSED:
        return day, _Exception(kind, source_url, None, None)
    regular_close = _time(path, item["regular_close"], f"exception {day} regular_close")
    extended_close = _time(path, item["extended_close"], f"exception {day} extended_close")
    half = _Hours(hours.premarket_open, hours.regular_open, regular_close, extended_close)
    _check_order(path, f"exception {day}", half)
    if not regular_close < hours.regular_close:
        raise _invalid(path, f"half day {day} does not close before the standard close")
    return day, _Exception(kind, source_url, regular_close, extended_close)


def _expect_keys(path: Path, raw: Any, keys: frozenset[str] | set[str], where: str) -> None:
    if not isinstance(raw, dict):
        raise _invalid(path, f"{where} must be an object")
    missing = sorted(set(keys) - raw.keys())
    unknown = sorted(raw.keys() - set(keys))
    if missing or unknown:
        raise _invalid(path, f"{where}: missing keys {missing}, unknown keys {unknown}")


def _check_order(path: Path, where: str, hours: _Hours) -> None:
    if not (
        hours.premarket_open < hours.regular_open < hours.regular_close <= hours.extended_close
    ):
        raise _invalid(path, f"{where}: times are not in session order")


def _text(path: Path, value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(path, f"{where} must be a non-empty string")
    return value


def _date(path: Path, value: Any, where: str) -> date:
    try:
        return date.fromisoformat(_text(path, value, where))
    except ValueError as exc:
        raise _invalid(path, f"{where} {value!r} is not an ISO date") from exc


def _time(path: Path, value: Any, where: str) -> time:
    text = _text(path, value, where)
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(path, f"{where} {value!r} is not HH:MM") from exc
    if parsed.second or parsed.microsecond or len(text) != len("HH:MM"):
        raise _invalid(path, f"{where} {value!r} is not HH:MM")
    return parsed
