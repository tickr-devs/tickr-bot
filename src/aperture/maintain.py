"""`aperture maintain`: archive closed days' capture and decision files, checkpoint the database.

Run with the bot stopped: the command holds the bot's SingleInstanceLock for its whole run and
refuses when another process has it. Dry run by default; `--apply` acts.

A day folder (`<capture_dir|decisions_dir>/<environment>/<NY date>/`) has its `*.jsonl` files
compressed in place to `*.jsonl.gz` (`aperture.archive.compress`; every reader opens both) only
when all of these hold, otherwise it is kept with one printed cause:
- it is older than the last `[run] maintain_keep_sessions` closed sessions before today;
- it is older than the oldest trade still open in the database (any environment);
- the journal has its `day-end` row for this environment (the day was finished by the bot).
- no plain file has a `.gz` twin that is unreadable or holds other bytes.

Refusals (exit 3, nothing touched): the lock is held, the calendar cannot count the sessions,
the database is missing or has another schema version. The journal is never edited or vacuumed;
`--apply` only runs a WAL checkpoint (TRUNCATE), which leaves every row in place.
"""

from __future__ import annotations

import re
import sqlite3
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from aperture.archive import GZ_SUFFIX, compress, same_content
from aperture.calendar import CalendarRefused, SessionCalendar, ny_date_of
from aperture.config import Config
from aperture.data.history import NOT_A_SESSION
from aperture.runtime.dayend import DAY_END
from aperture.store import MIGRATIONS, Store

DAY_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLAIN_SUFFIX = ".jsonl"
WAL_SUFFIX = "-wal"


class MaintainRefused(Exception):
    """Maintenance cannot tell which days are finished; nothing was changed."""


@dataclass(frozen=True)
class DayPlan:
    folder: Path
    files: tuple[Path, ...]  # plain JSONL files still to archive
    keep: str | None  # why the folder is left as is, or None to archive it


def keep_from(calendar: SessionCalendar, today: date, sessions: int) -> date:
    """The oldest of the last `sessions` sessions before `today`; it and later days are kept."""
    day, found = today, 0
    while found < sessions:
        day -= timedelta(days=1)
        try:
            calendar.day(day)
        except CalendarRefused as exc:
            if exc.cause in NOT_A_SESSION:
                continue
            raise MaintainRefused(f"calendar cannot count sessions: {exc}") from exc
        found += 1
    return day


def _journal_facts(database: Path, environment: str) -> tuple[date | None, set[date]]:
    """(New York date of the oldest open trade, days with a day-end row in `environment`)."""
    if not database.is_file():
        raise MaintainRefused(f"no database at {database}: no day is known to be finished")
    store = Store.open_readonly(database)
    try:
        if store.schema_version != len(MIGRATIONS):
            raise MaintainRefused(f"database schema v{store.schema_version}, this code "
                                  f"expects v{len(MIGRATIONS)}")
        opened = [ny_date_of(datetime.fromisoformat(r["opened_at"])) for r in store.query(
            "SELECT opened_at FROM trades WHERE closed_at IS NULL")]
        ended = {date.fromisoformat(r["key"]) for r in store.query(
            "SELECT j.key FROM journal j JOIN runs r ON r.run_id = j.run_id "
            "WHERE j.kind = ? AND r.environment = ?", (DAY_END, environment))}
    finally:
        store.close()
    return (min(opened) if opened else None), ended


def _conflict(files: tuple[Path, ...]) -> str | None:
    """Why the folder must be looked at: a plain file whose .gz twin holds other bytes."""
    for file in files:
        archived = file.with_name(file.name + GZ_SUFFIX)
        if not archived.is_file():
            continue
        try:
            same = same_content(file, archived)
        except (OSError, EOFError, zlib.error) as exc:  # not gzip, truncated or corrupt
            return f"{archived.name} unreadable ({exc}); look at it, then remove it"
        if not same:
            return f"{file.name} and {archived.name} differ; look at both, then remove one"
    return None


def plan(cfg: Config, calendar: SessionCalendar, database: Path, today: date) -> list[DayPlan]:
    keep_day = keep_from(calendar, today, cfg.run.maintain_keep_sessions)
    oldest_open, ended = _journal_facts(database, cfg.environment_name)
    data = cfg.root / cfg.run.data_dir
    plans = []
    for base in (cfg.market_data.capture_dir, cfg.observers.decisions_dir):
        root = data / base / cfg.environment_name
        folders = sorted(root.iterdir()) if root.is_dir() else []
        for folder in folders:
            if not (folder.is_dir() and DAY_FOLDER.match(folder.name)):
                continue
            files = tuple(sorted(p for p in folder.iterdir()
                                 if p.is_file() and p.name.endswith(PLAIN_SUFFIX)))
            if not files:
                continue
            day = date.fromisoformat(folder.name)
            keep = None
            if day >= keep_day:
                keep = f"within the last {cfg.run.maintain_keep_sessions} sessions"
            elif oldest_open is not None and day >= oldest_open:
                keep = f"a trade opened {oldest_open.isoformat()} is still open"
            elif day not in ended:
                keep = "no day-end record for this day"
            else:
                keep = _conflict(files)
            plans.append(DayPlan(folder, files, keep))
    return plans


def checkpoint(database: Path) -> str:
    """WAL checkpoint (TRUNCATE): moves committed pages into the database file, deletes no row."""
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)
    try:
        busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        db.close()
    return "done" if busy == 0 else "incomplete: another reader is open (the API?); harmless"


def run(cfg: Config, calendar: SessionCalendar, database: Path, today: date, *,
        apply: bool, out: Callable[[str], None]) -> int:
    """Print (and with `apply` do) the plan. Exit 0 done, 1 an archive failed (it stops)."""
    plans = plan(cfg, calendar, database, today)
    verb = "archive" if apply else "would archive"
    for p in plans:
        size = sum(f.stat().st_size for f in p.files)
        if p.keep is not None:
            out(f"keep {p.folder}: {p.keep}")
            continue
        out(f"{verb} {p.folder}: {len(p.files)} files, {size} bytes")
        if not apply:
            continue
        for file in p.files:
            try:
                compress(file)
            except OSError as exc:
                out(f"FAILED {file}: {exc}; stopped here, the file is left as it was")
                return 1
    wal = database.with_name(database.name + WAL_SUFFIX)
    wal_size = wal.stat().st_size if wal.is_file() else 0
    if apply:
        out(f"database checkpoint ({wal_size} WAL bytes): {checkpoint(database)}")
    else:
        out(f"would checkpoint the database ({wal_size} WAL bytes); dry run, run with --apply")
    return 0
