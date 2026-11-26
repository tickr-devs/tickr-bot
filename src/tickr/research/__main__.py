"""Command line: `python -m tickr.research <command> ...`. See docs/research.md."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tomllib
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tickr import config as bot_config
from tickr.calendar import CalendarRefused, SessionCalendar
from tickr.config import ConfigError
from tickr.replay.__main__ import platform_sizing, session_clock, strategy_factory
from tickr.replay.bars import load_bars
from tickr.replay.errors import ReplayError
from tickr.replay.settings import load_settings
from tickr.research import (
    calibration,
    compare,
    fill_quality,
    observer_report,
    refusals,
    report,
    slippage,
)
from tickr.research.errors import ResearchError
from tickr.research.latency import latency_report
from tickr.research.outcomes import outcomes
from tickr.research.records import load_records, write_records
from tickr.research.settings import load_research_settings
from tickr.research.whatif import what_if


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tickr.research",
        description="Signal outcomes (observed) and what-if analysis (simulated).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def bars_and_out(p: argparse.ArgumentParser) -> None:
        p.add_argument("--bars", type=Path, nargs="+", required=True, help="JSONL bar files")
        p.add_argument("--out", type=Path, required=True, help="output directory")

    def places_source(p: argparse.ArgumentParser) -> None:
        p.add_argument("--replay-settings", type=Path, default=Path("config/replay.toml"),
                       help="replay settings TOML (output_decimal_places for the reports)")
        p.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                       help="bot config: its [market_data] history_sessions goes to the replay "
                       "settings; the outcomes calendar gives each day's regular close")

    def clock(p: argparse.ArgumentParser) -> None:
        p.add_argument("--replay-settings", type=Path, default=Path("config/replay.toml"),
                       help="replay settings TOML (fill model)")
        p.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                       help="bot config: [session] offsets and the calendar give each day's clock")

    p = sub.add_parser("outcomes", help="price moves after WATCH/ENTER decisions")
    bars_and_out(p)
    p.add_argument("--decisions", type=Path, nargs="+", required=True, help="decision JSONL")
    p.add_argument("--settings", type=Path, required=True, help="research TOML")
    places_source(p)

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
    places_source(p)
    p.add_argument("--out", type=Path, required=True, help="output directory")

    p = sub.add_parser("latency", help="quote latency and stream notice counters from capture")
    p.add_argument("--quotes", type=Path, nargs="+", required=True, help="captured quotes.jsonl")
    p.add_argument("--notices", type=Path, nargs="*", default=[],
                   help="captured notices.jsonl")
    p.add_argument("--source", choices=("stream", "snapshot"), default="stream",
                   help="which captured quotes to measure")
    p.add_argument("--settings", type=Path, required=True, help="research TOML (time zone)")
    places_source(p)
    p.add_argument("--out", type=Path, required=True, help="output directory")

    p = sub.add_parser("refusals", help="per day and setup: first failed checks, refusals, entries")
    p.add_argument("--decisions", type=Path, nargs="+", required=True, help="decision JSONL")
    p.add_argument("--strategy", help="NAME or NAME:VERSION to keep")
    p.add_argument("--settings", type=Path, required=True, help="research TOML (time zone)")
    places_source(p)
    p.add_argument("--out", type=Path, required=True, help="output directory")

    p = sub.add_parser("slippage", help="stopped trades: planned against realized loss, per day")
    p.add_argument("--database", type=Path, required=True, help="the bot's database (read only)")
    p.add_argument("--environment", help="keep only trades of runs in this environment")
    p.add_argument("--settings", type=Path, required=True, help="research TOML (time zone)")
    places_source(p)
    p.add_argument("--out", type=Path, required=True, help="output directory")

    p = sub.add_parser("calibration", help="reliability table by tenths per probability column")
    p.add_argument("--answers", type=Path, required=True,
                   help="CSV of stored model answers: outcome (0/1) and p_<name> columns")
    p.add_argument("--settings", type=Path, required=True, help="research TOML (bucket count)")
    places_source(p)
    p.add_argument("--out", type=Path, required=True, help="output directory")

    p = sub.add_parser("fill-quality", help="entry, exit and stop slippage, fees and fill delay "
                       "of real fills; one record set, or two side by side")
    p.add_argument("--database", type=Path, required=True, help="the bot's database (read only)")
    p.add_argument("--environment", help="keep only trades of runs in this environment")
    p.add_argument("--quotes", type=Path, nargs="*", default=[],
                   help="captured quotes.jsonl for the quote-based figures")
    p.add_argument("--label", default="records", help="name of this set in the outputs")
    p.add_argument("--compare-database", type=Path, help="a second database, shown beside it")
    p.add_argument("--compare-environment", help="environment filter for the second database")
    p.add_argument("--compare-quotes", type=Path, nargs="*", default=[],
                   help="captured quotes.jsonl of the second set")
    p.add_argument("--compare-label", default="compare", help="name of the second set")
    p.add_argument("--quote-source", choices=("stream", "snapshot"), default="stream",
                   help="which captured quotes to use")
    p.add_argument("--settings", type=Path, required=True, help="research TOML (time zone)")
    places_source(p)
    p.add_argument("--out", type=Path, required=True, help="output directory")
    return parser


# Commands whose reports round to the replay settings' output_decimal_places (its only home).
REPORT_COMMANDS = frozenset({"outcomes", "diff", "latency", "refusals", "slippage", "calibration"})


def _fill_quality(args: argparse.Namespace) -> dict:
    settings = load_research_settings(args.settings)
    zone = settings.session_timezone
    replay = load_settings(args.replay_settings, _history_sessions(args))
    pairs = [(args.label, args.database, args.environment, args.quotes)]
    if args.compare_database is not None:
        pairs.append((args.compare_label, args.compare_database, args.compare_environment,
                      args.compare_quotes))
    labels = [label for label, *_ in pairs]
    if len(set(labels)) != len(labels) or not all(
            re.fullmatch(r"[A-Za-z0-9_-]+", label) for label in labels):
        raise ResearchError(f"set labels must differ and be plain names: {labels}")
    sets = []
    for label, database, environment, quote_files in pairs:
        quotes = (fill_quality.Quotes(quote_files, args.quote_source, zone)
                  if quote_files else None)
        sets.append((label, fill_quality.trade_fills(database, zone, quotes, environment),
                     slippage.stopped_trades(database, zone, environment)))
    return report.write_fill_quality(sets, {"slippage_percent": replay.slippage_percent},
                                     args.out, replay.output_decimal_places)


def _filter(records, spec: str | None):
    if spec is None:
        return records
    name, _, version = spec.partition(":")
    return compare.select(records, name, version or None)


def _history_sessions(args: argparse.Namespace) -> int:
    """The bot's `[market_data] history_sessions`: the replay settings do not repeat it."""
    return bot_config.load(args.config).market_data.history_sessions


def _run(args: argparse.Namespace) -> dict:
    if args.command in REPORT_COMMANDS:
        places = load_settings(args.replay_settings, _history_sessions(args)).output_decimal_places
    if args.command == "outcomes":
        settings = load_research_settings(args.settings)
        cfg = bot_config.load(args.config)
        calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
        rows = outcomes(load_records(args.decisions), load_bars(args.bars), settings,
                        lambda day: calendar.day(day).regular_close)
        return report.write_outcomes(rows, args.out, places)
    if args.command == "diff":
        settings = load_research_settings(args.settings)
        result = compare.diff(
            _filter(load_records(args.baseline), args.baseline_strategy),
            _filter(load_records(args.candidate), args.candidate_strategy),
            settings.session_timezone,
        )
        return report.write_diff(result, args.out, places)
    if args.command == "latency":
        settings = load_research_settings(args.settings)
        result = latency_report(args.quotes, args.notices, args.source,
                                settings.session_timezone)
        return report.write_latency(result, args.source, args.out,
                                    places)
    if args.command == "refusals":
        settings = load_research_settings(args.settings)
        rows = refusals.table(_filter(load_records(args.decisions), args.strategy),
                              settings.session_timezone)
        return report.write_refusals(rows, args.out, places)
    if args.command == "calibration":
        settings = load_research_settings(args.settings)
        reports = calibration.calibration(args.answers, settings.calibration_buckets)
        return report.write_calibration(reports, args.out, places)

    if args.command == "fill-quality":
        return _fill_quality(args)
    if args.command == "slippage":
        settings = load_research_settings(args.settings)
        trades = slippage.stopped_trades(args.database, settings.session_timezone,
                                         args.environment)
        return report.write_slippage(trades, args.environment, args.out,
                                     places)
    replay_settings = load_settings(args.replay_settings, _history_sessions(args))
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
    records = compare.decide(factory, load_bars(args.bars), replay_settings, clock_for,
                             platform_sizing(args.config))
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
        sqlite3.Error,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"research refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    print(f"outputs in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
