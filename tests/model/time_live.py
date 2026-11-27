"""Time one LiveFeatures minute end to end on a synthetic day (codex-108 point 2: "time the
full minute through feature delivery at peak").

    TICK_THREADS=1 python tests/model/time_live.py --minutes 09:35 15:30

A measurement, not a test (pytest does not collect it). Nothing leaves the machine; no
credential is read. What it builds, outside the timed span:
- full-market minute bars of the day and the 20 sessions before it (`--market` tickers, the
  density of tests/model/model_bars.py, vectorized), the DayState and today's `DayBars`;
- the cohort: the table code's rows of the timed minute m;
- for every cohort symbol, a synthetic tick day of `--trades` / `--quotes` events
  (tests/model/model_ticks.py: 90 % in the regular session, U-shaped, so the open is the peak),
  converted once by the frozen v11 through m - 1 in groups of `--chunk` symbols;
- the training-format reference, Benzinga and fl inputs for the cohort (model_inputs.py), as
  the capture holds them (every record with its receipt, model_inputs.capture);
- a LiveFeatures in the steady state of a running day: minutes through m - 1 closed (their tick
  rows as one part, the carry of every earlier event, every symbol entered), the TickStore
  holding the day before m as the stream put it (each symbol subscribed before its first event
  and covered by a complete REST window up to m - 1's end, so its snapshot proves it), and
  minute m's trades and quotes as the stream thread puts them (millisecond stamps).
Timed: `LiveFeatures.close(m)` = inputs ready -> the 191 columns of every cohort row delivered.
Stages: bars (DayBars frame + table rows), snapshot (the store's decision snapshot of the whole
day at the cutoff), ticks (snapshot read + carry + v11 + part append; v11
alone shown apart), features (cut_inputs reading the tick table and the input folders, then
assemble: reference, tick features, Benzinga, fl, the rest). The stage clocks wrap the pinned
functions for the run (values unchanged) and are removed afterwards.

v11's group count is the bot's `[model] tick_groups` (#806 pins it; `ticks.tick_rows` sets it
on every call), for the warm-up and the timed close alike.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

import model_inputs as inputs
import model_ticks as tick_maker
import numpy as np
import pandas as pd

from tickr import config
from tickr.model import features, live, massive
from tickr.model import ticks as feed
from tickr.model.features import full, ticks

ROOT = Path(__file__).resolve().parents[2]
DAY = "2024-03-06"  # an open-archive day with 20 sessions before it (as test_parity)
NEWS_MINUTES = [320, 338, 339, 340, 352, 360, 371, 384, 386, 400]
MS = 10**6


def _bars(day: str, tickers: list[str], seed: int, closes: dict[str, float]) -> pd.DataFrame:
    """One day of minute bars, the density of model_bars.day_bars (regular minutes busy
    0.2..0.9, other minutes a third of that; SPY and QQQ 0.95), vectorized."""
    rs = np.random.RandomState(seed)
    n, minutes = len(tickers), model_minutes()
    busy = rs.uniform(0.2, 0.9, n)
    busy[:2] = 0.95
    m = np.arange(minutes)
    regular = (m >= 330) & (m < 720)
    p = np.where(regular[None, :], busy[:, None], busy[:, None] / 3)
    on = rs.random_sample((n, minutes)) < p
    sd = np.where(np.arange(n) < 2, 0.004, 0.012)[:, None]
    step = np.exp(rs.normal(0, 1, (n, minutes)) * sd * on)
    p0 = np.array([closes.get(t, rs.uniform(0.5, 80)) for t in tickers])
    close = np.maximum(0.05, p0[:, None] * np.cumprod(step, axis=1))
    opn = np.concatenate([p0[:, None], close[:, :-1]], axis=1)
    hi = np.maximum(opn, close) * (1 + np.abs(rs.normal(0, 0.002, (n, minutes))))
    lo = np.minimum(opn, close) * (1 - np.abs(rs.normal(0, 0.002, (n, minutes))))
    vol = rs.lognormal(7, 1.2, (n, minutes)).astype(np.int64)
    tx = rs.randint(1, 50, (n, minutes))
    start = pd.Timestamp(day + " 04:00", tz="America/New_York").value
    k, j = np.nonzero(on)
    for i, t in enumerate(tickers):
        closes[t] = float(close[i, -1])
    return pd.DataFrame({"ticker": np.array(tickers, dtype=object)[k], "volume": vol[k, j],
                         "open": opn[k, j], "close": close[k, j], "high": hi[k, j],
                         "low": lo[k, j], "window_start": start + j * 60 * 10**9,
                         "transactions": tx[k, j]})


def context() -> full.LiveContext:
    """The live date context of the day (the training's sessions, model_inputs.market_day)."""
    return full.live_context(inputs.market_day(DAY))


def model_minutes() -> int:
    return int(context().code.w.NM)


def _messages(kind: str, frame: pd.DataFrame) -> list[dict]:
    """Stream messages (Massive T / Q) of flat-file rows, as tests/model/test_live.py sends."""
    out = []
    for r in frame.to_dict("records"):
        if kind == "trades":
            codes = [int(c) for c in r["conditions"].split(",")] if r["conditions"] else None
            m = {"ev": "T", "sym": r["ticker"], "x": int(r["exchange"]), "p": r["price"],
                 "s": int(r["size"]), "t": int(r["sip_timestamp"]) // MS,
                 "q": int(r["sequence_number"]), "c": codes,
                 "trfi": None if pd.isna(r["trf_id"]) else int(r["trf_id"]),
                 "pt": int(r["participant_timestamp"]) // MS}
        else:
            cond = [int(c) for c in r["conditions"].split(",")] if r["conditions"] else [None]
            ind = [int(c) for c in r["indicators"].split(",")] if r["indicators"] else None
            m = {"ev": "Q", "sym": r["ticker"], "ap": r["ask_price"], "as": r["ask_size"],
                 "t": int(r["sip_timestamp"]) // MS, "q": int(r["sequence_number"]),
                 "c": cond[0], "i": ind}
            if r["bid_price"] or r["bid_size"]:
                m.update(bp=r["bid_price"], bs=r["bid_size"])
        out.append(feed.stream_row(kind, {k: v for k, v in m.items() if v is not None}))
    return out


class _Stream:
    """The stream as LiveFeatures sees it in the steady state: every symbol subscribed."""

    def __init__(self) -> None:
        self.symbols: set[str] = set()

    def add(self, symbol: str) -> None:
        self.symbols.add(symbol)


class _NoBackfill:
    def backfill(self, *a, **k):
        raise AssertionError("a steady-state minute needs no backfill")


@contextlib.contextmanager
def _clocks(spent: dict[str, float]):
    """Wrap the pinned feature steps and v11 with wall clocks (values unchanged)."""
    code = context().code
    feat = code.feat
    targets = [(ticks, "tick_rows", "ticks: v11 conversion"),
               (feat, "cut_inputs", "features: cut_inputs (tick table + input folders)"),
               (feat, "ref_view", "features: reference"),
               (code.ref.Ref, "add", "features: reference"),
               (code.tk.Tick, "add", "features: tick features (wm2tk)"),
               (feat.l1tk2, "add", "features: tick features (l1tk2)"),
               (feat.l1bz.News, "add", "features: Benzinga"),
               (feat.l1bz.Earnings, "add", "features: Benzinga"),
               (feat.l1fl.Fl, "add", "features: fl")]
    saved = []
    for owner, name, label in targets:
        real = getattr(owner, name)
        saved.append((owner, name, real))

        def timed(*a, _real=real, _label=label, **k):
            t0 = time.perf_counter()
            try:
                return _real(*a, **k)
            finally:
                spent[_label] = spent.get(_label, 0.0) + time.perf_counter() - t0

        setattr(owner, name, timed)
    try:
        yield
    finally:
        for owner, name, real in saved:
            setattr(owner, name, real)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--minutes", nargs="+", default=["09:35", "15:30"],
                   help="HH:MM New York, each timed on its own steady-state LiveFeatures")
    p.add_argument("--market", type=int, default=3000, help="tickers with minute bars")
    p.add_argument("--trades", type=int, default=30000, help="trades per symbol-day (mean)")
    p.add_argument("--quotes", type=int, default=40000, help="quotes per symbol-day (mean)")
    p.add_argument("--chunk", type=int, default=50, help="symbols per v11 warm-up run")
    p.add_argument("--json", type=Path, help="also write the numbers here")
    args = p.parse_args(argv)
    threads = os.environ.get("TICK_THREADS")
    cfg = config.load(ROOT / "config" / "bot.toml")
    labels = inputs.research().labels
    cal = labels.calendar()
    days = cal[cal.index(DAY) - labels.HIST:cal.index(DAY) + 1]
    t0 = time.perf_counter()
    tickers = ["SPY", "QQQ"] + [f"M{i:05d}" for i in range(args.market - 2)]
    closes: dict[str, float] = {}
    frames = [(d, _bars(d, tickers, k, closes)) for k, d in enumerate(days)]
    state = features.history(frames[:-1], {}, DAY, None)
    today = frames[-1][1]
    print(f"bars and history: {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    results = []
    with tempfile.TemporaryDirectory(prefix="time-live-") as tmp:
        work = Path(tmp)
        for hm in args.minutes:
            m = labels.cut_minute(hm)
            results.append(_one(cfg, m, hm, today, state, args, work / hm.replace(":", "")))
    out = {"day": DAY, "market_tickers": args.market, "trades_per_symbol": args.trades,
           "quotes_per_symbol": args.quotes, "tick_threads": threads,
           "tick_groups": cfg.model.tick_groups, "minutes": results,
           "peak_rss_mb_whole_run": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024}
    text = json.dumps(out, indent=1)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", "utf-8")
    return 0


def _one(cfg, m: int, hm: str, today, state, args, work: Path) -> dict:
    work.mkdir(parents=True)
    t0 = time.perf_counter()
    bars = massive.DayBars(DAY)
    start = pd.Timestamp(DAY + " 04:00", tz="America/New_York").value
    end_ns = start + (m + 1) * 60 * 10**9
    for r in today[today.window_start < end_ns].to_dict("records"):
        bars.add({"sym": r["ticker"], "v": r["volume"], "o": r["open"], "c": r["close"],
                  "h": r["high"], "l": r["low"], "s": r["window_start"] // MS}, 0.0)
    ctx = context()
    received = bars.frame().assign(**{full.RECEIPT_COLUMN: end_ns})
    table = ctx.table_rows(received, m, state, None, ctx.mark(m))
    cohort = sorted(table.sym)
    source = work / "source"
    source.mkdir()
    given = full.FullInputs(
        inputs.tick_file(source / "tk.parquet", cohort[:1], range(m, m + 1)),
        inputs.reference(source / "reference", DAY, cohort, NEWS_MINUTES),
        inputs.benzinga(source / "benzinga", DAY, cohort, NEWS_MINUTES, "2023-01-02"),
        inputs.fl_facts(source / "fl-facts.csv", DAY, cohort))
    (work / "prepared").mkdir()
    inputs.capture(given, DAY, work / "prepared" / DAY, late=inputs.published_receipts(DAY))
    store = feed.TickStore(DAY)
    store.subscribe(cohort, start - 1)
    stream = _Stream()
    lf = live.LiveFeatures(cfg, ctx, state, None, bars, store, stream, _NoBackfill(),
                           live.FolderInputs(work / "prepared", inputs.CAPTURED), work / "ticks",
                           clock=lambda: end_ns)
    lo, hi = lf._end(m - 1), lf._end(m)
    through = m - 1 + features.DAY_START_MINUTE
    parts, carry_t, carry_q = [], [], []
    events = {"trades": 0, "quotes": 0}
    for i in range(0, len(cohort), args.chunk):
        names = cohort[i:i + args.chunk]
        tr, qu = tick_maker.day_events(DAY, len(names) + 2, args.trades, args.quotes,
                                       seed=11 + i)
        made = ["SPY", "QQQ"] + [f"S{k:04d}" for k in range(len(names))]
        rename = dict(zip(made[2:], names, strict=True))
        tr, qu = tr[tr.ticker.isin(rename)].copy(), qu[qu.ticker.isin(rename)].copy()
        tr["ticker"], qu["ticker"] = tr.ticker.map(rename), qu.ticker.map(rename)
        with tempfile.TemporaryDirectory(dir=work) as t:
            tf, qf = Path(t) / "t.csv", Path(t) / "q.csv"
            ticks.write_flat_file(tr[tr.sip_timestamp < lo], tf, ticks.trade_columns())
            ticks.write_flat_file(qu[qu.sip_timestamp < lo], qf, ticks.quote_columns())
            rows = ticks.tick_rows(tf, qf, DAY, through, groups=cfg.model.tick_groups)
        parts.append(rows[rows.m <= through])
        ct, cq = ticks.tail_carry(tr[tr.sip_timestamp < lo], qu[qu.sip_timestamp < lo])
        carry_t.append(ct[list(feed.columns("trades")[:-2])].astype(object))
        carry_q.append(cq[list(feed.columns("quotes")[:-2])].astype(object))
        for kind, f in (("trades", tr), ("quotes", qu)):
            store.add(kind, _messages(kind, f[f.sip_timestamp < lo]), lo, feed.STREAM)
            for s in names:
                store.window(feed.Window(s, kind, store.start_ns, lo, lo, 1, None, 0))
        for kind, f in (("trades", tr), ("quotes", qu)):
            now = f[(f.sip_timestamp >= lo) & (f.sip_timestamp < hi)]
            events[kind] += len(now)
            store.add(kind, _messages(kind, now), end_ns, feed.STREAM)
    lf._append(m - 1, pd.concat(parts, ignore_index=True).assign(**{full.RECEIPT_COLUMN: lo}))
    for s in cohort:
        stream.add(s)
        lf.symbols[s] = live._Symbol(since=start - 1, entered=m - 1)
    for kind, rows in store.snapshot(lo).rows.items():  # the day before m, converted already
        cols = feed.columns(kind)[:-2]
        seq = cols.index("sequence_number")
        lf.taken[kind] = {(r[0], r[seq]): r[:len(cols)] for r in rows}
    lf.closed = m - 1
    lf.carry = (pd.concat(carry_t, ignore_index=True), pd.concat(carry_q, ignore_index=True))
    print(f"{hm}: set up in {time.perf_counter() - t0:.1f} s, cohort {len(cohort)}, "
          f"events in the minute {events}", file=sys.stderr)
    spent: dict[str, float] = {}
    with _clocks(spent):
        t0 = time.perf_counter()
        got = lf.close(m)
        total = time.perf_counter() - t0
    assert not got.refused and len(got.rows) == len(cohort), got.refused
    assert list(got.rows.columns) == ["sym", *full.full_features()]
    return {"minute": hm, "cohort_rows": len(cohort), "events_in_minute": events,
            "total_seconds": round(total, 3),
            "steps": {k: round(v, 3) for k, v in got.seconds.items()},
            "inside": {k: round(v, 3) for k, v in sorted(spent.items())}}


if __name__ == "__main__":
    raise SystemExit(main())
