"""Judge a proposal on the withheld hold-out: `python -m tickr.advisor.judge --run <advisor
run folder> --results <backtest results of the proposal>`.

The advisor run (backtest mode) recorded in `tuning.json` the window and the hold-out start of
the base backtest it learned from, its pool (the data: symbol-days, sessions, symbols, replayed
and skipped counts) and the base's hold-out numbers. The candidate is any other strategy
version backtested on the same data, window and hold-out split: the proposed
values, or a module written from the advisor's idea. Only the hold-out decides; the fitting
period is shown for contrast and marked as no evidence. Mismatched data, windows, splits or a
candidate equal to the base are refused. Writes `judge-<name>-v<version>.md` into the run
folder. Every number is SIMULATED.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr.advisor.backtest_evidence import read_run
from tickr.advisor.errors import AdvisorError
from tickr.advisor.run import TUNING_FILE

COLUMNS = ("trades", "win_rate_percent", "mean_r", "median_r", "total_r", "profit_factor")


def _interval(stats: dict[str, Any] | None) -> tuple[Decimal, Decimal] | None:
    raw = (stats or {}).get("mean_r_interval")
    if not raw:
        return None
    try:
        low, high = (Decimal(str(v)) for v in raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AdvisorError(f"mean_r_interval is not two numbers: {raw!r}") from exc
    return low, high


def verdict(stats: dict[str, Any] | None) -> str:
    interval = _interval(stats)
    if interval is None:
        return "too few trades or days for an interval: no evidence either way"
    low, high = interval
    if low > 0:
        return f"interval {low} to {high} is above 0: evidence of an edge on the hold-out"
    if high < 0:
        return f"interval {low} to {high} is below 0: evidence of a negative edge on the hold-out"
    return f"interval {low} to {high} includes 0: no evidence of an edge on the hold-out"


def _row(name: str, stats: dict[str, Any] | None) -> str:
    stats = stats or {}
    cells = [str(stats.get(c, "")) if stats.get(c) is not None else "n/a" for c in COLUMNS]
    return f"| {name} | " + " | ".join(cells) + " |"


def judge(run_folder: Path, results: Path) -> str:
    try:
        tuning = json.loads((run_folder / TUNING_FILE).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisorError(f"{run_folder} is not a backtest-mode advisor run: {exc}") from exc
    candidate = read_run(results)
    identity = f"{candidate.strategy} v{candidate.version}"
    window = [d.isoformat() for d in candidate.window]
    if window != tuning["window"]:
        raise AdvisorError(f"{results} covers {window}, the advisor learned on {tuning['window']}")
    if candidate.holdout_starts.isoformat() != tuning["holdout_starts"]:
        raise AdvisorError(f"{results} splits its hold-out at {candidate.holdout_starts}, the "
                           f"advisor's at {tuning['holdout_starts']}: set the same "
                           "holdout_percent")
    pool = candidate.report.get("pool")
    if not pool or pool != tuning.get("pool"):
        raise AdvisorError(f"{results} ran on other data (pool {pool}) than the advisor's base "
                           f"(pool {tuning.get('pool')}): rerun both on the same fetch")
    if identity == tuning["base"]:
        raise AdvisorError(f"{results} is the base {identity} itself")
    split = candidate.report["holdout"]
    held = split.get("holdout")
    text = "\n".join([
        f"# SIMULATED hold-out check: {identity} against {tuning['base']}",
        "",
        f"Window {window[0]} .. {window[1]}; hold-out from {tuning['holdout_starts']}. The "
        "advisor saw only the sessions before it.",
        "",
        "| run | " + " | ".join(COLUMNS) + " |",
        "|---|" + "---|" * len(COLUMNS),
        _row(f"{tuning['base']} hold-out", tuning.get("base_holdout")),
        _row(f"{identity} hold-out", held),
        _row(f"{identity} fitting period (no evidence)", split.get("before")),
        "",
        f"- {tuning['base']}: {verdict(tuning.get('base_holdout'))}.",
        f"- {identity}: {verdict(held)}.",
        "",
        "Only the hold-out rows count. A better fitting period proves nothing: the proposal "
        "was fitted there. Fills are the replay's conservative model, gross of commissions.",
        "",
    ])
    out = run_folder / f"judge-{candidate.strategy}-v{candidate.version}.md"
    out.write_text(text, "utf-8")
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tickr.advisor.judge",
        description="Compare a proposal with its base on the withheld backtest hold-out.")
    parser.add_argument("--run", type=Path, required=True, help="advisor run folder")
    parser.add_argument("--results", type=Path, required=True,
                        help="backtest results folder of the proposal")
    args = parser.parse_args(argv)
    try:
        print(judge(args.run, args.results))
    except (AdvisorError, KeyError, OSError) as exc:
        print(f"judge refused: {exc!r}" if isinstance(exc, KeyError) else f"judge refused: {exc}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
