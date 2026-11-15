"""`tickr maintain`: archive closed days' capture and decision files, prune the chart and
history caches, delete very old archives (off by default), checkpoint the database.

Run with the bot stopped: the command holds the bot's SingleInstanceLock for its whole run and
refuses when another process has it. Dry run by default; `--apply` acts.

A day folder (`<capture_dir|decisions_dir>/<environment>/<NY date>/`) has its `*.jsonl` files
compressed in place to `*.jsonl.gz` (`tickr.archive.compress`; every reader opens both) only
when all of these hold, otherwise it is kept with one printed cause:
- it is older than the last `[run] maintain_keep_sessions` closed sessions before today;
- it is older than the oldest trade still open in the database (any environment);
- the journal has its `day-end` row for this environment (the day was finished by the bot).
- no plain file has a `.gz` twin that is unreadable or holds other bytes.

Chart cache (`<[chart] cache_dir>/<environment>/<symbol>/<NY date>.json`, written by the API):
day files older than `[chart] cache_max_age_days` before today are deleted (a later chart of that
day reads it from Webull again). Symbol folders are left in place, even empty, so a running API
never loses the folder it is writing into.

History cache (`<[market_data] history_dir>/<environment>/<NY date asked on>/`, written by the
bot): day folders older than `[market_data] history_cache_max_age_days` before today are deleted
whole (the bot reads only today's folder). A folder holding anything but plain cache files
(`<SYMBOL>-<sessions>.jsonl` or its `.partial`) is kept with a printed cause.

Old archives: with `[run] maintain_archive_max_age_days` above 0, a capture or decision day
folder older than that many days before today, and outside the kept sessions, whose files are
all archives (`*.jsonl.gz`, so
it was archived by an earlier run) is deleted, under the same guards as archiving (an open
trade, a missing day-end row keep it). 0, the shipped value, never deletes an archive.

Today is never touched by any step.

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

from tickr.archive import GZ_SUFFIX, compress, same_content
from tickr.calendar import CalendarRefused, SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.data.history import NOT_A_SESSION, SYMBOL_PATTERN
from tickr.runtime.dayend import DAY_END
from tickr.store import MIGRATIONS, Store

DAY_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLAIN_SUFFIX = ".jsonl"
CHART_SUFFIX = ".json"
WAL_SUFFIX = "-wal"
HISTORY_SUFFIXES = (".jsonl", ".partial")  # a cache file and its unfinished write (data.history)


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


@dataclass(frozen=True)
class FolderPlan:
    folder: Path
    files: tuple[Path, ...]  # every file the folder holds, to delete with it
    size: int  # their bytes
    keep: str | None  # why the folder is left as is, or None to delete it


def _day_folders(root: Path) -> list[tuple[Path, date]]:
    """(folder, NY date) of the real day folders directly under `root`; symlinks and names that
    are not a date are skipped (never ours to touch)."""
    found = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        if folder.is_symlink() or not folder.is_dir() or not DAY_FOLDER.match(folder.name):
            continue
        try:
            found.append((folder, date.fromisoformat(folder.name)))
        except ValueError:
            continue
    return found


def _whole_folder(folder: Path, ours: Callable[[str], bool]) -> FolderPlan:
    """The folder with all its files, kept with a cause when one entry is not `ours` (another
    name, a symlink, a subfolder) or vanishes meanwhile."""
    files, size = [], 0
    for entry in sorted(folder.iterdir()):
        if entry.is_symlink() or not entry.is_file() or not ours(entry.name):
            return FolderPlan(folder, (), 0, f"{entry.name} is not ours to delete; look at it")
        try:
            size += entry.stat().st_size
        except FileNotFoundError:
            return FolderPlan(folder, (), 0, f"{entry.name} changed meanwhile; run again")
        files.append(entry)
    return FolderPlan(folder, tuple(files), size, None)


def _history_file(name: str) -> bool:
    for suffix in HISTORY_SUFFIXES:
        if name.endswith(suffix):
            symbol, dash, sessions = name[:-len(suffix)].rpartition("-")
            return bool(dash) and sessions.isdigit() and SYMBOL_PATTERN.match(symbol) is not None
    return False


def history_root(cfg: Config) -> Path:
    return cfg.root / cfg.run.data_dir / cfg.market_data.history_dir / cfg.environment_name


def stale_history(cfg: Config, today: date) -> list[FolderPlan]:
    """History cache day folders older than `[market_data] history_cache_max_age_days`."""
    oldest_kept = today - timedelta(days=cfg.market_data.history_cache_max_age_days)
    return [_whole_folder(folder, _history_file)
            for folder, day in _day_folders(history_root(cfg)) if day < oldest_kept]


def old_archives(cfg: Config, today: date, keep_day: date, oldest_open: date | None,
                 ended: set[date]) -> list[FolderPlan]:
    """Fully archived capture and decision day folders older than `[run]
    maintain_archive_max_age_days` and than `keep_day` (the last `maintain_keep_sessions`
    sessions are never deleted, even when gzipped by hand); none when the age is 0. A folder
    that still holds a plain file is not an archive yet and is left to the archiving step."""
    if cfg.run.maintain_archive_max_age_days == 0:
        return []
    oldest_kept = min(keep_day, today - timedelta(days=cfg.run.maintain_archive_max_age_days))
    plans = []
    for root in _day_roots(cfg):
        for folder, day in _day_folders(root):
            if day >= oldest_kept or any(p.name.endswith(PLAIN_SUFFIX) for p in folder.iterdir()):
                continue
            found = _whole_folder(folder, lambda name: name.endswith(PLAIN_SUFFIX + GZ_SUFFIX))
            if not found.files and found.keep is None:
                continue  # empty: nothing to free
            if oldest_open is not None and day >= oldest_open:
                found = FolderPlan(folder, (), 0, f"a trade opened {oldest_open.isoformat()} is "
                                   "still open")
            elif day not in ended:
                found = FolderPlan(folder, (), 0, "no day-end record for this day")
            plans.append(found)
    return plans


def _day_roots(cfg: Config) -> tuple[Path, ...]:
    data = cfg.root / cfg.run.data_dir
    return tuple(data / base / cfg.environment_name
                 for base in (cfg.market_data.capture_dir, cfg.observers.decisions_dir))


def plan(cfg: Config, keep_day: date, oldest_open: date | None,
         ended: set[date]) -> list[DayPlan]:
    plans = []
    for root in _day_roots(cfg):
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


def chart_root(cfg: Config) -> Path:
    return cfg.root / cfg.run.data_dir / cfg.chart.cache_dir / cfg.environment_name


def stale_chart_files(cfg: Config, today: date) -> list[tuple[Path, int]]:
    """(file, size) of the chart cache day files of this environment older than `[chart]
    cache_max_age_days`. Symlinked folders and files are skipped (never followed out of the
    cache), and so is a file that vanishes meanwhile."""
    root = chart_root(cfg)
    oldest_kept = today - timedelta(days=cfg.chart.cache_max_age_days)
    stale = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        if folder.is_symlink() or not folder.is_dir():
            continue
        for file in sorted(folder.glob(f"*{CHART_SUFFIX}")):
            try:
                day = date.fromisoformat(file.stem) if DAY_FOLDER.match(file.stem) else None
            except ValueError:
                day = None  # not a day file: never ours to delete
            if day is None or day >= oldest_kept or file.is_symlink():
                continue
            try:
                stale.append((file, file.stat().st_size))
            except FileNotFoundError:
                continue  # deleted meanwhile
    return stale


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
    """Print (and with `apply` do) the plan. Exit 0 done, 1 an archive or delete failed (it
    stops)."""
    oldest_open, ended = _journal_facts(database, cfg.environment_name)
    keep_day = keep_from(calendar, today, cfg.run.maintain_keep_sessions)
    plans = plan(cfg, keep_day, oldest_open, ended)
    # Planned before archiving, so a folder archived by this run is never deleted by it too and
    # the dry run lists exactly what --apply deletes.
    deletions = (("history cache", stale_history(cfg, today)),
                 ("archived", old_archives(cfg, today, keep_day, oldest_open, ended)))
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
    stale = stale_chart_files(cfg, today)
    if stale:
        size = sum(size for _, size in stale)
        out(f"{'delete' if apply else 'would delete'} {len(stale)} chart cache files older than "
            f"{cfg.chart.cache_max_age_days} days, {size} bytes")
    if apply:
        inside = chart_root(cfg).resolve()
        for file, _ in stale:
            if file.is_symlink() or not file.resolve().is_relative_to(inside):
                out(f"skip {file}: not a plain file inside the chart cache")
                continue
            try:
                file.unlink(missing_ok=True)
            except OSError as exc:
                out(f"FAILED {file}: {exc}; stopped here")
                return 1
    for what, folders in deletions:
        if _delete_folders(what, folders, apply, out) != 0:
            return 1
    wal = database.with_name(database.name + WAL_SUFFIX)
    wal_size = wal.stat().st_size if wal.is_file() else 0
    if apply:
        out(f"database checkpoint ({wal_size} WAL bytes): {checkpoint(database)}")
    else:
        out(f"would checkpoint the database ({wal_size} WAL bytes); dry run, run with --apply")
    return 0


def _delete_folders(what: str, folders: list[FolderPlan], apply: bool,
                    out: Callable[[str], None]) -> int:
    """Print (and with `apply` delete) each folder and its files. 0 done, 1 a delete failed (it
    stops; what is left stays for the next run)."""
    verb = "delete" if apply else "would delete"
    for p in folders:
        if p.keep is not None:
            out(f"keep {what} {p.folder}: {p.keep}")
            continue
        out(f"{verb} {what} {p.folder}: {len(p.files)} files, {p.size} bytes")
        if not apply:
            continue
        try:
            for file in p.files:
                file.unlink()
            p.folder.rmdir()  # fails when something new appeared meanwhile: never deleted blind
        except OSError as exc:
            out(f"FAILED {p.folder}: {exc}; stopped here")
            return 1
    return 0
