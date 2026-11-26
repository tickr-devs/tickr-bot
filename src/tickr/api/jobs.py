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
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tickr.api import contract as api
from tickr.api.causes import safe_cause
from tickr.calendar import CalendarCause, CalendarRefused, SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.runtime.launchd import NIGHTLY_OUTCOME_FILE, LocalTime, schedule
from tickr.runtime.watch import BACKUP, BACKUP_FAILED
from tickr.store import DATABASE_FILE, Store

NO_DATABASE = "the bot has not created its database yet"
NO_BACKUP = "no backup recorded yet"
NIGHTLY_NEVER_RAN = "the nightly fetch has not run yet"


@dataclass(frozen=True)
class JobsSettings:
    database: Path
    calendar: Path  # the session calendar's folder
    reconcile_interval_seconds: Decimal
    last_entry_minutes_before_close: int
    flatten_minutes_before_close: int
    half_days_enabled: bool
    launchd: tuple[api.LaunchdJobV1, ...]
    nightly_outcome: Path  # the nightly job's nightly.json
    next_session_search_days: int  # how far ahead the next regular session is looked for

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
                     api.LaunchdJobV1("watchdog", _hhmm(plan.watchdog), days),
                     api.LaunchdJobV1("nightly-fetch", _hhmm(plan.nightly), days)),
            nightly_outcome=config.root / config.runtime.nightly_fetch_out / NIGHTLY_OUTCOME_FILE,
            next_session_search_days=config.api.next_session_search_days)


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
    next_session, next_cause = _next_session(settings, now)
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
        nightly_fetch=_nightly(settings.nightly_outcome),
        next_session=next_session,
        next_session_cause=next_cause,
    )


def _nightly(path: Path) -> api.NightlyFetchJobV1:
    """The nightly job's own outcome file, as it wrote it."""
    if not path.exists():
        return api.NightlyFetchJobV1(None, None, None, NIGHTLY_NEVER_RAN)
    try:
        body = json.loads(path.read_text("utf-8"))
        last = body["last_day"]
        return api.NightlyFetchJobV1(
            datetime.fromisoformat(body["finished_at"]),
            None if last is None else date.fromisoformat(last), int(body["new_sessions"]),
            body["cause"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return api.NightlyFetchJobV1(None, None, None,
                                     f"nightly.json unreadable ({type(exc).__name__})")


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
        return None, safe_cause(exc)
    if day.half_day and not settings.half_days_enabled:  # as preflight and admission decide
        return None, f"no session {day.ny_date}: half day, half_days_enabled = false"
    return api.SessionTimesV1(day.ny_date, day.regular_open, times.last_entry, times.flatten,
                              day.regular_close, day.premarket_open, day.extended_close), None


# A day the calendar answers with no session: the search for the next one goes on past it.
_NO_SESSION = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})


def _next_session(settings: JobsSettings,
                  now: datetime) -> tuple[api.NextSessionV1 | None, str | None]:
    """The first regular session in the calendar whose open is after `now`, looked for over
    `[api] next_session_search_days` days from today; any other calendar refusal is the cause.
    Display only: a half day is listed as one whether or not the bot trades it."""
    try:
        calendar = SessionCalendar.load(settings.calendar)
        today = ny_date_of(now)
        for ahead in range(settings.next_session_search_days + 1):
            try:
                day = calendar.day(today + timedelta(days=ahead))
            except CalendarRefused as refused:
                if refused.cause in _NO_SESSION:
                    continue
                raise
            if day.regular_open > now:
                kind = api.SessionKind.HALF_DAY if day.half_day else api.SessionKind.REGULAR
                return api.NextSessionV1(day.ny_date, day.regular_open, day.regular_close,
                                         kind), None
    except CalendarRefused as exc:
        return None, safe_cause(exc)
    return None, (f"no regular session within {settings.next_session_search_days} days after "
                  f"{today.isoformat()}")
