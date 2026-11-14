from __future__ import annotations

from datetime import date, datetime

import pytest
from conftest import BOT_TOML, FLAT, NY, enter, series

from aperture.replay.__main__ import session_clock
from aperture.replay.engine import SignalOutcome, replay
from aperture.replay.errors import ReplayError


def test_normal_day_clock_comes_from_bot_toml_and_calendar():
    clock = session_clock(BOT_TOML)(date(2026, 11, 10))
    assert clock.regular_open == datetime(2026, 11, 10, 9, 30, tzinfo=NY)
    assert clock.last_entry == datetime(2026, 11, 10, 15, 45, tzinfo=NY)
    assert clock.flatten == datetime(2026, 11, 10, 15, 55, tzinfo=NY)
    assert clock.entries_refused is None


def test_half_day_refuses_entries_when_disabled(settings, scripted):
    half_day = date(2026, 11, 27)
    clock_for = session_clock(BOT_TOML)
    clock = clock_for(half_day)
    assert clock.flatten == datetime(2026, 11, 27, 12, 55, tzinfo=NY)
    assert clock.entries_refused == "half day: trading disabled"
    strategy = scripted({0: enter("10.00", "10.10", "9.80")})
    bars = series("10:00", [FLAT, FLAT], day=half_day)
    result = replay(lambda: strategy, {"ABC": bars}, settings, clock_for)
    (signal,) = result.signals
    assert (signal.outcome, signal.cause) == (SignalOutcome.REJECTED, "half day: trading disabled")


@pytest.mark.parametrize("day", [date(2026, 11, 26), date(2026, 11, 14), date(2028, 6, 1)])
def test_bars_on_a_day_the_calendar_refuses_refuse_the_replay(settings, scripted, day):
    bars = series("10:00", [FLAT], day=day)
    with pytest.raises(ReplayError, match="calendar refuses"):
        replay(lambda: scripted({}), {"ABC": bars}, settings, session_clock(BOT_TOML))
