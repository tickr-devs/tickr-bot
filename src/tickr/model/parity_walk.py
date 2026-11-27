"""The incremental engine of the replay parity harness (`tickr.model.parity --engine incremental`):
one walk over the day's ticks with the carry of #820 (`features.ticks.tail_carry` /
`minute_rows`), so the rows of the minutes since the last chosen minute cost about those
minutes' events instead of the whole day so far. With every minute chosen (`--all-minutes`) the
walk converts one closed minute at a time (`minute_rows`); between two chosen minutes further
apart it converts the span in one call (`span_rows`, the same rows by v11's prefix rule).

What it gives at a table minute m is the tick table the full-prefix engine (`parity.minute_ticks`)
gives at m for the same variant, built from rows each converted once, at the close of their own
minute k or of the span holding it (minute of the New York day):
- the MARKER rows of k: `minute_rows(..., marker=True)`, the live path's conversion (#826 applies
  the marker per closed minute in the same way): a symbol that quoted in k gets one copy of its
  latest quote of k at the first nanosecond of k + 1, so that quote weighs to the end of k (v11
  caps a weight at its own minute end; the marker never enters the carry or a count); over a
  span, one marker after the span gives each row the same weight (#826's catch-up close);
- the PLAIN rows of k: `minute_rows(..., marker=False)` (that quote weighs 0), needed only for
  the symbols that quoted in k; a symbol without a quote in k has the same row either way.
The full-prefix table at m holds, for a symbol whose latest quote of the prefix lies in minute
L <= m: the marker row at every k < L (a later real quote exists in the prefix, and the weight is
capped at the minute end, so it equals the marker's), and at L
- `plain`: the plain row (no later update in the prefix: weight 0);
- `marker`: the marker row (the full-prefix marker stands at the end of m, capped at L's end);
- `marker-minute`: the marker row when L = m, else the plain row.
So each variant's table is the marker rows with the row (symbol, L) taken from the plain rows
where the variant says so. That equality is what `--verify-full-prefix` checks on the real day
and tests/model/test_parity_fast.py checks on synthetic days; a difference is reported, never
accepted.

Events are read once, in (sip_timestamp, sequence_number) order, from the harness's extracted
copy (`parity.extract_events`: the converter's columns as the file's text), and typed as v11
casts them (BIGINT stamps and codes, DOUBLE prices and sizes, text otherwise); v11 reads the
re-written text with the same casts, so the values it sees are the same.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tickr.model import headroom
from tickr.model.errors import ModelRefused
from tickr.model.features import ticks

# Rows fetched per read of an event file: 64 engine vectors of 2048 rows (memory pacing only).
BATCH_VECTORS = 64


class _Events:
    """The events of one extracted file in time order, handed out minute by minute."""

    def __init__(self, path: Path, columns: list[str], room: headroom.Headroom) -> None:
        import duckdb

        casts = []
        for c in columns:
            if c in ticks.TEXT_COLUMNS:
                casts.append(c)
            elif c in ticks.FLOAT_COLUMNS:
                casts.append(f"CAST({c} AS DOUBLE) AS {c}")
            else:
                casts.append(f"CAST({c} AS BIGINT) AS {c}")
        self.con = room.connect()
        self.path = path
        try:
            # the file is already in event order (`parity.extract_events`): read it in file
            # order with one thread instead of sorting it again; `take` refuses any step back
            self.con.execute("SET threads=1")
            self.con.execute("SET preserve_insertion_order=true")
            self.con.execute(f"SELECT {', '.join(casts)} FROM read_parquet(?)", [str(path)])
        except duckdb.Error as exc:
            self.con.close()
            raise ModelRefused("tick_file_unreadable", type(exc).__name__) from exc
        self.buffer: Any = None
        self.done = False
        self.last: tuple[int, int] | None = None  # (stamp, sequence) of the last event read

    def _ordered(self, more: Any) -> None:
        """Refuse `tick_file_unordered` unless `more` continues the event order."""
        import numpy as np

        stamp = more.sip_timestamp.to_numpy()
        seq = more.sequence_number.to_numpy()
        step = (stamp[1:] > stamp[:-1]) | ((stamp[1:] == stamp[:-1]) & (seq[1:] >= seq[:-1]))
        first = (int(stamp[0]), int(seq[0]))
        if not bool(np.all(step)) or (self.last is not None and first < self.last):
            raise ModelRefused("tick_file_unordered", self.path.name)
        self.last = (int(stamp[-1]), int(seq[-1]))

    def take(self, end: int) -> Any:
        """Every not yet taken event stamped before `end` (ns)."""
        import pandas as pd

        while not self.done and (self.buffer is None or not len(self.buffer)
                                 or self.buffer.sip_timestamp.iloc[-1] < end):
            more = self.con.fetch_df_chunk(BATCH_VECTORS)
            if not len(more):
                self.done = True
                if self.buffer is None:
                    self.buffer = more
                break
            self._ordered(more)
            self.buffer = more if self.buffer is None or not len(self.buffer) else pd.concat(
                [self.buffer, more], ignore_index=True)
        cut = int(self.buffer.sip_timestamp.searchsorted(end, side="left"))
        got = self.buffer.iloc[:cut].reset_index(drop=True)
        self.buffer = self.buffer.iloc[cut:].reset_index(drop=True)
        return got

    def close(self) -> None:
        self.con.close()


def span_rows(carry: tuple[Any, Any], trades: Any, quotes: Any, day: str, first: int,
              through: int, *, groups: int, marker: bool, tmp_dir: Path | None = None,
              memory: str | None = None) -> Any:
    """`ticks.minute_rows` for the minutes `first` .. `through` at once: the carry of every
    earlier event and the span's own events in one conversion, with `marker` one marker quote
    per symbol that quoted in the span (its latest quote, at the first nanosecond after the
    span); the rows of the span's minutes. By v11's prefix rule a row of minute k reads only
    events up to k and, for the weight of k's last quote, the stamp of the next update, capped
    at the end of k: inside the span that is the real next update, after the span's last quote
    of a symbol the marker, exactly as minute-by-minute closes give. `tmp_dir` / `memory` as
    in `ticks.tick_rows`."""
    import os
    import tempfile

    import pandas as pd

    lo = ticks._bounds(day, 0)[0]
    start = lo if first == 0 else ticks._bounds(day, first - 1)[1]
    hi = ticks._bounds(day, through)[1]
    for name, frame, a, b in (("carry trades", carry[0], lo, start),
                              ("carry quotes", carry[1], lo, start),
                              ("trades", trades, start, hi), ("quotes", quotes, start, hi)):
        if len(frame) and (frame.sip_timestamp.min() < a or frame.sip_timestamp.max() >= b):
            raise ModelRefused("tick_outside_minute", f"{name} of minutes {first}..{through}")
    tail_quotes = [carry[1], quotes]
    if marker and len(quotes):
        tail_quotes.append(ticks.marker_quotes(quotes, hi))
    with tempfile.TemporaryDirectory(prefix="tickr-span-", dir=tmp_dir) as tmp:
        t, q = os.path.join(tmp, "trades.csv"), os.path.join(tmp, "quotes.csv")
        ticks.write_flat_file(pd.concat([carry[0], trades], ignore_index=True), t,
                              ticks.trade_columns())
        ticks.write_flat_file(pd.concat(tail_quotes, ignore_index=True), q,
                              ticks.quote_columns())
        rows = ticks.tick_rows(t, q, day, through + 1 if marker else through, groups=groups,
                               tmp_dir=tmp, memory=memory)
    return rows[(rows.m >= first) & (rows.m <= through)].reset_index(drop=True)


class TickWalk:
    """The day's tick rows, one closed minute at a time (module docstring). `plain`: also the
    plain rows (variants plain and marker-minute); `groups`: v11's group count. Every
    conversion runs in a conversion folder of `room` (the harness's disk and memory guard,
    kinds `minute` and `span`: their files are plain csv written from frames)."""

    def __init__(self, room: headroom.Headroom, day: str, *, plain: bool, groups: int) -> None:
        self.day, self.plain, self.groups, self.room = day, plain, groups, room
        self.trades = _Events(room.work / "trades.parquet", ticks.trade_columns(), room)
        self.quotes = _Events(room.work / "quotes.parquet", ticks.quote_columns(), room)
        self.next = 0  # the next minute of the New York day to convert
        self.carry: tuple[Any, Any] | None = None
        self.parts: list[Any] = []  # marker rows of the converted minutes not yet in `whole`
        self.whole: Any = None  # marker rows of every converted minute, in minute order
        self.last_plain: Any = None  # per symbol: its plain row at its latest quote minute
        self.seconds = 0.0  # wall time spent converting
        self.calls = 0  # converter calls

    def advance(self, through: int) -> None:
        """Convert every minute up to `through` (minute of the New York day): one minute at a
        time through `ticks.minute_rows` when the walk stands one minute before it, else the
        span of minutes since the walk's last stop in one conversion (`span_rows`)."""
        import pandas as pd

        if through < self.next:
            raise ModelRefused("replay_engine_order", f"minute {through}, walk at {self.next}")
        t0 = time.perf_counter()
        first, end = self.next, ticks._bounds(self.day, through)[1]
        self.next = through + 1
        t, q = self.trades.take(end), self.quotes.take(end)
        if len(t) or len(q):
            carry = self.carry if self.carry is not None else (t.iloc[:0], q.iloc[:0])
            marked = self._convert(carry, t, q, first, through, True)
            if len(marked):
                self.parts.append(marked)
            if self.plain and len(q):
                plain = self._convert(carry, t, q, first, through, False)
                last = (pd.DataFrame({"sym": q.ticker.to_numpy(),
                                      "m": ticks._minutes(q.sip_timestamp).to_numpy()})
                        .groupby("sym", sort=False).m.max())
                at = plain.m.to_numpy() == last.reindex(plain.sym.to_numpy()).to_numpy()
                quoted = plain[at]
                old = self.last_plain
                self.last_plain = quoted if old is None else pd.concat(
                    [old[~old.sym.isin(set(quoted.sym))], quoted], ignore_index=True)
            self.carry = ticks.tail_carry(pd.concat([carry[0], t], ignore_index=True),
                                          pd.concat([carry[1], q], ignore_index=True))
        self.seconds += time.perf_counter() - t0

    def _convert(self, carry: tuple[Any, Any], t: Any, q: Any, first: int, through: int,
                 marker: bool) -> Any:
        self.calls += 1
        kind = "minute" if first == through else "span"
        memory = self.room.duckdb_memory()

        def run(folder: Path, frames: tuple[Any, Any, Any, Any]) -> Any:
            c, tt, qq = (frames[0], frames[1]), frames[2], frames[3]
            if first == through:
                return ticks.minute_rows(c, tt, qq, self.day, through, groups=self.groups,
                                         marker=marker, tmp_dir=folder, memory=memory)
            return span_rows(c, tt, qq, self.day, first, through, groups=self.groups,
                             marker=marker, tmp_dir=folder, memory=memory)

        frames = (carry[0], carry[1], t, q)
        # a marker per quoting symbol at most: counted as one row per quote symbol
        rows = sum(len(f) for f in frames) + (q.ticker.nunique() if marker and len(q) else 0)

        def probe(folder: Path, n: int) -> int:
            part = tuple(f.head(n) for f in frames)
            run(folder, part)
            return sum(len(f) for f in part)

        with self.room.conversion(kind, rows, probe) as folder:
            return run(folder, frames)

    def table(self, variant: str, through: int) -> Any:
        """The tick table of the variant up to `through` (every minute up to it converted), as
        `parity.minute_ticks` returns it: sorted by sym and m, a fresh index; None when no
        event came before the end of `through`. The walk must stand at `through` (a plain row
        is the one of the latest quote minute up to there)."""
        import pandas as pd

        if self.next != through + 1:
            raise ModelRefused("replay_engine_order", f"minute {through}, walk at {self.next}")
        if self.parts:
            self.whole = pd.concat(([self.whole] if self.whole is not None else [])
                                   + self.parts, ignore_index=True)
            self.parts = []
        if self.whole is None:
            return None
        rows = self.whole
        if variant != "marker" and self.last_plain is not None:
            fix = self.last_plain
            if variant == "marker-minute":
                fix = fix[fix.m != through]
            keys = pd.MultiIndex.from_frame(fix[["sym", "m"]])
            drop = pd.MultiIndex.from_frame(rows[["sym", "m"]]).isin(keys)
            if int(drop.sum()) != len(fix):
                raise ModelRefused("replay_engine_rows", f"plain rows without a marker row "
                                   f"through {through}")
            rows = pd.concat([rows[~drop], fix], ignore_index=True)
        return rows.sort_values(["sym", "m"], kind="stable").reset_index(drop=True)

    def close(self) -> None:
        self.trades.close()
        self.quotes.close()
