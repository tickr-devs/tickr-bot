"""GET /advisor: the AI advisor's runs for the apps' Lab and the Overview tag, read-only.

Each run folder under `[advisor] proposals_dir` holds `run.json` (advisor-run-v1), any
`judge-*.json` (advisor-judge-v1) and the owner's `decision.json` (advisor-decision-v1); the
formats are in tickr.advisor_files. A folder that cannot be read exactly (no run.json: a run from
before these files, a missing key, an unknown status, a malformed judge or decision file) is
listed under `unreadable` with its cause, never guessed. POST /advisor/decision writes only
decision.json (`record_decision`). Nothing here copies, loads or runs a strategy.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr import advisor_files as files
from tickr.api import contract as api
from tickr.api.causes import safe_cause

RUN_PREFIX = re.compile(r"[0-9]{8}T[0-9]{6}Z-")
"""advisor.run names each folder "<%Y%m%dT%H%M%SZ>-<strategy name>"; other folders are not runs."""
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9][A-Za-z0-9_.-]*")
"""The run ids the API reads or writes into; a run folder with another name is unreadable."""
NO_FOLDER = "no advisor run yet: the proposals folder does not exist"
_AWAITING = (api.AdvisorRunStatus.PROPOSED, api.AdvisorRunStatus.IDEA)


class Unreadable(ValueError):
    """A run file is not exactly its format; the message is the cause."""


def advisor_document(folder: Path, rows: int) -> api.AdvisorRunsV1:
    if not folder.is_dir():
        return api.AdvisorRunsV1((), (), 0, NO_FOLDER)
    try:
        ids = sorted((p.name for p in folder.iterdir()
                      if RUN_PREFIX.match(p.name) and p.is_dir()), reverse=True)
    except OSError as exc:
        return api.AdvisorRunsV1((), (), 0, f"proposals folder unreadable ({type(exc).__name__})")
    runs: list[api.AdvisorRunV1] = []
    unreadable: list[api.AdvisorUnreadableV1] = []
    awaiting = 0
    for index, run_id in enumerate(ids):
        try:
            run = read_run(folder / run_id)
        except Unreadable as exc:
            if index < rows:
                unreadable.append(api.AdvisorUnreadableV1(run_id, safe_cause(exc)))
            continue
        if run.status in _AWAITING and run.decision is None:
            awaiting += 1
        if index < rows:
            runs.append(run)
    return api.AdvisorRunsV1(tuple(runs), tuple(unreadable), awaiting, None)


class DecisionRefused(ValueError):
    """POST /advisor/decision cannot record this decision; `missing` = no such run (404),
    else a conflict (409). The message is the cause."""

    def __init__(self, cause: str, missing: bool = False) -> None:
        super().__init__(cause)
        self.missing = missing


def record_decision(folder: Path, run_id: str, verdict: api.OwnerVerdict, at: datetime) -> None:
    """Write the owner's verdict on a proposal or idea as decision.json: a note only, nothing
    copies, loads or deploys a strategy. A later verdict replaces an earlier one."""
    if not RUN_ID.fullmatch(run_id) or not (folder / run_id).is_dir():
        raise DecisionRefused(f"no advisor run {run_id!r}", missing=True)
    try:
        run = read_run(folder / run_id)
    except Unreadable as exc:
        raise DecisionRefused(f"run {run_id} is unreadable: {safe_cause(exc)}") from exc
    if run.status not in _AWAITING:
        raise DecisionRefused(f"run {run_id} is {run.status.value}: only a proposal or an idea "
                              "takes a decision")
    api.write_atomic(folder / run_id / files.DECISION_FILE, json.dumps(
        {"schema": files.DECISION_SCHEMA, "verdict": verdict.value, "at": at.isoformat()},
        indent=1) + "\n")


def read_run(path: Path) -> api.AdvisorRunV1:
    if not RUN_ID.fullmatch(path.name):
        raise Unreadable("the folder name is not <UTC time>-<strategy name>")
    raw = _json(path / files.RUN_FILE, files.RUN_SCHEMA)
    try:
        judge_paths = sorted(path.glob(f"{files.JUDGE_PREFIX}*.json"))
    except OSError as exc:
        raise Unreadable(f"folder unreadable ({type(exc).__name__})") from exc
    judges = tuple(_judge(p) for p in judge_paths)  # one bad judge file refuses the run
    decision_path = path / files.DECISION_FILE
    decision = _decision(decision_path) if decision_path.exists() else None
    try:
        idea = raw["idea"]
        return api.AdvisorRunV1(
            id=path.name,
            at=_time(raw["at"]),
            mode=api.AdvisorMode(raw["mode"]),
            status=api.AdvisorRunStatus(raw["status"]),
            base=_text(raw["base"]),
            proposed=None if raw["proposed"] is None else _text(raw["proposed"]),
            model=_text(raw["model"]),
            evidence=_text(raw["evidence"]),
            tuned_on=_text(raw["tuned_on"]),
            judge_on=_text(raw["judge_on"]),
            summary=_text(raw["summary"]),
            changes=tuple(api.AdvisorChangeV1(*(_text(c[k]) for k in (
                "section", "key", "now", "proposed", "reason", "evidence")))
                for c in raw["changes"]),
            expected_effect=_text(raw["expected_effect"]),
            risks=tuple(_text(r) for r in raw["risks"]),
            no_change_reason=_text(raw["no_change_reason"]),
            cause=_text(raw["cause"]),
            idea=None if idea is None else api.AdvisorIdeaV1(*(_text(idea[k]) for k in (
                "name", "hypothesis", "entry_rule", "exit_rule", "filters", "evidence",
                "test_plan"))),
            judges=judges,
            decision=decision,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Unreadable(f"{files.RUN_FILE}: {type(exc).__name__}") from exc


def _json(path: Path, schema: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise Unreadable(f"no {path.name} (a run written before the apps read runs, or "
                         "an interrupted one)") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Unreadable(f"{path.name}: {type(exc).__name__}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != schema:
        raise Unreadable(f"{path.name} is not {schema}")
    return raw


def _judge(path: Path) -> api.AdvisorJudgeV1:
    raw = _json(path, files.JUDGE_SCHEMA)
    try:
        return api.AdvisorJudgeV1(
            candidate=_text(raw["candidate"]),
            base=_text(raw["base"]),
            holdout_starts=date.fromisoformat(raw["holdout_starts"]),
            verdict=api.HoldoutVerdict(raw["verdict"]),
            base_verdict=api.HoldoutVerdict(raw["base_verdict"]),
            holdout=_stats(raw["holdout"]),
            base_holdout=_stats(raw["base_holdout"]),
            fitting=_stats(raw["fitting"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise Unreadable(f"{path.name}: {type(exc).__name__}") from exc


def _decision(path: Path) -> api.AdvisorDecisionV1:
    raw = _json(path, files.DECISION_SCHEMA)
    try:
        return api.AdvisorDecisionV1(api.OwnerVerdict(raw["verdict"]), _time(raw["at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise Unreadable(f"{path.name}: {type(exc).__name__}") from exc


def _stats(raw: Any) -> api.AdvisorStatsV1 | None:
    if raw is None:
        return None
    interval = raw["mean_r_interval"]
    if interval is not None and len(interval) != len(("low", "high")):
        raise ValueError(f"mean_r_interval is not two numbers: {interval!r}")
    trades = raw["trades"]
    return api.AdvisorStatsV1(
        trades=None if trades is None else int(trades),
        win_rate_percent=_number(raw["win_rate_percent"]),
        mean_r=_number(raw["mean_r"]),
        median_r=_number(raw["median_r"]),
        total_r=_number(raw["total_r"]),
        profit_factor=_number(raw["profit_factor"]),
        mean_r_interval_low=None if interval is None else _number(interval[0]),
        mean_r_interval_high=None if interval is None else _number(interval[-1]),
    )


def _number(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"number {value!r} is not text")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f"number {value!r} is not finite")
    return number


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{value!r} is not text")
    return value


def _time(value: Any) -> datetime:
    at = datetime.fromisoformat(_text(value))
    if at.tzinfo is None:
        raise ValueError(f"time {value!r} has no zone")
    return at
