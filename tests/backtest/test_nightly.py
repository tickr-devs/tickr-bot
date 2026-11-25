"""Nightly refresh: only the new sessions are fetched, then swapped in; current never changes
until the night succeeds."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from tickr.backtest import fetch, nightly
from tickr.backtest.errors import BacktestError
from tickr.backtest.settings import load_settings
from tickr.backtest.universe import PoolEntry, read_caps, read_pool, write_caps, write_pool

from .helpers import NY, ROOT, FakeHistory, calendar, cfg, daily_bar, flat_bars

SEED_DAYS = [date(2026, 11, d) for d in (6, 9, 10, 11, 12, 13)]
NEW_DAY = date(2026, 11, 16)  # a Monday
SETTINGS = replace(load_settings(ROOT / "config" / "backtest.toml"), nightly_window_sessions=5,
                   nightly_min_free_disk_mb=0)
HISTORY = 2


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), NY)


def entry(day: date, symbol: str) -> PoolEntry:
    return PoolEntry(day, 1, symbol, Decimal(10), Decimal(11), Decimal(12), Decimal(10**8))


def seed(tmp_path):
    """A fetch-all folder ending 2026-11-13: OLD gained on the 6th, UP on the 13th."""
    folder = tmp_path / "seed"
    fetch._write_lines(folder / fetch.UNIVERSE_FILE,
                       [{"symbol": s} for s in ("NEW", "OLD", "UP")])
    fetch._write_lines(folder / fetch.DAILY_DIR / "00000.jsonl", [])
    write_pool([entry(date(2026, 11, 6), "OLD"), entry(date(2026, 11, 13), "UP")],
               folder / "pool.csv")
    write_caps([], folder / "pool_cap.csv")
    for symbol, days in (("OLD", SEED_DAYS[:1]), ("UP", SEED_DAYS[-3:])):
        fetch._write_lines(folder / fetch.MINUTES_DIR / f"{symbol}.jsonl",
                           [fetch_record(b) for d in days for b in flat_bars(symbol, d)])
    fetch._write_lines(folder / fetch.MINUTES_STATUS_FILE, [])
    (folder / "raw").mkdir()
    return folder


def fetch_record(bar):
    from tickr.data.capture import bar_record

    return bar_record(bar)


def source() -> FakeHistory:
    flat = {s: [daily_bar(s, d, "10", "10", "10", "10", "1000000") for d in SEED_DAYS]
            for s in ("NEW", "OLD", "UP")}
    daily = {s: bars + [daily_bar(s, NEW_DAY, "10", high, "10", "11", "1000000")]
             for (s, bars), high in zip(sorted(flat.items()), ("11.2", "10", "10.8"),
                                        strict=True)}
    days = [*SEED_DAYS, NEW_DAY]
    minutes = {s: [b for d in days for b in flat_bars(s, d)] for s in ("NEW", "UP")}
    return FakeHistory({}, daily, minutes)


def night(tmp_path, history, now, settings=SETTINGS, seed_folder=None):
    clock = iter([now] * 1000)
    return nightly.run_nightly(history, calendar(), cfg(), settings, HISTORY,
                               tmp_path / "nightly", seed_folder, lambda: next(clock),
                               lambda _s: None, lambda _t: None)


def test_a_night_adds_only_the_new_session_and_trims_the_window(tmp_path):
    folder = seed(tmp_path)
    before = (folder / "pool.csv").read_text()
    up_before = (folder / fetch.MINUTES_DIR / "UP.jsonl").read_text()
    history = source()
    outcome = night(tmp_path, history, at(NEW_DAY, 20), seed_folder=folder)
    assert (outcome.first_day, outcome.last_day, outcome.new_sessions) == (
        "2026-11-16", "2026-11-16", 1)
    current = tmp_path / "nightly" / nightly.CURRENT
    pool = {(e.symbol, e.session_date) for e in read_pool(current / "pool.csv")}
    # NEW +12% and UP +8% on the 16th; OLD's day is older than the 5-session window.
    assert pool == {("UP", date(2026, 11, 13)), ("UP", NEW_DAY), ("NEW", NEW_DAY)}
    assert not (current / fetch.MINUTES_DIR / "OLD.jsonl").exists()
    # UP already held the 12th and 13th: only the 16th was asked for it.
    up_days = {datetime.fromisoformat(json.loads(line)["start"]).astimezone(NY).date()
               for line in (current / fetch.MINUTES_DIR / "UP.jsonl").read_text().splitlines()}
    assert up_days == {*SEED_DAYS[-3:], NEW_DAY}
    asked = [c for c in history.calls if c[0] == "minutes" and "UP" in c[1]]
    assert asked and all(c[2] <= at(NEW_DAY, 16) for c in asked)
    new_days = {datetime.fromisoformat(json.loads(line)["start"]).astimezone(NY).date()
                for line in (current / fetch.MINUTES_DIR / "NEW.jsonl").read_text().splitlines()}
    assert new_days == {date(2026, 11, 12), date(2026, 11, 13), NEW_DAY}
    # Daily rows of the new day only, in their own chunk; raw/ never copied.
    daily = fetch.load_daily(current / fetch.DAILY_DIR)
    assert {r["date"] for r in daily} == {"2026-11-16"}
    assert not (current / "raw").exists() and not (tmp_path / "nightly" / "staging").exists()
    # The seed folder is untouched (hard links were never written through).
    assert (folder / "pool.csv").read_text() == before
    assert (folder / fetch.MINUTES_DIR / "UP.jsonl").read_text() == up_before
    assert json.loads((tmp_path / "nightly" / "nightly.json").read_text())["cause"] is None
    # The next evening there is nothing new: no request at all.
    again = source()
    assert night(tmp_path, again, at(NEW_DAY, 23)).new_sessions == 0 and again.requests == 0


def test_a_swap_cut_short_restores_the_last_good_data(tmp_path):
    folder = seed(tmp_path)
    night(tmp_path, source(), at(NEW_DAY, 20), seed_folder=folder)
    root = tmp_path / "nightly"
    (root / nightly.CURRENT).rename(root / nightly.PREVIOUS)  # stopped inside the swap
    again = source()
    assert night(tmp_path, again, at(NEW_DAY, 23)).new_sessions == 0  # not re-seeded
    assert again.requests == 0 and (root / nightly.CURRENT / "pool.csv").exists()
    assert not (root / nightly.PREVIOUS).exists()


def test_refusals_leave_current_as_it_was(tmp_path):
    folder = seed(tmp_path)
    with pytest.raises(BacktestError, match="regular session"):
        night(tmp_path, source(), at(NEW_DAY, 10), seed_folder=folder)
    with pytest.raises(BacktestError, match="pass --seed"):
        night(tmp_path, source(), at(NEW_DAY, 20))
    # 07:45 Tuesday is past the stop (09:30 open minus 120 minutes): nothing is fetched.
    history = source()
    with pytest.raises(BacktestError, match="before the next session"):
        night(tmp_path, history, at(date(2026, 11, 17), 7, 45), seed_folder=folder)
    assert history.requests == 0
    outcome = json.loads((tmp_path / "nightly" / "nightly.json").read_text())
    assert "before the next session" in outcome["cause"]
    few = replace(SETTINGS, nightly_max_new_sessions=1)
    with pytest.raises(BacktestError, match="nightly_max_new_sessions"):
        night(tmp_path, source(), at(date(2026, 11, 17), 20), few, folder)
    assert not (tmp_path / "nightly" / nightly.CURRENT).exists()


def test_a_session_without_candidates_is_fetched_once_and_has_one_row(tmp_path):
    folder = seed(tmp_path)
    quiet = source()
    for symbol, bars in quiet.daily.items():  # no symbol gains on the new day
        quiet.daily[symbol] = [*bars[:-1], daily_bar(symbol, NEW_DAY, "10", "10", "10", "10",
                                                     "1000000")]
    first = night(tmp_path, quiet, at(NEW_DAY, 20), seed_folder=folder)
    assert first.new_sessions == 1 and quiet.requests > 0
    current = tmp_path / "nightly" / nightly.CURRENT
    assert max(e.session_date for e in read_pool(current / "pool.csv")) < NEW_DAY
    assert (current / nightly.LAST_SESSION_FILE).read_text().strip() == NEW_DAY.isoformat()
    # The rerun the same evening knows the 16th is held although it had no candidate.
    again = source()
    assert night(tmp_path, again, at(NEW_DAY, 23)).new_sessions == 0 and again.requests == 0
    caps = read_caps(current / "pool_cap.csv")
    assert [c.session_date for c in caps] == [NEW_DAY] and caps[0].candidates == 0
