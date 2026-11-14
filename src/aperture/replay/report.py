"""Write replay outputs: per-trade CSV, per-signal CSV and a summary in R and percent.

Every row and the summary carry the label `simulated`. Numbers are rounded only here, at the
output boundary, to `output_decimal_places`; the simulation itself keeps full precision.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from aperture.replay.engine import LABEL, ReplayResult, SignalOutcome

TRADE_COLUMNS = (
    "label",
    "strategy",
    "version",
    "symbol",
    "session_date",
    "setup",
    "signal_bar_start",
    "entry_time",
    "entry_price",
    "stop",
    "exit_time",
    "exit_price",
    "exit_reason",
    "bars_held",
    "r_multiple",
    "return_percent",
)

SIGNAL_COLUMNS = (
    "label",
    "strategy",
    "version",
    "symbol",
    "session_date",
    "bar_start",
    "setup",
    "entry_low",
    "entry_high",
    "stop",
    "outcome",
    "cause",
)


@dataclass(frozen=True)
class Summary:
    strategy: str
    version: str
    sessions: int
    bars: int
    signals: int
    signals_by_outcome: dict[str, int]
    trades: int
    wins: int
    losses: int
    win_rate_percent: Decimal | None
    total_r: Decimal
    average_r: Decimal | None
    max_drawdown_r: Decimal
    total_return_percent: Decimal
    average_return_percent: Decimal | None
    exits_by_reason: dict[str, int]
    verdict_counts: dict[str, int]


def summarize(result: ReplayResult) -> Summary:
    trades = result.trades
    count = len(trades)
    r_values = [t.r_multiple for t in trades]
    pct_values = [t.return_percent for t in trades]
    wins = sum(1 for r in r_values if r > 0)
    losses = sum(1 for r in r_values if r < 0)
    equity = peak = drawdown = Decimal(0)
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    outcomes = {o.value: 0 for o in SignalOutcome}
    for signal in result.signals:
        outcomes[signal.outcome.value] += 1
    reasons: dict[str, int] = {}
    for trade in trades:
        reasons[trade.exit_reason.value] = reasons.get(trade.exit_reason.value, 0) + 1
    return Summary(
        strategy=result.strategy,
        version=result.version,
        sessions=result.sessions,
        bars=result.bars,
        signals=len(result.signals),
        signals_by_outcome=outcomes,
        trades=count,
        wins=wins,
        losses=losses,
        win_rate_percent=Decimal(wins) / count * 100 if count else None,
        total_r=sum(r_values, Decimal(0)),
        average_r=sum(r_values, Decimal(0)) / count if count else None,
        max_drawdown_r=drawdown,
        total_return_percent=sum(pct_values, Decimal(0)),
        average_return_percent=sum(pct_values, Decimal(0)) / count if count else None,
        exits_by_reason=dict(sorted(reasons.items())),
        verdict_counts=dict(result.verdict_counts),
    )


def _fmt(value: Any, places: int) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(round(value, places))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def write_trades(result: ReplayResult, path: Path, places: int) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(TRADE_COLUMNS)
        for t in result.trades:
            row = (
                LABEL,
                t.strategy,
                t.version,
                t.symbol,
                t.session_date,
                t.setup,
                t.signal_bar_start,
                t.entry_time,
                t.entry_price,
                t.stop,
                t.exit_time,
                t.exit_price,
                t.exit_reason.value,
                t.bars_held,
                t.r_multiple,
                t.return_percent,
            )
            writer.writerow([_fmt(v, places) for v in row])


def write_signals(result: ReplayResult, path: Path, places: int) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SIGNAL_COLUMNS)
        for s in result.signals:
            row = (
                LABEL,
                s.strategy,
                s.version,
                s.symbol,
                s.session_date,
                s.bar_start,
                s.setup,
                s.entry_low,
                s.entry_high,
                s.stop,
                s.outcome.value,
                s.cause,
            )
            writer.writerow([_fmt(v, places) for v in row])


def summary_dict(summary: Summary, places: int) -> dict[str, Any]:
    out: dict[str, Any] = {"label": LABEL}
    for key, value in summary.__dict__.items():
        if isinstance(value, Decimal):
            out[key] = _fmt(value, places)
        else:
            out[key] = value
    return out


def write_summary(summary: Summary, path: Path, places: int) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary_dict(summary, places), handle, indent=2)
        handle.write("\n")


def render_summary(summary: Summary, places: int) -> str:
    d = summary_dict(summary, places)

    def show(key: str) -> str:
        value = d[key]
        return "n/a" if value is None else str(value)

    lines = [
        f"SIMULATED replay of {summary.strategy} v{summary.version}",
        f"  sessions={summary.sessions} bars={summary.bars} signals={summary.signals} "
        f"{summary.signals_by_outcome}",
        f"  trades={summary.trades} wins={summary.wins} losses={summary.losses} "
        f"win_rate={show('win_rate_percent')}%",
        f"  total R={show('total_r')} average R={show('average_r')} "
        f"max drawdown R={show('max_drawdown_r')}",
        f"  total %={show('total_return_percent')} average %={show('average_return_percent')}",
        f"  exits={summary.exits_by_reason}",
    ]
    return "\n".join(lines)


TRADES_FILE = "trades.csv"
SIGNALS_FILE = "signals.csv"
SUMMARY_FILE = "summary.json"


def write_all(result: ReplayResult, out_dir: Path, places: int) -> Summary:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(result)
    write_trades(result, out_dir / TRADES_FILE, places)
    write_signals(result, out_dir / SIGNALS_FILE, places)
    write_summary(summary, out_dir / SUMMARY_FILE, places)
    return summary
