"""Probe prev-day (`tickr probe-prev-day`): when do the previous session's inputs appear?

The bot starts at the calendar's pre-market open (04:00 ET) and needs the previous session then:
the 8 hist features and every feature built on the previous close read its minute bars, and the
carried state is put on today's scale with the splits. Absence at 23:00 does not establish
absence at 04:00 (codex-102, point 3), so this probe measures the real availability. In the
overnight window `[probe] prev_day_window_start`-`prev_day_window_end` (New York time), every
`prev_day_poll_seconds` it checks, read only:

- `minute_aggs`: the previous session's minute-bar day file, the one `massive.History` fetches
  (a HEAD signed as History signs its GET: status, size, ETag, Last-Modified);
- one input per `[probe] prev_day_tick_prefixes` entry (the trades and quotes day files), HEAD
  only;
- `splits`: Massive's splits listing (`massive.Reference.splits`) for the execution dates from
  the previous session through the start day: the count per date and a digest of the rows.

The night is named by its start day: the first trading day of the bot's calendar whose 04:00 ET
start comes after the window opens; the previous session is the calendar's session before it
(`massive.previous_sessions`), never a guessed weekday, so weekends, holidays and early closes
come out as the calendar says. Each answer is one JSON line in
`<data_dir>/<[probe] evidence_dir>/prev-day/<start day>.jsonl`, written and synced before the
next request; `event` is `first_present`, `changed` (size, ETag or Last-Modified differ from the
last present answer: a republish), `gone` or `check`. A status other than 200 or 404 and a failed
request are recorded with their cause and `present` null: unknown, never taken as absent. A
restart in the same night continues the file. Nothing is downloaded or kept besides those lines.

`tickr probe-prev-day --summary` prints, over every captured night, when each input was first
present relative to the start day's 04:00 ET. A night whose first check already found the input
only bounds that time (`<=`): it appeared before the window opened.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import statistics
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tickr.calendar import NEW_YORK, CalendarRefused, SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.model import massive
from tickr.model.errors import ModelRefused

FOLDER = "prev-day"
MINUTE_AGGS = "minute_aggs"
SPLITS = "splits"
HTTP_OK = 200
# The response headers that tell one published file from another.
FILE_HEADERS = {"size": "Content-Length", "etag": "ETag", "last_modified": "Last-Modified"}
# The flat files' and the REST lists' HTTP opener (a stand-in in the tests).
OPENER: massive.Opener = massive._open


@dataclass(frozen=True)
class Night:
    """One overnight run: the trading day the bot starts next and its previous session."""

    start_day: datetime.date
    deadline: datetime.datetime  # the start day's pre-market open, the bot's start
    previous: datetime.date
    previous_half_day: bool


def window(cfg: Config, at: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """(open, close) of the probe window that contains `at`, or of the next one."""
    start = datetime.time.fromisoformat(cfg.probe.prev_day_window_start)
    end = datetime.time.fromisoformat(cfg.probe.prev_day_window_end)
    today = ny_date_of(at)
    for base in (today - datetime.timedelta(days=1), today):
        opens = datetime.datetime.combine(base, start, NEW_YORK)
        close_day = base + datetime.timedelta(days=1) if end <= start else base
        closes = datetime.datetime.combine(close_day, end, NEW_YORK)
        if at < closes:
            return opens, closes
    opens = datetime.datetime.combine(today + datetime.timedelta(days=1), start, NEW_YORK)
    return window(cfg, opens)


def night_of(cfg: Config, calendar: SessionCalendar, opens: datetime.datetime) -> Night:
    """The night whose window opens at `opens`, from the bot's calendar."""
    day = ny_date_of(opens)
    for _ in range(cfg.massive.calendar_lookback_days):
        try:
            market = calendar.day(day)
        except CalendarRefused as exc:
            if exc.cause not in ("WEEKEND", "HOLIDAY"):
                raise ModelRefused("calendar", exc.detail) from exc
        else:
            if market.premarket_open > opens:
                (previous,) = massive.previous_sessions(
                    calendar, day, 1, cfg.massive.calendar_lookback_days)
                prev_day = datetime.date.fromisoformat(previous)
                return Night(day, market.premarket_open, prev_day,
                             calendar.day(prev_day).half_day)
        day += datetime.timedelta(days=1)
    raise ModelRefused("calendar", f"no trading day after {opens.isoformat()}")


def night_file(cfg: Config, start_day: datetime.date) -> Path:
    return (cfg.root / cfg.run.data_dir / cfg.probe.evidence_dir / FOLDER
            / f"{start_day.isoformat()}.jsonl")


# --- one check of each input --------------------------------------------------------------------


def head_file(cfg: Config, key: str, opener: massive.Opener,
              now: Callable[[], datetime.datetime]) -> dict[str, Any]:
    """HEAD of one flat file: status and, when present, size, ETag and Last-Modified."""
    m = cfg.massive
    path = "/" + urllib.parse.quote(f"{m.files_bucket}/{key}")
    headers = massive.sigv4_headers(
        host=m.files_host, path=path, region=m.files_region,
        key_id=massive.credential(m.files_key_id_variable),
        secret=massive.credential(m.files_secret_variable), moment=now(), method="HEAD")
    try:
        status, answer = massive.head(opener, f"https://{m.files_host}{path}", headers,
                                      m.http_timeout_seconds)
    except ModelRefused as exc:
        return {"status": None, "present": None, "cause": exc.cause, "detail": exc.detail}
    if status != HTTP_OK:
        return {"status": status,
                "present": False if status == massive.HTTP_NOT_FOUND else None}
    row: dict[str, Any] = {"status": status, "present": True}
    for name, header in FILE_HEADERS.items():
        row[name] = answer.get(header)  # recorded as sent, null when absent
    return row


def list_splits(reference: massive.Reference, night: Night) -> dict[str, Any]:
    """Splits listed for the previous session through the start day: count per date, digest."""
    try:
        listed = reference.splits(night.previous.isoformat(), night.start_day.isoformat())
    except ModelRefused as exc:
        return {"present": None, "cause": exc.cause, "detail": exc.detail}
    rows = {day: sorted(pairs) for day, pairs in sorted(listed.items())}
    return {"present": bool(rows.get(night.previous.isoformat())),
            "by_day": {day: len(pairs) for day, pairs in rows.items()},
            "digest": hashlib.sha256(json.dumps(rows).encode()).hexdigest()}


def fingerprint(row: dict[str, Any]) -> tuple[Any, ...] | None:
    """What tells a republish: the file's headers, or the splits rows' digest."""
    if row.get("present") is None or ("digest" not in row and not row["present"]):
        return None
    return tuple(row.get(k) for k in (*FILE_HEADERS, "digest"))


# --- the overnight run ---------------------------------------------------------------------------


class Recorder:
    """The night's file and, per input, the last answer that decides the next event."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: dict[str, bool] = {}  # input -> ever present this night
        self.last: dict[str, tuple[Any, ...] | None] = {}  # input -> last fingerprint
        self.was_present: dict[str, bool | None] = {}
        for row in read_rows(path):
            self._remember(row)

    def _event(self, row: dict[str, Any]) -> str:
        name, present, mark = row["input"], row.get("present"), fingerprint(row)
        if present is None:
            return "check"
        if present and not self.seen.get(name):
            return "first_present"
        if not present and self.was_present.get(name):
            return "gone"
        if mark is not None and self.last.get(name) not in (None, mark):
            return "changed"
        return "check"

    def _remember(self, row: dict[str, Any]) -> None:
        name, present = row["input"], row.get("present")
        if present is None:
            return
        self.seen[name] = self.seen.get(name, False) or bool(present)
        self.was_present[name] = present
        if fingerprint(row) is not None:
            self.last[name] = fingerprint(row)

    def write(self, row: dict[str, Any]) -> dict[str, Any]:
        row["event"] = self._event(row)
        self._remember(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        return row


def file_keys(cfg: Config, previous: datetime.date) -> dict[str, str]:
    """Input name -> bucket key of the previous session's day files."""
    day = previous.isoformat()
    tail = f"{day[:4]}/{day[5:7]}/{day}.csv.gz"
    keys = {MINUTE_AGGS: f"{cfg.massive.minute_aggs_prefix}/{tail}"}
    for prefix in cfg.probe.prev_day_tick_prefixes:
        keys[prefix.rstrip("/").rsplit("/", 1)[-1]] = f"{prefix}/{tail}"
    return keys


def check_round(cfg: Config, night: Night, recorder: Recorder, *, opener: massive.Opener,
                reference: massive.Reference, now: Callable[[], datetime.datetime],
                out: Callable[[str], None]) -> None:
    """One check of every input, each recorded as soon as it is answered."""
    answers: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (name, lambda key=key: head_file(cfg, key, opener, now))
        for name, key in file_keys(cfg, night.previous).items()]
    answers.append((SPLITS, lambda: list_splits(reference, night)))
    for name, ask in answers:
        answer = ask()
        at = now()
        row = recorder.write({
            "input": name, "at": at.astimezone(datetime.UTC).isoformat(),
            "since_deadline_s": round((at - night.deadline).total_seconds()),
            "start_day": night.start_day.isoformat(), "previous": night.previous.isoformat(),
            "previous_half_day": night.previous_half_day, **answer})
        if row["event"] != "check":
            out(f"{at.astimezone(NEW_YORK):%H:%M:%S} {name}: {row['event']}")


def run(cfg: Config, calendar: SessionCalendar, *, now: Callable[[], datetime.datetime],
        sleep: Callable[[float], None], out: Callable[[str], None]) -> int:
    """Wait for the window, check every input each poll until it closes. Refuses (ModelRefused)
    before the first request when a credential variable is missing or the calendar cannot
    name the night."""
    for name in (cfg.massive.files_key_id_variable, cfg.massive.files_secret_variable,
                 cfg.massive.api_key_variable):
        massive.credential(name)  # names a missing variable, never reads it out
    opener = OPENER
    reference = massive.Reference(cfg, opener)
    opens, closes = window(cfg, now())
    night = night_of(cfg, calendar, opens)
    recorder = Recorder(night_file(cfg, night.start_day))
    out(f"probe-prev-day: start day {night.start_day} (04:00 ET start "
        f"{night.deadline.isoformat()}), previous session {night.previous}"
        f"{' (early close)' if night.previous_half_day else ''}; window "
        f"{opens.isoformat()} to {closes.isoformat()}; checks in {recorder.path}")
    wait = (opens - now()).total_seconds()
    if wait > 0:
        sleep(wait)
    while (left := (closes - now()).total_seconds()) > 0:
        check_round(cfg, night, recorder, opener=opener, reference=reference, now=now, out=out)
        sleep(min(float(cfg.probe.prev_day_poll_seconds), left))
    out("window closed; summary: tickr probe-prev-day --summary")
    return 0


# --- the summary over every captured night -------------------------------------------------------


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def night_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per input: first present (seconds from 04:00 ET, and whether only bounded), the
    answer at the deadline, the republishes after it appeared."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        s = out.setdefault(row["input"], {"first_present_s": None, "bounded": False,
                                           "at_deadline": None, "changes_s": [],
                                           "answered": 0, "unknown": 0})
        if row.get("present") is None:
            s["unknown"] += 1
            continue
        s["answered"] += 1
        if row["since_deadline_s"] <= 0:
            s["at_deadline"] = row["present"]
        if row["event"] == "first_present" and s["first_present_s"] is None:
            s["first_present_s"] = row["since_deadline_s"]
            s["bounded"] = s["answered"] == 1
        elif row["event"] == "changed":
            s["changes_s"].append(row["since_deadline_s"])
    return out


def clock(seconds: float) -> str:
    sign = "-" if seconds < 0 else "+"
    minutes, secs = divmod(abs(int(seconds)), 60)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}:{secs:02d}"


def summary(cfg: Config, out: Callable[[str], None]) -> dict[str, Any]:
    """Print and return the first-available times of every input over all captured nights."""
    folder = cfg.root / cfg.run.data_dir / cfg.probe.evidence_dir / FOLDER
    nights = {p.stem: night_summary(read_rows(p)) for p in sorted(folder.glob("*.jsonl"))}
    if not nights:
        out(f"no captured nights in {folder}")
        return {}
    out("times relative to the start day's 04:00 ET; '<=' = present at the window's first "
        "check; at 04:00 = the last answer at or before it")
    inputs: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for day, per_input in nights.items():
        for name, s in per_input.items():
            inputs.setdefault(name, []).append((day, s))
    result: dict[str, Any] = {}
    for name, items in sorted(inputs.items()):
        firsts = [s["first_present_s"] for _, s in items
                  if s["first_present_s"] is not None and not s["bounded"]]
        result[name] = {
            "nights": len(items),
            "present_at_deadline": sum(1 for _, s in items if s["at_deadline"] is True),
            "never_present": sum(1 for _, s in items if s["first_present_s"] is None),
            "present_before_window": sum(1 for _, s in items if s["bounded"]),
            "republished": sum(1 for _, s in items if s["changes_s"]),
            # (earliest, median, latest) over the nights that saw it appear
            "first_present_s": (min(firsts), statistics.median(firsts), max(firsts))
            if firsts else None}
        r = result[name]
        seen = ("" if not firsts else " first present earliest/median/latest "
                + "/".join(clock(t) for t in r["first_present_s"]) + ",")
        out(f"{name}: {len(items)} nights,{seen} present at 04:00 on "
            f"{r['present_at_deadline']}, before the window on {r['present_before_window']}, "
            f"never on {r['never_present']}, republished on {r['republished']}")
        for day, s in items:
            first = s["first_present_s"]
            shown = "never" if first is None else (("<=" if s["bounded"] else "") + clock(first))
            changes = ", ".join(clock(c) for c in s["changes_s"]) or "none"
            out(f"  {day}: first present {shown}, at 04:00 {s['at_deadline']}, "
                f"changes {changes}, unknown answers {s['unknown']}")
    return result
