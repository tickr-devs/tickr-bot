"""Command line: `python -m aperture.backtest <step> ...`. See docs/backtest.md.

Fetch steps read the configured environment's market data (read-only) and need its
credentials; `select` and `run` are offline."""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from aperture import config
from aperture.backtest import fetch
from aperture.backtest.errors import BacktestError
from aperture.backtest.sessions import sessions_before
from aperture.backtest.settings import BacktestSettings, load_settings
from aperture.backtest.universe import (
    CAP_FILE,
    POOL_FILE,
    read_pool,
    select_pool,
    write_caps,
    write_pool,
)
from aperture.broker.base import BrokerError
from aperture.calendar import CalendarRefused, SessionCalendar
from aperture.config import Config, ConfigError
from aperture.data.base import MarketDataError
from aperture.replay.errors import ReplayError, SettingsError
from aperture.replay.settings import load_settings as load_replay_settings


def _calendar(cfg: Config) -> SessionCalendar:
    return SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)


def _history(cfg: Config, out: Path) -> fetch.HistorySource:
    from aperture.broker.webull import history_from_config

    return history_from_config(cfg, raw_dir=out / "raw")


def step_select(cfg: Config, settings: BacktestSettings, out: Path, first: date,
                last: date) -> Path:
    window = fetch.daily_window(_calendar(cfg), first, last,
                                cfg.market_data.history_max_lookback_days)
    rows = fetch.load_daily(out / fetch.DAILY_DIR)
    pool, caps = select_pool(rows, [d.ny_date for d in window], settings.gainer_change_percent,
                       cfg.market_data.min_session_dollar_volume_usd, settings.max_pool_per_day)
    path = out / POOL_FILE
    write_pool(pool, path)
    write_caps(caps, out / CAP_FILE)
    bound = [c for c in caps if c.dropped]
    if bound:
        print(f"pool cap {settings.max_pool_per_day} bound on {len(bound)} sessions, "
              f"{sum(c.dropped for c in bound)} symbol-days dropped (look-ahead: raise the cap)")
    days = len({e.session_date for e in pool})
    print(f"pool: {len(pool)} symbol-days over {days} sessions, "
          f"{len({e.symbol for e in pool})} symbols -> {path}")
    return path


def runs_from_pool(cfg: Config, out: Path, history_sessions: int) -> dict[str, list]:
    calendar = _calendar(cfg)
    pool = read_pool(out / POOL_FILE)
    if not pool:
        raise BacktestError("pool is empty")
    first, last = min(e.session_date for e in pool), max(e.session_date for e in pool)
    lookback = cfg.market_data.history_max_lookback_days
    before = sessions_before(calendar, first, history_sessions, lookback) \
        if history_sessions else []
    window = before + fetch.sessions_between(calendar, first, last)
    days_by_symbol: dict[str, set[date]] = {}
    for entry in pool:
        days_by_symbol.setdefault(entry.symbol, set()).add(entry.session_date)
    return {s: fetch.needed_runs(days, window, history_sessions)
            for s, days in days_by_symbol.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aperture.backtest",
                                     description="SIMULATED backtest over past gainers.")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"))
    parser.add_argument("--settings", type=Path, default=Path("config/backtest.toml"))
    parser.add_argument("--replay-settings", type=Path, default=Path("config/replay.toml"))
    sub = parser.add_subparsers(dest="step", required=True)
    for name, needs_window in (("fetch-universe", False), ("fetch-daily", True),
                               ("select", True), ("fetch-minutes", False), ("fetch-all", True)):
        step = sub.add_parser(name)
        step.add_argument("--out", type=Path, required=True, help="data directory")
        if needs_window:
            step.add_argument("--first", type=date.fromisoformat, required=True)
            step.add_argument("--last", type=date.fromisoformat, required=True)
    run = sub.add_parser("run")
    run.add_argument("--out", type=Path, required=True, help="data directory (fetched)")
    run.add_argument("--results", type=Path, required=True, help="output directory")
    run.add_argument("--strategy", required=True, help="strategy module exposing create()")
    run.add_argument("--values", type=Path, required=True, help="strategy.toml for create()")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config.load(args.config)
        settings = load_settings(args.settings)
        replay_settings = load_replay_settings(args.replay_settings)
        out: Path = args.out
        if args.step == "run":
            from aperture.backtest.run import run_backtest

            run_backtest(cfg, settings, replay_settings, out, args.results, args.strategy,
                         args.values)
            return 0
        if args.step == "select":
            step_select(cfg, settings, out, args.first, args.last)
            return 0
        source = _history(cfg, out)
        if args.step in ("fetch-universe", "fetch-all"):
            symbols = fetch.fetch_universe(source, cfg, settings, out)
        if args.step in ("fetch-daily", "fetch-all"):
            if args.step == "fetch-daily":
                symbols = [r["symbol"] for r in fetch.read_lines(out / fetch.UNIVERSE_FILE)]
            fetch.fetch_daily(source, _calendar(cfg), cfg, symbols, args.first, args.last, out)
        if args.step == "fetch-all":
            step_select(cfg, settings, out, args.first, args.last)
        if args.step in ("fetch-minutes", "fetch-all"):
            runs = runs_from_pool(cfg, out, replay_settings.history_sessions)
            fetch.fetch_minutes(source, cfg, settings, runs, out)
        print(f"{args.step}: done, {source.requests} requests")
    except (BacktestError, BrokerError, MarketDataError, ConfigError, CalendarRefused,
            ReplayError, SettingsError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"backtest refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
