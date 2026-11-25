"""Command line: `python -m tickr.replay ...`. See docs/replay.md."""

from __future__ import annotations

import argparse
import importlib
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from tickr import config
from tickr.calendar import TIMEZONE_NAME, CalendarRefused, SessionCalendar
from tickr.config import ConfigError
from tickr.replay.bars import load_bars
from tickr.replay.clock import ClockFor, calendar_clock
from tickr.replay.engine import replay
from tickr.replay.errors import ReplayError, SettingsError
from tickr.replay.report import render_summary, write_all
from tickr.replay.settings import load_settings
from tickr.strategy.base import SizingLimits, Strategy


def strategy_factory(module_name: str, values_path: Path) -> Callable[[], Strategy]:
    """Return a factory calling `<module>.create(values)` with the parsed strategy TOML."""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ReplayError(f"strategy module {module_name} cannot be imported: {exc}") from exc
    create = getattr(module, "create", None)
    if not callable(create):
        raise ReplayError(f"strategy module {module_name} has no create(values)")
    with open(values_path, "rb") as handle:
        values = tomllib.load(handle)
    try:
        create(values)
    except ValueError as exc:
        raise ReplayError(f"strategy values refused by {module_name}: {exc}") from exc
    return lambda: create(values)


def platform_sizing(config_path: Path) -> SizingLimits:
    """The bot's own sizing limits, bot.toml `[risk]` (one source of truth, owner 2026-11-15)."""
    return SizingLimits.of(config.load(config_path).risk)


def session_clock(config_path: Path) -> ClockFor:
    """Each day's clock from bot.toml `[session]` and the calendar it names."""
    cfg = config.load(config_path)
    if cfg.session.timezone != TIMEZONE_NAME:
        raise ReplayError(
            f"[session] timezone {cfg.session.timezone!r} is not the calendar's {TIMEZONE_NAME!r}"
        )
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    return calendar_clock(calendar, cfg.session)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tickr.replay",
        description="SIMULATED bar-by-bar replay of a strategy over historical M1 bars.",
    )
    parser.add_argument("--bars", type=Path, nargs="+", required=True, help="JSONL bar files")
    parser.add_argument("--strategy", required=True, help="strategy module exposing create()")
    parser.add_argument("--values", type=Path, required=True, help="strategy.toml for create()")
    parser.add_argument(
        "--settings", type=Path, default=Path("config/replay.toml"), help="replay settings TOML"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/bot.toml"),
        help="bot config: [session] offsets and the calendar give each day's clock; [risk] the "
        "planned loss and notional cap the strategy sizes on",
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.settings)
        clock_for = session_clock(args.config)
        factory = strategy_factory(args.strategy, args.values)
        bars = load_bars(args.bars)
        result = replay(factory, bars, settings, clock_for, platform_sizing(args.config))
        summary = write_all(result, args.out, settings.output_decimal_places)
    except (
        ReplayError,
        SettingsError,
        ConfigError,
        CalendarRefused,
        OSError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"replay refused: {exc}", file=sys.stderr)
        return 2
    print(render_summary(summary, settings.output_decimal_places))
    print(f"outputs in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
