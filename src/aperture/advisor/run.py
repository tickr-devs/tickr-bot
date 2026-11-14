"""One advisor run: evidence -> one model request -> checked proposal -> files.

Output folder: <data_dir>/<proposals_dir>/<UTC time>-<strategy>/ with
- evidence.json, request.txt (system + user text), response.json (the raw answer);
- report.md (always: proposed, no change, or refused with causes);
- strategies/<name>-v<N>/strategy.toml when a change was proposed and accepted by the strategy.
Nothing is copied into strategies/ of the repository: the owner does that by hand to run the
proposal as an observer.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from aperture.advisor import prompt
from aperture.advisor.client import AdvisorClient
from aperture.advisor.errors import AdvisorError
from aperture.advisor.evidence import build_evidence
from aperture.advisor.proposal import SCHEMA, Proposal, next_version, parse_reply, render_toml
from aperture.advisor.report import render_report
from aperture.calendar import TIMEZONE_NAME, SessionCalendar
from aperture.config import Config
from aperture.observers.errors import ObserverError
from aperture.observers.loader import (
    STRATEGIES_DIR,
    STRATEGY_FILE,
    load_strategy,
    parse_modules,
)
from aperture.replay.bars import load_bars
from aperture.replay.clock import ClockFor, calendar_clock
from aperture.replay.settings import ReplaySettings
from aperture.research.records import load_records
from aperture.research.settings import ResearchSettings

DECISIONS_FILE = "decisions.jsonl"
BARS_FILE = "bars.jsonl"


class Status(StrEnum):
    PROPOSED = "proposed"
    NO_CHANGE = "no_change"
    REFUSED = "refused"


@dataclass(frozen=True)
class RunResult:
    status: Status
    folder: Path
    proposal_folder: Path | None
    cause: str


def day_files(config: Config, day: date) -> tuple[Path, Path]:
    base = config.root / config.run.data_dir
    env = config.environment_name
    decisions = base / config.observers.decisions_dir / env / day.isoformat() / DECISIONS_FILE
    bars = base / config.market_data.capture_dir / env / day.isoformat() / BARS_FILE
    return decisions, bars


def run_advisor(
    config: Config,
    days: Sequence[date],
    client: AdvisorClient,
    research: ResearchSettings,
    replay: ReplaySettings,
    now: datetime,
    folder: str | None = None,
    out_dir: Path | None = None,
) -> RunResult:
    settings = config.advisor
    days = sorted(set(days))
    if len(days) < settings.min_days:
        raise AdvisorError(f"{len(days)} days given, [advisor] min_days is {settings.min_days}")
    decision_files, bar_files = [], []
    for day in days:
        decisions, bars = day_files(config, day)
        for path in (decisions, bars):
            if not path.is_file():
                raise AdvisorError(f"{day}: missing {path}")
        decision_files.append(decisions)
        bar_files.append(bars)
    folder = folder or config.strategy.active
    base_path = config.root / STRATEGIES_DIR / folder / STRATEGY_FILE
    base_text = base_path.read_text("utf-8")
    base_values = tomllib.loads(base_text)
    name = base_values["strategy"]["name"]
    version = str(base_values["strategy"]["version"])
    calendar = SessionCalendar.load((config.root / config.session.calendar_file).parent)
    evidence = build_evidence(
        days=days,
        records=load_records(decision_files),
        bars_by_symbol=load_bars(bar_files),
        strategy=name,
        version=version,
        research=research,
        close_for=lambda d: calendar.day(d).regular_close,
        replay=replay,
        clock_for=session_clock_from_config(config),
    )
    evidence_text = json.dumps(evidence, indent=1, sort_keys=True)
    if len(evidence_text.encode("utf-8")) > settings.max_evidence_bytes:
        raise AdvisorError(
            f"evidence is {len(evidence_text.encode('utf-8'))} bytes, "
            f"[advisor] max_evidence_bytes is {settings.max_evidence_bytes}"
        )
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    base_out = out_dir or config.root / config.run.data_dir / settings.proposals_dir
    out = base_out / f"{stamp}-{name}"
    out.mkdir(parents=True, exist_ok=False)
    system = prompt.system_prompt(settings.max_changes)
    user = prompt.user_prompt(base_text, evidence)
    (out / "evidence.json").write_text(evidence_text + "\n", "utf-8")
    (out / "request.txt").write_text(f"{system}\n----- user -----\n{user}\n", "utf-8")

    reply = client.propose(system, user, SCHEMA)
    (out / "response.json").write_text(reply.text + "\n", "utf-8")
    context = dict(model=reply.model, base=f"{name} v{version}", folder=folder, days=days,
                   now=now, evidence=evidence)
    try:
        proposal = parse_reply(reply.text, base_values, settings.max_changes)
    except AdvisorError as exc:
        return _finish(out, Status.REFUSED, None, str(exc), None, None, context)
    if not proposal.changes:
        return _finish(out, Status.NO_CHANGE, None, proposal.no_change_reason, proposal, None,
                       context)
    try:
        new_version = next_version(config.root / STRATEGIES_DIR, name)
        new_folder = f"{name}-v{new_version}"
        header = [
            f"PROPOSAL by the AI advisor ({reply.model}) on {now:%Y-%m-%d}, from {folder} "
            f"v{version} over {len(days)} days. NOT APPLIED.",
            f"To try it: copy this folder to strategies/{new_folder}/ and add "
            f'"{new_folder}" to [strategy] observers in config/bot.toml.',
        ]
        text = render_toml(base_text, proposal, new_version, header)
        target = out / STRATEGIES_DIR / new_folder
        target.mkdir(parents=True)
        (target / STRATEGY_FILE).write_text(text, "utf-8")
        load_strategy(out, new_folder, parse_modules(config.observers.strategy_modules))
    except (AdvisorError, ObserverError) as exc:
        return _finish(out, Status.REFUSED, None, str(exc), proposal, None, context)
    return _finish(out, Status.PROPOSED, target, "", proposal, f"{name} v{new_version}", context)


def session_clock_from_config(config: Config) -> ClockFor:
    """Each day's clock from `[session]` and the calendar, as the replay CLI builds it."""
    if config.session.timezone != TIMEZONE_NAME:
        raise AdvisorError(f"[session] timezone {config.session.timezone!r} is not the calendar's")
    calendar = SessionCalendar.load((config.root / config.session.calendar_file).parent)
    return calendar_clock(calendar, config.session)


def _finish(out: Path, status: Status, target: Path | None, cause: str,
            proposal: Proposal | None, new_identity: str | None, context: dict) -> RunResult:
    report = render_report(status=status.value, cause=cause, proposal=proposal,
                           new_identity=new_identity, target=target, out=out, **context)
    (out / "report.md").write_text(report, "utf-8")
    return RunResult(status, out, target, cause)
