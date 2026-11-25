"""Receipt-time probe of the news inputs (Codex items 9-12, backlog R34): `tickr probe-news-delay`.

The training counts a Benzinga article as known at `published + bz_lag_s` and a report's surprise
from `max(report time, last_updated)` (l1bz.py). Whether the live feed delivers that fast is not
known: this probe measures it. It polls the same Massive endpoints the training fetched its
Benzinga files from (bzfetch.py: `/benzinga/v2/news`, `/benzinga/v1/earnings`) every
`[massive] news_probe_poll_seconds` and writes one JSON line for every record it sees for the
first time, and for every later change of a record, with our receipt time. A record first seen
in a poll was absent from the previous successful poll, so it became available between
`previous_poll_sent_utc` and `received_utc`.

Records of the first poll after a start are written as `baseline`: they were there before the
probe ran and time nothing. Only the fields the training reads are kept (no title, no body).
A failed poll is recorded (`poll_failed`) and the next one runs at its time; a refused key (HTTP
401/403) or a missing credential stops the probe. The key is read by NAME at each request and is
never written. Massive's general news (`nw_*`, item 9) is polled the way the training fetched it
(the owner's fetch_ref2.py / reftopup.py: `/v2/reference/news`, `published_utc` window,
`order=asc`, `sort=published_utc`); every publisher is kept, so the delay is known per publisher
(the training counts only GlobeNewswire and The Motley Fool).
"""

from __future__ import annotations

import datetime
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tickr.config import Config
from tickr.model import massive
from tickr.model.errors import ModelRefused

NEW_YORK = massive.NEW_YORK
STOP_STATUSES = ("401 ", "403 ")  # http_status details of a key or plan the API refuses


@dataclass(frozen=True)
class Source:
    name: str
    id_field: str
    fields: tuple[str, ...]  # kept and compared: a change of any of them is a revision


NEWS = Source("benzinga_news", "benzinga_id", ("published", "last_updated", "tickers", "channels"))
GENERAL = Source("massive_news", "id", ("published_utc", "publisher", "tickers"))
EARNINGS = Source("benzinga_earnings", "benzinga_id",
                  ("ticker", "date", "time", "last_updated", "actual_eps", "estimated_eps"))


SOURCES = (NEWS, EARNINGS, GENERAL)


def _stamp(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.UTC).isoformat(timespec="milliseconds")


class Probe:
    """One probe run: what was seen per source, the previous poll, the day file."""

    def __init__(self, cfg: Config, opener: massive.Opener = massive._open,
                 now: Callable[[], datetime.datetime] | None = None) -> None:
        self.m = cfg.massive
        rel = Path(self.m.news_probe_dir)
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise ModelRefused("probe_config", "[massive] news_probe_dir must be relative")
        if (self.m.news_probe_poll_seconds < 1 or self.m.news_probe_news_lookback_seconds < 1
                or self.m.news_probe_earnings_lookback_days < 0):
            raise ModelRefused("probe_config", "[massive] news_probe_* pacing and windows")
        self.folder = cfg.root / cfg.run.data_dir / rel
        self.reference = massive.Reference(cfg, opener)
        self.now = now or (lambda: datetime.datetime.now(datetime.UTC))
        self.seen: dict[str, dict[str, tuple[str, ...]]] = {s.name: {} for s in SOURCES}
        self.previous: dict[str, datetime.datetime] = {}  # source -> sent time of the last success

    def _query(self, source: Source, sent: datetime.datetime) -> tuple[str, dict[str, str]]:
        since = sent - datetime.timedelta(seconds=self.m.news_probe_news_lookback_seconds)
        if source is GENERAL:  # the training's stamp form: 2021-11-16T00:00:00Z
            return self.m.massive_news_path, {
                "published_utc.gte": since.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "order": "asc", "sort": "published_utc"}
        if source is NEWS:
            return self.m.benzinga_news_path, {
                "published.gte": since.astimezone(datetime.UTC).isoformat(timespec="seconds"),
                "sort": "published.asc"}
        today = sent.astimezone(NEW_YORK).date()
        first = today - datetime.timedelta(days=self.m.news_probe_earnings_lookback_days)
        return self.m.benzinga_earnings_path, {
            "date.gte": first.isoformat(), "date.lte": today.isoformat(), "sort": "date.asc"}

    def _write(self, lines: list[dict[str, Any]], moment: datetime.datetime) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{moment.astimezone(NEW_YORK).date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, sort_keys=True) + "\n")

    def poll(self, source: Source) -> dict[str, int]:
        """One poll of one source; returns the counts it wrote."""
        sent = self.now()
        path, query = self._query(source, sent)
        try:
            rows = self.reference.listing(path, query)
        except ModelRefused as exc:
            if exc.cause == "credential_missing" or (
                    exc.cause == "http_status" and exc.detail.startswith(STOP_STATUSES)):
                raise
            self._write([{"event": "poll_failed", "source": source.name, "cause": exc.cause,
                          "detail": exc.detail, "poll_sent_utc": _stamp(sent)}], sent)
            return {"failed": 1}
        received = self.now()
        baseline = source.name not in self.previous
        before = self.previous.get(source.name)
        seen = self.seen[source.name]
        counts = {"new": 0, "revised": 0, "baseline": 0, "unreadable": 0}
        lines: list[dict[str, Any]] = []
        for row in rows:
            key = row.get(source.id_field) if isinstance(row, dict) else None
            if not isinstance(key, str | int) or isinstance(key, bool):
                counts["unreadable"] += 1
                lines.append({"event": "row_unreadable", "source": source.name,
                              "cause": f"no {source.id_field}", "received_utc": _stamp(received)})
                continue
            kept = {f: row[f] for f in source.fields if f in row}  # absent stays absent
            if isinstance(kept.get("publisher"), dict):  # the training keeps only its name
                kept["publisher"] = {"name": kept["publisher"].get("name")}
            values = tuple(json.dumps(kept.get(f), sort_keys=True) for f in source.fields)
            old = seen.get(str(key))
            if old == values:
                continue
            event = "baseline" if baseline else "new" if old is None else "revised"
            seen[str(key)] = values
            counts[event] += 1
            line: dict[str, Any] = {"event": event, "source": source.name, "id": str(key),
                                    "poll_sent_utc": _stamp(sent),
                                    "received_utc": _stamp(received),
                                    "previous_poll_sent_utc": _stamp(before) if before else None}
            line.update(kept)
            lines.append(line)
        lines.append({"event": "poll", "source": source.name, "rows": len(rows),
                      "poll_sent_utc": _stamp(sent), "received_utc": _stamp(received), **counts})
        self._write(lines, received)
        self.previous[source.name] = sent
        return counts


def run(cfg: Config, *, opener: massive.Opener = massive._open,
        now: Callable[[], datetime.datetime] | None = None,
        wait: Callable[[float], bool] | None = None, polls: int | None = None,
        out: Callable[[str], None] = print) -> int:
    """Poll every source every `news_probe_poll_seconds` until `wait` says stop (or `polls`
    rounds, for tests). Raises ModelRefused when the probe cannot go on."""
    probe = Probe(cfg, opener, now)
    wait = wait or threading.Event().wait
    out(f"probe-news-delay: records in {probe.folder}")
    done = 0
    while polls is None or done < polls:
        start = probe.now()
        for source in SOURCES:
            counts = probe.poll(source)
            if any(counts.get(k) for k in ("new", "revised", "failed", "unreadable")):
                out(f"{_stamp(probe.now())} {source.name} "
                    + " ".join(f"{k} {v}" for k, v in counts.items() if v))
        done += 1
        if polls is not None and done >= polls:
            break
        spent = (probe.now() - start).total_seconds()
        if wait(max(0.0, cfg.massive.news_probe_poll_seconds - spent)):
            break
    return 0


def main(cfg: Config, out: Callable[[str], None] = print, **kwargs: Any) -> int:
    """`tickr probe-news-delay`: 0 when stopped by the owner (Ctrl-C), 3 on a refusal."""
    try:
        return run(cfg, out=out, **kwargs)
    except ModelRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        out("probe-news-delay: stopped")
        return 0
