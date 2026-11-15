"""`python -m tickr.backtest nightly`: add the newest completed sessions to a rolling folder of
backtest data, for the Lab's replay and later backtests. SIMULATED inputs only; the bot never
reads them. See docs/backtest.md, "Nightly".

A full fetch of 60 sessions takes many hours under Webull's limits, so a night fetches only what
the new sessions add: their daily bars, their pool days, and the minute bars those pool days
need that the folder does not hold yet. Layout under `--out`:

- `current/`: the data the Lab reads (pool.csv, daily/, minutes/ ... as `fetch-all` writes);
- `staging/`: tonight's copy being built (minute and daily files hard-linked from `current`,
  every file it changes rewritten under a new name, so `current` is never modified);
- `raw/`: first and failed responses, outside the swap;
- `nightly.json`: the last night's outcome (finished_at, first_day, last_day, new_sessions,
  requests, or cause).

Only when everything is fetched is `staging` swapped in as `current`; a refused or interrupted
night leaves `current` as it was. Refused during the regular session, below the free disk
floor, with more missing sessions than `[backtest] nightly_max_new_sessions`, and once the
next session's open minus `[backtest] nightly_stop_minutes_before_open` is reached.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tickr.backtest import fetch
from tickr.backtest.errors import BacktestError
from tickr.backtest.sessions import ONE_DAY, session_or_none, sessions_before, sessions_between
from tickr.backtest.settings import BacktestSettings
from tickr.backtest.universe import (
    CAP_FILE,
    POOL_FILE,
    active_screen,
    read_caps,
    read_pool,
    select_pool,
    write_caps,
    write_pool,
)
from tickr.calendar import NEW_YORK, MarketDay, SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.runtime.launchd import NIGHTLY_CURRENT, NIGHTLY_OUTCOME_FILE

CURRENT = NIGHTLY_CURRENT
STAGING = "staging"
PREVIOUS = "previous"
RAW = "raw"
OUTCOME = NIGHTLY_OUTCOME_FILE
LINKED_DIRS = (fetch.DAILY_DIR, fetch.MINUTES_DIR)  # large, never rewritten in place
DAILY_PREFIX = "n"  # tonight's daily chunks: n<last day>-<chunk>.jsonl
BYTES_PER_MB = 1024 * 1024
SECONDS_PER_MINUTE = 60


@dataclass(frozen=True)
class Outcome:
    finished_at: str
    first_day: str | None
    last_day: str | None
    new_sessions: int
    requests: int
    cause: str | None


class Deadline:
    """Stops the night once the next session's open minus the margin is reached: checked
    before every request and around every wait for the request budget."""

    def __init__(self, at: datetime, now: Callable[[], datetime],
                 sleep: Callable[[float], None]) -> None:
        self.at, self.now, self._sleep = at, now, sleep

    def check(self) -> None:
        if self.now() >= self.at:
            raise BacktestError(f"stopped at {self.at.isoformat()}, before the next session "
                                "([backtest] nightly_stop_minutes_before_open); current is "
                                "unchanged")

    def sleep(self, seconds: float) -> None:
        self.check()
        self._sleep(seconds)
        self.check()


class _Guarded:
    """A HistorySource that checks the deadline before each request."""

    def __init__(self, source: fetch.HistorySource, deadline: Deadline) -> None:
        self._source, self._deadline = source, deadline

    @property
    def requests(self) -> int:
        return self._source.requests

    def instruments(self, sub_category: str, pagination_key: str | None) -> Any:
        self._deadline.check()
        return self._source.instruments(sub_category, pagination_key)

    def daily_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[Any]:
        self._deadline.check()
        return self._source.daily_bars(symbols, end, count)

    def minute_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[Any]:
        self._deadline.check()
        return self._source.minute_bars(symbols, end, count)


def last_completed(calendar: SessionCalendar, now: datetime, lookback: int) -> MarketDay:
    """The newest session whose regular close has passed at `now`."""
    today = session_or_none(calendar, now.astimezone(NEW_YORK).date())
    if today is not None and now >= today.regular_close:
        return today
    return sessions_before(calendar, now.astimezone(NEW_YORK).date(), 1,
                           lookback)[0]


def next_open(calendar: SessionCalendar, now: datetime, lookback: int) -> datetime:
    """The regular open of the first session that has not closed at `now`."""
    day = now.astimezone(NEW_YORK).date()
    for ahead in range(lookback + 1):
        found = session_or_none(calendar, day + timedelta(days=ahead))
        if found is not None and now < found.regular_close:
            return found.regular_open
    raise BacktestError(f"no session within {lookback} days after {day.isoformat()}")


def run_nightly(source: fetch.HistorySource, calendar: SessionCalendar, cfg: Config,
                settings: BacktestSettings, history_sessions: int, root: Path,
                seed: Path | None, now: Callable[[], datetime],
                sleep: Callable[[float], None] = time.sleep,
                log: Callable[[str], None] = print) -> Outcome:
    """One night: add the missing completed sessions to `root/current` (see the module doc).
    Writes `root/nightly.json` either way; raises BacktestError with the cause when refused."""
    deadline: Deadline | None = None
    guarded: fetch.HistorySource = source
    try:
        lookback = cfg.market_data.history_max_lookback_days
        start = now()
        today = session_or_none(calendar, start.astimezone(NEW_YORK).date())
        if today is not None and today.in_regular_session(start):
            raise BacktestError("refused during the regular session")
        margin = timedelta(minutes=settings.nightly_stop_minutes_before_open)
        deadline = Deadline(next_open(calendar, start, lookback) - margin, now, sleep)
        deadline.check()
        guarded = _Guarded(source, deadline)
        root.mkdir(parents=True, exist_ok=True)
        free_mb = shutil.disk_usage(root).free // BYTES_PER_MB
        if free_mb < settings.nightly_min_free_disk_mb:
            raise BacktestError(f"{free_mb} MB free, below [backtest] nightly_min_free_disk_mb "
                                f"{settings.nightly_min_free_disk_mb}")
        base = root / CURRENT
        _recover(root)
        if not (base / POOL_FILE).exists():
            if seed is None or not (seed / POOL_FILE).exists():
                raise BacktestError(f"no {CURRENT}/{POOL_FILE} yet: pass --seed with a folder "
                                    "made by fetch-all")
            base = seed
        outcome = _night(guarded, calendar, cfg, settings, history_sessions, root, base,
                         last_completed(calendar, start, lookback), deadline, log)
    except BacktestError as exc:
        _write_outcome(root, Outcome(now().isoformat(), None, None, 0, guarded.requests,
                                     str(exc)))
        raise
    _write_outcome(root, outcome)
    return outcome


def _night(source: fetch.HistorySource, calendar: SessionCalendar, cfg: Config,
           settings: BacktestSettings, history_sessions: int, root: Path, base: Path,
           target: MarketDay, deadline: Deadline, log: Callable[[str], None]) -> Outcome:
    lookback = cfg.market_data.history_max_lookback_days
    pool = read_pool(base / POOL_FILE)
    if not pool:
        raise BacktestError(f"{base / POOL_FILE} is empty")
    have = max(e.session_date for e in pool)
    if have >= target.ny_date:
        log(f"nightly: up to date, data ends {have.isoformat()}")
        return Outcome(deadline.now().isoformat(), None, have.isoformat(), 0, source.requests,
                       None)
    new = sessions_between(calendar, have + ONE_DAY, target.ny_date)
    if len(new) > settings.nightly_max_new_sessions:
        raise BacktestError(f"{len(new)} sessions missing after {have.isoformat()}, more than "
                            f"[backtest] nightly_max_new_sessions "
                            f"{settings.nightly_max_new_sessions}: run fetch-all instead")
    staging = _stage(root, base)
    first, last = new[0].ny_date, target.ny_date
    log(f"nightly: adding {first.isoformat()} .. {last.isoformat()} ({len(new)} sessions)")

    # Daily bars of the new sessions (plus the one before, for its close), in their own chunks.
    symbols = [r["symbol"] for r in fetch.read_lines(staging / fetch.UNIVERSE_FILE)]
    scratch = staging / ".nightly"
    fetch.fetch_daily(source, calendar, cfg, symbols, first, last, scratch, deadline.sleep, log)
    rows = fetch.load_daily(scratch / fetch.DAILY_DIR)
    window = fetch.daily_window(calendar, first, last, lookback)
    added, caps = select_pool(rows, [d.ny_date for d in window], settings.gainer_change_percent,
                              active_screen(cfg),
                              settings.max_pool_per_day)
    _keep_new_daily(scratch / fetch.DAILY_DIR, staging / fetch.DAILY_DIR, first, last)
    shutil.rmtree(scratch)

    # The rolling window: the newest `nightly_window_sessions` sessions.
    keep_from = sessions_before(calendar, last, settings.nightly_window_sessions - 1,
                                lookback)[0].ny_date if settings.nightly_window_sessions > 1 \
        else last
    pool = [e for e in pool + added if e.session_date >= keep_from]
    old_caps = read_caps(staging / CAP_FILE) if (staging / CAP_FILE).exists() else []
    write_pool(pool, staging / POOL_FILE)
    write_caps([c for c in old_caps + caps if c.session_date >= keep_from], staging / CAP_FILE)

    # Minute bars the new pool days need and the folder does not hold yet.
    runs = _missing_runs(calendar, added, staging, history_sessions, lookback)
    old_bars = {}
    for symbol in runs:
        path = staging / fetch.MINUTES_DIR / f"{symbol}.jsonl"
        if path.exists():
            old_bars[symbol] = fetch.read_lines(path)
            path.unlink()  # a hard link: removing it leaves current's file untouched
    fetch.fetch_minutes(source, cfg, settings, runs, staging, deadline.sleep, log)
    for symbol, old in old_bars.items():
        _merge_minutes(staging / fetch.MINUTES_DIR / f"{symbol}.jsonl", old)
    _trim(staging, {e.symbol for e in pool})
    _swap(root, staging)
    log(f"nightly: data ends {last.isoformat()}, {source.requests} requests")
    return Outcome(deadline.now().isoformat(), first.isoformat(), last.isoformat(), len(new),
                   source.requests, None)


def _stage(root: Path, base: Path) -> Path:
    """A fresh `staging` copy of `base`: the large folders hard-linked, every other file copied
    (some are rewritten or appended in place by the fetch steps), raw/ left out."""
    staging = root / STAGING
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    for item in base.iterdir():
        if item.name == RAW:
            continue
        if item.is_dir() and item.name in LINKED_DIRS:
            shutil.copytree(item, staging / item.name, copy_function=os.link)
        elif item.is_file():
            shutil.copy2(item, staging / item.name)
    return staging


def _keep_new_daily(scratch: Path, daily: Path, first: date, last: date) -> None:
    """Tonight's chunks into `daily/`, renamed, without the session before `first` (already in
    the folder)."""
    for chunk in sorted(scratch.glob("*.jsonl")):
        rows = [r for r in fetch.read_lines(chunk) if date.fromisoformat(r["date"]) >= first]
        fetch._write_lines(daily / f"{DAILY_PREFIX}{last.isoformat()}-{chunk.name}", rows)


def _missing_runs(calendar: SessionCalendar, added: Sequence[Any], staging: Path,
                  history_sessions: int, lookback: int) -> dict[str, list[tuple[MarketDay, ...]]]:
    """Per new pool symbol: contiguous runs of the sessions its new days need (each day and its
    `history_sessions` before) that its minute file has no bar on."""
    need: dict[str, set[date]] = {}
    sessions: dict[date, MarketDay] = {}
    for entry in added:
        days = sessions_before(calendar, entry.session_date, history_sessions, lookback) \
            if history_sessions else []
        day = session_or_none(calendar, entry.session_date)
        if day is None:
            raise BacktestError(f"pool day {entry.session_date} is not a session")
        for d in (*days, day):
            sessions[d.ny_date] = d
            need.setdefault(entry.symbol, set()).add(d.ny_date)
    runs: dict[str, list[tuple[MarketDay, ...]]] = {}
    for symbol, days in need.items():
        path = staging / fetch.MINUTES_DIR / f"{symbol}.jsonl"
        held = _days_with_bars(path) if path.exists() else set()
        missing = sorted(days - held)
        if missing:
            runs[symbol] = _contiguous([sessions[d] for d in missing], calendar, lookback)
    return runs


def _days_with_bars(path: Path) -> set[date]:
    return {ny_date_of(datetime.fromisoformat(r["start"])) for r in fetch.read_lines(path)}


def _contiguous(days: list[MarketDay], calendar: SessionCalendar,
                lookback: int) -> list[tuple[MarketDay, ...]]:
    """Group sessions into runs of consecutive calendar sessions."""
    runs: list[list[MarketDay]] = []
    for day in days:
        if runs and sessions_before(calendar, day.ny_date, 1, lookback)[0] == runs[-1][-1]:
            runs[-1].append(day)
        else:
            runs.append([day])
    return [tuple(run) for run in runs]


def _merge_minutes(path: Path, old: list[dict[str, Any]]) -> None:
    """Old and new bars of one symbol, one per start, oldest first; tonight's bar wins."""
    new = fetch.read_lines(path) if path.exists() else []
    merged = {r["start"]: r for r in old}
    merged.update({r["start"]: r for r in new})
    fetch._write_lines(path, (merged[k] for k in sorted(merged, key=datetime.fromisoformat)))


def _trim(staging: Path, symbols: set[str]) -> None:
    """Drop minute files and statuses of symbols with no pool day left in the window."""
    for path in (staging / fetch.MINUTES_DIR).glob("*.jsonl"):
        if path.stem not in symbols:
            path.unlink()
    statuses = fetch.load_statuses(staging)
    fetch._write_lines(staging / fetch.MINUTES_STATUS_FILE,
                       (s for s in statuses if s["symbol"] in symbols))


def _recover(root: Path) -> None:
    """A night that stopped inside `_swap` (between moving current/ aside and moving staging/
    in) left only previous/: that is the last good data, so it becomes current/ again."""
    current, previous = root / CURRENT, root / PREVIOUS
    if not current.exists() and previous.exists():
        previous.rename(current)


def _swap(root: Path, staging: Path) -> None:
    current, previous = root / CURRENT, root / PREVIOUS
    if previous.exists():
        shutil.rmtree(previous)
    if current.exists():
        current.rename(previous)
    staging.rename(current)
    if previous.exists():
        shutil.rmtree(previous)


def _write_outcome(root: Path, outcome: Outcome) -> None:
    root.mkdir(parents=True, exist_ok=True)
    partial = root / (OUTCOME + ".partial")
    partial.write_text(json.dumps(outcome.__dict__, sort_keys=True) + "\n", "utf-8")
    os.replace(partial, root / OUTCOME)

