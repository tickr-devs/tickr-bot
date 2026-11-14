import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from aperture.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    SessionCalendar,
    ny_date_of,
)

REPO_CALENDARS = Path(__file__).resolve().parents[1] / "config" / "calendar"

SOURCE = "https://example.invalid/calendar"
BASE = {
    "schema": "webull-us-equity-session-calendar-v1",
    "market": "US",
    "timezone": "America/New_York",
    "coverage_start": "2026-01-01",
    "coverage_end": "2026-12-31",
    "standard_hours": {
        "premarket_open": "04:00",
        "regular_open": "09:30",
        "regular_close": "16:00",
        "extended_close": "20:00",
        "source_url": SOURCE,
        "verified_on": "2026-10-28",
    },
    "exceptions": [
        {
            "day": "2026-11-26",
            "market": "US",
            "day_kind": "CLOSED",
            "source_url": SOURCE,
            "verified_on": "2026-11-14",
        },
        {
            "day": "2026-11-27",
            "market": "US",
            "day_kind": "HALF_DAY",
            "regular_close": "13:00",
            "extended_close": "17:00",
            "source_url": SOURCE,
            "verified_on": "2026-10-28",
        },
    ],
}


def write(directory: Path, data: dict, name: str = "us-equity-2026.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(data), encoding="utf-8")
    return directory


@pytest.fixture
def cal(tmp_path: Path) -> SessionCalendar:
    return SessionCalendar.load(write(tmp_path, BASE))


def refused(fn, *args) -> CalendarCause:
    with pytest.raises(CalendarRefused) as info:
        fn(*args)
    return info.value.cause


def ny(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NEW_YORK)


# ----------------------------------------------------------------- the repository file


def test_repository_calendar_loads_and_knows_half_days():
    repo = SessionCalendar.load(REPO_CALENDARS)
    for half in (date(2026, 11, 27), date(2026, 12, 24)):
        day = repo.day(half)
        assert day.half_day
        assert day.regular_close == ny(half.year, half.month, half.day, 13, 0)
        assert day.extended_close == ny(half.year, half.month, half.day, 17, 0)
    assert not repo.day(date(2026, 11, 16)).half_day


@pytest.mark.parametrize(
    "closed",
    [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ],
)
def test_repository_calendar_knows_2026_nyse_closures(closed):
    repo = SessionCalendar.load(REPO_CALENDARS)
    assert refused(repo.day, closed) is CalendarCause.HOLIDAY


# ----------------------------------------------------------------- ordinary days and DST


def test_regular_day(cal):
    day = cal.day(date(2026, 11, 16))
    assert day.ny_date == date(2026, 11, 16)
    assert day.premarket_open == ny(2026, 11, 16, 4, 0)
    assert day.regular_open == ny(2026, 11, 16, 9, 30)
    assert day.regular_close == ny(2026, 11, 16, 16, 0)
    assert day.extended_close == ny(2026, 11, 16, 20, 0)
    assert not day.half_day
    assert day.source_url == SOURCE


@pytest.mark.parametrize(
    ("ny_date", "open_utc_hour"),
    [
        (date(2026, 3, 6), 14),  # Friday before DST starts (EST, UTC-5)
        (date(2026, 3, 9), 13),  # Monday after DST starts (EDT, UTC-4)
        (date(2026, 10, 30), 13),  # Friday before DST ends (EDT)
        (date(2026, 11, 2), 14),  # Monday after DST ends (EST)
    ],
)
def test_open_in_utc_follows_dst(cal, ny_date, open_utc_hour):
    day = cal.day(ny_date)
    open_utc = day.regular_open.astimezone(UTC)
    assert (open_utc.hour, open_utc.minute) == (open_utc_hour, 30)
    assert day.regular_close - day.regular_open == timedelta(hours=6, minutes=30)


def test_ny_date_of_uses_new_york_not_utc(cal):
    # 2026-11-17 02:00 UTC is still the evening of 2026-11-16 in New York.
    instant = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)
    assert ny_date_of(instant) == date(2026, 11, 16)
    assert cal.day_at(instant).ny_date == date(2026, 11, 16)


def test_in_regular_session_bounds(cal):
    day = cal.day(date(2026, 7, 15))
    assert not day.in_regular_session(ny(2026, 7, 15, 9, 29))
    assert day.in_regular_session(ny(2026, 7, 15, 9, 30))
    assert day.in_regular_session(day.regular_open.astimezone(UTC))
    assert day.in_regular_session(ny(2026, 7, 15, 15, 59))
    assert not day.in_regular_session(ny(2026, 7, 15, 16, 0))


# ----------------------------------------------------------------- refusals


def test_weekend_refused(cal):
    assert refused(cal.day, date(2026, 11, 14)) is CalendarCause.WEEKEND
    assert refused(cal.day, date(2026, 11, 15)) is CalendarCause.WEEKEND


def test_holiday_refused(cal):
    assert refused(cal.day, date(2026, 11, 26)) is CalendarCause.HOLIDAY


def test_half_day(cal):
    day = cal.day(date(2026, 11, 27))
    assert day.half_day
    assert day.regular_close == ny(2026, 11, 27, 13, 0)
    assert day.extended_close == ny(2026, 11, 27, 17, 0)


@pytest.mark.parametrize("d", [date(2025, 12, 31), date(2027, 1, 4)])
def test_out_of_coverage_refused(cal, d):
    assert refused(cal.day, d) is CalendarCause.OUT_OF_COVERAGE


def test_naive_datetime_refused(cal):
    assert refused(cal.day_at, datetime(2026, 11, 16, 10, 0)) is CalendarCause.NAIVE_TIME
    day = cal.day(date(2026, 11, 16))
    assert refused(day.in_regular_session, datetime(2026, 11, 16, 10, 0)) is (
        CalendarCause.NAIVE_TIME
    )


def test_datetime_is_not_a_date(cal):
    with pytest.raises(TypeError):
        cal.day(datetime(2026, 11, 16, 10, 0, tzinfo=UTC))


# ----------------------------------------------------------------- offsets


def test_session_times_regular_day(cal):
    times = cal.day(date(2026, 11, 16)).session_times(15, 5)
    assert times.last_entry == ny(2026, 11, 16, 15, 45)
    assert times.flatten == ny(2026, 11, 16, 15, 55)


def test_session_times_half_day_use_the_early_close(cal):
    times = cal.day(date(2026, 11, 27)).session_times(15, 5)
    assert times.last_entry == ny(2026, 11, 27, 12, 45)
    assert times.flatten == ny(2026, 11, 27, 12, 55)


@pytest.mark.parametrize("d", [date(2026, 3, 9), date(2026, 11, 2)])
def test_session_times_across_dst(cal, d):
    times = cal.day(d).session_times(15, 5)
    assert times.last_entry.astimezone(NEW_YORK).time().isoformat() == "15:45:00"
    assert times.flatten - times.last_entry == timedelta(minutes=10)


@pytest.mark.parametrize("minutes", [-1, 390, 1000, True, 1.5, "15"])
def test_bad_offsets_refused(cal, minutes):
    day = cal.day(date(2026, 11, 16))
    assert refused(day.before_close, minutes) is CalendarCause.INVALID_OFFSET


def test_offset_past_half_day_open_refused(cal):
    # 210 min before 13:00 is 09:30: not after the open.
    assert refused(cal.day(date(2026, 11, 27)).before_close, 210) is CalendarCause.INVALID_OFFSET


def test_flatten_must_follow_last_entry(cal):
    day = cal.day(date(2026, 11, 16))
    assert refused(day.session_times, 5, 15) is CalendarCause.INVALID_OFFSET
    assert refused(day.session_times, 5, 5) is CalendarCause.INVALID_OFFSET


# ----------------------------------------------------------------- malformed files


def mutate(fn):
    data = copy.deepcopy(BASE)
    fn(data)
    return data


def _drop(key):
    return lambda d: d.pop(key)


def _set(key, value):
    return lambda d: d.__setitem__(key, value)


def _hours(key, value):
    return lambda d: d["standard_hours"].__setitem__(key, value)


def _exc(index, key, value):
    return lambda d: d["exceptions"][index].__setitem__(key, value)


BAD = {
    "missing key": _drop("timezone"),
    "unknown key": _set("extra", 1),
    "wrong schema": _set("schema", "other"),
    "wrong market": _set("market", "HK"),
    "wrong timezone": _set("timezone", "UTC"),
    "coverage outside year": _set("coverage_end", "2027-01-01"),
    "coverage reversed": _set("coverage_start", "2026-12-31"),
    "bad time": _hours("regular_open", "9:30am"),
    "time with seconds": _hours("regular_open", "09:30:00"),
    "hours out of order": _hours("regular_open", "16:30"),
    "unknown day kind": _exc(0, "day_kind", "EARLY"),
    "exception on weekend": _exc(0, "day", "2026-11-28"),
    "exception out of coverage": _exc(0, "day", "2027-01-04"),
    "duplicate exception": _exc(0, "day", "2026-11-27"),
    "half day closes late": _exc(1, "regular_close", "16:00"),
    "closed day with times": _exc(0, "regular_close", "13:00"),
    "missing source": lambda d: d["exceptions"][1].pop("source_url"),
    "exceptions not a list": _set("exceptions", {}),
}


@pytest.mark.parametrize("change", BAD.values(), ids=BAD.keys())
def test_malformed_file_refused(tmp_path, change):
    data = mutate(change)
    directory = write(tmp_path, data)
    assert refused(SessionCalendar.load, directory) is CalendarCause.CALENDAR_INVALID


def test_unparseable_json_refused(tmp_path):
    tmp_path.joinpath("us-equity-2026.json").write_text("{", encoding="utf-8")
    assert refused(SessionCalendar.load, tmp_path) is CalendarCause.CALENDAR_INVALID


def test_empty_or_missing_directory_refused(tmp_path):
    assert refused(SessionCalendar.load, tmp_path) is CalendarCause.CALENDAR_INVALID
    missing = tmp_path / "nope"
    assert refused(SessionCalendar.load, missing) is CalendarCause.CALENDAR_INVALID


def test_file_year_must_match_coverage(tmp_path):
    write(tmp_path, BASE, name="us-equity-2027.json")
    assert refused(SessionCalendar.load, tmp_path) is CalendarCause.CALENDAR_INVALID


# ----------------------------------------------------------------- several years


def test_two_years_cover_new_year(tmp_path):
    write(tmp_path, BASE)
    next_year = copy.deepcopy(BASE)
    next_year["coverage_start"] = "2027-01-01"
    next_year["coverage_end"] = "2027-12-31"
    next_year["exceptions"] = []
    write(tmp_path, next_year, name="us-equity-2027.json")
    cal = SessionCalendar.load(tmp_path)
    assert cal.coverage == (
        (date(2026, 1, 1), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 12, 31)),
    )
    assert cal.day(date(2027, 1, 4)).regular_open == ny(2027, 1, 4, 9, 30)
    assert refused(cal.day, date(2028, 1, 3)) is CalendarCause.OUT_OF_COVERAGE


def test_partial_coverage_gap_refused(tmp_path):
    data = copy.deepcopy(BASE)
    data["coverage_end"] = "2026-06-30"
    data["exceptions"] = []
    cal = SessionCalendar.load(write(tmp_path, data))
    assert cal.day(date(2026, 6, 30)).regular_open == ny(2026, 6, 30, 9, 30)
    assert refused(cal.day, date(2026, 7, 1)) is CalendarCause.OUT_OF_COVERAGE


def test_other_files_ignored(tmp_path):
    write(tmp_path, BASE)
    tmp_path.joinpath("README.md").write_text("notes", encoding="utf-8")
    tmp_path.joinpath("us-equity-2026.json.bak").write_text("{", encoding="utf-8")
    assert SessionCalendar.load(tmp_path).day(date(2026, 11, 16))
