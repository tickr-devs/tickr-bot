"""One advisor run: evidence -> one model request -> checked proposal -> files.

Evidence comes from recorded live days (`run_advisor`) or from the fitting period of a
SIMULATED backtest (`run_backtest_advisor`, advisor.backtest_evidence). Both then share `ask`.

Output folder: <data_dir>/<proposals_dir>/<UTC time>-<strategy>/ with
- evidence.json, request.txt (system + user text), response.json (the raw answer);
- report.md (always: proposed, idea, no change, or refused with causes);
- strategies/<name>-v<N>/strategy.toml when a change was proposed and accepted by the strategy;
- idea.md when the model described a new strategy idea (text for a developer, never code);
- tuning.json in backtest mode: the window and hold-out start, for advisor.judge.
Nothing is copied into strategies/ of the repository: the owner does that by hand to run the
proposal as an observer.
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from tickr.advisor import prompt
from tickr.advisor.backtest_evidence import (
    build_backtest_evidence,
    fitting_signals,
    fitting_trades,
    load_bars_for,
    read_run,
)
from tickr.advisor.client import AdvisorClient
from tickr.advisor.errors import AdvisorError
from tickr.advisor.evidence import build_evidence
from tickr.advisor.proposal import SCHEMA, Proposal, next_version, parse_reply, render_toml
from tickr.advisor.report import render_idea, render_report
from tickr.archive import existing
from tickr.calendar import TIMEZONE_NAME, SessionCalendar
from tickr.config import Config
from tickr.data.capture import CaptureError, read_captured_bars
from tickr.observers.errors import ObserverError
from tickr.observers.loader import (
    STRATEGIES_DIR,
    STRATEGY_FILE,
    load_strategy,
    parse_modules,
)
from tickr.replay.clock import ClockFor, calendar_clock
from tickr.replay.settings import ReplaySettings
from tickr.research.records import load_records
from tickr.research.settings import ResearchSettings
from tickr.types import Bar

DECISIONS_FILE = "decisions.jsonl"
IDEA_FILE = "idea.md"
TUNING_FILE = "tuning.json"
BARS_FILE = "bars.jsonl"


class Status(StrEnum):
    PROPOSED = "proposed"
    NO_CHANGE = "no_change"
    IDEA = "idea"
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


@dataclass(frozen=True)
class Base:
    """The strategy folder the proposal starts from."""

    folder: str
    text: str
    values: dict
    name: str
    version: str
    source: str
    """The strategy module's source code (the model reads what the values steer)."""

    @property
    def identity(self) -> str:
        return f"{self.name} v{self.version}"


@dataclass(frozen=True)
class Tuning:
    """What the proposal was fitted on and where it may be judged (hold-out rule)."""

    tuned_on: str
    judge_on: str
    label: str
    """One line for the report header: the evidence source."""
    headline: tuple[str, ...]
    backtest: bool = False
    record: dict | None = None
    """Written to tuning.json (never sent to the model) for advisor.judge; None = live."""


def read_base(config: Config, folder: str | None) -> Base:
    folder = folder or config.strategy.active
    text = (config.root / STRATEGIES_DIR / folder / STRATEGY_FILE).read_text("utf-8")
    values = tomllib.loads(text)
    name = values["strategy"]["name"]
    module = parse_modules(config.observers.strategy_modules).get(name)
    if module is None:
        raise AdvisorError(f"[observers] strategy_modules has no module for {name}")
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        raise AdvisorError(f"strategy module {module} not found")
    source = Path(spec.origin).read_text("utf-8")
    return Base(folder, text, values, name, str(values["strategy"]["version"]), source)


def run_advisor(
    config: Config,
    days: Sequence[date],
    client: AdvisorClient,
    research: ResearchSettings,
    replay: ReplaySettings,
    now: datetime,
    folder: str | None = None,
    out_dir: Path | None = None,
    notes: str = "",
) -> RunResult:
    """Live mode: evidence from recorded trading days."""
    settings = config.advisor
    days = sorted(set(days))
    if len(days) < settings.min_days:
        raise AdvisorError(f"{len(days)} days given, [advisor] min_days is {settings.min_days}")
    decision_files, bar_files = [], []
    for day in days:  # sorted: captured_bars lets a later day's receipt revise a bar
        decisions, bars = day_files(config, day)
        for path in (decisions, bars):
            if existing(path) is None:  # plain or archived (.gz)
                raise AdvisorError(f"{day}: missing {path}")
        decision_files.append(decisions)
        bar_files.append(bars)
    base = read_base(config, folder)
    calendar = SessionCalendar.load((config.root / config.session.calendar_file).parent)
    evidence = build_evidence(
        days=days,
        records=load_records(decision_files),
        bars_by_symbol=captured_bars(bar_files),
        strategy=base.name,
        version=base.version,
        research=research,
        close_for=lambda d: calendar.day(d).regular_close,
        replay=replay,
        clock_for=session_clock_from_config(config),
    )
    listed = ", ".join(d.isoformat() for d in days)
    simulated = evidence["not_taken_enters_simulated"]
    tuning = Tuning(
        tuned_on=listed,
        judge_on=f"days after {days[-1].isoformat()} (observers) or on replay days outside "
                 "that list",
        label=f"{len(days)} recorded days ({days[0].isoformat()} .. {days[-1].isoformat()})",
        headline=(
            f"Decisions: {evidence['decisions']} over {evidence['symbols']} symbols; "
            f"verdicts {evidence['verdicts']}",
            "First failed check (IGNORE/WATCH): "
            f"{evidence['first_failed_check_on_ignore_and_watch']}",
            f"ENTER refusals: {evidence['enter_refusal_causes']}",
            f"Not-taken ENTERs simulated: {simulated['trades']} trades, "
            f"total R {simulated['total_r']}",
        ),
    )
    return ask(config, client, base, evidence, tuning, now, out_dir, notes)


def run_backtest_advisor(
    config: Config,
    results: Path,
    data: Path,
    client: AdvisorClient,
    replay: ReplaySettings,
    now: datetime,
    folder: str | None = None,
    out_dir: Path | None = None,
    notes: str = "",
) -> RunResult:
    """Backtest mode: evidence from the fitting period of a backtest run; the hold-out is
    withheld and judges the proposal later (advisor.judge)."""
    run = read_run(results)
    base = read_base(config, folder)
    if (run.strategy, run.version) != (base.name, base.version):
        raise AdvisorError(f"{results} is a backtest of {run.strategy} v{run.version}, the "
                           f"strategy folder {base.folder} is {base.identity}")
    trades = fitting_trades(run)
    evidence = build_backtest_evidence(
        run, trades, fitting_signals(run), load_bars_for(data, [t.symbol for t in trades]),
        config.advisor, replay.output_decimal_places)
    first, last = evidence["fitting_period"]
    starts = run.holdout_starts.isoformat()
    t = evidence["trades"]
    tuning = Tuning(
        tuned_on=f"backtest sessions {first} .. {last} (before the hold-out from {starts})",
        judge_on=f"the backtest hold-out from {starts} (python -m tickr.advisor.judge) or "
                 "observer days after the proposal",
        label=f"SIMULATED backtest {results}, fitting period {first} .. {last}; hold-out from "
              f"{starts} withheld",
        headline=(
            f"Fitting trades: {t['trades']}, win rate {t['win_rate_percent']}%, mean R "
            f"{t['mean_r']}, total R {t['total_r']}",
            f"By exit reason: { {k: v['mean_r'] for k, v in evidence['by_exit_reason'].items()} }"
            " (mean R)",
            f"Best high while open (R): {evidence['excursions']['best_high_while_open_r']}",
        ),
        backtest=True,
        record={
            "source": "backtest",
            "base": base.identity,
            "results": str(results.resolve()),
            "window": [d.isoformat() for d in run.window],
            "holdout_starts": starts,
            "base_holdout": run.report["holdout"].get("holdout"),
            "pool": run.report.get("pool"),
        },
    )
    return ask(config, client, base, evidence, tuning, now, out_dir, notes)


def ask(config: Config, client: AdvisorClient, base: Base, evidence: dict, tuning: Tuning,
        now: datetime, out_dir: Path | None, notes: str = "") -> RunResult:
    """One request, then the checked proposal and its files. Shared by both modes.
    `notes`: the owner's text the model should also read (for example ideas already tested
    and rejected), sent as written; it counts against max_evidence_bytes."""
    settings = config.advisor
    name, version, folder = base.name, base.version, base.folder
    evidence_text = json.dumps(evidence, indent=1, sort_keys=True)
    size = len(evidence_text.encode("utf-8")) + len(notes.encode("utf-8"))
    if size > settings.max_evidence_bytes:
        raise AdvisorError(
            f"evidence and notes are {size} bytes, "
            f"[advisor] max_evidence_bytes is {settings.max_evidence_bytes}"
        )
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    base_out = out_dir or config.root / config.run.data_dir / settings.proposals_dir
    out = base_out / f"{stamp}-{name}"
    out.mkdir(parents=True, exist_ok=False)
    system = prompt.system_prompt(settings.max_changes, backtest=tuning.backtest)
    user = prompt.user_prompt(base.text, evidence, base.source, notes)
    (out / "evidence.json").write_text(evidence_text + "\n", "utf-8")
    (out / "request.txt").write_text(f"{system}\n----- user -----\n{user}\n", "utf-8")
    record = dict(tuning.record) if tuning.record is not None else None

    reply = client.propose(system, user, SCHEMA)
    (out / "response.json").write_text(reply.text + "\n", "utf-8")
    context = dict(model=reply.model, base=base.identity, folder=folder, tuning=tuning, now=now)
    try:
        proposal = parse_reply(reply.text, base.values, settings.max_changes)
    except AdvisorError as exc:
        return _finish(out, Status.REFUSED, None, str(exc), None, None, context, record)
    if proposal.idea is not None:
        (out / IDEA_FILE).write_text(render_idea(proposal.idea, tuning), "utf-8")
    if not proposal.changes:
        status = Status.IDEA if proposal.idea is not None else Status.NO_CHANGE
        return _finish(out, status, None, proposal.no_change_reason, proposal, None, context,
                       record)
    try:
        new_version = next_version(config.root / STRATEGIES_DIR, name)
        new_folder = f"{name}-v{new_version}"
        header = [
            f"PROPOSAL by the AI advisor ({reply.model}) on {now:%Y-%m-%d}, from {folder} "
            f"v{version}. NOT APPLIED.",
            f"To try it: copy this folder to strategies/{new_folder}/ and add "
            f'"{new_folder}" to [strategy] observers in config/bot.toml.',
            f"Tuned on (hold-out rule: judge it only on other days): {tuning.tuned_on}.",
        ]
        text = render_toml(base.text, proposal, new_version, header)
        target = out / STRATEGIES_DIR / new_folder
        target.mkdir(parents=True)
        (target / STRATEGY_FILE).write_text(text, "utf-8")
        load_strategy(out, new_folder, parse_modules(config.observers.strategy_modules))
    except (AdvisorError, ObserverError) as exc:
        return _finish(out, Status.REFUSED, None, str(exc), proposal, None, context, record)
    return _finish(out, Status.PROPOSED, target, "", proposal, f"{name} v{new_version}",
                   context, record)


def captured_bars(paths: Sequence[Path]) -> dict[str, tuple[Bar, ...]]:
    """Bars per symbol, oldest first, from captured bars.jsonl files (plain or archived).

    Capture is split by receipt day and discovery asks for the latest bars, so a day's file
    can repeat bars of the day before (a request just after the open). The rule is that of
    `read_captured_bars`, applied across files given oldest day first: one bar per (symbol,
    start), and when the values differ the later receipt is the provider's revision and wins.
    """
    found: dict[tuple[str, datetime], Bar] = {}
    for path in paths:
        try:
            read = read_captured_bars(path)
        except CaptureError as exc:
            raise AdvisorError(f"capture bars refused: {exc}") from exc
        found.update(read.bars)
    by_symbol: dict[str, list[Bar]] = {}
    for symbol, start in sorted(found):
        by_symbol.setdefault(symbol, []).append(found[symbol, start])
    return {symbol: tuple(bars) for symbol, bars in by_symbol.items()}


def session_clock_from_config(config: Config) -> ClockFor:
    """Each day's clock from `[session]` and the calendar, as the replay CLI builds it."""
    if config.session.timezone != TIMEZONE_NAME:
        raise AdvisorError(f"[session] timezone {config.session.timezone!r} is not the calendar's")
    calendar = SessionCalendar.load((config.root / config.session.calendar_file).parent)
    return calendar_clock(calendar, config.session)


def _finish(out: Path, status: Status, target: Path | None, cause: str,
            proposal: Proposal | None, new_identity: str | None, context: dict,
            record: dict | None) -> RunResult:
    report = render_report(status=status.value, cause=cause, proposal=proposal,
                           new_identity=new_identity, target=target, out=out, **context)
    (out / "report.md").write_text(report, "utf-8")
    if record is not None:
        record = {**record, "status": status.value, "proposed": new_identity,
                  "idea": proposal.idea.name if proposal and proposal.idea else None}
        (out / TUNING_FILE).write_text(json.dumps(record, indent=1) + "\n", "utf-8")
    return RunResult(status, out, target, cause)
