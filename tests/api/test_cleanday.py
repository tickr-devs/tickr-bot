"""Clean-day verdicts of GET /days (api/cleanday.py): from journal rows only, never a guess."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aperture.api import cleanday
from aperture.api import contract as api
from aperture.api.cleanday import clean_days
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.engine.core import Fence
from aperture.runtime import account, dayend, market, service, watch
from aperture.runtime.alerts import AlertKind
from aperture.store import Store

DAY = date(2026, 11, 16)
NEXT = date(2026, 11, 17)
TODAY = date(2026, 11, 20)
OPEN = datetime(2026, 11, 16, 9, 30, tzinfo=NEW_YORK)
Kind = api.CleanCauseKind
CALENDAR_DIR = Path(__file__).resolve().parents[2] / "config" / "calendar"
CALENDAR = SessionCalendar.load(CALENDAR_DIR)
MAX_GAP = Decimal(900)
EVERY = timedelta(minutes=5)


class Journal:
    def __init__(self, tmp_path, environment: str = "sandbox") -> None:
        self.now = OPEN
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=1,
                                now=lambda: self.now)
        self.run = self.store.start_run(environment=environment, config_digest="d",
                                        strategy="s", strategy_version="1", code_version="c")

    def at(self, when: datetime) -> Journal:
        self.now = when
        return self

    def row(self, kind: str, body: dict[str, Any], key: str | None = None) -> int:
        with self.store.transaction():
            return self.store.append(kind, body, key=key, run_id=self.run)

    def reconcile(self, matches: bool = True, mismatch: str | None = None) -> int:
        return self.row(account.RECONCILE_CHECKED, {"matches_records": matches,
                                                    "mismatch": mismatch})

    def day_end(self, day: date = DAY, day_clean: bool | None = True) -> int:
        self.now = datetime.combine(day, datetime.min.time(), NEW_YORK) + timedelta(hours=16)
        return self.row(dayend.DAY_END, {"day": day.isoformat(), "broker_flat": day_clean,
                                         "agree": day_clean, "open_trades": 0,
                                         "day_clean": day_clean}, key=day.isoformat())

    def session(self, day: date = DAY, skip: tuple[time, time] | None = None) -> None:
        """Agreeing rows every 5 minutes from 09:20 to 16:05 New York, as the bot writes them
        ([orders] reconcile_proof_seconds 300), none inside `skip`."""
        at = datetime.combine(day, time(9, 20), NEW_YORK)
        while at <= datetime.combine(day, time(16, 5), NEW_YORK):
            if skip is None or not skip[0] <= at.time() < skip[1]:
                self.at(at).reconcile()
            at += EVERY

    def verdict(self, day: date = DAY, today: date = TODAY,
                calendar: SessionCalendar | None = CALENDAR) -> api.CleanDay:
        return clean_days(self.store, "sandbox", [day], today, calendar, MAX_GAP)[day]


def _clean_day(tmp_path) -> Journal:
    journal = Journal(tmp_path)
    journal.session()
    journal.day_end()
    return journal


def test_the_names_read_are_the_names_the_bot_writes():
    assert cleanday.RECONCILE_CHECKED == account.RECONCILE_CHECKED
    assert cleanday.DAY_END == dayend.DAY_END
    assert cleanday.ALERT == watch.ALERT
    assert cleanday.COMMAND == service.COMMAND
    assert cleanday.UNPROTECTED_ALERT == AlertKind.UNPROTECTED_POSITION
    assert market.STREAM_LOST_EXIT in cleanday.SAFETY_EXITS
    assert cleanday.MISMATCH | cleanday.UNKNOWN_OUTCOME >= account.MISMATCH_FENCES


def test_agreeing_reconcile_rows_and_a_clean_day_end_are_clean(tmp_path):
    journal = _clean_day(tmp_path)
    verdict = journal.verdict()
    assert (verdict.verdict, verdict.cause, verdict.causes) == (api.CleanVerdict.CLEAN, None, ())
    assert verdict.reconcile_rows == 82
    assert verdict.first_reconcile_at == OPEN - timedelta(minutes=10)
    assert verdict.last_reconcile_at == datetime(2026, 11, 16, 16, 5, tzinfo=NEW_YORK)
    assert verdict.day_end_seq == journal.store.journal(dayend.DAY_END)[0].seq


def test_a_day_before_the_rows_were_recorded_is_unknown_not_recorded(tmp_path):
    journal = Journal(tmp_path)
    journal.day_end()
    verdict = journal.verdict()
    assert (verdict.verdict, verdict.cause) == (api.CleanVerdict.UNKNOWN, cleanday.NOT_RECORDED)
    assert verdict.reconcile_rows == 0 and verdict.day_end_seq is not None


@pytest.mark.parametrize(("day_clean", "verdict", "cause"), [
    (None, api.CleanVerdict.UNKNOWN, cleanday.DAY_END_UNKNOWN),
    ("missing", api.CleanVerdict.UNKNOWN, cleanday.NO_DAY_END),
    (False, api.CleanVerdict.NOT_CLEAN, None),
])
def test_the_day_end_row_decides_what_the_reconcile_rows_cannot(tmp_path, day_clean, verdict,
                                                                cause):
    journal = Journal(tmp_path)
    journal.reconcile()
    if day_clean != "missing":
        journal.day_end(day_clean=day_clean)
    found = journal.verdict()
    assert (found.verdict, found.cause) == (verdict, cause)
    if verdict is api.CleanVerdict.NOT_CLEAN:
        assert [c.kind for c in found.causes] == [Kind.DAY_END_NOT_CLEAN]
        assert found.causes[0].journal_seqs == (found.day_end_seq,)


def test_today_is_unknown_until_it_is_over(tmp_path):
    journal = _clean_day(tmp_path)
    assert journal.verdict(today=DAY).cause == cleanday.NOT_OVER
    assert journal.verdict(today=NEXT).verdict is api.CleanVerdict.CLEAN


def test_a_mismatching_pass_and_a_mismatch_fence_are_not_clean_with_their_rows(tmp_path):
    journal = _clean_day(tmp_path)
    journal.at(OPEN + timedelta(hours=1))
    bad = journal.reconcile(False, "foreign-order:m1: open order not in our records")
    fence = journal.row("fence-set", {"cause": "open order not in our records"},
                        key=f"{Fence.FOREIGN_ORDER}:m1")
    found = journal.verdict()
    assert found.verdict is api.CleanVerdict.NOT_CLEAN and found.cause is None
    (cause,) = found.causes
    assert cause.kind is Kind.RECONCILE_MISMATCH and cause.journal_seqs == (bad, fence)
    assert "foreign-order:m1" in cause.text


def test_an_unknown_outcome_counts_only_while_unresolved_at_the_end_of_the_day(tmp_path):
    journal = _clean_day(tmp_path)
    key = f"{Fence.UNKNOWN_ORDER}:ap-1-e1"
    journal.at(OPEN + timedelta(hours=1)).row("fence-set", {"cause": "crashed during send"}, key)
    journal.at(OPEN + timedelta(hours=1, minutes=1)).row("fence-cleared", {}, key)
    assert journal.verdict().verdict is api.CleanVerdict.CLEAN
    later = journal.at(OPEN + timedelta(hours=5)).row("fence-set", {"cause": "again"}, key)
    found = journal.verdict()
    assert [(c.kind, c.journal_seqs) for c in found.causes] == [
        (Kind.UNKNOWN_ORDER_OUTCOME, (later,))]
    # Still set on the next day: that day is not clean either, until it is cleared.
    journal.session(NEXT)
    journal.day_end(NEXT)
    assert journal.verdict(NEXT).causes[0].journal_seqs == (later,)
    journal.at(datetime(2026, 11, 18, 9, 30, tzinfo=NEW_YORK)).row("fence-cleared", {}, key)
    assert journal.verdict(NEXT).verdict is api.CleanVerdict.NOT_CLEAN
    journal.session(date(2026, 11, 18))
    journal.day_end(date(2026, 11, 18))
    assert journal.verdict(date(2026, 11, 18)).verdict is api.CleanVerdict.CLEAN


def test_unprotected_position_and_safety_exits_are_faults(tmp_path):
    journal = _clean_day(tmp_path)
    journal.at(OPEN + timedelta(hours=1))
    # The ordinary wait for a fresh fill's stop is no fault.
    journal.row("fence-set", {"cause": "verified stop covers 0 of 2"}, f"{Fence.UNPROTECTED}:t1")
    journal.row("exit-requested", {"cause": "session-flatten"}, "t1")
    journal.row("command", {"kind": "flatten", "outcome": "done"}, "c0")
    assert journal.verdict().verdict is api.CleanVerdict.CLEAN
    timeout = journal.row("fence-set", {"cause": "no verified stop for 30 s; exiting"},
                          f"{Fence.UNPROTECTED_TIMEOUT}:t1")
    alert = journal.row("alert", {"kind": "unprotected-position", "text": "t1"})
    stream = journal.row("exit-requested", {"cause": "stream-lost"}, "t1")
    emergency = journal.row("command", {"kind": "emergency", "outcome": "done"}, "c1")
    causes = {c.kind: c.journal_seqs for c in journal.verdict().causes}
    assert causes == {Kind.UNPROTECTED_POSITION: (timeout, alert),
                      Kind.SAFETY_EXIT: (stream, emergency)}


def test_a_fault_makes_a_day_not_clean_even_before_reconcile_rows_were_recorded(tmp_path):
    journal = Journal(tmp_path)
    seq = journal.row("exit-requested", {"cause": "stop-rejected"}, "t1")
    found = journal.verdict()
    assert found.verdict is api.CleanVerdict.NOT_CLEAN
    assert found.causes == (api.CleanCause(Kind.SAFETY_EXIT, "exit stop-rejected for trade t1",
                                           (seq,)),)


def test_another_environment_and_another_day_are_not_read(tmp_path):
    journal = _clean_day(tmp_path)
    journal.at(OPEN + timedelta(days=1)).reconcile(False, "x")
    other = Journal(tmp_path / "other", environment="production")
    other.reconcile(False, "x")
    assert journal.verdict().verdict is api.CleanVerdict.CLEAN
    assert other.verdict().cause == cleanday.NOT_RECORDED  # its rows are not "sandbox"


def test_an_unreadable_row_makes_every_day_unknown(tmp_path):
    journal = _clean_day(tmp_path)
    with journal.store.transaction():
        journal.store.execute(
            "INSERT INTO journal(at_utc, run_id, kind, key, body) VALUES (?, ?, ?, ?, ?)",
            ("not a time", journal.run, account.RECONCILE_CHECKED, None, "{}"))
    found = journal.verdict()
    assert found.verdict is api.CleanVerdict.UNKNOWN
    assert found.cause is not None and found.cause.startswith("journal unreadable: journal row")


def test_no_database_is_unknown(tmp_path):
    found = clean_days(None, "sandbox", [DAY, TODAY], TODAY, CALENDAR, MAX_GAP)
    assert found[DAY].cause == cleanday.NOT_RECORDED and found[TODAY].cause == cleanday.NOT_OVER


def test_days_carry_the_verdict(tmp_path):
    from aperture.api.history import History, HistoryPaths

    journal = _clean_day(tmp_path)
    journal.store.close()
    paths = HistoryPaths("sandbox", tmp_path / "db.sqlite3", tmp_path / "capture", CALENDAR_DIR,
                         MAX_GAP)
    (summary,) = History(paths).days(TODAY).days
    assert summary.day == DAY and summary.clean.verdict is api.CleanVerdict.CLEAN
    assert api.loads(api.DaysV1, api.dumps(History(paths).days(TODAY))).days == (summary,)


@pytest.mark.parametrize(("skip", "gap"), [
    ((time(11, 0), time(13, 0)), ("15:55:00", "18:00:00")),  # hours of failed reads
    ((time(9, 0), time(10, 0)), ("14:30:00", "15:00:00")),  # no row near the open
    ((time(15, 30), time(17, 0)), ("20:25:00", "21:00:00")),  # none near the close
])
def test_a_long_gap_between_reconcile_rows_in_the_session_is_unknown(tmp_path, skip, gap):
    journal = Journal(tmp_path)
    journal.session(skip=skip)
    journal.day_end()
    found = journal.verdict()
    assert found.verdict is api.CleanVerdict.UNKNOWN
    assert found.cause is not None and found.cause.startswith(
        f"no reconcile-checked row from 2026-11-16T{gap[0]}.000Z to 2026-11-16T{gap[1]}.000Z")
    assert "reconcile_max_gap_seconds 900" in found.cause


def test_a_gap_up_to_the_largest_allowed_and_one_outside_the_session_stay_clean(tmp_path):
    journal = Journal(tmp_path)
    # 10:00 -> 10:15 is exactly 900 s; 16:05 -> 19:00 is after the close.
    journal.session(skip=(time(10, 1), time(10, 15)))
    journal.at(datetime(2026, 11, 16, 19, 0, tzinfo=NEW_YORK)).reconcile()
    journal.day_end()
    assert journal.verdict().verdict is api.CleanVerdict.CLEAN


def test_without_a_calendar_no_day_is_clean(tmp_path):
    journal = _clean_day(tmp_path)
    assert journal.verdict(calendar=None).cause == cleanday.NO_CALENDAR
    weekend = date(2026, 11, 14)
    journal.session(weekend)
    journal.day_end(weekend)
    assert journal.verdict(weekend).cause.startswith("calendar: ")
