"""Command line: `python -m aperture.research <command> ...`. See docs/research.md."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aperture import config as bot_config
from aperture.calendar import CalendarRefused, SessionCalendar
from aperture.config import ConfigError
from aperture.replay.__main__ import session_clock, strategy_factory
from aperture.replay.bars import load_bars
from aperture.replay.errors import ReplayError
from aperture.replay.settings import load_settings
from aperture.research import compare, observer_report, report
from aperture.research.errors import ResearchError
from aperture.research.outcomes import outcomes
from aperture.research.records import load_records, write_records
from aperture.research.settings import load_research_settings
from aperture.research.whatif import what_if


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aperture.research",
        description="Signal outcomes (observed) and what-if analysis (simulated).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def bars_and_out(p: argparse.ArgumentParser) -> None:
        p.add_argument("--bars", type=Path, nargs="+", required=True, help="JSONL bar files")
        p.add_argument("--out", type=Path, required=True, help="output directory")

    def clock(p: argparse.ArgumentParser) -> None:
        p.add_argument("--replay-settings", type=Path, default=Path("config/replay.toml"),
                       help="replay settings TOML (fill model)")
        p.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                       help="bot config: [session] offsets and the calendar give each day's clock")

    p = sub.add_parser("outcomes", help="price moves after WATCH/ENTER decisions")
    bars_and_out(p)
    p.add_argument("--decisions", type=Path, nargs="+", required=True, help="decision JSONL")
    p.add_argument("--settings", type=Path, required=True, help="research TOML")
    p.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                   help="bot config: its calendar gives each day's regular close")

    p = sub.add_parser("whatif", help="simulate ENTER decisions that were not taken")
    bars_and_out(p)
    clock(p)
    p.add_argument("--decisions", type=Path, nargs="+", required=True, help="decision JSONL")
    p.add_argument("--quantity", help="shares for USD results (default: recorded quantity)")

    p = sub.add_parser("decide", help="record a strategy version's decisions over the bars")
    bars_and_out(p)
    clock(p)
    p.add_argument("--strategy", required=True, help="strategy module exposing create()")
    p.add_argument("--values", type=Path, required=True, help="strategy.toml for create()")

    p = sub.add_parser("observers", help="daily report: observers against the active strategy")
    bars_and_out(p)
    clock(p)
    p.add_argument("--decisions", type=Path, nargs="+", required=True, help="decision JSONL")

    p = sub.add_parser("diff", help="diff two decision record sets bar by bar")
    p.add_argument("--baseline", type=Path, nargs="+", required=True, help="decision JSONL")
    p.add_argument("--candidate", type=Path, nargs="+", required=True, help="decision JSONL")
    p.add_argument("--baseline-strategy", help="NAME or NAME:VERSION to keep from baseline")
    p.add_argument("--candidate-strategy", help="NAME or NAME:VERSION to keep from candidate")
    p.add_argument("--settings", type=Path, required=True, help="research TOML (time zone)")
    p.add_argument("--out", type=Path, required=True, help="output directory")
    return parser


def _filter(records, spec: str | None):
    if spec is None:
        return records
    name, _, version = spec.partition(":")
    return compare.select(records, name, version or None)


def _run(args: argparse.Namespace) -> dict:
    if args.command == "outcomes":
        settings = load_research_settings(args.settings)
        cfg = bot_config.load(args.config)
        calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
        rows = outcomes(load_records(args.decisions), load_bars(args.bars), settings,
                        lambda day: calendar.day(day).regular_close)
        return report.write_outcomes(rows, args.out, settings.output_decimal_places)
    if args.command == "diff":
        settings = load_research_settings(args.settings)
        result = compare.diff(
            _filter(load_records(args.baseline), args.baseline_strategy),
            _filter(load_records(args.candidate), args.candidate_strategy),
            settings.session_timezone,
        )
        return report.write_diff(result, args.out, settings.output_decimal_places)
    replay_settings = load_settings(args.replay_settings)
    clock_for = session_clock(args.config)
    places = replay_settings.output_decimal_places
    if args.command == "whatif":
        quantity = None
        if args.quantity is not None:
            try:
                quantity = Decimal(args.quantity)
            except InvalidOperation as exc:
                raise ResearchError(f"--quantity is not a number: {args.quantity!r}") from exc
        rows = what_if(load_records(args.decisions), load_bars(args.bars), replay_settings,
                       clock_for, quantity)
        return report.write_whatif(rows, args.out, places)
    if args.command == "observers":
        result = observer_report.report(load_records(args.decisions), load_bars(args.bars),
                                        replay_settings, clock_for)
        return report.write_observer_report(result, args.out, places)
    factory = strategy_factory(args.strategy, args.values)
    records = compare.decide(factory, load_bars(args.bars), replay_settings, clock_for)
    args.out.mkdir(parents=True, exist_ok=True)
    write_records(args.out / "decisions.jsonl", records)
    verdicts: dict[str, int] = {}
    for r in records:
        verdicts[r.decision.verdict.value] = verdicts.get(r.decision.verdict.value, 0) + 1
    return {"decisions": len(records), "by_verdict": dict(sorted(verdicts.items()))}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = _run(args)
    except (
        ResearchError,
        ReplayError,
        ConfigError,
        CalendarRefused,
        OSError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"research refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    print(f"outputs in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
