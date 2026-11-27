"""Live layer-1 features: at the end of each minute m, the 191-column rows of the cohort.

Prep only: the model stays off and nothing here reaches the order path. No bot loop calls this
module; a test harness (tests/model/test_live.py) or a later runner drives `LiveFeatures.close`.

It composes the pieces that already exist, each the training's own code or its file format,
in the live date context of the day (`full.LiveContext`, built by the caller from the bot's
calendar and the bundle's session policy: the date boundary, the feature-only table code, the
day's close). Each close takes the wall clock once (`clock`): that instant is the RECEIPT CUTOFF
of the minute (`full`, EACH MINUTE HAS TWO BOUNDS), the receipt of every bar and tick row the
close hands in, and the cutoff of the close's tick snapshot (`TickStore.snapshot`): the close
uses only what had arrived by that instant (a minute closed before its end refuses
`cutoff_before_mark`).

- bars: today's full-market minute bars from `MinuteStream` into `DayBars`, cut at m (bars of a
  later minute are left out, the prefix rule), and the 20-session `DayState`; the table code's
  rows of m (`incremental.DayTable`: the rows of `LiveContext.table_rows`, bit for bit, without
  rebuilding the whole market's day at every minute) are the COHORT: the training's row rule
  decides which symbols get a row. Webull's lists (what the bot may trade) stay apart: a symbol
  the caller wants that has no row is refused `not_in_cohort`.
- ticks: `TickStream` / `TickStore` (every version of each trade and quote as flat-file rows)
  and `TickBackfill`. Each close reads the store's decision snapshot as of its cutoff
  (`TickSnapshot`, codex-109 point 4): per key the version chosen by that instant, immutable, so
  a later arrival or REST correction never changes what an earlier close saw. A symbol is
  subscribed the first minute it has a row (it is TRACKED from then on, for the day). The
  snapshot says whether its ticks are proven complete from New York midnight to the cutoff
  (`TickSnapshot.refusal`: subscribed, a complete REST window from midnight reaching past the
  first stream row of each kind after the subscription, every stream gap filled, no unresolved
  conflict). While it says no initial state (or a closed gap is unfilled), the close backfills
  the symbol from midnight to now (`TickBackfill.initial`: sent once per such close, never
  retried); that window is received after the cutoff, so the next close's snapshot reads it.
  The first close whose snapshot proves the symbol complete ENTERS it: its whole day so far is
  converted then, and it stays in the conversion for the day (its later rows need the earlier
  minutes: windows of `wm2tk`, the converter's carried events). Each later close converts the
  keys its snapshot holds that the conversion has not taken yet (or whose chosen version
  changed); one of a minute already converted came late (`tick_late`).
- conversion: the frozen converter v11 once per close, on the incremental input of #820
  (`tail_carry` of every earlier event, plus the new events), together with the whole day so far
  of the symbols entering at this close; v11 partitions every column by symbol, so both give the
  rows of the whole-day file. `[model] live_marker_quote` adds the marker quote of #820 (off,
  the model is off; Codex codex-108 point 1 uses it in the parity experiment): one copy of the
  latest real quote of every symbol that has a real quote among the events this close converts,
  stamped at the end of the closed minute. It exists only inside that one conversion (local to
  v11's weight `d`): it never enters the store (raw capture), the carry handed to the next
  close, any count (qn and the k_qn* columns stay those of the real quotes) or a delivered
  minute (its own rows, of the next minute, are dropped). On a one-minute close the symbols are
  those with a real quote in that minute. On a catch-up close (several minutes, or an entering
  symbol's whole day) a symbol whose latest quote lies in an earlier minute k of the span gets
  the same weight as a marker at the start of k + 1 (v11 caps a weight at its own minute end)
  without the marker entering minutes k + 1 .. m (the symbol has no quote there; trades join
  quotes strictly before them). The rows are appended to the day's tick table as one parquet
  part per close (`<tick_dir>/<day>/part-MMMM.parquet`, v11's schema: sym VARCHAR, m INTEGER,
  tn / qn BIGINT, 59 DOUBLE, then `full.RECEIPT_COLUMN`, the close's instant); `tick_file` is
  the glob of the parts, which the training's `l1feat.cut_inputs` reads with duckdb as it reads
  one day file.
- reference, Benzinga and fl: folders in the training's formats prepared before the session,
  through `TrainingInputs` (fetching them is not in scope), each record with its receipt as
  `full` INPUTS requires (a record received after the cutoff is left out; anything the
  training's cut would still remove refuses `future_input`).
- features: the training's `cut_inputs` and `assemble` (`LiveContext.assemble_rows`) on the
  rows of m.
  They read the whole tick table so far, the reference and Benzinga folders and the facts file
  at every minute (pinned code, not edited here): that cost is measured, not avoided.

REFUSALS (hard rule 7): every row of the cohort is either served with all 191 values or refused
with one cause, never filled. The tick causes are the snapshot's (`TickSnapshot.refusal`), named
as live.py names them: `tick_no_initial_state` and `tick_backfill_incomplete` are
`backfill_missing` (the snapshot's cause as the detail), `tick_gap` stays `tick_gap` (the
gap's cause as the detail), and `tick_not_subscribed`, `tick_coverage_unverified` and
`tick_conflict` keep #793's names. Before a symbol enters, its cause is the snapshot's at that
close (it can still enter later). Sticky causes hold for the rest of the day, because the lost
events stay in the converter's carry and in the 30-minute windows:
- `backfill_missing`: a REST backfill of the symbol failed or came back incomplete (sent once,
  never retried);
- any snapshot refusal of an entered symbol (`tick_gap`: a stream gap recorded by TickStream
  for the symbol or the whole market, open or unfilled at the cutoff; the others above);
- `tick_late`: a version of an event in a minute already converted arrived after that close;
- `bar_gap`: the minute-bar stream had a gap today (DayBars cannot say which minutes).
Per minute:
- `input_missing`: a training-format input folder or file is not there;
- `feature_code_refused` (or the training code's own cause): the pinned builder refused;
- `rank_peer_refused`: another row of the minute is refused for a tick cause; the kr_* columns
  rank each row among the minute's rows, so no row of that minute equals the training's.
A refusal of the whole minute before any tick is processed (bars that do not pass the
training's checks, a minute out of order) is raised as `ModelRefused` and changes nothing; one
while converting (the converter's own refusal) loses the day's tick state, and every later close
refuses `live_state_lost`.

WAITING: how long after the end of minute m its events are complete is not known; it awaits the
arrival measurement (#796). `[model] live_wait_after_minute_seconds` is empty until then and a
clock-driven close (`due`) refuses `feature_wait_unmeasured` instead of guessing; `close` itself
takes the minute the caller says is over. A restart mid-day refuses (`live_ticks_exist`): the
converter's carry is not persisted.
"""

from __future__ import annotations

import datetime
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tickr.config import Config, live_wait_seconds
from tickr.model import features
from tickr.model import ticks as feed
from tickr.model.errors import ModelRefused
from tickr.model.features import full, incremental, ticks

INPUT_NAMES = ("reference", "benzinga", "fl_facts")
# the snapshot's causes (TickSnapshot.refusal) that live.py names `backfill_missing`
BACKFILL_CAUSES = ("tick_no_initial_state", "tick_backfill_incomplete")
# the snapshot's causes a backfill from midnight can cure for a symbol not entered yet
BACKFILL_CURES = (*BACKFILL_CAUSES, "tick_gap")
TICK_CAUSES = ("backfill_missing", "tick_gap", "tick_late", "tick_not_subscribed",
               "tick_coverage_unverified", "tick_conflict")
PART_GLOB = "part-*.parquet"
QUOTE_SIDE_COLUMNS = ("bid_price", "bid_size", "ask_price", "ask_size")
NS_PER_SECOND = 10**9
NS_PER_MICROSECOND = 10**3
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


class TrainingInputs(Protocol):
    """The day's reference folder, Benzinga folder and fl facts file in the training's formats
    (`tickr.model.features.full`, INPUTS), prepared before the session. None = not there."""

    def path(self, name: str, day: str) -> Path | None: ...


@dataclass(frozen=True)
class FolderInputs:
    """TrainingInputs from one folder per day: <root>/<day>/reference/, <root>/<day>/benzinga/
    and <root>/<day>/fl-facts.csv."""

    root: Path
    names: dict[str, str] = field(default_factory=lambda: {
        "reference": "reference", "benzinga": "benzinga", "fl_facts": "fl-facts.csv"})

    def path(self, name: str, day: str) -> Path | None:
        p = self.root / day / self.names[name]
        return p if p.exists() else None


@dataclass(frozen=True)
class Refusal:
    cause: str
    detail: str = ""


@dataclass
class MinuteResult:
    """One closed minute: the served rows (`sym` and the 191 features, training order and types),
    the refused symbols with one cause each, and the wall time of each step (seconds)."""

    minute: int
    rows: Any
    refused: dict[str, Refusal]
    seconds: dict[str, float]


@dataclass
class _Symbol:
    since: int  # wall-clock ns the subscription was asked (TickStream.add)
    entered: int | None = None  # the table minute whose close entered it in the conversion


class LiveFeatures:
    """See the module docstring. `context`: the live date context of the day
    (`full.live_context`). `state` / `types`: the DayState of the 20 earlier sessions and the
    training's stock types (None = no filter), as `features.minute_rows` takes them. `clock`
    gives wall-clock ns since the epoch (comparable with SIP stamps)."""

    def __init__(self, cfg: Config, context: full.LiveContext, state: features.DayState,
                 types: set[str] | None, bars: Any, store: feed.TickStore,
                 stream: feed.TickStream, backfill: feed.TickBackfill, inputs: TrainingInputs,
                 tick_dir: Path, clock: Callable[[], int] = time.time_ns) -> None:
        day = context.day
        self.context = context
        self.day, self.state, self.types = day, state, types
        self.bars, self.store, self.stream, self.backfill = bars, store, stream, backfill
        self.inputs, self.clock = inputs, clock
        self.table = incremental.DayTable(context, state, types, cfg.model.live_table_workers)
        self.marker = cfg.model.live_marker_quote
        self.groups = cfg.model.tick_groups
        self.wait = live_wait_seconds(cfg.model)
        self.parts = Path(tick_dir) / day
        if self.parts.exists() and any(self.parts.glob(PART_GLOB)):
            raise ModelRefused("live_ticks_exist", str(self.parts))
        self.parts.mkdir(parents=True, exist_ok=True)
        if store.day != day:
            raise ModelRefused("live_day_mismatch", f"store {store.day}, day {day}")
        self.closed = -1  # last closed table minute
        self.symbols: dict[str, _Symbol] = {}  # tracked: subscribed for the day
        self.broken: dict[str, Refusal] = {}
        # kind -> (ticker, sequence_number) -> flat-file columns of the version taken
        self.taken: dict[str, dict[tuple[str, int], tuple[Any, ...]]] = {
            kind: {} for kind in feed.KINDS}
        self.snapshot: feed.TickSnapshot | None = None  # the last close's
        self.pending: dict[str, dict[str, dict[int, dict[str, Any]]]] = {
            kind: {} for kind in feed.KINDS}
        self.carry: tuple[Any, Any] | None = None
        self.lost: str | None = None  # why the tick state is gone (every later close refuses)

    @property
    def tick_file(self) -> Path:
        """The day's tick table so far: the glob of its parts."""
        return self.parts / PART_GLOB

    def _end(self, minute: int) -> int:
        """SIP ns of the end of table minute `minute` (-1: 04:00)."""
        return ticks._bounds(self.day, minute + features.DAY_START_MINUTE)[1]

    def due(self, minute: int, now_ns: int) -> bool:
        """Whether a clock-driven runner may close `minute` at `now_ns`: its end plus the measured
        wait has passed. Refuses while the wait is not measured."""
        if self.wait is None:
            raise ModelRefused("feature_wait_unmeasured",
                               "[model] live_wait_after_minute_seconds is empty")
        return now_ns >= self._end(minute) + int(self.wait * NS_PER_SECOND)

    # --- one close ------------------------------------------------------------------------

    def close(self, minute: int, wanted: Iterable[str] = ()) -> MinuteResult:
        """Close table minute `minute` (from 04:00 New York): every minute after the last close
        up to it is converted; the rows of `minute` are computed. `wanted`: symbols the caller
        asks about (e.g. Webull's lists), refused `not_in_cohort` without a row."""
        import pandas as pd

        if self.lost is not None:
            raise ModelRefused("live_state_lost", self.lost)
        ctx = self.context
        if not self.closed < minute < ctx.code.w.NM:
            raise ModelRefused("minute_order", f"{minute} after {self.closed}")
        received = self.clock() // NS_PER_MICROSECOND * NS_PER_MICROSECOND
        cutoff = EPOCH + datetime.timedelta(microseconds=received // NS_PER_MICROSECOND)
        seconds: dict[str, float] = {}
        t0 = time.perf_counter()
        bars = self.bars.frame()
        bars = bars[bars.window_start < self._end(minute)]
        table = (None if bars.empty  # no bar yet today: no row (an empty frame has no types)
                 else self.table.rows(bars.assign(**{full.RECEIPT_COLUMN: received}), minute,
                                      cutoff))
        cohort = [] if table is None else list(table.sym)
        seconds["bars"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        snap = self.store.snapshot(received)  # what had arrived by the cutoff, frozen
        self.snapshot = snap
        seconds["snapshot"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        for s in cohort:
            if s not in self.symbols:
                self.stream.add(s)
                self.symbols[s] = _Symbol(since=self.clock())
        entering = [s for s, info in self.symbols.items() if info.entered is None
                    and s not in self.broken and snap.refusal(s) is None]
        self._backfill(snap)
        seconds["backfill"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        try:  # from here the snapshot's rows are consumed: a refusal loses the day's tick state
            self._check_entered(snap)
            self._read_snapshot(snap, entering)
            rows = self._convert(minute, set(entering))
            seconds["convert"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            self._append(minute, rows.assign(**{full.RECEIPT_COLUMN: received}))
        except ModelRefused as exc:
            self.lost = f"{exc.cause} at minute {minute}"
            raise
        self.closed = minute
        for s in entering:
            if s not in self.broken:
                self.symbols[s].entered = minute
        seconds["append"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        refused = self._refusals(cohort, snap)
        paths = {n: self.inputs.path(n, self.day) for n in INPUT_NAMES}
        absent = [n for n, p in paths.items() if p is None]
        served = [s for s in cohort if s not in refused]
        if absent:
            refused.update({s: Refusal("input_missing", ", ".join(absent)) for s in served})
            served = []
        out = pd.DataFrame(columns=["sym", *ctx.names])
        if served:
            given = full.FullInputs(self.tick_file, paths["reference"], paths["benzinga"],
                                    paths["fl_facts"])
            try:
                out = ctx.assemble_rows(table, minute, self.state, given, cutoff)
            except ModelRefused as exc:
                refused.update({s: Refusal(exc.cause, exc.detail) for s in served})
                served = []
        out = out[out.sym.isin(served)].reset_index(drop=True)
        seconds["features"] = time.perf_counter() - t0
        for s in wanted:
            if s not in cohort:
                refused[s] = Refusal("not_in_cohort")
        return MinuteResult(minute, out, refused, seconds)

    def _break(self, symbol: str, cause: str, detail: str = "") -> None:
        if symbol not in self.broken:
            self.broken[symbol] = Refusal(cause, detail)
            for kind in feed.KINDS:
                self.pending[kind].pop(symbol, None)

    def _backfill(self, snap: feed.TickSnapshot) -> None:
        """REST from New York midnight to now for every tracked symbol not entered yet whose
        snapshot lacks what a backfill can give (no initial state, or a closed gap unfilled).
        A failed or incomplete backfill is sent once, never retried: the symbol is refused for
        the day."""
        for s, info in self.symbols.items():
            if (s in self.broken or info.entered is not None
                    or snap.refusal(s) not in BACKFILL_CURES
                    or any(g.up_ns is None
                           for g in snap.gaps.get(s, ()) + snap.gaps.get(feed.MARKET, ()))):
                continue
            try:
                windows = self.backfill.initial(s)
            except ModelRefused as exc:
                self._break(s, "backfill_missing", exc.cause)
                continue
            causes = [w.cause for w in windows.values() if w.cause is not None]
            if causes:
                self._break(s, "backfill_missing", causes[0])

    @staticmethod
    def _tick_refusal(snap: feed.TickSnapshot, symbol: str) -> Refusal | None:
        """The snapshot's refusal of `symbol` named as live.py names it (module text)."""
        cause = snap.refusal(symbol)
        if cause is None:
            return None
        if cause in BACKFILL_CAUSES:
            return Refusal("backfill_missing", cause)
        if cause == "tick_gap":
            gaps = snap.gaps.get(symbol, ()) + snap.gaps.get(feed.MARKET, ())
            return Refusal(cause, gaps[-1].cause if gaps else "")
        return Refusal(cause)

    def _check_entered(self, snap: feed.TickSnapshot) -> None:
        """Sticky refusals of the entered symbols the snapshot does not prove complete."""
        for s, info in self.symbols.items():
            if info.entered is not None and s not in self.broken:
                refusal = self._tick_refusal(snap, s)
                if refusal is not None:
                    self._break(s, refusal.cause, refusal.detail)

    def _read_snapshot(self, snap: feed.TickSnapshot, entering: list[str]) -> None:
        """The snapshot's chosen versions the conversion has not taken yet (a new key, or a
        version other than the one taken), of the entered and `entering` symbols; one of an
        entered symbol in a minute already converted refuses it `tick_late`."""
        done = self._end(self.closed)
        joining = set(entering)
        for kind in feed.KINDS:
            cols = feed.columns(kind)[:-2]
            seq, ts = cols.index("sequence_number"), cols.index("sip_timestamp")
            taken, held = self.taken[kind], self.pending[kind]
            for version in snap.rows[kind]:
                s = version[0]
                info = self.symbols.get(s)
                if (info is None or s in self.broken
                        or (info.entered is None and s not in joining)):
                    continue
                body = version[:len(cols)]
                key = (s, body[seq])
                if taken.get(key) == body:
                    continue
                if info.entered is not None and body[ts] < done:
                    self._break(s, "tick_late", kind)
                    continue
                taken[key] = body
                held.setdefault(s, {})[body[seq]] = dict(zip(cols, body, strict=True))

    def _take(self, kind: str, symbols: Iterable[str], end: int) -> Any:
        """The pending rows of `symbols` before `end` as a flat-file frame (removed from
        pending); later rows wait for their minute."""
        import pandas as pd

        rows = []
        for s in symbols:
            held = self.pending[kind].get(s, {})
            now = [q for q, r in held.items() if r["sip_timestamp"] < end]
            rows.extend(held.pop(q) for q in now)
        frame = pd.DataFrame([[r[c] for c in feed.columns(kind)[:-2]] for r in rows],
                             columns=list(feed.columns(kind)[:-2]), dtype=object)
        if kind == "quotes":
            # an empty side is absent on the stream and 0 in the flat file; v11 does NOT treat
            # them alike (qbid, qbsl, qofi, qbad read the raw NULL), so the converter gets the
            # file's encoding of "no bid / no ask". Only an absent value becomes 0.
            for c in QUOTE_SIDE_COLUMNS:
                frame[c] = frame[c].map(lambda v: 0 if v is None else v).astype(object)
        return frame

    def _convert(self, minute: int, joiners: set[str]) -> Any:
        """The converter's rows of every minute after the last close up to `minute` for the
        tracked symbols, and of the whole day so far for `joiners`."""
        import pandas as pd

        end = self._end(minute)
        alive = [s for s, info in self.symbols.items() if s not in self.broken
                 and (info.entered is not None or s in joiners)]
        trades = self._take("trades", alive, end)
        quotes = self._take("quotes", alive, end)
        old_t, old_q = self.carry if self.carry is not None else (trades.iloc[:0],
                                                                  quotes.iloc[:0])
        old_t = old_t[old_t.ticker.isin(alive)]
        old_q = old_q[old_q.ticker.isin(alive)]
        all_t = pd.concat([old_t, trades], ignore_index=True)
        all_q = pd.concat([old_q, quotes], ignore_index=True)
        self.carry = ticks.tail_carry(all_t, all_q)
        through = minute + features.DAY_START_MINUTE
        conv_q = all_q
        if self.marker and len(quotes):
            conv_q = pd.concat([all_q, ticks.marker_quotes(quotes, end)], ignore_index=True)
        with tempfile.TemporaryDirectory(prefix="tickr-live-") as tmp:
            tf, qf = os.path.join(tmp, "trades.csv"), os.path.join(tmp, "quotes.csv")
            ticks.write_flat_file(all_t, tf, ticks.trade_columns())
            ticks.write_flat_file(conv_q, qf, ticks.quote_columns())
            rows = ticks.tick_rows(tf, qf, self.day, through + 1 if self.marker else through,
                                   groups=self.groups)
        first = self.closed + 1 + features.DAY_START_MINUTE
        keep = (rows.m <= through) & ((rows.m >= first) | rows.sym.isin(joiners))
        return rows[keep].reset_index(drop=True)

    def _append(self, minute: int, rows: Any) -> None:
        """One part of the day's tick table (v11's schema), written once."""
        import duckdb

        path = self.parts / f"part-{minute:04d}.parquet"
        if path.exists():
            raise ModelRefused("live_ticks_exist", str(path))
        con = duckdb.connect()
        try:
            con.register("tk", rows)
            con.execute(f"COPY (SELECT * REPLACE (CAST(sym AS VARCHAR) AS sym) FROM tk) TO "
                        f"'{path}' (FORMAT parquet)")
        finally:
            con.close()

    def _refusals(self, cohort: list[str], snap: feed.TickSnapshot) -> dict[str, Refusal]:
        refused: dict[str, Refusal] = {}
        if self.bars.gaps:
            refused = {s: Refusal("bar_gap", str(len(self.bars.gaps))) for s in cohort}
        for s in cohort:
            if s in refused:
                continue
            if s in self.broken:
                refused[s] = self.broken[s]
            elif self.symbols[s].entered is None:  # not proven complete at this cutoff yet
                waiting = self._tick_refusal(snap, s)
                if waiting is not None:
                    refused[s] = waiting
        tick_bad = sorted(s for s, r in refused.items() if r.cause in TICK_CAUSES)
        if tick_bad:
            for s in cohort:
                if s not in refused:
                    refused[s] = Refusal("rank_peer_refused", tick_bad[0])
        return refused
