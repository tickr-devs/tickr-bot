"""Command line: `python -m aperture.replay ...`. See docs/replay.md."""

from __future__ import annotations

import argparse
import importlib
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from aperture.replay.bars import load_bars
from aperture.replay.engine import replay
from aperture.replay.errors import ReplayError, SettingsError
from aperture.replay.report import render_summary, write_all
from aperture.replay.settings import load_settings, parse_clock
from aperture.strategy.base import Strategy


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aperture.replay",
        description="SIMULATED bar-by-bar replay of a strategy over historical M1 bars.",
    )
    parser.add_argument("--bars", type=Path, nargs="+", required=True, help="JSONL bar files")
    parser.add_argument("--strategy", required=True, help="strategy module exposing create()")
    parser.add_argument("--values", type=Path, required=True, help="strategy.toml for create()")
    parser.add_argument("--settings", type=Path, required=True, help="replay settings TOML")
    parser.add_argument("--last-entry", required=True, help="last entry time, HH:MM exchange")
    parser.add_argument("--flatten", required=True, help="flatten time, HH:MM exchange")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.settings)
        last_entry = parse_clock("--last-entry", args.last_entry)
        flatten = parse_clock("--flatten", args.flatten)
        factory = strategy_factory(args.strategy, args.values)
        bars = load_bars(args.bars)
        result = replay(factory, bars, settings, last_entry, flatten)
        summary = write_all(result, args.out, settings.output_decimal_places)
    except (ReplayError, SettingsError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"replay refused: {exc}", file=sys.stderr)
        return 2
    print(render_summary(summary, settings.output_decimal_places))
    print(f"outputs in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
