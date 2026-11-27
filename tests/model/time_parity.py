"""Time the replay parity harness per minute with both engines on a synthetic day.

    python tests/model/time_parity.py [--symbols 500] [--trades 2000] [--quotes 4000]
        [--minutes 04:30 09:45 12:00 15:30] [--mode both] [--keep FOLDER]

A measurement, not a test (pytest does not collect it). It builds, outside the timed runs: the
full-market bars of the day and its 20 sessions (tests/model/model_market.py, `--symbols`
tickers), one synthetic tick day for every ticker (model_ticks.day_events), the training-format
reference, Benzinga and fl inputs (model_inputs.py), and the training's own table of the day
(its whole-day tick file from v11's `local` mode and l1feat.assemble, as test_parity.py builds
it). Then it runs `python -m tickr.model.parity` in-process at `--minutes`, once with
`--engine full-prefix` and once with `--engine incremental`, and prints the seconds per minute
that each run's journal records (bars, ticks, features, compare) and the incremental walk's
conversion time. Nothing leaves the machine; no credential is read.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import model_inputs as inputs
import model_market as market
import model_ticks as tick_maker
import numpy as np

from tickr.model import parity, parity_walk
from tickr.model.features import ticks

DAY = "2024-03-06"  # an open-archive day with 20 sessions before it (as test_parity)
NEWS_MINUTES = [320, 338, 339, 340, 352, 360, 371, 384, 386, 400]


def build(root: Path, symbols: int, trades: int, quotes: int) -> list[str]:
    """The day's files under `root`; the arguments of a parity run, without minutes."""
    code = inputs.research()
    labels, w = code.labels, code.w
    cal = labels.calendar()
    days = cal[cal.index(DAY) - labels.HIST:cal.index(DAY) + 1]
    bars = root / "bars"
    bars.mkdir(parents=True)
    for d, frame in market.market(days, symbols, 5):
        frame.to_csv(bars / f"{d}.csv.gz", index=False, compression="gzip")
    t, q = tick_maker.day_events(DAY, symbols, trades, quotes)
    tf, qf = root / "trades.csv.gz", root / "quotes.csv.gz"
    ticks.write_flat_file(t, tf, tick_maker.TRADE_FILE_COLUMNS)
    ticks.write_flat_file(q, qf, tick_maker.QUOTE_FILE_COLUMNS)
    names = market.tickers(symbols)
    ref = inputs.reference(root / "ref", DAY, names, NEWS_MINUTES)
    bz = inputs.benzinga(root / "bz", DAY, names, NEWS_MINUTES, "2023-01-02")
    fl = inputs.fl_facts(root / "fl.csv", DAY, names)
    whole = root / "tk-whole.parquet"
    subprocess.run([sys.executable, str(ticks.PINNED_DIR / ticks.CONVERTER_FILE), "local",
                    str(tf), str(qf), str(whole)], check=True, capture_output=True)
    need = labels.needed(cal, labels.bar_names(str(bars))[0], DAY)
    body = (ref / "splits.jsonl").read_bytes()
    sp = labels.Splits(labels.splits_parse(body), str(ref / "splits.jsonl"), labels.md5b(body))
    saved = w.load
    w.load = labels.bar_load
    try:
        (prev, mprev, hist), _ = labels.history(str(bars), None, need, w.day2, sp)
        table = w.day2(str(bars / f"{DAY}.csv.gz"), prev, None, hist, mprev, want=True)[0]
    finally:
        w.load = saved
    table = table.reset_index(drop=True)
    pc = np.array([prev.get(s, np.nan) for s in table.sym.values], np.float64)
    work = root / "assemble"
    work.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        o, rec = code.feat.assemble(table, pc, DAY, str(whole), str(ref), str(bz), str(fl),
                                    code.settings, str(work), w, code.ref, code.tk)
        code.feat.write(o, rec, str(root / "table"), DAY)
    return ["--day", DAY, "--bars", str(bars), "--types", "-", "--trades", str(tf), "--quotes",
            str(qf), "--reference", str(ref), "--benzinga", str(bz), "--fl", str(fl),
            "--table", str(root / "table" / f"{DAY}.parquet")]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--symbols", type=int, default=500, help="tickers with bars and ticks")
    p.add_argument("--trades", type=int, default=2000, help="trades per symbol-day (mean)")
    p.add_argument("--quotes", type=int, default=4000, help="quotes per symbol-day (mean)")
    p.add_argument("--minutes", nargs="+", default=list(parity.BASE_MINUTES))
    p.add_argument("--json", type=Path, help="also write the numbers here")
    p.add_argument("--all-minutes", action="store_true", help="every minute of the day")
    p.add_argument("--mode", default="both", choices=sorted(parity.MODES))
    p.add_argument("--engines", nargs="+", default=list(parity.ENGINES),
                   choices=parity.ENGINES)
    p.add_argument("--workers", default="1", help="--workers of the parity runs")
    p.add_argument("--keep", type=Path, help="build or reuse the synthetic day here")
    args = p.parse_args(argv)
    ticks.converter()
    with tempfile.TemporaryDirectory(prefix="time-parity-") as tmp:
        root = args.keep or Path(tmp) / "day"
        t0 = time.perf_counter()
        made = root / "args.json"
        if made.exists():
            base = json.loads(made.read_text())
        else:
            base = build(root, args.symbols, args.trades, args.quotes)
            made.write_text(json.dumps(base))
        print(f"synthetic day ready in {time.perf_counter() - t0:.0f} s", file=sys.stderr)
        walked: list[float] = []
        real = parity_walk.TickWalk.close

        def close(self):
            walked.append(self.seconds)
            walked.append(self.calls)
            real(self)

        parity_walk.TickWalk.close = close
        out = {"symbols": args.symbols, "trades_per_symbol": args.trades,
               "quotes_per_symbol": args.quotes, "mode": args.mode, "engines": {}}
        for engine in args.engines:
            dest = Path(tmp) / f"out-{engine}"
            t0 = time.perf_counter()
            with contextlib.redirect_stdout(io.StringIO()):
                when = ["--all-minutes"] if args.all_minutes else ["--minutes", *args.minutes]
                code = parity.main([*base, *when, "--mode", args.mode,
                                    "--out", str(dest), "--engine", engine,
                                    "--workers", args.workers])
            total = time.perf_counter() - t0
            if code:
                raise SystemExit(f"{engine}: exit {code}")
            report = json.loads((dest / f"parity-{DAY}.json").read_text())
            out["engines"][engine] = {
                "run_seconds": round(total, 1), "per_minute": report["seconds"],
                "converted_symbols": report["converted_symbols"],
                "rows": {hm: r["rows_replay"] for hm, r in
                         next(iter(report["variants"].values())).items()}}
            if engine == "incremental":
                out["engines"][engine]["walk_seconds"] = round(walked[-2], 1)
                out["engines"][engine]["converter_calls"] = walked[-1]
        out["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(json.dumps(out, indent=1))
    if args.json:
        args.json.write_text(json.dumps(out, indent=1) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
