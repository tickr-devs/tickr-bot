"""Clean-day verdicts for GET /days, from the bot's journal only (docs/api.md, "Clean day").

A New York date is clean when the journal proves the broker and the bot's records agreed all
day: reconcile passes were recorded and none mismatched, no unknown order outcome was left
unresolved at the day's end, no position went unprotected, no exit was forced by a safety fault
and no emergency command ran, and the day-end check found the broker flat and agreeing.

Refuse, never guess: a fault proven by a row makes the date `not_clean` (each cause names its
rows); otherwise anything the journal does not prove makes it `unknown` with one cause (the day
is not over, no `reconcile-checked` row that date, no day-end row, unknown day-end reads, a gap
between reconcile rows in the regular session longer than `[orders] reconcile_max_gap_seconds`,
a row that cannot be read). Read-only and display only: nothing in the bot reads it.

The journal kinds read here are written by the runtime; `tests/api/test_cleanday.py` checks
that these names equal the writers' own constants.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from tickr.api import contract as api
from tickr.calendar import NEW_YORK, CalendarRefused, SessionCalendar, ny_date_of
from tickr.engine.core import Fence
from tickr.store import Store

RECONCILE_CHECKED = "reconcile-checked"  # runtime.account.RECONCILE_CHECKED
DAY_END = "day-end"  # runtime.dayend.DAY_END
ALERT = "alert"  # runtime.watch.ALERT
COMMAND = "command"  # runtime.service.COMMAND
FENCE_SET, FENCE_CLEARED = "fence-set", "fence-cleared"  # engine.core
EXIT_REQUESTED = "exit-requested"  # engine.core.request_exit
KINDS = (RECONCILE_CHECKED, DAY_END, ALERT, COMMAND, FENCE_SET, FENCE_CLEARED, EXIT_REQUESTED)

# Fences whose setting proves the broker and the records differed (runtime.account
# MISMATCH_FENCES without the unknown-outcome ones below).
MISMATCH = frozenset({Fence.FOREIGN_POSITION, Fence.FOREIGN_ORDER, Fence.QUANTITY_MISMATCH,
                      Fence.RESURRECTED, Fence.ORDER_MISSING, Fence.OVER_PROTECTED})
# Fences of an order (or cancel, or replace) whose outcome is not known; a fault when still set
# at the end of the date.
UNKNOWN_OUTCOME = frozenset({Fence.UNKNOWN_ORDER, Fence.STATUS_UNKNOWN, Fence.CANCEL_UNKNOWN,
                             Fence.REPLACE_UNRESOLVED, Fence.DETAIL_UNREADABLE,
                             Fence.ORDER_CONFLICT})
UNPROTECTED = frozenset({Fence.UNPROTECTED_TIMEOUT, Fence.STOP_REJECTED})
UNPROTECTED_ALERT = "unprotected-position"  # runtime.alerts.AlertKind.UNPROTECTED_POSITION
# Exit causes the engine and runtime give when a safety fault forces the close
# (engine.core: stop-rejected, unprotected-too-long; runtime.market.STREAM_LOST_EXIT).
SAFETY_EXITS = frozenset({"stop-rejected", "unprotected-too-long", "stream-lost"})
EMERGENCY = str(api.CommandKind.EMERGENCY)

NOT_OVER = "the day is not over"
NOT_RECORDED = ("not recorded: no reconcile-checked row that date (the bot records them since "
                "[orders] reconcile_proof_seconds was added)")
NO_DAY_END = "no day-end row that date (the bot was not running after the flatten time)"
DAY_END_UNKNOWN = "the day-end check had no broker reads (day_clean null)"
NO_CALENDAR = "the session calendar cannot be read"


class _Unreadable(ValueError):
    pass


@dataclass
class _Day:
    reconcile: list[tuple[int, datetime]] = field(default_factory=list)
    faults: dict[api.CleanCauseKind, list[tuple[int, str]]] = field(default_factory=dict)
    day_end: tuple[int, dict[str, Any]] | None = None

    def copy(self) -> _Day:
        """A copy a verdict may add its end-of-day faults to (the fold's own stays as read)."""
        return _Day(list(self.reconcile), {k: list(v) for k, v in self.faults.items()},
                    self.day_end)

    def fault(self, kind: api.CleanCauseKind, seq: int, text: str) -> None:
        self.faults.setdefault(kind, []).append((seq, text))


def _end_of(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(), NEW_YORK)


def _read(row: Any) -> tuple[datetime, dict[str, Any]]:
    try:
        at = datetime.fromisoformat(row["at_utc"])
        body = json.loads(row["body"])
        if at.tzinfo is None or not isinstance(body, dict):
            raise ValueError("no UTC offset or not an object")
        return at, body
    except (ValueError, TypeError) as exc:
        raise _Unreadable(f"journal row {row['seq']} ({row['kind']}): {exc}") from exc


class JournalFold:
    """The clean-day rows of one environment folded in journal order: rows bucketed by New
    York date, and every unknown-outcome fence's (set seq, set at, cleared at) stretches.

    Extended with only the rows after the last one it read, so a long-running reader (the API
    server answering GET /days every few seconds) decodes each journal row once, not the whole
    journal per request (backlog T17). The journal is append-only and SQLite commits one
    writer at a time, so a row never appears below one already read. A database where the
    last row read is gone or differs is another file (restored from a backup, say): it is
    folded again from the start."""

    def __init__(self, environment: str) -> None:
        self.environment = environment
        self._reset()

    def _reset(self) -> None:
        self.days: dict[date, _Day] = {}
        self.stretches: dict[str, list[tuple[int, datetime, datetime | None]]] = {}
        self.last_seq = 0
        self.last_at: str | None = None  # at_utc of row `last_seq` (same file check)
        self.unreadable: str | None = None  # the first row that could not be read

    def extend(self, store: Store) -> None:
        if self.last_seq:
            same = store.query("SELECT at_utc FROM journal WHERE seq = ?", (self.last_seq,))
            if not same or same[0]["at_utc"] != self.last_at:
                self._reset()
        if self.unreadable is not None:
            return  # every verdict is unknown with that cause until the file changes
        marks = ", ".join("?" for _ in KINDS)
        rows = store.query(
            f"SELECT seq, at_utc, kind, key, body FROM journal WHERE kind IN ({marks}) "
            "AND seq > ? AND run_id IN (SELECT run_id FROM runs WHERE environment = ?) "
            "ORDER BY seq", (*KINDS, self.last_seq, self.environment))
        try:
            for row in rows:
                self._add(row)
                self.last_seq, self.last_at = row["seq"], row["at_utc"]
        except _Unreadable as exc:
            self.unreadable = str(exc)

    def _add(self, row: Any) -> None:
        at, body = _read(row)
        kind, key, seq = row["kind"], row["key"] or "", row["seq"]
        day = self.days.setdefault(ny_date_of(at), _Day())
        fence = key.split(":", 1)[0]
        stretches = self.stretches
        if kind == RECONCILE_CHECKED:
            matches = body.get("matches_records")
            if not isinstance(matches, bool):
                raise _Unreadable(f"journal row {seq} ({kind}): no matches_records")
            day.reconcile.append((seq, at))
            if not matches:
                day.fault(api.CleanCauseKind.RECONCILE_MISMATCH, seq,
                          f"reconcile: {body.get('mismatch')}")
        elif kind == DAY_END:
            if key == ny_date_of(at).isoformat():
                day.day_end = (seq, body)
        elif kind == FENCE_SET and fence in MISMATCH:
            day.fault(api.CleanCauseKind.RECONCILE_MISMATCH, seq, f"{key}: {body.get('cause')}")
        elif kind == FENCE_SET and fence in UNPROTECTED:
            day.fault(api.CleanCauseKind.UNPROTECTED_POSITION, seq,
                      f"{key}: {body.get('cause')}")
        elif kind == FENCE_SET and fence in UNKNOWN_OUTCOME:
            stretches.setdefault(key, []).append((seq, at, None))
        elif kind == FENCE_CLEARED and fence in UNKNOWN_OUTCOME and stretches.get(key):
            first, since, cleared = stretches[key][-1]
            if cleared is None:
                stretches[key][-1] = (first, since, at)
        elif kind == ALERT and body.get("kind") == UNPROTECTED_ALERT:
            day.fault(api.CleanCauseKind.UNPROTECTED_POSITION, seq,
                      f"alert: {body.get('text')}")
        elif kind == EXIT_REQUESTED and body.get("cause") in SAFETY_EXITS:
            day.fault(api.CleanCauseKind.SAFETY_EXIT, seq,
                      f"exit {body.get('cause')} for trade {key}")
        elif kind == COMMAND and body.get("kind") == EMERGENCY:
            day.fault(api.CleanCauseKind.SAFETY_EXIT, seq,
                      f"emergency command ({body.get('outcome')})")


def _gap(day: date, times: list[datetime], calendar: SessionCalendar | None,
         max_gap: Decimal) -> str | None:
    """Why the reconcile rows do not cover the regular session, or None when they do: every
    gap between the last row before the open (else the open), the rows inside the session and
    the first row after the close (else the close) is at most `max_gap` seconds."""
    if calendar is None:
        return NO_CALENDAR
    try:
        session = calendar.day(day)
    except CalendarRefused as exc:
        return f"calendar: {exc}"
    start, end = session.regular_open, session.regular_close
    before = [t for t in times if t <= start]
    after = [t for t in times if t >= end]
    points = [max(before, default=start), *sorted(t for t in times if start < t < end),
              min(after, default=end)]
    for earlier, later in zip(points, points[1:], strict=False):
        seconds = (later - earlier).total_seconds()
        if seconds > max_gap:
            return (f"no reconcile-checked row from {api.format_time(earlier)} to "
                    f"{api.format_time(later)} ({seconds:.0f} s, more than [orders] "
                    f"reconcile_max_gap_seconds {max_gap})")
    return None


def _verdict(day: date, today: date, found: _Day,
             stretches: dict[str, list[tuple[int, datetime, datetime | None]]],
             calendar: SessionCalendar | None, max_gap: Decimal) -> api.CleanDay:
    end = _end_of(day)
    for key, spans in sorted(stretches.items()):
        for seq, since, cleared in spans:
            if since < end and (cleared is None or cleared >= end):
                found.fault(api.CleanCauseKind.UNKNOWN_ORDER_OUTCOME, seq,
                            f"{key}: still set at the end of the day")
    if found.day_end is not None and found.day_end[1].get("day_clean") is False:
        body = found.day_end[1]
        found.fault(api.CleanCauseKind.DAY_END_NOT_CLEAN, found.day_end[0],
                    f"day end: broker_flat {body.get('broker_flat')}, agree "
                    f"{body.get('agree')}, open_trades {body.get('open_trades')}")
    times = [at for _, at in found.reconcile]
    causes = tuple(
        api.CleanCause(kind, "; ".join(dict.fromkeys(text for _, text in rows)),
                       tuple(sorted(seq for seq, _ in rows)))
        for kind, rows in sorted(found.faults.items()))
    day_end = None if found.day_end is None else found.day_end[0]
    cause = (NOT_OVER if day >= today
             else None if causes
             else NOT_RECORDED if not times
             else NO_DAY_END if found.day_end is None
             else DAY_END_UNKNOWN if found.day_end[1].get("day_clean") is not True
             else _gap(day, times, calendar, max_gap))
    verdict = (api.CleanVerdict.UNKNOWN if cause is not None
               else api.CleanVerdict.NOT_CLEAN if causes else api.CleanVerdict.CLEAN)
    return api.CleanDay(verdict, cause, causes if verdict is api.CleanVerdict.NOT_CLEAN else (),
                        len(times), min(times, default=None), max(times, default=None),
                        day_end)


def unknown(day_cause: str) -> api.CleanDay:
    return api.CleanDay(api.CleanVerdict.UNKNOWN, day_cause, (), 0, None, None, None)


def clean_days(store: Store | None, environment: str, days: Iterable[date], today: date,
               calendar: SessionCalendar | None, max_gap: Decimal,
               fold: JournalFold | None = None) -> dict[date, api.CleanDay]:
    """The verdict of each date. `store` None (no database yet): every date is unknown. A
    journal row that cannot be read makes every date unknown with that cause. `calendar` (None:
    it cannot be read) gives the regular session the reconcile rows must cover with no gap
    longer than `max_gap` seconds ([orders] reconcile_max_gap_seconds). `fold` (the caller's
    own, of `environment`, kept between calls) is extended with the new rows only; None folds
    the whole journal for this call."""
    wanted = list(days)
    if store is None:
        return {d: unknown(NOT_OVER if d >= today else NOT_RECORDED) for d in wanted}
    fold = fold if fold is not None else JournalFold(environment)
    fold.extend(store)
    if fold.unreadable is not None:
        return {d: unknown(f"journal unreadable: {fold.unreadable}") for d in wanted}
    return {d: _verdict(d, today, fold.days.get(d, _Day()).copy(), fold.stretches, calendar,
                        max_gap)
            for d in wanted}
