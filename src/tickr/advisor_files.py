"""Machine-readable files of an advisor run, next to its markdown, for GET /advisor (the apps).

- `run.json` (advisor-run-v1): written by advisor.run with report.md, whatever the outcome.
- `judge-<name>-v<N>.json` (advisor-judge-v1): written by advisor.judge with its markdown.
- `decision.json` (advisor-decision-v1): the owner's review verdict, written by POST
  /advisor/decision. It is a note only: nothing reads it to copy, load or deploy a strategy.

The names, codes and the judge's format live here, outside the advisor package, because the API
reads them and the bot never imports the advisor (tests/test_invariants.py); advisor.record
writes run.json. Values are the advisor's own texts and the backtest's own numbers as strings;
nothing is recomputed. Every number is SIMULATED or observed, never real profit.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

RUN_FILE = "run.json"
RUN_SCHEMA = "advisor-run-v1"
JUDGE_PREFIX = "judge-"
JUDGE_SCHEMA = "advisor-judge-v1"
DECISION_FILE = "decision.json"
DECISION_SCHEMA = "advisor-decision-v1"
STATS_KEYS = ("trades", "win_rate_percent", "mean_r", "median_r", "total_r", "profit_factor")


class Verdict(StrEnum):
    """What a bootstrap interval of mean R says on the hold-out (advisor.judge)."""

    EDGE = "edge"  # the whole interval is above 0
    NEGATIVE_EDGE = "negative_edge"  # the whole interval is below 0
    NO_EVIDENCE = "no_evidence"  # the interval includes 0
    TOO_FEW = "too_few"  # too few trades or days for an interval


class OwnerVerdict(StrEnum):
    """The owner's review of a run. A note only: nothing copies, loads or deploys a strategy
    from it; trying a proposal stays a hand edit of strategies/ and config."""

    ACCEPTED = "accepted"  # the owner will try it (by hand: observer or module PR)
    REJECTED = "rejected"  # the owner will not


def stats(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """A backtest report's stats block, only the keys the apps show, values as strings."""
    if not raw:
        return None
    shown: dict[str, Any] = {k: None if raw.get(k) is None else str(raw[k]) for k in STATS_KEYS}
    interval = raw.get("mean_r_interval")
    shown["mean_r_interval"] = [str(v) for v in interval] if interval else None
    return shown


def judge_record(*, candidate: str, base: str, window: list[str], holdout_starts: str,
                 verdict: Verdict, base_verdict: Verdict, holdout: dict[str, Any] | None,
                 base_holdout: dict[str, Any] | None,
                 fitting: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "schema": JUDGE_SCHEMA,
        "candidate": candidate,
        "base": base,
        "window": window,
        "holdout_starts": holdout_starts,
        "verdict": verdict.value,
        "base_verdict": base_verdict.value,
        "holdout": stats(holdout),
        "base_holdout": stats(base_holdout),
        "fitting": stats(fitting),
    }
