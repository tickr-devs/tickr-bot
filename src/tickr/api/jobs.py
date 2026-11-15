"""GET /jobs: the bot's scheduled work for the apps' Settings jobs strip (read-only).

Sources, none of them written here: the bot's status file (state, run, last broker read), the
database opened read-only (the run's start, the last `backup` and `backup-failed` journal rows),
`[runtime]` (the launchd schedule, validated by runtime.launchd), `[orders]
reconcile_interval_seconds`, and the session calendar with `[session]` cutoffs. The trading path
is not involved: the bot neither reads nor writes anything for this endpoint.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tickr.api import contract as api
from tickr.calendar import CalendarRefused, SessionCalendar
from tickr.config import Config
from tickr.runtime.launchd import LocalTime, schedule
from tickr.runtime.watch import BACKUP, BACKUP_FAILED
from tickr.store import DATABASE_FILE, Store

NO_DATABASE = "the bot has not created its database yet"
NO_BACKUP = "no backup recorded yet"


@dataclass(frozen=True)
class JobsSettings:
    database: Path
    calendar: Path  # the session calendar's folder
    reconcile_interval_seconds: Decimal
    last_entry_minutes_before_close: int
    flatten_minutes_before_close: int
    half_days_enabled: bool
    launchd: tuple[api.LaunchdJobV1, ...]

    @classmethod
    def from_config(cls, config: Config) -> JobsSettings:
        plan = schedule(config.runtime)
        days = plan.weekdays
        return cls(
            database=config.root / config.run.data_dir / DATABASE_FILE,
            calendar=(config.root / config.session.calendar_file).parent,
            reconcile_interval_seconds=Decimal(config.orders.reconcile_interval_seconds),
            last_entry_minutes_before_close=config.session.last_entry_minutes_before_close,
            flatten_minutes_before_close=config.session.flatten_minutes_before_close,
            half_days_enabled=config.session.half_days_enabled,
            launchd=(api.LaunchdJobV1("preflight", _hhmm(plan.preflight), days),
                     api.LaunchdJobV1("bot", _hhmm(plan.bot), days),
                     api.LaunchdJobV1("watchdog", _hhmm(plan.watchdog), days)))


def _hhmm(at: LocalTime) -> str:
    return f"{at.hour:02d}:{at.minute:02d}"


def jobs_document(settings: JobsSettings, response: api.StatusResponseV1) -> api.JobsV1:
    """The jobs document at `response.served_at`, from the status the server just read."""
    now = response.served_at
    status = response.status
    running = (status is not None and not response.stale
               and status.bot_state is api.BotState.RUNNING)
    last_read = status.freshness.last_broker_read_at if status is not None else None
    next_read = None
    if running and last_read is not None:
        next_read = last_read + timedelta(seconds=float(settings.reconcile_interval_seconds))
    started, backup = _from_database(settings, status.run_id if status is not None else None)
    session, cause = _session(settings, now)
    return api.JobsV1(
        served_at=now,
        bot_state=status.bot_state if status is not None else None,
        status_stale=response.stale,
        bot_started_at=started,
        reconcile=api.ReconcileJobV1(settings.reconcile_interval_seconds, last_read, next_read),
        backup=backup,
        launchd=settings.launchd,
        session=session,
        session_cause=cause,
    )


def _from_database(settings: JobsSettings,
                   run_id: str | None) -> tuple[datetime | None, api.BackupJobV1]:
    if not settings.database.exists():
        return None, api.BackupJobV1(None, None, None, NO_DATABASE)
    try:
        store = Store.open_readonly(settings.database)
        try:
            started = None
            if run_id is not None:
                rows = store.query("SELECT started_at FROM runs WHERE run_id = ?", (run_id,))
                started = datetime.fromisoformat(rows[0]["started_at"]) if rows else None
            done = _last(store, BACKUP)
            failed = _last(store, BACKUP_FAILED)
        finally:
            store.close()
    except (sqlite3.Error, ValueError) as exc:
        return None, api.BackupJobV1(None, None, None,
                                     f"database unreadable ({type(exc).__name__})")
    last_at = done[0] if done is not None else None
    failed_at, error = (None, None)
    if failed is not None and (last_at is None or failed[0] > last_at):
        failed_at, error = failed[0], str(failed[1].get("error") or "unknown")
    return started, api.BackupJobV1(last_at, failed_at, error,
                                    None if last_at is not None else NO_BACKUP)


def _last(store: Store, kind: str) -> tuple[datetime, dict[str, object]] | None:
    rows = store.query("SELECT at_utc, body FROM journal WHERE kind = ? ORDER BY seq DESC "
                       "LIMIT 1", (kind,))
    if not rows:
        return None
    return datetime.fromisoformat(rows[0]["at_utc"]), json.loads(rows[0]["body"])


def _session(settings: JobsSettings,
             now: datetime) -> tuple[api.SessionTimesV1 | None, str | None]:
    try:
        day = SessionCalendar.load(settings.calendar).day_at(now)
        times = day.session_times(settings.last_entry_minutes_before_close,
                                  settings.flatten_minutes_before_close)
    except CalendarRefused as exc:
        return None, str(exc)
    if day.half_day and not settings.half_days_enabled:  # as preflight and admission decide
        return None, f"no session {day.ny_date}: half day, half_days_enabled = false"
    return api.SessionTimesV1(day.ny_date, day.regular_open, times.last_entry, times.flatten,
                              day.regular_close), None
