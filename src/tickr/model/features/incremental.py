"""The table code's rows of one minute without rebuilding the whole market's day at every minute.

`LiveContext.table_rows` (`tickr.model.features.full`) hands the context's feature-only `day2`
every bar of the day so far, for every ticker of the market, at every minute: 27-40 s per minute
at 11,000 tickers (#826). `DayTable.rows` gives exactly the same rows (every column of that
table, bit for bit) in the same live date context, from two parts:

1. THE MARKET PART, carried minute by minute over every ticker (`Market`). `day2` reads the
   whole market for seven columns and for the row rule, and for nothing else:
   - the per-minute lists (top TOPN by cumulative volume, by change, by fall; in post-market by
     change from the regular close; the depth-200 copies): `inl`, `nin` (the count of in-list
     minutes since 04:00), `in2`;
   - the places among all tickers of the minute: `r_chg`, `r_rv`, `r_rng` and `brup`;
   - the row rule: a ticker has a row at m when it was in a list with a bar and a price of at
     least MINP at some minute of the session up to m, and has a bar at m.
   Each of these at minute m is a function of per-ticker running values at m (last close,
   cumulative volume and dollar volume, highest high and lowest low so far, the last regular
   close, whether a post-market bar was seen) and of the per-ticker carry of the earlier minutes
   (the in-list count and the session flags). `Market.step` repeats `day2`'s own arithmetic on
   the column of minute m only: the same numpy and pandas operations, in the same order, on
   the same float types, so every value is the same bits (a running sum adds the same numbers
   in the same order as `np.cumsum`; a rank of one column is that column of the frame's rank).
   That is about 60 lines that copy the pinned text; `tests/model/test_incremental.py` compares
   them with `day2` on randomized markets at every tested minute.
2. THE TICKER PART, the context's `day2` itself, run on the whole day so far of the cohort only (the
   tickers with a row at m), in chunks of at most TOPN tickers with SPY and QQQ (the market
   columns read them). Every other column of a row is a function of that ticker's own bars, its
   previous close, its history days and the two ETFs: `day2` computes them per ticker (rolling
   windows, EWMs and cumulative sums run along the minutes of one ticker), so a smaller market
   gives the same values. In a chunk of at most TOPN tickers every ticker with a defined change
   or any volume is in the lists, so each cohort ticker of the chunk has its row at m; the seven
   market columns of those rows are then replaced with the market part's. A cohort ticker
   without its row in the chunk's table is refused (`incremental_row_missing`), never filled.

Why not the pinned code on a bounded input for part 1 too: every EWM, cumulative and since-04:00
value of `day2` is computed over the full 960-minute day, and the lists rank all tickers at every
minute. A carry row cannot stand for a running state exactly (a synthetic bar that restores the
cumulative dollar volume changes the last close, and EWMs of different spans need different
states), so the per-ticker columns keep the whole day of their ticker and the market columns are
carried by `Market`.

Late or corrected bars: `rows` takes the day's bars so far as `LiveContext.table_rows` does
(with their receipts: a bar received after the minute's cutoff is left out, the minute's bounds
refuse first) and consumes the minutes after the last one. When a bar of a consumed minute was
added, removed or changed since (the row count and a sum of row hashes of those minutes
differ), the market part consumes the whole day again from 04:00 (`rebuilds`), so the rows are
always those of the bars handed in.
Minute index m counts from 04:00 New York, as in the training.
"""

from __future__ import annotations

from typing import Any

from tickr.model import features as base
from tickr.model.errors import ModelRefused
from tickr.model.features import full

# The seven columns of a row that the whole market decides (part 1 of the docstring).
MARKET_COLUMNS = ("inl", "nin", "in2", "r_chg", "r_rv", "r_rng", "brup")
N2 = 200  # the depth of the in2 lists, a literal inside day2 (pinned text, not a bot setting)
# day2's regular session slice C[:, 330:720] (minute indices of the pinned text): the regular close
# and, from its end, the post-market lists by change from that close.
REGULAR = (330, 720)
HASHED = ("ticker", "volume", "open", "close", "high", "low", "window_start")


def _rank(col: Any) -> Any:
    """Places 0..1 of one minute's values among the tickers, as day2 ranks its matrices
    (`pd.DataFrame(x).rank(axis=0, pct=True)`, column by column)."""
    import numpy as np
    import pandas as pd

    return pd.DataFrame(col[:, None]).rank(axis=0, pct=True).values[:, 0].astype(np.float32)


class Market:
    """The whole market's carried values, one minute at a time (part 1). `w`: the table code
    module of a live context (its settings: TOPN, MINP, SESS)."""

    def __init__(self, w: Any, state: base.DayState, types: set[str] | None) -> None:
        import numpy as np

        self.w = w
        self.state, self.types = state, types
        self.tick = np.array([], dtype=object)
        self.where: dict[str, int] = {}  # ticker -> index in tick
        self.minute = -1
        n = 0
        self.pc = np.zeros(n)
        self.p = np.zeros(n)  # last close so far (day2's P at m)
        self.cv = np.zeros(n)  # cumulative volume (CV)
        self.dv = np.zeros(n)  # cumulative dollar volume in float64 (DVa before its cast)
        self.hi = np.zeros(n)  # highest high so far (cummax of Hf)
        self.lo = np.zeros(n)  # lowest low so far
        self.rc = np.zeros(n)  # last regular-session close
        self.post = np.zeros(n, np.bool_)  # a post-market bar was seen
        self.nin = np.zeros(n)
        self.flag = np.zeros((len(self.w.SESS), n), np.bool_)  # in a list with a bar this session
        # per history day (oldest first): each ticker's row in that day's summary, -1 = none
        self.hj = np.zeros((len(state.hist), n), np.int64)
        self.nh = np.zeros(n)
        self.out: dict[str, Any] = {}

    def _grow(self, names: Any) -> None:
        import numpy as np

        new = [t for t in names if t not in self.where]
        if not new:
            return
        tick = np.array(sorted([*self.tick, *new]), dtype=object)  # np.unique's order
        self.where = {t: k for k, t in enumerate(tick)}
        pos = np.array([self.where[t] for t in self.tick], dtype=np.int64)
        nan, n = np.nan, len(tick)

        def moved(old: Any, fill: Any) -> Any:
            a = np.full(old.shape[:-1] + (n,), fill, dtype=old.dtype)
            a[..., pos] = old
            return a

        self.p, self.hi, self.lo, self.rc = (moved(a, nan) for a in
                                             (self.p, self.hi, self.lo, self.rc))
        self.cv, self.dv, self.nin = (moved(a, 0.0) for a in (self.cv, self.dv, self.nin))
        self.post, self.flag = moved(self.post, False), moved(self.flag, False)
        self.pc, self.hj = moved(self.pc, nan), moved(self.hj, -1)
        self.tick = tick
        new = np.array(sorted(new), dtype=object)
        at = np.array([self.where[t] for t in new], dtype=np.int64)
        self.pc[at] = [self.state.prev.get(t, np.nan) for t in new]
        for k, h in enumerate(self.state.hist):  # day2's alignment of a history day
            j = np.searchsorted(h["tick"], new)
            j[j >= len(h["tick"])] = 0
            self.hj[k, at] = np.where(h["tick"][j] == new, j, -1)
        self.nh = (self.hj >= 0).sum(axis=0).astype(np.float64)

    def step(self, minute: int, bars: Any, want: bool) -> None:
        """Consume the bars of `minute` (a frame from `bars_frame`, every row m == minute) and
        compute the lists of the minute; with `want` also the places among all tickers."""
        import numpy as np

        w = self.w
        if self.types is not None:
            bars = bars[bars.ticker.isin(self.types)]
        names = bars.ticker.values
        self._grow(set(names))
        n = len(self.tick)
        i = np.array([self.where[t] for t in names], dtype=np.int64)
        has = np.zeros(n, np.bool_)
        has[i] = True
        c = np.full(n, np.nan)
        c[i] = bars.close.values
        v0 = np.zeros(n)
        v0[i] = np.nan_to_num(bars.volume.values.astype(np.float64))
        hf = np.full(n, np.nan)
        hf[i] = bars.high.values
        lf = np.full(n, np.nan)
        lf[i] = bars.low.values
        self.p = np.where(has, c, self.p)
        self.cv = self.cv + v0
        self.dv = self.dv + np.nan_to_num(c) * v0
        self.hi = np.where(has, np.fmax(self.hi, hf), self.hi)
        self.lo = np.where(has, np.fmin(self.lo, lf), self.lo)
        reg = REGULAR
        if reg[0] <= minute < reg[1]:
            self.rc = np.where(has, c, self.rc)
        if minute >= reg[1]:
            self.post |= has
        p, pc = self.p, self.pc
        with np.errstate(divide="ignore", invalid="ignore"):
            chg = np.where(pc > 0, p / pc - 1, np.nan)
        inl = np.zeros(n, np.bool_)
        in2 = np.zeros(n, np.bool_)
        v = self.cv
        iv = np.where(v > 0)[0]
        iv = iv[np.argsort(-v[iv], kind="stable")]
        inl[iv[:w.TOPN]] = True
        in2[iv[:N2]] = True
        self._lists(chg, inl, in2)
        if minute >= reg[1]:
            with np.errstate(divide="ignore", invalid="ignore"):
                chg2 = np.where((self.rc > 0) & self.post, p / self.rc - 1, np.nan)
            self._lists(chg2, inl, in2)
        self.nin = self.nin + inl
        ok = inl & has & (np.nan_to_num(c) >= w.MINP)
        for s, (a, b, _fl) in enumerate(w.SESS):
            if a <= minute <= b:
                self.flag[s] |= ok
        self.minute = minute
        self.out = {"has": has, "inl": inl, "in2": in2}
        if want:
            self._places(chg)

    def _lists(self, chg: Any, inl: Any, in2: Any) -> None:
        import numpy as np

        topn = self.w.TOPN
        ids = np.where(~np.isnan(chg))[0]
        if len(ids) == 0:
            return
        o = np.argsort(chg[ids], kind="stable")
        og_ = np.argsort(-chg[ids], kind="stable")
        inl[ids[og_[:topn]]] = True
        inl[ids[o[:topn]]] = True
        in2[ids[og_[:N2]]] = True
        in2[ids[o[:N2]]] = True

    def _places(self, chg: Any) -> None:
        import numpy as np

        m, n = self.minute, len(self.tick)
        a = np.zeros(n, np.float32)
        for h, hj in zip(self.state.hist, self.hj, strict=True):
            ok = hj >= 0
            a[ok] += h["cdv"][hj[ok], m]
        dva = self.dv.astype(np.float32)
        with np.errstate(divide="ignore", invalid="ignore"):
            a = np.where(self.nh >= self.w.MINH, a / self.nh, np.nan)
            rv = np.where(a > 0, dva / a, np.nan)
            rng = self.hi / self.lo - 1
            brup = np.nansum(chg > 0, axis=0) / np.maximum(1, (~np.isnan(chg)).sum(axis=0))
        self.out.update(r_chg=_rank(chg), r_rv=_rank(np.where(dva > 0, rv, np.nan)),
                        r_rng=_rank(rng), brup=brup)

    def cohort(self) -> Any:
        """Indices of the tickers with a row at the last minute (day2's R and its row filter)."""
        import numpy as np

        m = self.minute
        for s, (a, _b, fl) in enumerate(self.w.SESS):
            if a <= m < fl:
                if m > fl - 2:
                    break
                return np.where(self.flag[s] & self.out["has"])[0]
        return np.zeros(0, np.int64)


def chunk_state(state: base.DayState, names: Any) -> base.DayState:
    """The part of `state` that day2 reads for the tickers `names` (sorted): their previous
    closes, the ETFs' and each history day's rows of those tickers. day2 aligns a history day
    to its tickers by search, so the rows it finds hold the same values."""
    import collections

    import numpy as np

    sub = base.DayState(prev={t: state.prev[t] for t in names if t in state.prev},
                        mprev=dict(state.mprev), hist=collections.deque())
    for h in state.hist:
        j = np.searchsorted(h["tick"], names)
        j[j >= len(h["tick"])] = 0
        j = j[h["tick"][j] == names]
        sub.hist.append({k: v[j] for k, v in h.items()})
    return sub


def chunk_rows(ctx: full.LiveContext, minute: int, frame: Any, state: base.DayState,
               types: set[str] | None, want: Any) -> Any:
    """The context's day2 on one chunk's bars: the rows of `minute` of the tickers `want`, in
    their order, or None when one of them has no row."""
    t = ctx.table(frame, state, types)
    if t is None:
        return None
    t = t.loc[t.m == minute]
    t = t[t.sym.isin(set(want))]
    return t if len(t) == len(want) else None


def _job(market_day: Any, policy: str, *args: Any) -> Any:
    """chunk_rows in a worker, in the worker's own live context of the same day and policy
    (`full.live_context`): a refusal comes back as (cause, detail), raised again here."""
    try:
        return chunk_rows(full.live_context(market_day, policy), *args)
    except ModelRefused as exc:
        return (exc.cause, exc.detail)


class DayTable:
    """The rows of each closed minute of one day; see the module docstring. `context`: the live
    date context of the day (`full.live_context`). `state` / `types` as `features.minute_rows`
    takes them. Minutes are closed in increasing order. `workers`: processes that run the
    chunks of part 2 side by side (1 = in this process), each in its own context of the day."""

    def __init__(self, context: full.LiveContext, state: base.DayState,
                 types: set[str] | None, workers: int = 1) -> None:
        self.context = context
        self.day, self.state, self.types = context.day, state, types
        self.workers = workers
        self.pool: Any = None
        self.last = -1  # the last minute asked for
        self.rebuilds = 0  # how often a changed consumed bar made the market part start again
        self.seconds: dict[str, float] = {}  # wall time of the last close: market / table part
        self._reset()

    def _reset(self) -> None:
        """Forget every consumed minute: the next close consumes the day from 04:00."""
        self.market = Market(self.context.code.w, self.state, self.types)
        self.frames: list[Any] = []  # the consumed bars (bars_frame rows), minute by minute
        self.codes: list[Any] = []  # a number per ticker for each consumed row (fast selection)
        self.code: dict[str, int] = {}
        self.count = 0  # consumed rows
        self.digest = 0  # sum of their row hashes (mod 2**64)
        self.closed = -1  # the last consumed minute

    def _hash(self, frame: Any) -> int:
        import numpy as np
        import pandas as pd

        if not len(frame):
            return 0
        h = pd.util.hash_pandas_object(frame[list(HASHED)], index=False).to_numpy(np.uint64)
        return int(h.sum(dtype=np.uint64))

    def rows(self, rows: Any, minute: int, cutoff: Any, chunk: int | None = None) -> Any:
        """The table code's rows of `minute` (as `LiveContext.table_rows`) from the day's bars
        received by `cutoff` (the minute's bounds first, see `LiveContext.received_bars`); bars
        after `minute` refuse. `chunk`: tickers per pinned call (default and most TOPN).
        A bar of a consumed minute that was added, removed or changed since (a late or
        corrected bar) makes the market part consume the day again from 04:00 (`rebuilds`):
        the rows stay those of the bars handed in, as with the whole-day call."""
        import time

        import numpy as np
        import pandas as pd

        w = self.market.w
        t0 = time.perf_counter()
        if not self.last < minute < w.NM:
            raise ModelRefused("minute_order", f"{minute} after {self.last}")
        rows = self.context.received_bars(rows, minute, cutoff)
        start = pd.Timestamp(self.day, tz=base.NEW_YORK).value
        if (rows.window_start.to_numpy() >= start + (base.DAY_START_MINUTE + minute + 1)
                * base.NS_PER_MINUTE).any():
            raise ModelRefused("bar_after_minute", f"minute {minute}")
        old = rows.window_start.to_numpy() < (start + (base.DAY_START_MINUTE + self.closed + 1)
                                              * base.NS_PER_MINUTE)
        if self.closed >= 0 and (int(old.sum()) != self.count
                                 or self._hash(rows[old]) != self.digest):
            self._reset()
            self.rebuilds += 1
            old[:] = False
        new = rows[~old]
        # refuses bad bars before anything is consumed
        frame = base.bars_frame(new, self.day, w.NM)
        if (frame.m > minute).any():
            raise ModelRefused("bar_after_minute", f"minute {minute}")
        try:
            by = dict(tuple(frame.groupby("m", sort=True))) if len(frame) else {}
            empty = frame.iloc[:0]
            for m in range(self.closed + 1, minute + 1):
                self.market.step(m, by.get(m, empty), want=m == minute)
        except BaseException:  # a half-consumed minute: the next close starts from 04:00
            self._reset()
            raise
        self.frames.append(frame)
        self.codes.append(np.array([self.code.setdefault(t, len(self.code))
                                    for t in frame.ticker.values], dtype=np.int64))
        self.count += len(new)
        self.digest = (self.digest + self._hash(new)) % (1 << 64)
        self.closed = self.last = minute
        t1 = time.perf_counter()
        table = self._table(minute, chunk or w.TOPN)
        self.seconds = {"market": t1 - t0, "table": time.perf_counter() - t1}
        return table

    def _table(self, minute: int, chunk: int) -> Any:
        import numpy as np
        import pandas as pd

        mk, w = self.market, self.market.w
        ci = mk.cohort()
        if not len(ci):
            return None
        names = mk.tick[ci]
        size = min(chunk, w.TOPN) - len(w.ETF)
        if len(self.frames) > 1:
            self.frames = [pd.concat(self.frames, ignore_index=True)]
            self.codes = [np.concatenate(self.codes)]
        day, codes = self.frames[0], self.codes[0]
        etf = [self.code[e] for e in w.ETF if e in self.code]
        jobs = []
        for k in range(0, len(names), size):
            want = names[k:k + size]
            sub = day[np.isin(codes, [self.code[t] for t in want] + etf)]
            every = np.array(sorted(set(want) | set(w.ETF)), dtype=object)
            jobs.append((minute, sub, chunk_state(self.state, every), self.types, want))
        if self.workers > 1 and len(jobs) > 1:
            if self.pool is None:
                import multiprocessing
                from concurrent.futures import ProcessPoolExecutor

                self.pool = ProcessPoolExecutor(
                    self.workers, mp_context=multiprocessing.get_context("spawn"))
            ctx = self.context
            parts = list(self.pool.map(_job, [ctx.market_day] * len(jobs),
                                       [ctx.session_policy] * len(jobs),
                                       *zip(*jobs, strict=True)))
            for p in parts:
                if isinstance(p, tuple):
                    raise ModelRefused(*p)
        else:
            parts = [chunk_rows(self.context, *job) for job in jobs]
        if any(p is None for p in parts):
            raise ModelRefused("incremental_row_missing", f"minute {minute}")
        t = pd.concat(parts, ignore_index=True)
        if not np.array_equal(t.sym.to_numpy(), names):
            raise ModelRefused("incremental_row_missing", f"minute {minute}: order")
        if any(c not in t.columns for c in MARKET_COLUMNS):
            raise ModelRefused("table_code_settings", "a market column is not in the table")
        out = mk.out
        t["inl"] = out["inl"][ci].astype(np.float64).astype(np.float32)
        t["nin"] = mk.nin[ci].astype(np.float64).astype(np.float32)
        t["in2"] = out["in2"][ci].astype(np.int8)
        for k in ("r_chg", "r_rv", "r_rng"):
            t[k] = out[k][ci]
        t["brup"] = np.full(len(ci), out["brup"], dtype=np.float64).astype(np.float32)
        return t

    def close(self) -> None:
        """Stop the worker processes (a later minute starts them again)."""
        if self.pool is not None:
            self.pool.shutdown()
            self.pool = None

