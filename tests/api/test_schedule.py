"""The bot's run schedule in GET /status (api/schedule.py): timer plus session calendar."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.reasons import prose_reasons
from tickr.api.schedule import ScheduleSettings, bot_schedule
from tickr.api.server import make_server, settings_from_config

from .test_server import Clock

ROOT = Path(__file__).resolve().parents[2]
NY = ZoneInfo("America/New_York")


def ny(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=NY)


@pytest.fixture
def cfg(tmp_path):
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    return config.load(ROOT / "config" / "bot.toml", root=tmp_path)


@pytest.fixture
def settings(cfg):
    plan = ScheduleSettings.from_config(cfg)
    # The tests below read the owner's settings: a 04:00 start on weekdays, no half days.
    assert (plan.bot_start.hour, plan.bot_start.minute) == (4, 0)
    assert plan.weekdays == frozenset({1, 2, 3, 4, 5}) and not plan.half_days_enabled
    return plan


def at(settings: ScheduleSettings, now: datetime) -> contract.BotScheduleV1:
    plan, cause = bot_schedule(settings, now.astimezone(UTC))
    assert cause is None and plan is not None
    return plan


def test_inside_a_run_it_ends_at_the_extended_close(settings):
    plan = at(settings, ny(2026, 11, 16, 10, 0))  # Monday 10:00
    assert plan.timezone == "America/New_York" and plan.expected_running
    assert plan.run_ends_at == ny(2026, 11, 16, 20, 0)
    assert plan.run_started_at == ny(2026, 11, 16, 4, 0)
    assert (plan.next_start_at, plan.next_end_at) == (ny(2026, 11, 17, 4, 0),
                                                      ny(2026, 11, 17, 20, 0))
    assert plan.last_trading_day == date(2026, 11, 16)


def test_before_the_start_and_at_the_close_the_bot_is_off(settings):
    early = at(settings, ny(2026, 11, 16, 3, 59))
    assert not early.expected_running and early.run_ends_at is None
    assert early.run_started_at is None
    assert early.next_start_at == ny(2026, 11, 16, 4, 0)
    assert early.last_trading_day == date(2026, 11, 13)  # the Friday before
    start = at(settings, ny(2026, 11, 16, 4, 0))
    assert start.expected_running and start.next_start_at == ny(2026, 11, 17, 4, 0)
    close = at(settings, ny(2026, 11, 16, 20, 0))
    assert not close.expected_running and close.next_start_at == ny(2026, 11, 17, 4, 0)
    assert close.last_trading_day == date(2026, 11, 16)


def test_weekend_is_off_until_monday_premarket(settings):
    plan = at(settings, ny(2026, 11, 21, 10, 0))  # Saturday
    assert not plan.expected_running
    assert plan.next_start_at == ny(2026, 11, 23, 4, 0)
    assert plan.last_trading_day == date(2026, 11, 20)


def test_holiday_and_half_day_are_skipped_unless_half_days_are_enabled(settings):
    # Thanksgiving week: Thursday closed, Friday a half day (extended close 17:00).
    plan = at(settings, ny(2026, 11, 25, 21, 0))
    assert plan.next_start_at == ny(2026, 11, 30, 4, 0)
    assert at(settings, ny(2026, 11, 28, 10, 0)).last_trading_day == date(2026, 11, 25)
    half = at(replace(settings, half_days_enabled=True), ny(2026, 11, 25, 21, 0))
    assert (half.next_start_at, half.next_end_at) == (ny(2026, 11, 27, 4, 0),
                                                      ny(2026, 11, 27, 17, 0))


def test_start_is_new_york_wall_time_across_daylight_saving(settings):
    plan = at(settings, ny(2026, 3, 7, 12, 0))  # Saturday before the March change
    assert plan.next_start_at == datetime(2026, 3, 9, 8, 0, tzinfo=UTC)  # 04:00 EDT
    assert plan.last_trading_day == date(2026, 3, 6)


def test_a_day_the_calendar_does_not_cover_refuses_with_its_cause(settings):
    plan, cause = bot_schedule(settings, ny(2027, 12, 31, 21, 0).astimezone(UTC))
    assert plan is None
    assert cause == "OUT_OF_COVERAGE: 2028-01-03 is not covered by any file"


def test_no_run_within_the_search_refuses(settings):
    weekends = replace(settings, weekdays=frozenset({6, 7}))  # the calendar never opens them
    plan, cause = bot_schedule(weekends, ny(2026, 11, 18, 10, 0).astimezone(UTC))
    assert plan is None and cause == "no bot run within 10 days before 2026-11-18"
    assert [r.code for r in prose_reasons(cause)] == ["no_bot_run"]
    assert prose_reasons(cause)[0].params == {"days": "10", "way": "before",
                                              "day": "2026-11-18"}


def test_launchd_sunday_zero_is_iso_sunday(cfg):
    runtime = replace(cfg.runtime, launch_weekdays=[0, 1])
    plan = ScheduleSettings.from_config(replace(cfg, runtime=runtime))
    assert plan.weekdays == frozenset({7, 1})


def test_status_response_carries_the_schedule(cfg):
    server = make_server(replace(settings_from_config(cfg), port=0),
                         Clock(ny(2026, 11, 21, 10, 0).astimezone(UTC)))
    try:
        response = server.status_response()
    finally:
        server.server_close()
    assert response.status is None  # no status file: the schedule still answers
    assert response.schedule_cause is None and response.schedule is not None
    assert not response.schedule.expected_running
    assert response.schedule.next_start_at == ny(2026, 11, 23, 4, 0)
