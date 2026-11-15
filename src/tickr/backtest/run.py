"""The backtest run: every pool (symbol, session) whose history is complete is replayed through
the replay engine with the strategy behind the gainer gate (gate.py), one process per symbol.

A pool day is skipped, with its cause in `skipped.csv`, when the symbol's M1 file lacks the day
or one of the `history_sessions` sessions before it, or the fetch recorded that run incomplete:
live, the bot refuses a symbol whose prior-session history is incomplete, so it never trades it.
A symbol whose replay the engine refuses (a bad bar, such as a 0.01 USD print that makes the
synthetic bid non-positive) is replayed again one pool day at a time on only that day's bars,
and only the days that are still refused are skipped (`REPLAY_REFUSED`), never the whole run.
"""

from __future__ import annotations

import csv
import importlib
import tomllib
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from tickr.backtest import fetch, report
from tickr.backtest.errors import BacktestError
from tickr.backtest.gate import GainerGate
from tickr.backtest.sessions import sessions_before
from tickr.backtest.settings import BacktestSettings
from tickr.backtest.universe import CAP_FILE, POOL_FILE, PoolEntry, read_caps, read_pool
from tickr.calendar import SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.replay.bars import load_bars
from tickr.replay.clock import calendar_clock
from tickr.replay.engine import ReplayResult, replay
from tickr.replay.errors import ReplayError
from tickr.replay.report import write_all
from tickr.replay.settings import ReplaySettings
from tickr.strategy.base import Strategy

SKIPPED_FILE = "skipped.csv"


@dataclass(frozen=True)
class Task:
    symbol: str
    bars_file: Path
    pool_days: tuple[date, ...]
    strategy_module: str
    values: dict
    change_percent: Decimal
    calendar_dir: Path
    cfg: Config
    settings: ReplaySettings


def factory(module_name: str, values: dict) -> Callable[[], Strategy]:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise BacktestError(f"strategy module {module_name} cannot be imported: {exc}") from exc
    create = getattr(module, "create", None)
    if not callable(create):
        raise BacktestError(f"strategy module {module_name} has no create(values)")
    try:
        create(values)
    except ValueError as exc:
        raise BacktestError(f"strategy values refused by {module_name}: {exc}") from exc
    return lambda: create(values)


def run_symbol(task: Task) -> tuple[ReplayResult | None, list[dict[str, str]]]:
    """The symbol's replay and the pool days it had to skip (see the module docstring)."""
    create = factory(task.strategy_module, task.values)
    bars = load_bars([task.bars_file])
    if set(bars) != {task.symbol}:
        raise BacktestError(f"{task.bars_file}: holds {sorted(bars)}, not {task.symbol}")
    calendar = SessionCalendar.load(task.calendar_dir)
    clock = calendar_clock(calendar, task.cfg.session)

    def one(days: tuple[date, ...], series: tuple) -> ReplayResult:
        pool = [(task.symbol, d) for d in days]
        return replay(lambda: GainerGate(create(), pool, task.change_percent),
                      {task.symbol: series}, task.settings, clock)

    try:
        return one(task.pool_days, bars[task.symbol]), []
    except ReplayError:
        pass
    parts: list[ReplayResult] = []
    skipped: list[dict[str, str]] = []
    lookback = task.cfg.market_data.history_max_lookback_days
    for day in task.pool_days:
        wanted = {d.ny_date for d in sessions_before(calendar, day, task.settings.history_sessions,
                                                      lookback)} | {day}
        series = tuple(b for b in bars[task.symbol] if ny_date_of(b.start) in wanted)
        try:
            parts.append(one((day,), series))
        except ReplayError as exc:
            skipped.append({"symbol": task.symbol, "session_date": day.isoformat(),
                            "cause": f"REPLAY_REFUSED: {exc}"})
    if not parts:
        return None, skipped
    # The per-day replays overlap in their history sessions: count the symbol's file once.
    series = bars[task.symbol]
    return replace(merge(parts), sessions=len({ny_date_of(b.start) for b in series}),
                   bars=len(series)), skipped


def tradable_days(cfg: Config, calendar: SessionCalendar, pool: list[PoolEntry], out: Path,
                  history_sessions: int, tolerance_percent: Decimal,
                  ) -> tuple[dict[str, list[date]], list[dict[str, str]]]:
    """Pool days per symbol that can be replayed as live would see them, and the skipped ones."""
    incomplete: dict[str, set[date]] = defaultdict(set)
    for status in fetch.load_statuses(out):
        if not status["complete"]:
            first = date.fromisoformat(status["first_session"])
            last = date.fromisoformat(status["last_session"])
            incomplete[status["symbol"]].update(
                d.ny_date for d in fetch.sessions_between(calendar, first, last))
    session_days: dict[str, dict[date, Decimal]] = {}
    days: dict[str, list[date]] = defaultdict(list)
    skipped: list[dict[str, str]] = []
    lookback = cfg.market_data.history_max_lookback_days
    for entry in pool:
        symbol, day = entry.symbol, entry.session_date
        path = out / fetch.MINUTES_DIR / f"{symbol}.jsonl"
        if not path.exists():
            skipped.append({"symbol": symbol, "session_date": day.isoformat(),
                            "cause": "NO_MINUTE_FILE"})
            continue
        if day in incomplete.get(symbol, set()):
            skipped.append({"symbol": symbol, "session_date": day.isoformat(),
                            "cause": "FETCH_INCOMPLETE"})
            continue
        if symbol not in session_days:
            session_days[symbol] = _last_closes(path)
        closes = session_days[symbol]
        wanted = [d.ny_date for d in sessions_before(calendar, day, history_sessions, lookback)]
        missing = [d for d in [*wanted, day] if d not in closes]
        if missing:
            skipped.append({"symbol": symbol, "session_date": day.isoformat(),
                            "cause": "SESSION_WITHOUT_BARS: "
                            + " ".join(d.isoformat() for d in missing)})
            continue
        # Daily bars are forward-adjusted, M1 bars are not: on the day's M1 scale the pool's
        # previous close matches the previous session's last M1 close unless a split (or a
        # dividend above the tolerance) falls between them.
        if wanted:
            expected = entry.previous_close_on_minute_scale(closes[day])
            raw = closes[wanted[-1]]
            if abs(expected / raw - 1) * 100 > tolerance_percent:
                skipped.append({"symbol": symbol, "session_date": day.isoformat(),
                                "cause": f"SPLIT_BETWEEN_SESSIONS: previous close {raw} on M1, "
                                f"{expected:.4f} on the day's M1 scale"})
                continue
        days[symbol].append(day)
    return dict(days), skipped


def _last_closes(path: Path) -> dict[date, Decimal]:
    """Last M1 close of every session date in the file (bars are oldest first)."""
    closes: dict[date, Decimal] = {}
    for series in load_bars([path]).values():
        for bar in series:
            closes[ny_date_of(bar.start)] = bar.close
    return closes


def last_sessions(calendar: SessionCalendar, pool: list[PoolEntry], count: int,
                  max_lookback_days: int) -> tuple[list[PoolEntry], list[date]]:
    """The pool cut to the `count` latest open sessions that end at its last day, and those
    sessions (oldest first). Sessions without a pool day stay counted: the window is calendar
    sessions, not days that happened to have gainers."""
    if count <= 0:
        raise BacktestError(f"--last-sessions must be positive, got {count}")
    if not pool:
        raise BacktestError("the pool is empty")
    last = max(e.session_date for e in pool)
    before = sessions_before(calendar, last, count - 1, max_lookback_days) if count > 1 else []
    sessions = [d.ny_date for d in before] + [last]
    kept = set(sessions)
    return [e for e in pool if e.session_date in kept], sessions


def run_backtest(cfg: Config, settings: BacktestSettings, replay_settings: ReplaySettings,
                 out: Path, results: Path, strategy_module: str, values_path: Path,
                 last_session_count: int | None = None) -> None:
    calendar_dir = (cfg.root / cfg.session.calendar_file).parent
    calendar = SessionCalendar.load(calendar_dir)
    with open(values_path, "rb") as handle:
        values = tomllib.load(handle)
    factory(strategy_module, values)  # refuse bad values before any work
    pool = read_pool(out / POOL_FILE)
    window: list[date] | None = None
    if last_session_count is not None:
        pool, window = last_sessions(calendar, pool, last_session_count,
                                     cfg.market_data.history_max_lookback_days)
    days, skipped = tradable_days(cfg, calendar, pool, out, replay_settings.history_sessions,
                                  settings.split_tolerance_percent)
    caps = read_caps(out / CAP_FILE)
    if window is not None:
        caps = [c for c in caps if c.session_date in set(window)]
    tasks = [Task(symbol, out / fetch.MINUTES_DIR / f"{symbol}.jsonl", tuple(sorted(d)),
                  strategy_module, values, settings.gainer_change_percent, calendar_dir,
                  cfg, replay_settings) for symbol, d in sorted(days.items())]
    if not tasks:
        raise BacktestError("no pool day has complete history")
    print(f"run: {sum(len(t.pool_days) for t in tasks)} symbol-days over {len(tasks)} symbols, "
          f"{len(skipped)} skipped, {settings.workers} workers")
    with ProcessPoolExecutor(max_workers=settings.workers) as pool_executor:
        outcomes = list(pool_executor.map(run_symbol, tasks))
    parts = [part for part, _ in outcomes if part is not None]
    refused = [row for _, rows in outcomes for row in rows]
    skipped = skipped + refused
    if not parts:
        raise BacktestError("every pool day was refused by the replay engine")
    merged = merge(parts)
    results.mkdir(parents=True, exist_ok=True)
    write_all(merged, results, replay_settings.output_decimal_places)
    with open(results / SKIPPED_FILE, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("symbol", "session_date", "cause"))
        writer.writeheader()
        writer.writerows(skipped)
    traded_days = sum(len(t.pool_days) for t in tasks) - len(refused)
    recent = None
    if window is not None:
        gone = {(r["symbol"], r["session_date"]) for r in refused}
        replayed = {d for s, ds in days.items() for d in ds if (s, d.isoformat()) not in gone}
        recent = report.LastSessions(last_session_count, window[0], window[-1],
                                     sum(1 for d in window if d not in replayed))
    text = report.write_report(merged, pool, caps, traded_days, skipped, cfg, settings,
                               replay_settings, results, recent)
    print(text)


def merge(parts: list[ReplayResult]) -> ReplayResult:
    verdicts: dict[str, int] = {}
    for part in parts:
        for verdict, count in part.verdict_counts.items():
            verdicts[verdict] = verdicts.get(verdict, 0) + count
    first = parts[0]
    return ReplayResult(
        strategy=first.strategy, version=first.version,
        trades=tuple(sorted((t for p in parts for t in p.trades),
                            key=lambda t: (t.entry_time, t.symbol))),
        signals=tuple(sorted((s for p in parts for s in p.signals),
                             key=lambda s: (s.bar_start, s.symbol))),
        verdict_counts=dict(sorted(verdicts.items())),
        sessions=sum(p.sessions for p in parts), bars=sum(p.bars for p in parts))
