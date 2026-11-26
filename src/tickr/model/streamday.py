"""Stream-built day files (Codex 102, point 3): can a previous session built from the live
minute-bar stream replace the published `minute_aggs` file at 04:00? Measurement only: nothing
here trades, scores, decides or guards.

`build` turns the day's feed-arrival captures (`tickr.model.arrival`, the retained stream: every
AM message with its receipt time, and the connection gaps) into the day the model's `DayBars`
holds at the end of the stream: each message is replayed through `DayBars.add` in receipt
order, so a later message of the same symbol-minute (a correction) replaces the earlier one,
exactly as live. Each row also carries its first and last receipt (wall ns) and its count of
corrections (messages after the first).

`write` keeps it under `[run] data_dir` / `[massive] stream_day_dir`: `<date>.gaps.json` (the
day's gaps: connection drops, the recorder not running, and the day before the first run or
after the last), then `<date>.csv.gz` in the flat file's columns (`features.BAR_COLUMNS`) plus
`first_receipt_ns`, `last_receipt_ns`, `corrections`. Each file is written to `.part`, synced
and renamed; the CSV last, so its presence means the day is complete. The recorder writes them
when it reaches the calendar's extended close (early closes included).

`compare` sets a stream-built day against the published file of the same day, per (ticker,
minute): equal in every bar column, different (which column, by how much), only in the stream,
only in the file. Rows of a minute a gap may have touched (a gap overlapping the minute or the
`[massive] arrival_report_points_seconds` last point after its end, the window the arrival
report also uses) are counted apart. REPORT ONLY: no guard reads the result.
"""

from __future__ import annotations

import datetime
import gzip
import io
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tickr.calendar import MarketDay
from tickr.config import Config
from tickr.model import arrival
from tickr.model.errors import ModelRefused
from tickr.model.massive import FILE_COLUMNS, NEW_YORK, DayBars

NS = 10**9
NS_PER_MS = 10**6
MINUTE_NS = 60 * NS
RECEIPT_COLUMNS = ("first_receipt_ns", "last_receipt_ns", "corrections")
COLUMNS = (*FILE_COLUMNS, *RECEIPT_COLUMNS)
VALUE_COLUMNS = ("open", "high", "low", "close", "volume")
OUTCOMES = ("equal", "different", "stream_only", "file_only")
QUANTILES = (0.5, 0.9)


@dataclass
class StreamDay:
    day: str
    rows: Any  # pandas frame of COLUMNS
    gaps: list[tuple[int, int, str]] = field(default_factory=list)  # (from, to) wall ns, cause
    messages: int = 0
    unreadable: int = 0
    other_day: int = 0
    captures: int = 0


def folder(cfg: Config) -> Path:
    rel = Path(cfg.massive.stream_day_dir)
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise ModelRefused("stream_day_dir_outside", "[massive] stream_day_dir must be relative")
    return cfg.root / cfg.run.data_dir / rel


def paths(cfg: Config, day: str) -> tuple[Path, Path]:
    """(day file, gaps file) of `day`."""
    base = folder(cfg)
    return base / f"{day}.csv.gz", base / f"{day}.gaps.json"


def _ns(t: datetime.datetime) -> int:
    return int(t.timestamp()) * NS


def build(captures: Sequence[Path], day: MarketDay) -> StreamDay:
    """The day `DayBars` holds after replaying every captured AM message in receipt order."""
    import pandas as pd

    if not captures:
        raise ModelRefused("capture_missing", day.ny_date.isoformat())
    name = day.ny_date.isoformat()
    messages: list[tuple[int, int, Mapping[str, Any]]] = []
    for path in captures:
        for rec in arrival._lines(path):
            if rec.get("t") == "bar":
                messages.append((rec["w"], len(messages), rec.get("msg")))
    messages.sort(key=lambda m: m[:2])  # receipt order; file order within one receipt
    bars = DayBars(name)
    receipts: dict[tuple[str, int], list[int]] = {}  # key -> [first, last, messages]
    unreadable = 0
    for w, _, msg in messages:
        try:
            bars.add(msg, w)
        except ModelRefused:
            unreadable += 1
            continue
        key = (msg["sym"], int(msg["s"]))
        if key not in bars.rows:
            continue  # another New York day, counted by DayBars
        seen = receipts.setdefault(key, [w, w, 0])
        seen[1] = w
        seen[2] += 1
    frame = bars.frame()
    keys = list(bars.rows)  # frame rows are in this order
    frame["first_receipt_ns"] = [receipts[k][0] for k in keys]
    frame["last_receipt_ns"] = [receipts[k][1] for k in keys]
    frame["corrections"] = [receipts[k][2] - 1 for k in keys]
    frame = frame.sort_values(["ticker", "window_start"], kind="stable").reset_index(drop=True)
    cap = arrival.read_captures(captures)
    gaps = list(cap.outages)
    lo, hi = _ns(day.premarket_open), _ns(day.extended_close)
    if cap.spans and cap.spans[0][0] > lo:
        gaps.append((lo, cap.spans[0][0], "not running"))
    if cap.spans and cap.spans[-1][1] < hi:
        gaps.append((cap.spans[-1][1], hi, "not running"))
    if not cap.spans:
        gaps.append((lo, hi, "not running"))
    return StreamDay(name, pd.DataFrame(frame, columns=list(COLUMNS)), sorted(gaps),
                     messages=len(messages), unreadable=unreadable, other_day=bars.other_day,
                     captures=len(captures))


def _replace(target: Path, body: bytes) -> None:
    """Write `body` to `target` through `<target>.part`, synced before and after the rename."""
    part = target.with_name(target.name + ".part")
    with open(part, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, target)
    fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(cfg: Config, sd: StreamDay) -> Path:
    """Keep the stream-built day (gaps first, the day file last); returns the day file."""
    day_path, gaps_path = paths(cfg, sd.day)
    day_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"day": sd.day, "captures": sd.captures, "messages": sd.messages,
            "unreadable": sd.unreadable, "other_day": sd.other_day, "rows": int(len(sd.rows)),
            "gaps": [{"from_ns": a, "to_ns": b, "cause": c} for a, b, c in sd.gaps]}
    _replace(gaps_path, (json.dumps(meta, indent=1) + "\n").encode())
    text = sd.rows.to_csv(index=False, lineterminator="\n").encode()
    _replace(day_path, gzip.compress(text, mtime=0))
    return day_path


def retain(cfg: Config, day: MarketDay) -> Path:
    """Build and keep `day` from all of its captures (the recorder calls it at the day's end)."""
    return write(cfg, build(arrival.day_captures(cfg, day.ny_date.isoformat()), day))


def read(cfg: Config, day: str, day_path: Path | None = None) -> StreamDay:
    """A kept stream-built day; a missing or unreadable one refuses with its path."""
    import pandas as pd

    day_path = day_path or paths(cfg, day)[0]
    gaps_path = day_path.with_name(day_path.name.removesuffix(".csv.gz") + ".gaps.json")
    if not day_path.is_file():
        raise ModelRefused("stream_day_missing", str(day_path))
    try:
        rows = pd.read_csv(io.BytesIO(gzip.decompress(day_path.read_bytes())),
                           dtype={"ticker": str}, na_filter=False)
        meta = json.loads(gaps_path.read_text())
        gaps = [(int(g["from_ns"]), int(g["to_ns"]), str(g["cause"])) for g in meta["gaps"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ModelRefused("stream_day_unreadable", f"{day_path}: {type(exc).__name__}") from exc
    missing = [c for c in COLUMNS if c not in rows.columns]
    if missing:
        raise ModelRefused("stream_day_unreadable", f"{day_path}: no {', '.join(missing)}")
    return StreamDay(day, rows, gaps, messages=meta.get("messages", 0),
                     unreadable=meta.get("unreadable", 0), other_day=meta.get("other_day", 0),
                     captures=meta.get("captures", 0))


# --- the comparison ------------------------------------------------------------------------------


def _stats(values: Any) -> dict[str, float | None]:
    import numpy as np

    v = np.asarray(values, dtype=float)
    if not len(v):
        return {"n": 0, **{f"p{round(q * 100)}": None for q in QUANTILES}, "max": None}
    return {"n": int(len(v)), **{f"p{round(q * 100)}": float(np.quantile(v, q))
                                 for q in QUANTILES}, "max": float(v.max())}


def compare(sd: StreamDay, flat: Any, day: MarketDay, tail_seconds: float) -> dict[str, Any]:
    """Stream-built day `sd` against the day's published minute_aggs frame `flat`, per
    session (see the module doc). `tail_seconds`: how long after a minute's end a gap still
    counts as touching it."""
    import numpy as np
    import pandas as pd

    bounds = {"pre": (_ns(day.premarket_open), _ns(day.regular_open)),
              "regular": (_ns(day.regular_open), _ns(day.regular_close)),
              "post": (_ns(day.regular_close), _ns(day.extended_close))}
    lo, hi = bounds["pre"][0], bounds["post"][1]
    key = ["ticker", "window_start"]

    def side(frame: Any, extra: Sequence[str]) -> Any:
        out = pd.DataFrame({"ticker": frame.ticker.astype(str),
                            "window_start": frame.window_start.astype("int64"),
                            **{c: frame[c].astype(float) for c in VALUE_COLUMNS},
                            **{c: frame[c] for c in extra}})
        return out[(out.window_start >= lo) & (out.window_start < hi)]

    stream = side(sd.rows, ["corrections"])
    ref = side(flat, [])
    both = stream.merge(ref, on=key, how="outer", suffixes=("_s", "_f"), indicator=True)
    start = both.window_start.to_numpy()
    tail = int(float(tail_seconds) * NS)
    gapped = arrival._overlaps(sd.gaps, start, start + MINUTE_NS + tail)
    paired = (both._merge == "both").to_numpy()
    diff_cols = {c: paired & (both[f"{c}_s"].to_numpy() != both[f"{c}_f"].to_numpy())
                 for c in VALUE_COLUMNS}
    different = np.zeros(len(both), dtype=bool)
    for flags in diff_cols.values():
        different |= flags
    outcome = np.where(both._merge == "left_only", "stream_only",
                       np.where(both._merge == "right_only", "file_only",
                                np.where(different, "different", "equal")))
    corrected = both.corrections.fillna(0).to_numpy() > 0

    sessions: dict[str, Any] = {}
    for name, (a, b) in bounds.items():
        here = (start >= a) & (start < b)
        counts = {}
        for label, part in (("clean", here & ~gapped), ("gapped", here & gapped)):
            counts[label] = {o: int((part & (outcome == o)).sum()) for o in OUTCOMES}
        clean = here & ~gapped
        columns = {}
        for c, flags in diff_cols.items():
            hit = clean & flags
            s, f = both[f"{c}_s"].to_numpy()[hit], both[f"{c}_f"].to_numpy()[hit]
            d = np.abs(s - f)
            rel = d / np.where(f == 0, np.nan, np.abs(f))
            columns[c] = {"rows": int(hit.sum()), "abs": _stats(d),
                          "rel": _stats(rel[~np.isnan(rel)])}
        sessions[name] = {
            **counts, "columns": columns,
            "different_corrected": int((clean & (outcome == "different") & corrected).sum()),
            "equal_corrected": int((clean & (outcome == "equal") & corrected).sum()),
            "gaps": [(g0, g1, c) for g0, g1, c in sd.gaps if g0 < b and g1 > a],
        }
    return {"day": sd.day, "rows_stream": int(len(stream)), "rows_file": int(len(ref)),
            "captures": sd.captures, "messages": sd.messages, "unreadable": sd.unreadable,
            "other_day": sd.other_day, "tail_seconds": float(tail_seconds),
            "sessions": sessions}


def _fmt(stats: Mapping[str, Any]) -> str:
    if not stats["n"]:
        return "n=0"
    return f"n={stats['n']} " + " ".join(f"{k}={stats[k]:.6g}" for k in ("p50", "p90", "max"))


def _wall(ns: int) -> str:
    return datetime.datetime.fromtimestamp(ns / NS, NEW_YORK).strftime("%H:%M:%S")


def render(result: Mapping[str, Any]) -> list[str]:
    lines = [f"stream-built day {result['day']} vs published file: {result['rows_stream']} "
             f"stream rows, {result['rows_file']} file rows ({result['captures']} capture(s), "
             f"{result['messages']} messages, {result['unreadable']} unreadable, "
             f"{result['other_day']} of another day); gapped = a gap within the minute or "
             f"{result['tail_seconds']:g} s after it"]
    for name, s in result["sessions"].items():
        lines.append(f"[{name}]")
        for label in ("clean", "gapped"):
            c = s[label]
            lines.append(f"  {label}: equal {c['equal']}, different {c['different']}, only in "
                         f"stream {c['stream_only']}, only in file {c['file_only']}")
        lines.append(f"  clean rows with corrections: equal {s['equal_corrected']}, different "
                     f"{s['different_corrected']}")
        for col, d in s["columns"].items():
            if d["rows"]:
                lines.append(f"  {col} differs in {d['rows']}: |stream-file| {_fmt(d['abs'])}; "
                             f"relative {_fmt(d['rel'])}")
        for a, b, cause in s["gaps"]:
            lines.append(f"  gap {_wall(a)} .. {_wall(b)} ({(b - a) / NS:.1f} s): {cause}")
    return lines
