"""GET /jobs: the bot's scheduled work, read-only (status file, database, config, calendar)."""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.jobs import NIGHTLY_NEVER_RAN, NO_BACKUP, NO_DATABASE, _nightly, jobs_document
from tickr.api.server import make_server, settings_from_config
from tickr.runtime.watch import BACKUP, BACKUP_FAILED
from tickr.store import Store

from .samples import T0, status
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]
MONDAY = date(2026, 11, 16)  # T0 is 10:00 New York on it


@pytest.fixture
def cfg(tmp_path):
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    return config.load(ROOT / "config" / "bot.toml", root=tmp_path)


def _response(settings, now, written):
    contract.write_status(settings.status_path, written)
    server = make_server(replace(settings, port=0), Clock(now))
    try:
        return server.status_response()
    finally:
        server.server_close()


def test_without_status_or_database_only_config_and_calendar_answer(cfg):
    settings = replace(settings_from_config(cfg), port=0)
    clock = Clock(T0)
    server = make_server(settings, clock)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        resp, payload = request(server, "GET", "/jobs")
        body = doc(payload)
        assert resp.status == 200
        assert errors(contract.json_schema(contract.JobsV1), body) == []
        assert request(server, "POST", "/jobs", {})[0].status == 405
    finally:
        server.shutdown()
        server.server_close()
    assert (body["bot_state"], body["bot_started_at"]) == (None, None)
    assert body["reconcile"] == {"interval_seconds": str(cfg.orders.reconcile_interval_seconds),
                                 "last_at": None, "next_at": None}
    assert body["backup"]["cause"] == NO_DATABASE and body["backup"]["last_at"] is None
    runtime = cfg.runtime
    assert [(j["name"], j["local_time"], j["weekdays"]) for j in body["launchd"]] == [
        ("preflight", runtime.preflight_start_local, list(runtime.launch_weekdays)),
        ("bot", runtime.bot_start_local, list(runtime.launch_weekdays)),
        ("watchdog", runtime.watchdog_start_local, list(runtime.launch_weekdays)),
        ("nightly-fetch", runtime.nightly_fetch_start_local, list(runtime.launch_weekdays))]
    assert body["nightly_fetch"] == {"finished_at": None, "last_day": None,
                                     "new_sessions": None, "cause": NIGHTLY_NEVER_RAN,
                                     "cause_codes": [{"code": "nightly_never_ran",
                                                      "params": {}}]}
    session = body["session"]
    assert session["day"] == MONDAY.isoformat() and body["session_cause"] is None
    close = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
    assert contract.parse_time(session["regular_close"]) == close
    assert contract.parse_time(session["last_entry_at"]) == close - timedelta(
        minutes=cfg.session.last_entry_minutes_before_close)
    assert contract.parse_time(session["flatten_at"]) == close - timedelta(
        minutes=cfg.session.flatten_minutes_before_close)
    # The calendar's extended hours: pre-market from 04:00, post-market to 20:00 New York.
    premarket, extended = datetime(2026, 11, 16, 9, 0, tzinfo=UTC), close + timedelta(hours=4)
    assert contract.parse_time(session["premarket_open"]) == premarket
    assert contract.parse_time(session["extended_close"]) == extended


def test_running_bot_gives_its_start_next_reconcile_and_last_backup(cfg, tmp_path):
    settings = settings_from_config(cfg)
    clock = iter(T0 - timedelta(hours=2) + timedelta(seconds=s) for s in range(100))
    store = Store.open(settings.jobs.database, busy_timeout_seconds=1, now=lambda: next(clock))
    run_id = store.start_run(environment="sandbox", config_digest="d", strategy="s",
                             strategy_version="1", code_version="c")
    with store.transaction():
        store.append(BACKUP_FAILED, {"error": "OperationalError", "detail": "/private/path"},
                     run_id=run_id)
        store.append(BACKUP, {"file": "tickr-2026-11-16.sqlite3"}, run_id=run_id)
    store.close()
    written = status(run_id=run_id)
    jobs = jobs_document(settings.jobs, _response(settings, T0 + timedelta(seconds=2), written))
    assert jobs.bot_state is contract.BotState.RUNNING and not jobs.status_stale
    assert jobs.bot_started_at == T0 - timedelta(hours=2)
    last_read = written.freshness.last_broker_read_at
    assert (jobs.reconcile.last_at, jobs.reconcile.next_at) == (
        last_read, last_read + timedelta(seconds=float(cfg.orders.reconcile_interval_seconds)))
    # The failure came before the backup that worked: not shown.
    assert jobs.backup == contract.BackupJobV1(T0 - timedelta(hours=2) + timedelta(seconds=3),
                                               None, None, None)

    # A newer failure is shown by its error type only (its text may hold paths).
    store = Store.open(settings.jobs.database, busy_timeout_seconds=1,
                       now=lambda: T0 - timedelta(minutes=1))
    with store.transaction():
        store.append(BACKUP_FAILED, {"error": "OSError", "detail": "/Users/someone/x"},
                     run_id=run_id)
    store.close()
    jobs = jobs_document(settings.jobs, _response(settings, T0 + timedelta(seconds=2), written))
    assert (jobs.backup.last_failed_at, jobs.backup.last_failed_error) == (
        T0 - timedelta(minutes=1), "OSError")
    assert "Users" not in contract.dumps(jobs)


def test_stopped_or_stale_bot_has_no_next_reconcile_and_a_weekend_no_session(cfg):
    settings = settings_from_config(cfg)
    saturday = T0 + timedelta(days=5)
    store = Store.open(settings.jobs.database, busy_timeout_seconds=1, now=lambda: T0)
    store.close()
    stale = jobs_document(settings.jobs, _response(settings, saturday, status()))
    assert stale.status_stale and stale.reconcile.next_at is None
    assert stale.reconcile.last_at is not None
    assert stale.backup == contract.BackupJobV1(None, None, None, NO_BACKUP)
    assert stale.session is None and "weekend" in (stale.session_cause or "")
    stopped = jobs_document(settings.jobs, _response(
        settings, T0, status(bot_state=contract.BotState.STOPPED)))
    assert not stopped.status_stale and stopped.reconcile.next_at is None


def test_half_day_off_is_no_session(cfg):
    settings = settings_from_config(replace(cfg, session=replace(cfg.session,
                                                                 half_days_enabled=False)))
    black_friday = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)  # a 13:00 close
    jobs = jobs_document(settings.jobs, _response(settings, black_friday,
                                                  status(written_at=black_friday)))
    assert jobs.session is None and "half_days_enabled = false" in (jobs.session_cause or "")
    on = settings_from_config(replace(cfg, session=replace(cfg.session, half_days_enabled=True)))
    jobs = jobs_document(on.jobs, _response(on, black_friday, status(written_at=black_friday)))
    assert jobs.session is not None
    assert jobs.session.regular_close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_nightly_fetch_outcome_is_read_as_written(tmp_path):
    from tickr.backtest.nightly import Outcome, _write_outcome

    _write_outcome(tmp_path, Outcome("2026-11-17T00:42:07+00:00", "2026-11-16", "2026-11-16", 1,
                                     812, None))
    done = _nightly(tmp_path / "nightly.json")
    assert (done.last_day, done.new_sessions, done.cause) == (date(2026, 11, 16), 1, None)
    _write_outcome(tmp_path, Outcome("2026-11-17T00:42:07+00:00", None, None, 0, 3,
                                     "refused during the regular session"))
    refused = _nightly(tmp_path / "nightly.json")
    assert refused.last_day is None and refused.cause == "refused during the regular session"
    (tmp_path / "nightly.json").write_text("{")
    assert _nightly(tmp_path / "nightly.json").cause.startswith("nightly.json unreadable")


def _next(settings, now):
    return jobs_document(settings.jobs, _response(settings, now, status(written_at=now)))


@pytest.mark.parametrize(("now", "day", "kind"), [
    # Inside Monday's session the next to open is Tuesday's; Monday before its open is Monday's.
    (T0, date(2026, 11, 17), contract.SessionKind.REGULAR),
    (datetime(2026, 11, 16, 12, 0, tzinfo=UTC), MONDAY, contract.SessionKind.REGULAR),
    # A weekday evening: tomorrow.
    (datetime(2026, 11, 17, 23, 30, tzinfo=UTC), date(2026, 11, 18), contract.SessionKind.REGULAR),
    # Friday after the close: Monday.
    (datetime(2026, 11, 20, 22, 0, tzinfo=UTC), date(2026, 11, 23), contract.SessionKind.REGULAR),
    # The evening before Thanksgiving (closed): the day after, a half day (13:00 close).
    (datetime(2026, 11, 25, 22, 0, tzinfo=UTC), date(2026, 11, 27), contract.SessionKind.HALF_DAY),
    # Christmas Eve's half day after its close: past Christmas and the weekend, to Monday.
    (datetime(2026, 12, 24, 19, 0, tzinfo=UTC), date(2026, 12, 28), contract.SessionKind.REGULAR),
    # New Year's Eve after the close: past New Year's Day and the weekend, in the next year's file.
    (datetime(2026, 12, 31, 22, 0, tzinfo=UTC), date(2027, 1, 4), contract.SessionKind.REGULAR),
])
def test_next_session_is_the_next_regular_open_after_now(cfg, now, day, kind):
    settings = settings_from_config(cfg)
    jobs = _next(settings, now)
    assert jobs.next_session_cause is None
    nxt = jobs.next_session
    assert nxt is not None and (nxt.day, nxt.kind) == (day, kind) and nxt.regular_open > now
    assert nxt.regular_open == datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(
        hour=14, minute=30)
    close_hour = 18 if kind is contract.SessionKind.HALF_DAY else 21
    assert nxt.regular_close.astimezone(UTC).hour == close_hour


def test_half_day_is_listed_even_when_the_bot_does_not_trade_it(cfg):
    off = settings_from_config(replace(cfg, session=replace(cfg.session, half_days_enabled=False)))
    jobs = _next(off, datetime(2026, 11, 25, 22, 0, tzinfo=UTC))
    assert jobs.next_session is not None
    assert jobs.next_session.kind is contract.SessionKind.HALF_DAY


def test_no_next_session_when_the_calendar_cannot_answer(cfg, tmp_path):
    for later in (tmp_path / "config" / "calendar").glob("us-equity-2027*.json"):
        later.unlink()
    settings = settings_from_config(cfg)
    # New Year's Eve after the close, with no 2027 calendar file: not covered.
    jobs = _next(settings, datetime(2026, 12, 31, 22, 0, tzinfo=UTC))
    assert jobs.next_session is None
    assert jobs.next_session_cause == "OUT_OF_COVERAGE: 2027-01-01 is not covered by any file"
    body = contract.to_jsonable(jobs)
    assert body["next_session_cause_codes"] == [{"code": "calendar_not_covered",
                                                 "params": {"day": "2027-01-01"}}]


def test_no_next_session_within_the_search_days(cfg):
    short = replace(cfg, api=replace(cfg.api, next_session_search_days=1))
    settings = settings_from_config(short)
    # Friday evening: Saturday is the last day looked at.
    jobs = _next(settings, datetime(2026, 11, 20, 22, 0, tzinfo=UTC))
    assert jobs.next_session is None
    assert jobs.next_session_cause == "no regular session within 1 days after 2026-11-20"
    body = contract.to_jsonable(jobs)
    assert body["next_session_cause_codes"] == [{"code": "no_next_session",
                                                 "params": {"days": "1", "day": "2026-11-20"}}]
