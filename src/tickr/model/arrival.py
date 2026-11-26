"""Feed-arrival measurement (Codex 102, point 1): when do Massive's minute bars and Webull's
three lists actually reach the bot? Nothing here trades, scores or decides.

`record` runs one trading day (`tickr probe-feed-arrival`): it subscribes to Massive's
full-market minute aggregates through the model's own `MinuteStream` and takes the three Webull
lists through the bot's own `MarketData.screener` reads, at the bot's cadence (`[market_data]
discovery_interval_seconds` in the regular session, `screener_record_seconds` in the pre-market
and post-market, counted from the start of the minute as the bot does) and inside the same
request budget. Everything goes, as received, to one gzip JSONL capture per run under `[run]
data_dir` / `[massive] arrival_dir` / <New York date>/. Every record carries the local wall
clock (`w`, ns since the epoch) and the monotonic clock (`m`, ns) of its receipt:

- `start` / `stop`: the run's ends; `refused` / `failed`: why the stream ended the run.
- `bar`: one AM message exactly as the stream sent it (`msg`), stamped with the receipt of its
  batch. A later message for the same symbol and minute is a correction and is kept too.
- `gap`: the stream connection dropped (`down`) and the next connection started (`up`), both
  monotonic ns, with what ended it. Bars sent while it was down are lost.
- `list`: one screener read: `kind`, `session`, request start (`w0`, `m0`), response receipt
  (`w1`, `m1`) and the members `[rank, symbol, instrument_id]`; `list_skipped`: kinds not read
  this round (`budget` or the error type), as the bot's own recorder marks them.

`report` reads the captures of a day with the day's flat file (the later reference) and answers,
per session, for each report point (`[massive] arrival_report_points_seconds` after a minute's
end, REPORT ONLY: no guard reads them): which reference symbol-minutes were present, late, lost
in a connection gap or never streamed. A minute without a trade has no row in the file, so it is
never counted as missing; a streamed bar the file does not hold is counted apart.
"""

from __future__ import annotations

import bisect
import datetime
import gzip
import json
import threading
import time
from array import array
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tickr.broker.base import BrokerError, RateLimited
from tickr.calendar import MarketDay
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError, ScreenerKind, ScreenerSession
from tickr.data.discovery import SKIPPED_BUDGET
from tickr.model.errors import ModelRefused
from tickr.model.massive import NEW_YORK, MinuteStream, credential
from tickr.runtime.screeners import extended_session

NS = 10**9
NS_PER_MS = 10**6
MINUTE_NS = 60 * NS
CAPTURE_GLOB = "arrival-*.jsonl.gz"
SESSIONS = ("pre", "regular", "post")
PRICE_KEYS = ("o", "h", "l", "c", "v")  # AM keys compared with the file's open..volume
FILE_KEYS = ("open", "high", "low", "close", "volume")
QUANTILES = (0.5, 0.9, 0.99)


def capture_folder(cfg: Config, day: str) -> Path:
    rel = Path(cfg.massive.arrival_dir)
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise ModelRefused("arrival_dir_outside", "[massive] arrival_dir must be relative")
    return cfg.root / cfg.run.data_dir / rel / day


class Capture:
    """One run's capture file: gzip JSONL, flushed every `[massive] arrival_flush_seconds`
    (a killed run leaves a readable file up to its last flush). Thread-safe."""

    def __init__(self, path: Path, flush_seconds: float, wall_ns: Callable[[], int],
                 mono_ns: Callable[[], int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._raw = open(path, "xb")  # never over an earlier run's file
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="wb")
        self._lock = threading.Lock()
        self._flush_ns = int(flush_seconds * NS)
        self.wall_ns, self.mono_ns = wall_ns, mono_ns
        self._flushed = mono_ns()
        self.counts: dict[str, int] = {}

    def write(self, kind: str, record: Mapping[str, Any], mono: int | None = None) -> None:
        """Append one record of `kind`, stamped with the wall and monotonic clocks now (or
        with the monotonic receipt `mono` the caller took, and the wall clock now)."""
        now_m = self.mono_ns()
        line = {"t": kind, "w": self.wall_ns(), "m": now_m if mono is None else mono, **record}
        data = (json.dumps(line, separators=(",", ":")) + "\n").encode()
        with self._lock:
            self._gz.write(data)
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if now_m - self._flushed >= self._flush_ns:
                self._gz.flush()
                self._raw.flush()
                self._flushed = now_m

    def close(self) -> None:
        with self._lock:
            self._gz.close()
            self._raw.close()


class StreamRecorder:
    """The `BarSink` of the measurement: every AM message and every gap goes to the capture
    as received (nothing is merged, dropped or checked: the report reads the raw messages)."""

    def __init__(self, capture: Capture) -> None:
        self.capture = capture

    def add(self, message: Mapping[str, Any], arrived: int) -> None:
        self.capture.write("bar", {"msg": message}, mono=arrived)

    def gap(self, down: int, up: int, cause: str) -> None:
        self.capture.write("gap", {"down": down, "up": up, "cause": cause})


def list_session(day: MarketDay, now: datetime.datetime) -> ScreenerSession | None:
    """The session whose lists the bot reads at `now`: REGULAR in the regular session (its
    discovery), PRE or POST outside it (its screener recorder), None outside the day."""
    if day.in_regular_session(now):
        return ScreenerSession.REGULAR
    return extended_session(day, now)


class ListPoller:
    """The bot's three screener reads, at its own cadence: a round in the first loop pass of a
    minute, then the next one interval later counted from that minute's start."""

    def __init__(self, cfg: Config, market: MarketData, capture: Capture) -> None:
        md = cfg.market_data
        self.regular = datetime.timedelta(seconds=float(md.discovery_interval_seconds))
        self.extended = datetime.timedelta(seconds=float(md.screener_record_seconds))
        self.market = market
        self.capture = capture
        self._next: datetime.datetime | None = None

    def tick(self, day: MarketDay, now: datetime.datetime) -> bool:
        """One round when due; True when a round was started."""
        session = list_session(day, now)
        if session is None or (self._next is not None and now < self._next):
            return False
        interval = self.regular if session is ScreenerSession.REGULAR else self.extended
        self._next = now.replace(second=0, microsecond=0) + interval
        kinds = list(ScreenerKind)
        for number, kind in enumerate(kinds):
            w0, m0 = self.capture.wall_ns(), self.capture.mono_ns()
            try:
                rows = self.market.screener(kind, session)
            except (RateLimited, MarketDataError, BrokerError) as exc:
                cause = SKIPPED_BUDGET if isinstance(exc, RateLimited) else type(exc).__name__
                self.capture.write("list_skipped", {
                    "kinds": [k.value for k in kinds[number:]], "session": session.value,
                    "cause": cause, "w0": w0, "m0": m0})
                break
            m1 = self.capture.mono_ns()
            self.capture.write("list", {
                "kind": kind.value, "session": session.value, "w0": w0, "m0": m0,
                "w1": self.capture.wall_ns(), "m1": m1,
                "rows": [[r.rank, r.symbol, r.instrument_id] for r in rows]}, mono=m1)
        return True


def record(cfg: Config, day: MarketDay, market: MarketData, stop: threading.Event,
           say: Callable[[str], None], *, connect: Callable[..., Any] | None = None,
           wall_ns: Callable[[], int] = time.time_ns,
           mono_ns: Callable[[], int] = time.monotonic_ns,
           wait: Callable[[float], object] | None = None) -> int:
    """Record `day` until the calendar's extended close plus `[massive] arrival_tail_seconds`
    or until `stop`. Exit code: 0 recorded to the end (or stopped by the operator), 3 the
    stream refused (credential, login, a message it cannot read), 1 the stream failed."""
    m = cfg.massive
    credential(m.api_key_variable)  # refuses with the variable's NAME before anything starts
    wait = wait or stop.wait
    start = datetime.datetime.fromtimestamp(wall_ns() / NS, NEW_YORK)
    folder = capture_folder(cfg, day.ny_date.isoformat())
    capture = Capture(folder / f"arrival-{start:%H%M%S}.jsonl.gz",
                      float(m.arrival_flush_seconds), wall_ns, mono_ns)
    end = day.extended_close + datetime.timedelta(seconds=m.arrival_tail_seconds)
    capture.write("start", {"day": day.ny_date.isoformat(), "provider": market.provider,
                            "stream": f"{m.stream_host}{m.stream_path}", "until": end.isoformat()})
    say(f"probe-feed-arrival {day.ny_date.isoformat()}: capture {capture.path}; "
        f"ends {end.isoformat()} or on Ctrl-C")
    outcome: list[tuple[str, str, str]] = []
    stream = MinuteStream(cfg, StreamRecorder(capture), connect, clock=mono_ns)

    def run_stream() -> None:
        try:
            stream.run(stop)
        except ModelRefused as exc:
            outcome.append(("refused", exc.cause, exc.detail))
        except Exception as exc:  # noqa: BLE001 - recorded and ends the run, never silent
            outcome.append(("failed", type(exc).__name__, ""))
        finally:
            stop.set()

    thread = threading.Thread(target=run_stream, name="feed-arrival-stream", daemon=True)
    thread.start()
    polls = ListPoller(cfg, market, capture)
    reached_end = False
    try:
        while not stop.is_set():
            now = datetime.datetime.fromtimestamp(wall_ns() / NS, NEW_YORK)
            if now >= end:
                reached_end = True
                break
            polls.tick(day, now)
            wait(float(m.arrival_loop_seconds))
    finally:
        stop.set()
        thread.join()
        for kind, cause, detail in outcome:
            capture.write(kind, {"cause": cause, "detail": detail})
        capture.write("stop", {"cause": outcome[0][0] if outcome else "end"})
        capture.close()
    counts = capture.counts
    say(f"recorded {counts.get('bar', 0)} bar messages, {counts.get('list', 0)} list reads "
        f"({counts.get('list_skipped', 0)} rounds cut short), {counts.get('gap', 0)} "
        f"connection gaps: {capture.path}")
    for kind, cause, detail in outcome:
        say(f"stream {kind}: {cause}" + (f" ({detail})" if detail else ""))
    if reached_end and not outcome:  # the day's stream is over: keep the stream-built day
        from tickr.model import streamday

        try:
            say(f"stream-built day: {streamday.retain(cfg, day)}")
        except (ModelRefused, OSError) as exc:
            say(f"stream-built day not written: {exc}")
            return 1
    if outcome:
        return 3 if outcome[0][0] == "refused" else 1
    return 0


# --- reading a capture ------------------------------------------------------------------------


@dataclass
class Captured:
    """The day's captures, read back. Times are wall-clock ns; a monotonic time is put on the
    wall clock with the wall/monotonic pair of the record that carries it."""

    syms: list[str] = field(default_factory=list)
    starts_ms: array = field(default_factory=lambda: array("q"))  # bar start, ms
    received: array = field(default_factory=lambda: array("q"))  # receipt, wall ns
    values: dict[str, array] = field(
        default_factory=lambda: {k: array("d") for k in PRICE_KEYS})
    spans: list[tuple[int, int]] = field(default_factory=list)  # each run's (start, stop)
    outages: list[tuple[int, int, str]] = field(default_factory=list)  # (from, to, cause)
    lists: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    unreadable: int = 0  # bar messages without the fields the report reads
    truncated: int = 0  # captures that end without a stop record (the run was killed)
    ends: list[str] = field(default_factory=list)  # each run's stop cause


def _lines(path: Path) -> Iterable[dict[str, Any]]:
    """The records of one capture; a file cut short (killed run) ends at its last whole line."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        while True:
            try:
                line = f.readline()
            except EOFError:
                return
            if not line:
                return
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                return


def read_captures(paths: Sequence[Path]) -> Captured:
    out = Captured()
    for path in paths:
        first = last = None
        stopped = False
        bars_from = len(out.received)
        gaps: list[tuple[int, int, str]] = []
        for rec in _lines(path):
            w, m = rec["w"], rec["m"]
            first = w if first is None else first
            last = w
            kind = rec["t"]
            if kind == "bar":
                msg = rec.get("msg")
                try:
                    sym, start = str(msg["sym"]), int(msg["s"])
                    vals = [float(msg[k]) for k in PRICE_KEYS]
                except (KeyError, TypeError, ValueError):
                    out.unreadable += 1
                    continue
                out.syms.append(sym)
                out.starts_ms.append(start)
                out.received.append(w)
                for k, v in zip(PRICE_KEYS, vals, strict=True):
                    out.values[k].append(v)
            elif kind == "gap":
                gaps.append((w - (m - rec["down"]), w - (m - rec["up"]), rec["cause"]))
            elif kind == "list":
                out.lists.append(rec)
            elif kind == "list_skipped":
                out.skipped.append(rec)
            elif kind == "stop":
                stopped = True
                out.ends.append(rec["cause"])
        if first is None:
            continue
        out.spans.append((first, last))
        out.truncated += not stopped
        # a gap ends when data flows again: at the first bar received after the reconnect
        got = sorted(out.received[bars_from:])
        for down, up, cause in gaps:
            after = bisect.bisect_left(got, up)
            out.outages.append((down, got[after] if after < len(got) else last, cause))
    out.spans.sort()
    for (_, stop), (start, _) in zip(out.spans, out.spans[1:], strict=False):
        if start > stop:  # the recorder was not running between two runs
            out.outages.append((stop, start, "not running"))
    out.outages.sort()
    return out


# --- the report -------------------------------------------------------------------------------


def _stats(seconds: Any) -> dict[str, float | None]:
    import numpy as np

    v = np.asarray(seconds, dtype=float)
    if not len(v):
        return {"n": 0, **{f"p{round(q * 100)}": None for q in QUANTILES}, "max": None}
    return {"n": int(len(v)), **{f"p{round(q * 100)}": float(np.quantile(v, q))
                                 for q in QUANTILES}, "max": float(v.max())}


def _overlaps(outages: Sequence[tuple[int, int, str]], lo: Any, hi: Any) -> Any:
    import numpy as np

    hit = np.zeros(len(lo), dtype=bool)
    for a, b, _ in outages:
        hit |= (lo < b) & (hi > a)
    return hit


def _session_bounds(day: MarketDay) -> dict[str, tuple[int, int]]:
    def ns(t: datetime.datetime) -> int:
        return int(t.timestamp()) * NS

    return {"pre": (ns(day.premarket_open), ns(day.regular_open)),
            "regular": (ns(day.regular_open), ns(day.regular_close)),
            "post": (ns(day.regular_close), ns(day.extended_close))}


def _session_of(bounds: Mapping[str, tuple[int, int]], t: Any) -> Any:
    import numpy as np

    out = np.full(len(t), "", dtype=object)
    for name, (a, b) in bounds.items():
        out[(t >= a) & (t < b)] = name
    return out


def report(cap: Captured, flat: Any, day: MarketDay,
           points: Sequence[float]) -> dict[str, Any]:
    """The measurement of one day, per session: see the module doc. `flat` is the day's
    minute_aggs frame (the reference), `points` the report points in seconds."""
    import numpy as np
    import pandas as pd

    bounds = _session_bounds(day)
    day_lo, day_hi = bounds["pre"][0], bounds["post"][1]
    pts = [int(float(p) * NS) for p in points]
    far = pts[-1]
    msgs = pd.DataFrame({"sym": cap.syms, "s": np.frombuffer(cap.starts_ms, dtype=np.int64),
                         "w": np.frombuffer(cap.received, dtype=np.int64),
                         **{k: np.frombuffer(v, dtype=np.float64)
                            for k, v in cap.values.items()}})
    msgs["start"] = msgs.s * NS_PER_MS
    other_day = int(((msgs.start < day_lo) | (msgs.start >= day_hi)).sum())
    msgs = msgs[(msgs.start >= day_lo) & (msgs.start < day_hi)]
    msgs = msgs.sort_values("w", kind="stable")
    count = msgs.groupby(["sym", "s"], sort=False).size().rename("n")
    first = msgs.drop_duplicates(["sym", "s"], keep="first").set_index(["sym", "s"])
    last = msgs.drop_duplicates(["sym", "s"], keep="last").set_index(["sym", "s"])
    streamed = first.join(count).join(last[["w", *PRICE_KEYS]], rsuffix="_last")
    changed = np.zeros(len(streamed), dtype=bool)
    for k in PRICE_KEYS:
        changed |= streamed[k].to_numpy() != streamed[f"{k}_last"].to_numpy()
    streamed["changed"] = changed
    streamed = streamed.reset_index()
    streamed["end"] = streamed.start + MINUTE_NS
    streamed["session"] = _session_of(bounds, streamed.start.to_numpy())

    ref = pd.DataFrame({"sym": flat.ticker.astype(str),
                        "s": flat.window_start.astype("int64") // NS_PER_MS,
                        **{f"f_{k}": flat[c].astype(float) for k, c in
                           zip(PRICE_KEYS, FILE_KEYS, strict=True)}})
    ref = ref[(ref.s * NS_PER_MS >= day_lo) & (ref.s * NS_PER_MS < day_hi)]
    both = streamed.merge(ref, on=["sym", "s"], how="outer", indicator=True)
    both["start"] = both.s * NS_PER_MS
    both["end"] = both.start + MINUTE_NS
    both["session"] = _session_of(bounds, both.start.to_numpy())
    cov_lo = cap.spans[0][0] if cap.spans else 0
    cov_hi = cap.spans[-1][1] if cap.spans else 0
    lost = _overlaps(cap.outages, both.end.to_numpy(), both.end.to_numpy() + far)

    sessions: dict[str, Any] = {}
    for name in SESSIONS:
        s_rows = both[both.session == name]
        s_lost = lost[(both.session == name).to_numpy()]
        in_file = (s_rows._merge != "left_only").to_numpy()
        got = (s_rows._merge != "right_only").to_numpy()
        inside = ((s_rows.end >= cov_lo) & (s_rows.end + far <= cov_hi)).to_numpy() & in_file
        delay = (s_rows.w - s_rows.end).to_numpy()  # first receipt after the minute's end
        at = []
        for p, secs in zip(pts, points, strict=True):
            present = inside & got & (delay <= p)
            late = inside & got & (delay > p)
            gap = inside & ~got & s_lost
            never = inside & ~got & ~s_lost
            total = int(inside.sum())
            at.append({"seconds": float(secs), "reference": total,
                       **{k: int(v.sum()) for k, v in (("present", present), ("late", late),
                                                        ("gap", gap), ("never", never))}})
        recv = s_rows[got]
        file_got = s_rows[got & in_file]
        same_first = np.ones(len(file_got), dtype=bool)
        same_last = np.ones(len(file_got), dtype=bool)
        for k in PRICE_KEYS:
            same_first &= file_got[k].to_numpy() == file_got[f"f_{k}"].to_numpy()
            same_last &= file_got[f"{k}_last"].to_numpy() == file_got[f"f_{k}"].to_numpy()
        corr = recv[recv.n > 1]
        lo, hi = bounds[name]
        gaps = [(a, b, c) for a, b, c in cap.outages if lo <= a < hi]
        sessions[name] = {
            "delay": _stats((recv.w - recv.end) / NS),
            "points": at,
            "outside_capture": int((in_file & ~inside).sum()),
            "late_after_last_point": int((delay[got] > far).sum()),
            "corrections": int(len(corr)), "corrections_changed": int(corr.changed.sum()),
            "correction_delay": _stats((corr.w_last - corr.end) / NS),
            "first_differs_from_file": int((~same_first).sum()),
            "last_differs_from_file": int((~same_last).sum()),
            "never_streamed": int((inside & ~got & ~s_lost).sum()),
            "lost_in_gaps": int((inside & ~got & s_lost).sum()),
            "stream_only": int((got & ~in_file).sum()),
            "gaps": len(gaps), "gap_seconds": sum((b - a) for a, b, _ in gaps) / NS,
            "lists": _lists(cap, name, bounds, streamed, ref),
        }
    return {"day": day.ny_date.isoformat(), "sessions": sessions, "other_day": other_day,
            "unreadable": cap.unreadable, "truncated": cap.truncated, "runs": len(cap.spans),
            "outages": [(a, b, c) for a, b, c in cap.outages]}


def _lists(cap: Captured, name: str, bounds: Mapping[str, tuple[int, int]], streamed: Any,
           ref: Any) -> dict[str, Any]:
    """Snapshot latency and cohort coverage of the Webull lists read in session `name`. Each
    member is looked up at the snapshot's decision minute: the last minute that ended before
    the request was sent."""
    import pandas as pd

    lo, hi = bounds[name]
    reads = [r for r in cap.lists if lo <= r["w0"] < hi]
    skipped: dict[str, int] = {}
    for r in cap.skipped:
        if lo <= r["w0"] < hi:
            skipped[r["cause"]] = skipped.get(r["cause"], 0) + 1
    members = pd.DataFrame(
        [(i, row[1], (r["w0"] // MINUTE_NS - 1) * MINUTE_NS // NS_PER_MS, r["w1"])
         for i, r in enumerate(reads) for row in r["rows"]],
        columns=["read", "sym", "s", "w1"])
    out: dict[str, Any] = {
        "reads": len(reads), "skipped": skipped,
        "latency": _stats([(r["w1"] - r["w0"]) / NS for r in reads]),
        "members": int(len(members)), "symbols": int(members.sym.nunique())}
    if members.empty:
        return out | {k: 0 for k in ("present", "late", "never", "untraded", "not_in_file")}
    m = members.merge(streamed[["sym", "s", "w"]], on=["sym", "s"], how="left")
    m = m.merge(ref[["sym", "s"]].assign(in_file=True), on=["sym", "s"], how="left")
    known = m.sym.isin(set(ref.sym))
    has = m.w.notna()
    in_file = m.in_file.notna()
    return out | {
        "present": int((has & (m.w <= m.w1)).sum()),
        "late": int((has & (m.w > m.w1)).sum()),
        "never": int((~has & in_file).sum()),
        "untraded": int((~has & ~in_file & known).sum()),
        "not_in_file": int((~has & ~known).sum()),
    }


def _fmt(stats: Mapping[str, Any], unit: str = "s") -> str:
    if not stats["n"]:
        return "n=0"
    return (f"n={stats['n']} " + " ".join(
        f"{k}={stats[k]:.3f}{unit}" for k in ("p50", "p90", "p99", "max")))


def render(result: Mapping[str, Any]) -> list[str]:
    """The report as printed lines."""
    lines = [f"feed arrival {result['day']}: {result['runs']} run(s), "
             f"{result['truncated']} cut short, {result['unreadable']} unreadable bar messages, "
             f"{result['other_day']} bars of another day"]
    for name in SESSIONS:
        s = result["sessions"][name]
        lines += [f"[{name}]",
                  f"  bar delay after minute end: {_fmt(s['delay'])}",
                  "  at end+N s (reference symbol-minutes; present / late / gap / never):"]
        for p in s["points"]:
            total = p["reference"] or 1
            lines.append(f"    +{p['seconds']:g}s of {p['reference']}: " + " / ".join(
                f"{p[k]} ({100 * p[k] / total:.2f}%)"
                for k in ("present", "late", "gap", "never")))
        lines += [
            f"  late after the last point: {s['late_after_last_point']}; outside the capture: "
            f"{s['outside_capture']}",
            f"  corrections: {s['corrections']} ({s['corrections_changed']} changed values), "
            f"last at {_fmt(s['correction_delay'])}",
            f"  stream vs file: first message differs {s['first_differs_from_file']}, last "
            f"differs {s['last_differs_from_file']}; file minutes never streamed "
            f"{s['never_streamed']}, lost in gaps {s['lost_in_gaps']}; streamed but not in the "
            f"file {s['stream_only']}",
            f"  connection gaps: {s['gaps']} ({s['gap_seconds']:.1f} s)"]
        li = s["lists"]
        lines.append(f"  webull lists: {li['reads']} reads, skipped {li['skipped'] or 'none'}, "
                     f"latency {_fmt(li['latency'])}")
        if li["members"]:
            lines.append(
                f"  list members ({li['members']} member-reads, {li['symbols']} symbols) at the "
                f"decision minute: bar received by the list's receipt {li['present']}, later "
                f"{li['late']}, never streamed {li['never']}, no trade that minute "
                f"{li['untraded']}, symbol not in the file {li['not_in_file']}")
    for a, b, cause in result["outages"]:
        lines.append(f"gap {_wall(a)} .. {_wall(b)} ({(b - a) / NS:.1f} s): {cause}")
    return lines


def _wall(ns: int) -> str:
    return datetime.datetime.fromtimestamp(ns / NS, NEW_YORK).strftime("%H:%M:%S.%f")[:-3]


def day_captures(cfg: Config, day: str) -> list[Path]:
    return sorted(capture_folder(cfg, day).glob(CAPTURE_GLOB))
