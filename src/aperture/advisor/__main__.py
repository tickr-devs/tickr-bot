"""Command line: `python -m aperture.advisor --days 2026-11-17 ...` (recorded live days) or
`python -m aperture.advisor --backtest <results> --backtest-data <data>` (fitting period of a
SIMULATED backtest). See docs/advisor.md."""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from aperture import config as config_module
from aperture.advisor.client import AnthropicClient
from aperture.advisor.errors import AdvisorError
from aperture.advisor.run import Status, run_advisor, run_backtest_advisor
from aperture.calendar import CalendarRefused
from aperture.observers.errors import ObserverError
from aperture.replay.errors import ReplayError
from aperture.replay.settings import load_settings
from aperture.research.errors import ResearchError
from aperture.research.settings import load_research_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aperture.advisor",
        description="Ask Claude for a proposed next strategy version. Never applied.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--days", nargs="+", help="recorded NY dates, YYYY-MM-DD")
    source.add_argument("--backtest", type=Path,
                        help="results folder of `python -m aperture.backtest run`")
    parser.add_argument("--backtest-data", type=Path,
                        help="its data folder (fetched minutes), required with --backtest")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"))
    parser.add_argument("--research-settings", type=Path, default=Path("config/research.toml"))
    parser.add_argument("--replay-settings", type=Path, default=Path("config/replay.toml"))
    parser.add_argument("--notes", type=Path,
                        help="text file the model also reads, e.g. ideas already rejected")
    parser.add_argument("--strategy-folder", help="folder under strategies/ (default: active)")
    parser.add_argument("--out", type=Path, help="proposals folder (default from [advisor])")
    return parser


def main(argv: Sequence[str] | None = None, client_factory=AnthropicClient) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.backtest is not None and args.backtest_data is None:
        parser.error("--backtest needs --backtest-data")
    if args.backtest is None and args.backtest_data is not None:
        parser.error("--backtest-data goes with --backtest")
    try:
        cfg = config_module.load(args.config)
        notes = args.notes.read_text("utf-8") if args.notes else ""
        if args.backtest is not None:
            replay = load_settings(args.replay_settings)
            result = run_backtest_advisor(cfg, args.backtest, args.backtest_data,
                                          client_factory(cfg.advisor), replay, datetime.now(UTC),
                                          folder=args.strategy_folder, out_dir=args.out,
                                          notes=notes)
        else:
            days = [date.fromisoformat(d) for d in args.days]
            result = run_advisor(
                cfg,
                days,
                client_factory(cfg.advisor),
                load_research_settings(args.research_settings),
                load_settings(args.replay_settings),
                datetime.now(UTC),
                folder=args.strategy_folder,
                out_dir=args.out,
                notes=notes,
            )
    except (AdvisorError, ResearchError, ReplayError, ObserverError, CalendarRefused,
            config_module.ConfigError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"advisor refused: {exc}", file=sys.stderr)
        return 2
    print(f"{result.status.value}: {result.folder / 'report.md'}")
    if result.cause:
        print(result.cause)
    return 0 if result.status is not Status.REFUSED else 2


if __name__ == "__main__":
    raise SystemExit(main())
