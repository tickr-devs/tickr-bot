"""Evidence bundle from a SIMULATED backtest run (`python -m tickr.backtest run`).

Reads the run's `report.json`, `trades.csv` and `signals.csv` (docs/backtest.md) and the
fetched M1 bars of the traded symbols. Hold-out rule (docs/advisor.md): the report splits the
window at `holdout.starts`; only sessions BEFORE it (the fitting period) enter the bundle. No
number of the hold-out is read into it, not even its dates, so the model cannot fit to it; the
hold-out later judges the proposal (advisor.judge).

What goes in, fitting period only, aggregates only (no rows):
- signal outcomes and the causes of signals that did not fill;
- trade statistics overall and by month, setup, exit reason, entry hour (New York), entry
  price, stop distance;
- excursions in R from the entry: the best high while a trade was open (did it ever work?)
  and, for stop exits, the best high after the stop until the session end (was the stop the
  problem or the entry?). The replay stamps a trade with the starts of its entry and exit
  bars; the entry bar counts as open, the exit bar counts for neither (inside it the order of
  high and stop is unknown), so both measures lean low. A trade closed in its entry bar has
  no open bars and is counted apart.
Any missing file, column, mismatched identity or unparsable value is refused with its cause.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr.advisor.errors import AdvisorError
from tickr.backtest.report import stats
from tickr.calendar import NEW_YORK, ny_date_of
from tickr.config import AdvisorConfig
from tickr.replay.bars import load_bars
from tickr.replay.errors import ReplayError
from tickr.types import Bar

REPORT_JSON = "report.json"
TRADES_CSV = "trades.csv"
SIGNALS_CSV = "signals.csv"
MINUTES_DIR = "minutes"
LABEL = "simulated"
STOP_EXIT = "stop"
TRADE_COLUMNS = frozenset({
    "label", "strategy", "version", "symbol", "session_date", "setup", "entry_time",
    "entry_price", "stop", "exit_time", "exit_price", "exit_reason", "bars_held",
})
SIGNAL_COLUMNS = frozenset({"label", "strategy", "version", "session_date", "outcome", "cause"})
FILLED = "filled"
ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class BacktestRun:
    """The parts of one backtest results folder the advisor and the judge use."""

    folder: Path
    strategy: str
    version: str
    window: tuple[date, date]
    holdout_starts: date
    report: dict[str, Any]


@dataclass(frozen=True)
class Trade:
    symbol: str
    session_date: date
    setup: str
    entry_time: datetime
    entry_price: Decimal
    stop: Decimal
    exit_time: datetime
    exit_price: Decimal
    exit_reason: str

    @property
    def risk(self) -> Decimal:
        return self.entry_price - self.stop

    @property
    def r(self) -> Decimal:
        return (self.exit_price - self.entry_price) / self.risk


def read_run(folder: Path) -> BacktestRun:
    """report.json of a backtest results folder, checked for what the advisor relies on."""
    path = folder / REPORT_JSON
    try:
        report = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisorError(f"cannot read {path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("label") != LABEL:
        raise AdvisorError(f"{path}: not a {LABEL} backtest report")
    holdout = report.get("holdout")
    window = report.get("window")
    try:
        strategy, version = str(report["strategy"]), str(report["version"])
        first, last = (date.fromisoformat(d) for d in window)
        starts = date.fromisoformat(holdout["starts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AdvisorError(f"{path}: needs strategy, version, window and holdout.starts "
                           f"(a window of at least two sessions): {exc!r}") from exc
    if not first < starts <= last:
        raise AdvisorError(f"{path}: hold-out start {starts} is not inside the window "
                           f"{first}..{last}")
    return BacktestRun(folder, strategy, version, (first, last), starts, report)


def _rows(path: Path, columns: frozenset[str]) -> list[dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = columns - set(reader.fieldnames or ())
            if missing:
                raise AdvisorError(f"{path}: missing columns {sorted(missing)}")
            return list(reader)
    except OSError as exc:
        raise AdvisorError(f"cannot read {path}: {exc}") from exc


def _checked(path: Path, run: BacktestRun, rows: list[dict[str, str]]) -> None:
    for number, row in enumerate(rows, start=2):
        if (row["label"], row["strategy"], row["version"]) != (LABEL, run.strategy, run.version):
            raise AdvisorError(f"{path} line {number}: not a {LABEL} row of {run.strategy} "
                               f"v{run.version}")


def _trade(path: Path, number: int, row: Mapping[str, str]) -> Trade:
    try:
        trade = Trade(
            symbol=row["symbol"],
            session_date=date.fromisoformat(row["session_date"]),
            setup=row["setup"] or "none",
            entry_time=datetime.fromisoformat(row["entry_time"]),
            entry_price=Decimal(row["entry_price"]),
            stop=Decimal(row["stop"]),
            exit_time=datetime.fromisoformat(row["exit_time"]),
            exit_price=Decimal(row["exit_price"]),
            exit_reason=row["exit_reason"],
        )
    except (ValueError, InvalidOperation) as exc:
        raise AdvisorError(f"{path} line {number}: {exc!r}") from exc
    if trade.entry_time.tzinfo is None or trade.exit_time.tzinfo is None:
        raise AdvisorError(f"{path} line {number}: times need a UTC offset")
    if trade.risk <= 0:
        raise AdvisorError(f"{path} line {number}: stop {trade.stop} is not below the entry "
                           f"{trade.entry_price}")
    return trade


def fitting_trades(run: BacktestRun) -> list[Trade]:
    path = run.folder / TRADES_CSV
    rows = _rows(path, TRADE_COLUMNS)
    _checked(path, run, rows)
    trades = [_trade(path, n, row) for n, row in enumerate(rows, start=2)]
    return [t for t in trades if t.session_date < run.holdout_starts]


def fitting_signals(run: BacktestRun) -> list[dict[str, str]]:
    path = run.folder / SIGNALS_CSV
    rows = _rows(path, SIGNAL_COLUMNS)
    _checked(path, run, rows)
    try:
        return [r for r in rows if date.fromisoformat(r["session_date"]) < run.holdout_starts]
    except ValueError as exc:
        raise AdvisorError(f"{path}: {exc!r}") from exc


def bucket(value: Decimal, edges: Sequence[Decimal]) -> str:
    if value < edges[0]:
        return f"<{edges[0]}"
    for low, high in zip(edges, edges[1:], strict=False):
        if low <= value < high:
            return f"{low}..{high}"
    return f">={edges[-1]}"


def _breakdown(trades: Sequence[Trade], key: Callable[[Trade], str]) -> dict[str, Any]:
    groups: dict[str, list[Decimal]] = defaultdict(list)
    for trade in trades:
        groups[key(trade)].append(trade.r)
    return {k: stats(v) for k, v in sorted(groups.items())}


def _counts(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def excursions(trades: Sequence[Trade], bars_by_symbol: Mapping[str, Sequence[Bar]],
               edges: Sequence[Decimal]) -> dict[str, Any]:
    """Best high while open and, for stop exits, after the stop until the session end, in R
    from the entry, bucketed (see the module docstring for which bars count)."""
    open_best: list[str] = []
    after_stop: list[str] = []
    no_bars = 0
    for trade in trades:
        bars = bars_by_symbol.get(trade.symbol, ())
        held = [b.high for b in bars if trade.entry_time <= b.start < trade.exit_time]
        if not held:
            no_bars += 1
        else:
            open_best.append(bucket((max(held) - trade.entry_price) / trade.risk, edges))
        if trade.exit_reason == STOP_EXIT:
            later = [b.high for b in bars if b.start > trade.exit_time
                     and ny_date_of(b.start) == trade.session_date]
            if later:
                after_stop.append(bucket((max(later) - trade.entry_price) / trade.risk, edges))
    return {
        "best_high_while_open_r": _counts(open_best),
        "trades_closed_in_entry_bar_or_without_bars": no_bars,
        "best_high_after_stop_exit_until_session_end_r": _counts(after_stop),
    }


def load_bars_for(data: Path, symbols: Sequence[str]) -> dict[str, tuple[Bar, ...]]:
    """M1 bars of the traded symbols from the backtest data folder (`minutes/<SYMBOL>.jsonl`)."""
    paths = [data / MINUTES_DIR / f"{symbol}.jsonl" for symbol in sorted(set(symbols))]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise AdvisorError(f"minute bars missing for traded symbols: {missing[:3]} "
                           f"({len(missing)} files)")
    try:
        return load_bars(paths) if paths else {}
    except ReplayError as exc:
        raise AdvisorError(f"backtest bars refused: {exc}") from exc


def _plain(value: Any, places: int) -> Any:
    if isinstance(value, Decimal):
        return str(round(value, places))
    if isinstance(value, dict):
        return {k: _plain(v, places) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v, places) for v in value]
    return value


def build_backtest_evidence(run: BacktestRun, trades: Sequence[Trade],
                            signals: Sequence[dict[str, str]],
                            bars_by_symbol: Mapping[str, Sequence[Bar]],
                            settings: AdvisorConfig, places: int) -> dict[str, Any]:
    if len(trades) < settings.backtest_min_fitting_trades:
        raise AdvisorError(
            f"{len(trades)} trades before the hold-out, [advisor] backtest_min_fitting_trades is "
            f"{settings.backtest_min_fitting_trades}")
    days = sorted({s["session_date"] for s in signals} | {t.session_date.isoformat()
                                                          for t in trades})
    not_filled = [s["outcome"] + ": " + s["cause"].split(":")[0]
                  for s in signals if s["outcome"] != FILLED]
    by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for trade in trades:
        by_day[trade.session_date] += trade.r
    bundle = {
        "strategy": f"{run.strategy} v{run.version}",
        "source": "simulated backtest over past gainers (docs/backtest.md)",
        "fitting_period": [days[0], days[-1]] if days else None,
        "sessions_with_signals": len(days),
        "signals_by_outcome": _counts([s["outcome"] for s in signals]),
        "not_filled_signal_causes": dict(Counter(not_filled).most_common()),
        "trades": stats([t.r for t in trades]),
        "positive_days_share_percent": Decimal(sum(1 for v in by_day.values() if v > 0))
        / len(by_day) * HUNDRED,
        "by_month": _breakdown(trades, lambda t: t.session_date.strftime("%Y-%m")),
        "by_setup": _breakdown(trades, lambda t: t.setup),
        "by_exit_reason": _breakdown(trades, lambda t: t.exit_reason),
        "by_entry_hour_new_york": _breakdown(
            trades, lambda t: f"{t.entry_time.astimezone(NEW_YORK).hour:02d}"),
        "by_entry_price_usd": _breakdown(
            trades, lambda t: bucket(t.entry_price, settings.backtest_price_edges_usd)),
        "by_stop_distance_percent": _breakdown(
            trades, lambda t: bucket(t.risk / t.entry_price * HUNDRED,
                                     settings.backtest_stop_distance_edges_percent)),
        "excursions": excursions(trades, bars_by_symbol, settings.backtest_excursion_edges_r),
        "notes": [
            "every number is SIMULATED (replay fill model: window top + slippage, stop first, "
            "synthetic quotes, gross of commissions); it proves nothing about real fills",
            "only the fitting period is shown; the most recent sessions are withheld and will "
            "judge the proposal",
            "R = (exit - entry) / (entry - stop)",
        ],
    }
    return _plain(bundle, places)
