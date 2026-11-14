"""Command line: `python -m aperture.advisor --days 2026-11-17 ... `. See docs/advisor.md."""

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
from aperture.advisor.run import Status, run_advisor
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
    parser.add_argument("--days", nargs="+", required=True, help="recorded NY dates, YYYY-MM-DD")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"))
    parser.add_argument("--research-settings", type=Path, default=Path("config/research.toml"))
    parser.add_argument("--replay-settings", type=Path, default=Path("config/replay.toml"))
    parser.add_argument("--strategy-folder", help="folder under strategies/ (default: active)")
    parser.add_argument("--out", type=Path, help="proposals folder (default from [advisor])")
    return parser


def main(argv: Sequence[str] | None = None, client_factory=AnthropicClient) -> int:
    args = _parser().parse_args(argv)
    try:
        days = [date.fromisoformat(d) for d in args.days]
        cfg = config_module.load(args.config)
        result = run_advisor(
            cfg,
            days,
            client_factory(cfg.advisor),
            load_research_settings(args.research_settings),
            load_settings(args.replay_settings),
            datetime.now(UTC),
            folder=args.strategy_folder,
            out_dir=args.out,
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
