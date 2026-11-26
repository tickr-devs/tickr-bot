"""Revisions of Benzinga earnings and news as received, and the snapshot as of a cutoff (Codex
codex-108 point 7, 2026-11-25; precedence, rescheduling and missing fields per codex-109 point 8,
2026-11-26).

The training reads Benzinga's archive: one final version of each report and article
(`l1bz.Earnings` gates er_surp on the FINAL `last_updated`). Live, a report is revised after its
first receipt; the archive's final revision cannot be known at decision time. So every version
the bot receives is kept, and a decision reads the snapshot as of its cutoff.

STORE. `RevisionStore(folder, kind)` appends one JSON line per new version of a record, keyed by
the vendor id (`benzinga_id`): the full record exactly as received, our receipt time, the send
time of the previous successful poll (the version became available between the two) and
whether it was in the first poll after a start (`baseline`: it was there before we looked, so
its receipt times nothing). A record equal to the latest stored line of its id is a retry and is
not stored again; any other record is kept as an observation, also one equal to an older version
(a stale redelivery: counted `repeated`, and it never moves that version forward). Lines are
append-only, one file per New York receipt day under `<folder>/<kind>/`, each append flushed and
fsynced. An unreadable line refuses the whole store (`revision_store_
unreadable`), never skipped. Nothing is ever rewritten, so a replay at an old cutoff recovers
the record originally seen.

AS OF A CUTOFF (the ruling). The record of an id is rebuilt only from what was received by the
cutoff. Vendor stamps are read as `l1bz.utc_s` reads them: a fraction of a second rounds UP to
the next second. Per id:

1. the versions are the distinct records received by the cutoff, each at its FIRST receipt (a
   retry or a stale redelivery adds nothing); a version whose readable `last_updated` is after
   the cutoff is not known yet and is left out (all left out: refused `updated_after_cutoff`);
2. the CURRENT version is chosen before any report-time check, so a correction that reschedules
   a report supersedes the old schedule: when every version has a readable `last_updated` the
   source orders them and the largest wins (a stale redelivery of an older update never wins);
   otherwise the source gives no order and the latest first receipt wins. Two different records
   sharing the winning key cannot be ordered: the id is refused `version_tie`;
3. the current version is placed when its required fields are present and its report time
   (earnings: `date` + `time`, New York) or published time (news) is at or before the cutoff,
   else the id is refused with its one cause (`field_missing:<f>`, `stamp_unreadable:<f>`,
   `report_after_cutoff`, `published_after_cutoff`). An older version is never used instead;
4. earnings: er_surp needs both EPS values and a usable `last_updated` on the current version.
   A placed report without them is kept (er_since, er_next) and listed `placed_only`; `l1bz`
   leaves er_surp missing for it, as for an archive report without them. A missing
   `last_updated` never erases the report's timing; an unreadable one refuses (the reader
   could not parse it). Nothing is filled in from another version.

Ids with nothing received by the cutoff are simply unknown.

COVERAGE. The poller (`tickr.model.revpoll`) logs every poll of a kind in `<folder>/coverage/
<kind>.jsonl`: `start` (a run began), `ok` (send time of a poll that answered and was stored),
`failed` (a poll that did not, with its typed cause). `Coverage` replays that log. The span
between two consecutive `ok` polls is covered only when nothing came between them: after a
`failed` poll or a restart, the versions that existed only between the two polls were never
seen (the later poll lists only the state it finds), so that span is a GAP typed
`poll_failed:<cause>` or `restart`, recorded and never closed by a later success. Before the first
`ok` and after the last one nothing is covered.

OUTPUT. `write_l1bz` writes the snapshot as `<kind>-<first>_<last>.jsonl.gz`, the file form
`l1bz.files` reads (one raw record a line, as Benzinga returned it), with the records whose
date (earnings `date`, news `published` in New York) lies in first..last. The caller states the
range and passes the kind's `Coverage`: the range is the New York days first..last (in update
time, up to the cutoff), and a range not fully covered refuses (`revision_range_not_covered`
with the first gap's cause).

REPORT. `python -m tickr.model.revisions report --config config/bot.toml LOG...` reads store
files and/or the records of `tickr probe-news-delay` and prints the observed delays
(receipt - last_updated, receipt - report time, receipt - published) with the quantiles of
`[massive] revision_report_quantiles`, and per field how often the first-received, the as-of
and the final version differ. The as-of cutoff of an id is the time the training says the value
is known (`l1bz`: earnings max(report time, last_updated) of the final version; news published +
`bz_lag_s` of the pinned settings, unchanged for this measurement). The final version is the
archive's (`--archive` files in the training's form) or, without one, the last one received.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tickr.model.errors import ModelRefused

NEW_YORK = ZoneInfo("America/New_York")
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
NS = 10**9
# l1bz's stamp form (l1bz.STAMP): UTC or an offset, any number of fraction digits.
STAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
                   r"(Z|[+-]\d{2}:?\d{2})$")


@dataclass(frozen=True)
class Kind:
    name: str  # the l1bz file prefix
    source: str  # the source name in the records of `tickr probe-news-delay`
    required: tuple[str, ...]  # a version without one of these is not placed
    preferred: tuple[str, ...]  # the inputs of the value read from it (er_surp)
    id_field: str = "benzinga_id"


EARNINGS = Kind("earnings", "benzinga_earnings",
                ("benzinga_id", "ticker", "date", "time"),
                ("actual_eps", "estimated_eps", "last_updated"))
NEWS = Kind("news", "benzinga_news", ("benzinga_id", "published"), ())
KINDS = {k.name: k for k in (EARNINGS, NEWS)}
BY_SOURCE = {k.source: k for k in (EARNINGS, NEWS)}


@dataclass(frozen=True)
class Version:
    id: str
    received_ns: int
    record: Mapping[str, Any]
    previous_poll_ns: int | None = None
    baseline: bool = False


@dataclass
class AsOf:
    """The snapshot of one kind at `cutoff_ns`: per id the record, how it was chosen, or the
    one cause it was refused."""

    cutoff_ns: int
    records: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    placed_only: set[str] = field(default_factory=set)  # placed without er_surp inputs
    refused: dict[str, str] = field(default_factory=dict)


# --- time ---------------------------------------------------------------------------------------

def ns_of(moment: datetime.datetime) -> int:
    """Nanoseconds since the epoch of an aware datetime, exact to the microsecond."""
    if moment.tzinfo is None:
        raise ModelRefused("revision_time_naive", moment.isoformat())
    return (moment - EPOCH) // datetime.timedelta(microseconds=1) * 1000


def vendor_s(text: Any) -> int | None:
    """A vendor stamp as l1bz.utc_s reads it: the first whole second at which it is in the
    past (a non-zero fraction rounds up). None when it is not of that form."""
    m = STAMP.match(text) if isinstance(text, str) else None
    if not m:
        return None
    y, mo, d, hh, mi, ss = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
    zone = m.group(8)
    off = 0
    if zone != "Z":
        z = zone.replace(":", "")
        off = (1 if z[0] == "+" else -1) * (int(z[1:3]) * 3600 + int(z[3:5]) * 60)
    try:
        datetime.datetime(y, mo, d, hh, mi, ss)
    except ValueError:
        return None
    frac = m.group(7)
    return calendar.timegm((y, mo, d, hh, mi, ss)) - off + (1 if frac and frac.strip("0") else 0)


def report_s(record: Mapping[str, Any]) -> int | None:
    """The report time, `date` + `time` in New York, read as l1bz.Earnings reads it."""
    try:
        hh, mm, ss = (int(x) for x in str(record["time"]).split(":"))
        d = datetime.date.fromisoformat(str(record["date"]))
        return int(datetime.datetime(d.year, d.month, d.day, hh, mm, ss,
                                     tzinfo=NEW_YORK).timestamp())
    except (KeyError, ValueError, TypeError):
        return None


def _present(record: Mapping[str, Any], name: str) -> bool:
    return record.get(name) not in (None, "")


def _stamps(kind: Kind, record: Mapping[str, Any]) -> tuple[dict[str, int], str | None]:
    """The vendor times of a version that must lie at or before a cutoff, or the cause."""
    out: dict[str, int] = {}
    if kind is EARNINGS:
        rt = report_s(record)
        if rt is None:
            return out, "stamp_unreadable:date_time"
        out["report"] = rt
    else:
        pub = vendor_s(record.get("published"))
        if pub is None:
            return out, "stamp_unreadable:published"
        out["published"] = pub
    if _present(record, "last_updated"):
        lu = vendor_s(record["last_updated"])
        if lu is None:
            return out, "stamp_unreadable:last_updated"
        out["updated"] = lu
    return out, None


def check(kind: Kind, record: Mapping[str, Any], cutoff_ns: int) -> str | None:
    """None when the version may be placed at the cutoff, else its one cause."""
    for name in kind.required:
        if not _present(record, name):
            return f"field_missing:{name}"
    stamps, cause = _stamps(kind, record)
    if cause:
        return cause
    for what, s in stamps.items():
        if s * NS > cutoff_ns:
            return f"{what}_after_cutoff"
    return None


def _updated_s(record: Mapping[str, Any]) -> int | None:
    """The readable `last_updated` of a version, else None (missing or unreadable)."""
    return vendor_s(record["last_updated"]) if _present(record, "last_updated") else None


def select(kind: Kind, versions: Sequence[Version], cutoff_ns: int
           ) -> tuple[Version | None, str | None, bool]:
    """(version, refusal cause, placed_only) of one id at the cutoff; versions in receipt
    order. (None, None, False) when nothing was received by the cutoff."""
    got = [v for v in versions if v.received_ns <= cutoff_ns]
    if not got:
        return None, None, False
    first: dict[str, Version] = {}
    for v in got:  # a retry or a stale redelivery keeps its version's first receipt
        first.setdefault(_canon(v.record), v)
    known = [v for v in first.values()
             if (_updated_s(v.record) or 0) * NS <= cutoff_ns]
    if not known:
        return None, check(kind, got[-1].record, cutoff_ns), False
    stamps = [_updated_s(v.record) for v in known]
    if all(s is not None for s in stamps):  # the source orders the versions
        keys = [int(s) for s in stamps if s is not None]
    else:
        keys = [v.received_ns for v in known]
    top = max(keys)
    if keys.count(top) > 1:
        return None, "version_tie", False
    current = known[keys.index(top)]
    cause = check(kind, current.record, cutoff_ns)
    if cause:
        return None, cause, False
    return current, None, not all(_present(current.record, f) for f in kind.preferred)


def snapshot(kind: Kind, by_id: Mapping[str, Sequence[Version]], cutoff_ns: int) -> AsOf:
    out = AsOf(cutoff_ns)
    for key, versions in by_id.items():
        v, cause, placed_only = select(kind, versions, cutoff_ns)
        if cause:
            out.refused[key] = cause
        elif v is not None:
            out.records[key] = v.record
            if placed_only:
                out.placed_only.add(key)
    return out


# --- store --------------------------------------------------------------------------------------

def _stamp(ns: int) -> str:
    moment = EPOCH + datetime.timedelta(microseconds=ns // 1000)
    return moment.isoformat(timespec="microseconds")


def _canon(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


class RevisionStore:
    """Append-only versions of one kind of Benzinga record (see the module docstring)."""

    def __init__(self, folder: Path, kind: Kind) -> None:
        self.kind = kind
        self.folder = Path(folder) / kind.name
        self.by_id: dict[str, list[Version]] = {}
        self._last: dict[str, str] = {}
        if self.folder.is_dir():
            for path in sorted(self.folder.glob("*.jsonl")):
                for v in read_log(path, kind):
                    self._keep(v)

    def _keep(self, v: Version) -> None:
        self.by_id.setdefault(v.id, []).append(v)
        self._last[v.id] = _canon(v.record)

    def append(self, rows: Iterable[Any], received: datetime.datetime, *,
               previous_poll: datetime.datetime | None = None,
               baseline: bool = False) -> dict[str, int]:
        """Store each row that is new or differs from its id's latest version; one write,
        flushed and fsynced. Returns counts: saved, unchanged (a retry of the latest line, not
        stored), repeated (saved, but equal to an older version of its id: a stale redelivery),
        unreadable (no id; not stored, it cannot be keyed)."""
        received_ns = ns_of(received)
        prev_ns = ns_of(previous_poll) if previous_poll is not None else None
        counts = {"saved": 0, "unchanged": 0, "repeated": 0, "unreadable": 0}
        lines: list[str] = []
        fresh: list[Version] = []
        pending: dict[str, str] = {}
        for row in rows:
            key = row.get(self.kind.id_field) if isinstance(row, dict) else None
            if not isinstance(key, str | int) or isinstance(key, bool) or key == "":
                counts["unreadable"] += 1
                continue
            key = str(key)
            canon = _canon(row)
            if pending.get(key, self._last.get(key)) == canon:
                counts["unchanged"] += 1
                continue
            pending[key] = canon
            if any(_canon(old.record) == canon for old in self.by_id.get(key, ())):
                counts["repeated"] += 1
            v = Version(key, received_ns, dict(row), prev_ns, baseline)
            fresh.append(v)
            lines.append(json.dumps({
                "kind": self.kind.name, "id": key, "received_utc": _stamp(received_ns),
                "previous_poll_sent_utc": _stamp(prev_ns) if prev_ns is not None else None,
                "baseline": baseline, "record": row}) + "\n")
        if lines:
            self.folder.mkdir(parents=True, exist_ok=True)
            day = received.astimezone(NEW_YORK).date().isoformat()
            path = self.folder / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(lines))
                f.flush()
                os.fsync(f.fileno())
            for v in fresh:
                self._keep(v)
            counts["saved"] = len(fresh)
        return counts

    def as_of(self, cutoff: datetime.datetime) -> AsOf:
        return snapshot(self.kind, self.by_id, ns_of(cutoff))


@dataclass(frozen=True)
class Gap:
    start_ns: int  # send time of the last poll before
    end_ns: int | None  # send time of the next successful poll; None = none yet
    cause: str


class Coverage:
    """What the polls of one kind saw completely (see the module docstring), from its log."""

    def __init__(self, events: Iterable[Mapping[str, Any]]) -> None:
        self.spans: list[list[int]] = []  # covered [from, to] send times, in order
        self.gaps: list[Gap] = []
        pending: str | None = None
        for e in events:
            what = e["event"]
            if what == "start":
                pending = pending or "restart"
            elif what == "failed":
                pending = pending or f"poll_failed:{e['cause']}"
            elif what == "ok":
                sent = _receipt_ns(e["sent_utc"])
                if self.spans:
                    last = self.spans[-1][1]
                    if pending:
                        self.gaps.append(Gap(last, sent, pending))
                        self.spans.append([sent, sent])
                    else:
                        self.spans[-1][1] = sent
                else:
                    self.spans.append([sent, sent])
                pending = None
            else:
                raise ValueError(f"coverage event {what!r}")
        if pending and self.spans:
            self.gaps.append(Gap(self.spans[-1][1], None, pending))

    @property
    def last_ok_ns(self) -> int | None:
        return self.spans[-1][1] if self.spans else None

    def refusal(self, start_ns: int, end_ns: int) -> str | None:
        """None when [start, end] is inside one covered span, else the one cause."""
        if any(a <= start_ns and end_ns <= b for a, b in self.spans):
            return None
        if not self.spans:
            return "no_polls"
        for g in self.gaps:
            if g.start_ns < end_ns and (g.end_ns is None or g.end_ns > start_ns):
                to = _stamp(g.end_ns) if g.end_ns is not None else "now"
                return f"gap:{g.cause}:{_stamp(g.start_ns)}..{to}"
        return "before_first_poll" if start_ns < self.spans[0][0] else "after_last_poll"

    def require(self, start_ns: int, end_ns: int) -> None:
        cause = self.refusal(start_ns, end_ns)
        if cause:
            raise ModelRefused("revision_range_not_covered",
                               f"{cause} for {_stamp(start_ns)}..{_stamp(end_ns)}")


class CoverageLog:
    """Append-only poll log of one kind: `<folder>/coverage/<kind>.jsonl`."""

    def __init__(self, folder: Path, kind: Kind) -> None:
        self.path = Path(folder) / "coverage" / f"{kind.name}.jsonl"
        self.events: list[dict[str, Any]] = []
        if self.path.is_file():
            with self.path.open(encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if line.strip():
                        try:
                            self.events.append(json.loads(line))
                        except ValueError:
                            raise ModelRefused("revision_store_unreadable",
                                               f"coverage line {n}") from None
        self.coverage()  # a log that cannot be replayed refuses now

    def add(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.events.append(event)

    def coverage(self) -> Coverage:
        try:
            return Coverage(self.events)
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelRefused("revision_store_unreadable",
                               f"coverage log {self.path.name}: {type(exc).__name__}") from None


def read_log(path: Path, kind: Kind | None = None) -> Iterable[Version]:
    """Versions from a store file, or from a record file of `tickr probe-news-delay` (only the
    fields it keeps). A line that is not JSON, or a store line without its parts, refuses."""
    with Path(path).open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                j = json.loads(line)
                if "record" in j:
                    k = KINDS[j["kind"]]
                    record = j["record"]
                    baseline = bool(j["baseline"])
                elif j.get("event") in ("new", "revised", "baseline"):
                    k = BY_SOURCE.get(j["source"])  # type: ignore[assignment]
                    if k is None:
                        continue  # Massive general news: not a Benzinga kind
                    record = {f: v for f, v in j.items() if f not in PROBE_META}
                    record.setdefault(k.id_field, j["id"])
                    baseline = j["event"] == "baseline"
                else:
                    continue  # poll lines, failures: no record
                if kind is not None and k is not kind:
                    continue
                prev = j.get("previous_poll_sent_utc")
                yield Version(str(j["id"]), _receipt_ns(j["received_utc"]), record,
                              _receipt_ns(prev) if prev else None, baseline)
            except (ValueError, KeyError, TypeError) as exc:
                raise ModelRefused("revision_store_unreadable",
                                   f"{Path(path).name} line {n}: {type(exc).__name__}") from None


PROBE_META = frozenset({"event", "source", "id", "poll_sent_utc", "received_utc",
                        "previous_poll_sent_utc"})


def _receipt_ns(text: str) -> int:
    moment = datetime.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("receipt time without a zone")
    return ns_of(moment)


# --- the training's file form ----------------------------------------------------------------

def date_key(kind: Kind, record: Mapping[str, Any]) -> str:
    if kind is EARNINGS:
        return str(record["date"])
    s = vendor_s(record["published"])
    if s is None:  # never for a placed record: check() read it
        raise ModelRefused("revision_stamp_unreadable", str(record.get("benzinga_id")))
    return datetime.datetime.fromtimestamp(s, NEW_YORK).date().isoformat()


def write_l1bz(snap: AsOf, kind: Kind, folder: Path, first: str, last: str, *,
               coverage: Coverage) -> Path:
    """`<kind>-<first>_<last>.jsonl.gz` in `folder` with the snapshot's records dated
    first..last (one raw record a line). A range past the cutoff's New York date refuses: the
    training refuses a file named past the day. A range the polls did not cover completely
    (New York days first..last, up to the cutoff) refuses with the gap's cause."""
    try:
        a, b = datetime.date.fromisoformat(first), datetime.date.fromisoformat(last)
    except ValueError:
        raise ModelRefused("revision_file_range", f"{first}..{last}") from None
    cut_day = (EPOCH + datetime.timedelta(microseconds=snap.cutoff_ns // 1000)).astimezone(
        NEW_YORK).date()
    if a > b or b > cut_day:
        raise ModelRefused("revision_file_range", f"{first}..{last} with cutoff {cut_day}")
    start = datetime.datetime(a.year, a.month, a.day, tzinfo=NEW_YORK)
    after = b + datetime.timedelta(days=1)
    end = datetime.datetime(after.year, after.month, after.day, tzinfo=NEW_YORK)
    coverage.require(ns_of(start), min(ns_of(end), snap.cutoff_ns))
    rows = sorted(((date_key(kind, r), key, r) for key, r in snap.records.items()
                   if first <= date_key(kind, r) <= last), key=lambda x: (x[0], x[1]))
    Path(folder).mkdir(parents=True, exist_ok=True)
    path = Path(folder) / f"{kind.name}-{first}_{last}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for _, _, r in rows))
    return path


# --- report -------------------------------------------------------------------------------------

def quantile(values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile of sorted values."""
    return values[max(0, math.ceil(q * len(values)) - 1)]


def delays(kind: Kind, by_id: Mapping[str, Sequence[Version]]) -> dict[str, list[float]]:
    """Seconds from a vendor time to our receipt (and to the poll before, the lower bound of
    the arrival), over versions that are not baseline. Earnings: every version's receipt -
    last_updated; per id the first version with an actual and an estimate, receipt - report
    time. News: the first version's receipt - published; later versions' receipt -
    last_updated."""
    out: dict[str, list[float]] = {}

    def add(name: str, v: Version, s: int | None) -> None:
        if s is None or v.baseline:
            return
        out.setdefault(name, []).append((v.received_ns - s * NS) / NS)
        if v.previous_poll_ns is not None:
            out.setdefault(name + " (poll before)", []).append(
                (v.previous_poll_ns - s * NS) / NS)

    for versions in by_id.values():
        if versions[0].baseline:
            continue
        if kind is EARNINGS:
            for v in versions:
                add("receipt - last_updated", v, vendor_s(v.record.get("last_updated")))
            full = next((v for v in versions
                         if all(_present(v.record, f) for f in kind.preferred)), None)
            if full is not None:
                add("receipt - report time (first with actual and estimate)", full,
                    report_s(full.record))
        else:
            add("receipt - published (first receipt)", versions[0],
                vendor_s(versions[0].record.get("published")))
            for v in versions[1:]:
                add("receipt - last_updated (revisions)", v,
                    vendor_s(v.record.get("last_updated")))
    return {k: sorted(v) for k, v in out.items()}


def known_s(kind: Kind, record: Mapping[str, Any], lag_s: int) -> int | None:
    """When the training counts the value of a final record as known (l1bz)."""
    if kind is EARNINGS:
        rt, lu = report_s(record), vendor_s(record.get("last_updated"))
        return None if rt is None or lu is None else max(rt, lu)
    pub = vendor_s(record.get("published"))
    return None if pub is None else pub + lag_s


def differences(kind: Kind, by_id: Mapping[str, Sequence[Version]],
                finals: Mapping[str, Mapping[str, Any]], lag_s: int) -> dict[str, Any]:
    """Per field, how often first-received, as-of (at the training's known time of the final
    version) and final differ; ids first seen in a baseline poll are left out."""
    counts: dict[str, dict[str, int]] = {}
    out: dict[str, Any] = {"ids": 0, "baseline_ids": 0, "no_final": 0, "final_unusable": 0,
                           "as_of_absent": 0, "as_of_placed_only": 0, "as_of_refused": {},
                           "fields": counts}
    for key, versions in by_id.items():
        if versions[0].baseline:
            out["baseline_ids"] += 1
            continue
        final = finals.get(key)
        if final is None:
            out["no_final"] += 1
            continue
        kn = known_s(kind, final, lag_s)
        if kn is None:
            out["final_unusable"] += 1
            continue
        out["ids"] += 1
        v, cause, placed_only = select(kind, versions, kn * NS)
        asof = v.record if v is not None else None
        if cause:
            out["as_of_refused"][cause] = out["as_of_refused"].get(cause, 0) + 1
        elif v is None:
            out["as_of_absent"] += 1
        out["as_of_placed_only"] += int(placed_only)
        first = versions[0].record
        for name in sorted(set(first) | set(final) | set(asof or {})):
            c = counts.setdefault(name, {"first_vs_final": 0, "asof_vs_final": 0,
                                         "first_vs_asof": 0})
            c["first_vs_final"] += int(first.get(name) != final.get(name))
            if asof is not None:
                c["asof_vs_final"] += int(asof.get(name) != final.get(name))
                c["first_vs_asof"] += int(first.get(name) != asof.get(name))
    return out


def load_versions(paths: Iterable[Path], kind: Kind) -> dict[str, list[Version]]:
    """Versions of one kind from logs, in receipt order (stable for equal receipts), each at its
    first receipt: a record equal to an earlier one of its id is dropped (a retry, a stale
    redelivery, or a probe and a store file both holding it)."""
    allv = [v for p in paths for v in read_log(p, kind)]
    allv.sort(key=lambda v: v.received_ns)
    by_id: dict[str, list[Version]] = {}
    seen: set[tuple[str, str]] = set()
    for v in allv:
        canon = _canon(v.record)
        if (v.id, canon) not in seen:
            seen.add((v.id, canon))
            by_id.setdefault(v.id, []).append(v)
    return by_id


def load_archive(paths: Iterable[Path], kind: Kind) -> dict[str, Mapping[str, Any]]:
    """Final versions from training-form files (`<kind>-<a>_<b>.jsonl.gz`); the first row of
    an id wins, as l1bz reads them."""
    out: dict[str, Mapping[str, Any]] = {}
    for p in paths:
        if not Path(p).name.startswith(kind.name + "-"):
            continue
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    j = json.loads(line)
                    if kind.id_field in j:
                        out.setdefault(str(j[kind.id_field]), j)
    return out


def pinned_lag_s() -> int:
    """`bz_lag_s` of the pinned builder settings (md5-checked): the training's news lag, kept
    unchanged for this measurement."""
    from tickr.model import features
    from tickr.model.features import full

    path = features.PINNED_DIR / full.SETTINGS_FILE
    data = path.read_bytes()
    if hashlib.md5(data).hexdigest() != full.FULL_PINS[full.SETTINGS_FILE]:
        raise ModelRefused("table_code_md5", full.SETTINGS_FILE)
    return int(json.loads(data)["bz_lag_s"])


def report(logs: Sequence[Path], archive: Sequence[Path], quantiles: Sequence[float],
           lag_s: int) -> dict[str, Any]:
    out: dict[str, Any] = {"bz_lag_s": lag_s}
    for kind in (EARNINGS, NEWS):
        by_id = load_versions(logs, kind)
        finals = load_archive(archive, kind) if archive else {
            k: v[-1].record for k, v in by_id.items()}
        d = delays(kind, by_id)
        section: dict[str, Any] = {
            "final": "archive" if archive else "last received",
            "ids": len(by_id), "versions": sum(len(v) for v in by_id.values()),
            "revised_ids": sum(1 for v in by_id.values() if len(v) > 1),
            "delays": {name: {"n": len(v), "min": v[0], "max": v[-1],
                              **{f"q{q}": quantile(v, q) for q in quantiles}}
                       for name, v in d.items() if v},
            "delay_values": d,
            "differences": differences(kind, by_id, finals, lag_s)}
        if kind is NEWS:
            first = d.get("receipt - published (first receipt)", [])
            section["first_receipt_after_bz_lag_s"] = sum(1 for x in first if x > lag_s)
        out[kind.name] = section
    return out


def _print(rep: Mapping[str, Any], out: Any) -> None:
    print(f"bz_lag_s (pinned training setting): {rep['bz_lag_s']} s", file=out)
    for kind in (EARNINGS, NEWS):
        s = rep[kind.name]
        print(f"\n[{kind.name}] ids {s['ids']}, versions {s['versions']}, revised ids "
              f"{s['revised_ids']}, final = {s['final']}", file=out)
        for name, st in s["delays"].items():
            print(f"  {name}, s: " + ", ".join(f"{k} {v:g}" for k, v in st.items()), file=out)
        if "first_receipt_after_bz_lag_s" in s:
            print(f"  first receipts later than published + bz_lag_s: "
                  f"{s['first_receipt_after_bz_lag_s']}", file=out)
        diff = s["differences"]
        print("  compared ids {ids}, baseline ids left out {baseline_ids}, without final "
              "{no_final}, final unusable {final_unusable}; as of the training's known time: "
              "absent {as_of_absent}, placed without er_surp fields {as_of_placed_only}, "
              "refused {as_of_refused}".format(**diff), file=out)
        for name, c in diff["fields"].items():
            print(f"    {name}: first/final {c['first_vs_final']}, as-of/final "
                  f"{c['asof_vs_final']}, first/as-of {c['first_vs_asof']}", file=out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tickr.model.revisions")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("report", help="delays and first/as-of/final differences of a log")
    p.add_argument("logs", type=Path, nargs="+", help="store or probe-news-delay JSONL files")
    p.add_argument("--archive", type=Path, nargs="*", default=[],
                   help="training-form earnings-/news-<a>_<b>.jsonl.gz files (final versions)")
    p.add_argument("--config", type=Path, default=Path("config/bot.toml"))
    p.add_argument("--out", type=Path, help="write the whole report, every delay, as JSON")
    args = parser.parse_args(argv)
    from tickr import config as bot_config

    try:
        cfg = bot_config.load(args.config)
        rep = report(args.logs, args.archive,
                     [float(q) for q in cfg.massive.revision_report_quantiles], pinned_lag_s())
    except (ModelRefused, bot_config.ConfigError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    _print(rep, sys.stdout)
    if args.out:
        args.out.write_text(json.dumps(rep, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
