"""api.warm: the latest past day is read into the caches once per New York date."""

from __future__ import annotations

from datetime import date, timedelta

from tickr.api.warm import PastDayWarmer

TODAY = date(2026, 11, 17)
PAST = date(2026, 11, 16)


def _warmer(latest=lambda today: PAST, fail=None):
    warmed = []

    def warm(day, today):
        if fail is not None:
            raise fail
        warmed.append((day, today))

    warmer = PastDayWarmer(latest, warm, (OSError,))
    return warmer, warmed


def _request(warmer, today):
    warmer.on_request(today)
    if warmer.thread is not None:
        warmer.thread.join()


def test_off_until_enabled():
    warmer, warmed = _warmer()
    _request(warmer, TODAY)
    assert warmed == [] and warmer.thread is None


def test_once_per_new_york_date():
    warmer, warmed = _warmer()
    warmer.enabled = True
    _request(warmer, TODAY)
    _request(warmer, TODAY)
    _request(warmer, TODAY + timedelta(days=1))
    assert warmed == [(PAST, TODAY), (PAST, TODAY + timedelta(days=1))]


def test_no_past_day_warms_nothing():
    warmer, warmed = _warmer(latest=lambda today: None)
    warmer.enabled = True
    _request(warmer, TODAY)
    assert warmed == []


def test_a_failed_read_is_logged_never_raised(capsys):
    warmer, _ = _warmer(fail=OSError("capture file unreadable"))
    warmer.enabled = True
    _request(warmer, TODAY)
    assert "could not warm the latest past day: OSError: capture file unreadable" in \
        capsys.readouterr().err
