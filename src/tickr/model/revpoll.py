"""Poller that fills the Benzinga revision store (Codex codex-108 point 7): `tickr poll-revisions`.

Every `[massive] news_probe_poll_seconds` (the probe's pacing key) it lists Benzinga earnings
changed since the previous successful poll (`last_updated.gte`, reaching back
`revision_poll_overlap_seconds` more) and Benzinga news published in the last
`news_probe_news_lookback_seconds` (`published.gte`, `sort=published.asc`, as the probe does; the
endpoints and the key are the probe's). The news endpoint does not filter or sort by
`last_updated` (measured 2026-11-25: HTTP 400 on the sort, the filter silently ignored), so a
news revision after publication is seen only while the article is inside the re-polled window.
`listing_query` refuses a news `last_updated` filter (`news_last_updated_unsupported`) and a poll
whose rows fall below the bound it asked for is failed (`bound_ignored`) and stores nothing.
Every full record that differs from the latest stored version of its id is appended to
`tickr.model.revisions.RevisionStore`, with our receipt time and the send time of the previous
successful poll. The
first poll after the very first start is `baseline`: those records were there before we looked.

COVERAGE. Each poll is logged (`CoverageLog`): `ok` after its records are stored, `failed` with
its typed cause when the listing was refused, ended early or held a row without an id (the
readable rows are stored, the poll still counts as failed: that row's version is lost). A
restart is logged as `start`. The next successful poll lists the state it finds, so a version
that came and went between two polls with a failure or a restart in between is never seen:
`Coverage` records that span as a gap and `write_l1bz` refuses a range that touches one. The
poller never closes a gap by a later success. State is the stored files only: a new process
resumes from the last logged `ok`.

A missing credential or a refused key (HTTP 401/403) is logged as a failed poll and stops the
run. The key is read by NAME at each request and is never written.
"""

from __future__ import annotations

import datetime
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tickr.config import Config
from tickr.model import massive
from tickr.model.errors import ModelRefused
from tickr.model.revisions import EARNINGS, EPOCH, NEWS, Coverage, CoverageLog, Kind, RevisionStore

KINDS = (EARNINGS, NEWS)
STOP_STATUSES = ("401 ", "403 ")  # http_status details of a key or plan the API refuses


BOUND = {EARNINGS.name: "last_updated", NEWS.name: "published"}  # the filter each endpoint honours


def listing_query(kind: Kind, since: datetime.datetime,
                  field: str | None = None) -> dict[str, str]:
    """The query of one poll: records of `kind` whose `field` is at or after `since`."""
    field = field or BOUND[kind.name]
    if field != BOUND[kind.name]:
        raise ModelRefused("news_last_updated_unsupported" if kind is NEWS
                           else "revision_poll_config", f"{kind.name} {field}.gte")
    return {f"{field}.gte": since.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sort": f"{field}.asc"}


def below_bound(rows: list[Any], query: dict[str, str]) -> str | None:
    """The first row whose `last_updated` or `published` lies before the requested bound (the
    server ignored the filter), as a short detail; None when every readable stamp is inside."""
    field = next(k for k in query if k.endswith(".gte"))
    bound = datetime.datetime.fromisoformat(query[field])
    for row in rows:
        for name in ("last_updated", "published"):
            try:
                stamp = datetime.datetime.fromisoformat(str(row[name]))
            except (KeyError, TypeError, ValueError):
                continue  # absent or unreadable: the store's own checks refuse it later
            if stamp.tzinfo is not None and stamp < bound:
                return f"{name} {row[name]} before {query[field]} ({field})"
    return None


def _stamp(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.UTC).isoformat(timespec="microseconds")


class Poller:
    """The stores, coverage logs and listing client of one run."""

    def __init__(self, cfg: Config, opener: massive.Opener = massive._open,
                 now: Callable[[], datetime.datetime] | None = None) -> None:
        self.m = cfg.massive
        if not self.m.revision_poll_enabled:
            raise ModelRefused("revision_poll_disabled", "[massive] revision_poll_enabled")
        rel = Path(self.m.revision_store_dir)
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise ModelRefused("revision_poll_config",
                               "[massive] revision_store_dir must be relative")
        if (self.m.news_probe_poll_seconds < 1 or self.m.news_probe_news_lookback_seconds < 1
                or self.m.revision_poll_overlap_seconds < 0):
            raise ModelRefused("revision_poll_config", "[massive] poll pacing and windows")
        self.folder = cfg.root / cfg.run.data_dir / rel
        self.reference = massive.Reference(cfg, opener)
        self.now = now or (lambda: datetime.datetime.now(datetime.UTC))
        self.stores = {k.name: RevisionStore(self.folder, k) for k in KINDS}
        self.logs = {k.name: CoverageLog(self.folder, k) for k in KINDS}
        for log in self.logs.values():
            log.add({"event": "start", "at_utc": _stamp(self.now())})

    def coverage(self, kind: Kind) -> Coverage:
        """What the polls of `kind` saw completely; pass it to `revisions.write_l1bz`."""
        return self.logs[kind.name].coverage()

    def _path(self, kind: Kind) -> str:
        return self.m.benzinga_earnings_path if kind is EARNINGS else self.m.benzinga_news_path

    def poll(self, kind: Kind) -> dict[str, int]:
        """One poll of one kind; returns the counts (`failed` 1 on a failed poll)."""
        log, store = self.logs[kind.name], self.stores[kind.name]
        before_ns = log.coverage().last_ok_ns
        sent = self.now()
        if before_ns is None or kind is NEWS:
            since = sent - datetime.timedelta(seconds=self.m.news_probe_news_lookback_seconds)
        else:
            since = _moment(before_ns) - datetime.timedelta(
                seconds=self.m.revision_poll_overlap_seconds)
        query = listing_query(kind, since)
        try:
            rows = self.reference.listing(self._path(kind), query)
        except ModelRefused as exc:
            log.add({"event": "failed", "sent_utc": _stamp(sent), "cause": exc.cause,
                     "detail": exc.detail})
            if exc.cause == "credential_missing" or (
                    exc.cause == "http_status" and exc.detail.startswith(STOP_STATUSES)):
                raise
            return {"failed": 1}
        received = self.now()
        ignored = below_bound(rows, query)
        if ignored:
            log.add({"event": "failed", "sent_utc": _stamp(sent), "cause": "bound_ignored",
                     "detail": ignored})
            return {"failed": 1}
        counts: dict[str, Any] = store.append(
            rows, received, previous_poll=_moment(before_ns), baseline=before_ns is None)
        if counts["unreadable"]:
            log.add({"event": "failed", "sent_utc": _stamp(sent), "cause": "row_unreadable",
                     "detail": f"{counts['unreadable']} rows without {kind.id_field}"})
            return {**counts, "failed": 1}
        log.add({"event": "ok", "sent_utc": _stamp(sent), "received_utc": _stamp(received),
                 "rows": len(rows), **counts})
        return counts


def _moment(ns: int | None) -> Any:
    return None if ns is None else EPOCH + datetime.timedelta(microseconds=ns // 1000)


def run(cfg: Config, *, opener: massive.Opener = massive._open,
        now: Callable[[], datetime.datetime] | None = None,
        wait: Callable[[float], bool] | None = None, polls: int | None = None,
        out: Callable[[str], None] = print) -> int:
    """Poll both kinds every `news_probe_poll_seconds` until `wait` says stop (or `polls`
    rounds, for tests). Raises ModelRefused when the poller cannot go on."""
    poller = Poller(cfg, opener, now)
    wait = wait or threading.Event().wait
    out(f"poll-revisions: store in {poller.folder}")
    done = 0
    while polls is None or done < polls:
        start = poller.now()
        for kind in KINDS:
            counts = poller.poll(kind)
            if counts.get("saved") or counts.get("failed"):
                out(f"{kind.name} " + " ".join(f"{k} {v}" for k, v in counts.items() if v))
        done += 1
        if polls is not None and done >= polls:
            break
        spent = (poller.now() - start).total_seconds()
        if wait(max(0.0, cfg.massive.news_probe_poll_seconds - spent)):
            break
    return 0


def main(cfg: Config, out: Callable[[str], None] = print, **kwargs: Any) -> int:
    """`tickr poll-revisions`: 0 when stopped by the owner (Ctrl-C), 3 on a refusal."""
    try:
        return run(cfg, out=out, **kwargs)
    except ModelRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        out("poll-revisions: stopped")
        return 0
