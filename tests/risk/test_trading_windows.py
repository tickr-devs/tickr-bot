"""Trading windows of a day (risk/timers.py): the regular session plus enabled extended ones."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from baseline import load_baseline

from tickr.calendar import NEW_YORK, CalendarRefused, SessionCalendar
from tickr.risk.timers import WindowKind, trading_windows, window_at
from tickr.types import TradingSession

ROOT = Path(__file__).resolve().parents[2]
CFG = load_baseline()
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY = CALENDAR.day(date(2026, 11, 16))
HALF_DAY = CALENDAR.day(date(2026, 11, 27))
BOTH = replace(CFG.session, premarket_enabled=True, postmarket_enabled=True)


def ny(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEW_YORK)


def test_shipped_config_trades_the_regular_session_only() -> None:
    (regular,) = trading_windows(MONDAY, CFG.session)
    assert regular.kind is WindowKind.REGULAR
    assert regular.order_session is TradingSession.CORE
    assert regular.size_fraction == 1
    times = MONDAY.session_times(CFG.session.last_entry_minutes_before_close,
                                 CFG.session.flatten_minutes_before_close)
    assert (regular.last_entry, regular.flatten) == (times.last_entry, times.flatten)
    assert window_at((regular,), ny(MONDAY.ny_date, 17, 0)) is None


def test_extended_windows_follow_the_shipped_offsets() -> None:
    pre, regular, post = trading_windows(MONDAY, BOTH)
    d = MONDAY.ny_date
    assert (pre.opens, pre.first_entry, pre.last_entry, pre.flatten, pre.closes) == (
        ny(d, 4, 0), ny(d, 5, 0), ny(d, 9, 15), ny(d, 9, 25), ny(d, 9, 30))
    assert (post.opens, post.first_entry, post.last_entry, post.flatten, post.closes) == (
        ny(d, 16, 0), ny(d, 16, 5), ny(d, 19, 0), ny(d, 19, 30), ny(d, 20, 0))
    assert pre.order_session is post.order_session is TradingSession.ALL
    assert pre.size_fraction == post.size_fraction == Decimal("0.5")
    windows = (pre, regular, post)
    assert window_at(windows, ny(d, 3, 59)) is None
    assert window_at(windows, ny(d, 9, 29)) is pre
    assert window_at(windows, ny(d, 9, 30)) is regular
    assert window_at(windows, ny(d, 16, 0)) is post
    assert window_at(windows, ny(d, 20, 0)) is None


def test_half_day_post_market_ends_at_the_calendar_extended_close() -> None:
    post = trading_windows(HALF_DAY, BOTH)[-1]
    assert post.kind is WindowKind.POST
    assert post.closes == ny(HALF_DAY.ny_date, 17, 0)
    assert post.flatten == ny(HALF_DAY.ny_date, 16, 30)


@pytest.mark.parametrize("change", [
    {"postmarket_last_entry_minutes_before_close": 20},  # last entry after flatten
    {"premarket_first_entry_minutes_after_open": 400},  # first entry after last entry
    {"premarket_size_fraction": Decimal(0)},
    {"postmarket_size_fraction": Decimal("1.5")},
    {"premarket_flatten_minutes_before_close": -1},
])
def test_invalid_offsets_are_refused(change: dict) -> None:
    with pytest.raises(CalendarRefused):
        trading_windows(MONDAY, replace(BOTH, **change))


def test_windows_never_overlap_and_flatten_before_they_close() -> None:
    windows = trading_windows(MONDAY, BOTH)
    for a, b in zip(windows, windows[1:], strict=False):
        assert a.closes <= b.opens
    for w in windows:
        assert w.flatten < w.closes and w.first_entry < w.last_entry
    assert all(w.opens.tzinfo is not None for w in windows)
    assert windows[0].opens.astimezone(UTC).date() == date(2026, 11, 16)
