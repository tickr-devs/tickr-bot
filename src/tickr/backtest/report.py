"""Backtest report: does the strategy show an edge, and how would the bot's limits have played?

Two views of the same simulated trades (all `simulated`):
1. Every signal: each pool symbol replayed on its own (the replay engine's view). Mean R per
   trade with a bootstrap confidence interval that resamples whole days (trades of one day are
   correlated): an interval above 0 is evidence of an edge under these fill assumptions, one
   that contains 0 is not. The same for the window before the hold-out (the last
   `holdout_percent` of sessions) and for the hold-out alone.
2. The bot: trades taken in entry order through the live money checks (`risk.sizing.check_size`,
   `risk.daily.check_daily` with the day's net realized loss, decisions 2026-11-14) and
   `max_open_positions`. USD = (exit - entry) x the strategy's quantity, gross: commissions and
   regulatory fees are not modelled.

Breakdowns by month, setup, exit reason, entry hour (New York) and entry price help find where
an edge, if any, lives. Outputs: report.json and report.md in the results directory.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from tickr.backtest.settings import BacktestSettings
from tickr.backtest.universe import CapRecord, PoolEntry
from tickr.calendar import NEW_YORK
from tickr.config import Config
from tickr.replay.engine import LABEL, ReplayResult, SimulatedTrade
from tickr.replay.report import summarize
from tickr.replay.settings import ReplaySettings
from tickr.risk.causes import AdmissionCause
from tickr.risk.daily import check_daily
from tickr.risk.sizing import check_size

REPORT_JSON = "report.json"
REPORT_MD = "report.md"
ZERO = Decimal(0)
HUNDRED = Decimal(100)
TWO = Decimal(2)


def stats(r_values: Sequence[Decimal]) -> dict[str, Any]:
    n = len(r_values)
    if n == 0:
        return {"trades": 0}
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r < 0]
    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    ordered = sorted(r_values)
    middle = n // 2
    median = ordered[middle] if n % 2 else (ordered[middle - 1] + ordered[middle]) / TWO
    return {
        "trades": n,
        "win_rate_percent": Decimal(len(wins)) / n * HUNDRED,
        "mean_r": sum(r_values, ZERO) / n,
        "median_r": median,
        "total_r": sum(r_values, ZERO),
        "average_win_r": gross_win / len(wins) if wins else None,
        "average_loss_r": -gross_loss / len(losses) if losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
    }


def bootstrap_interval(trades_by_day: Sequence[Sequence[Decimal]], samples: int, seed: int,
                       confidence: Decimal) -> tuple[Decimal, Decimal] | None:
    """Percentile bootstrap interval of the mean R per trade, resampling whole DAYS with
    replacement (trades of one day share the market and are correlated, so resampling single
    trades would make the interval too narrow). Statistic: total R / trades of the resample."""
    days = [[float(r) for r in day] for day in trades_by_day if day]
    if len(days) < 2:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        picked = rng.choices(days, k=len(days))
        means.append(sum(sum(d) for d in picked) / sum(len(d) for d in picked))
    means.sort()
    tail = float((HUNDRED - confidence) / TWO / HUNDRED)
    low = means[int(tail * (samples - 1))]
    high = means[int((1 - tail) * (samples - 1))]
    return Decimal(str(round(low, 6))), Decimal(str(round(high, 6)))


def by_day(trades: Sequence[SimulatedTrade]) -> list[list[Decimal]]:
    days: dict[date, list[Decimal]] = defaultdict(list)
    for trade in trades:
        days[trade.session_date].append(trade.r_multiple)
    return [days[d] for d in sorted(days)]


def period(trades: Sequence[SimulatedTrade], settings: BacktestSettings) -> dict[str, Any]:
    """Stats of a set of trades with the day-resampled interval of the mean R."""
    return {
        **stats([t.r_multiple for t in trades]),
        "days_with_trades": len({t.session_date for t in trades}),
        "mean_r_interval": bootstrap_interval(by_day(trades), settings.bootstrap_samples,
                                              settings.bootstrap_seed,
                                              settings.confidence_percent),
    }


def holdout_start(sessions: Sequence[date], percent: Decimal) -> date | None:
    """First session of the hold-out: the last `percent` of the window's sessions."""
    if len(sessions) < 2:
        return None
    count = max(1, int(len(sessions) * percent / HUNDRED))
    return sessions[-count]


def breakdown(trades: Sequence[SimulatedTrade],
              key: Callable[[SimulatedTrade], str]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Decimal]] = defaultdict(list)
    for trade in trades:
        groups[key(trade)].append(trade.r_multiple)
    return {k: stats(v) for k, v in sorted(groups.items())}


def price_bucket(price: Decimal, edges: Sequence[Decimal]) -> str:
    if price < edges[0]:
        return f"<{edges[0]}"
    for low, high in zip(edges, edges[1:], strict=False):
        if low <= price < high:
            return f"{low}-{high}"
    return f">={edges[-1]}"


def bot_view(trades: Sequence[SimulatedTrade], cfg: Config) -> dict[str, Any]:
    """Trades the bot could have taken, first come first served, through the live money checks
    (risk/admission.py `_check_money`): the strategy's quantity must pass `check_size` against
    bot.toml [risk] `planned_loss_usd` and `position_notional_cap_usd` at the window top and
    the stop, and `check_daily` must pass with the day's net realized loss, the planned loss of
    positions still open, and this trade's own planned loss = quantity x (entry_high - stop).
    At most `max_open_positions` at once. Refusals are counted by cause.

    Near the ceiling the live strategy sizes down to the room left (loss_budget_usd, while the
    room is at least `min_daily_room_usd`). Here the quantity becomes
    min(quantity, floor(room / (entry_high - stop))) and the USD result scales with it; the
    strategy's own sizing risk also counts spread and slippage reserve, so live it would size
    a little smaller still."""
    risk = cfg.risk
    taken: list[SimulatedTrade] = []
    planned: dict[int, Decimal] = {}
    pnl: dict[int, Decimal] = {}
    sized_down = 0
    refused: dict[str, int] = defaultdict(int)
    for trade in sorted(trades, key=lambda t: (t.entry_time, t.signal_bar_start, t.symbol)):
        if trade.quantity is None or trade.entry_high is None:
            refused["DECISION_WITHOUT_QUANTITY"] += 1
            continue
        open_now = [t for t in taken if t.exit_time > trade.entry_time]
        if len(open_now) >= risk.max_open_positions:
            refused["POSITION_OPEN"] += 1
            continue
        size = check_size(trade.quantity, trade.entry_high, trade.stop, risk.planned_loss_usd,
                          risk.position_notional_cap_usd)
        if not size.ok or size.planned_loss_usd is None:
            refused[str(size.cause)] += 1
            continue
        net = sum((pnl[id(t)] for t in taken if t.session_date == trade.session_date
                   and t.exit_time <= trade.entry_time), ZERO)
        realized = max(-net, ZERO)
        reserved = sum((planned[id(t)] for t in open_now), ZERO)
        quantity, planned_loss = trade.quantity, size.planned_loss_usd
        room = risk.max_daily_loss_usd - realized - reserved
        if room >= risk.min_daily_room_usd and planned_loss > room:
            # Live (#70): the strategy gets loss_budget_usd = room and sizes down to it.
            quantity = min(quantity, _floor(room / (trade.entry_high - trade.stop)))
            if quantity < 1:
                refused[str(AdmissionCause.QUANTITY_BELOW_ONE)] += 1
                continue
            planned_loss = quantity * (trade.entry_high - trade.stop)
            sized_down += 1
        daily = check_daily(realized, reserved, planned_loss, risk.max_daily_loss_usd,
                            risk.min_daily_room_usd)
        if not daily.ok:
            refused[str(daily.cause)] += 1
            continue
        taken.append(trade)
        planned[id(trade)] = planned_loss
        pnl[id(trade)] = (trade.exit_price - trade.entry_price) * quantity
    per_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for trade in taken:
        per_day[trade.session_date] += pnl[id(trade)]
    equity = peak = drawdown = ZERO
    for day in sorted(per_day):
        equity += per_day[day]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    days = list(per_day.values())
    return {
        **stats([t.r_multiple for t in taken]),
        "refused": dict(sorted(refused.items())),
        "sized_down_to_daily_room": sized_down,
        "total_usd": sum(days, ZERO),
        "days_with_trades": len(days),
        "average_day_usd": sum(days, ZERO) / len(days) if days else None,
        "best_day_usd": max(days) if days else None,
        "worst_day_usd": min(days) if days else None,
        "positive_days": sum(1 for d in days if d > 0),
        "max_drawdown_usd": drawdown,
    }


def _floor(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


def _plain(value: Any, places: int) -> Any:
    if isinstance(value, Decimal):
        return str(round(value, places))
    if isinstance(value, dict):
        return {k: _plain(v, places) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v, places) for v in value]
    return value


def build(result: ReplayResult, pool: Sequence[PoolEntry], caps: Sequence[CapRecord],
          traded_days: int, skipped: Sequence[dict[str, str]], cfg: Config,
          settings: BacktestSettings) -> dict[str, Any]:
    trades = result.trades
    summary = summarize(result)
    causes: dict[str, int] = defaultdict(int)
    for item in skipped:
        causes[item["cause"].split(":")[0]] += 1
    sessions = sorted({e.session_date for e in pool})
    return {
        "label": LABEL,
        "strategy": result.strategy,
        "version": result.version,
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()] if sessions else None,
        "pool": {"symbol_days": len(pool), "sessions": len(sessions),
                 "symbols": len({e.symbol for e in pool}), "replayed_symbol_days": traded_days,
                 "skipped": dict(sorted(causes.items())),
                 "cap": settings.max_pool_per_day,
                 "cap_bound_sessions": sum(1 for c in caps if c.dropped),
                 "cap_dropped_symbol_days": sum(c.dropped for c in caps),
                 "most_candidates_in_a_session": max((c.candidates for c in caps), default=0)},
        "signals_by_outcome": summary.signals_by_outcome,
        "all_signals": {
            **period(trades, settings),
            "confidence_percent": settings.confidence_percent,
            "max_drawdown_r": summary.max_drawdown_r,
            "total_usd": sum((t.pnl_usd or ZERO for t in trades), ZERO),
        },
        "bot": bot_view(trades, cfg),
        "holdout": _holdout(trades, sessions, settings),
        "by_month": breakdown(trades, lambda t: t.session_date.strftime("%Y-%m")),
        "by_setup": breakdown(trades, lambda t: t.setup or "none"),
        "by_exit": breakdown(trades, lambda t: t.exit_reason.value),
        "by_entry_hour_ny": breakdown(
            trades, lambda t: f"{t.entry_time.astimezone(NEW_YORK).hour:02d}"),
        "by_entry_price_usd": breakdown(
            trades, lambda t: price_bucket(t.entry_price, settings.price_bucket_edges_usd)),
    }


def _holdout(trades: Sequence[SimulatedTrade], sessions: Sequence[date],
             settings: BacktestSettings) -> dict[str, Any] | None:
    start = holdout_start(sessions, settings.holdout_percent)
    if start is None:
        return None
    return {"starts": start.isoformat(),
            "before": period([t for t in trades if t.session_date < start], settings),
            "holdout": period([t for t in trades if t.session_date >= start], settings)}


def _interval_text(s: dict[str, Any]) -> str:
    interval = s.get("mean_r_interval")
    if not interval:
        return "too few days for an interval"
    verdict = ("includes 0: no evidence of an edge" if interval[0] <= 0 <= interval[1]
               else "above 0: evidence of an edge" if interval[0] > 0
               else "below 0: evidence of a negative edge")
    return f"{interval[0]:.3f} to {interval[1]:.3f} ({verdict})"


def _row(name: str, s: dict[str, Any]) -> str:
    if not s.get("trades"):
        return f"| {name} | 0 | | | | |"
    pf = s["profit_factor"]
    return (f"| {name} | {s['trades']} | {s['win_rate_percent']:.1f}% | {s['mean_r']:.3f} | "
            f"{s['total_r']:.2f} | {'n/a' if pf is None else f'{pf:.2f}'} |")


def markdown(data: dict[str, Any]) -> str:
    a, b = data["all_signals"], data["bot"]
    lines = [
        f"# SIMULATED backtest: {data['strategy']} v{data['version']}",
        "",
        f"Window {data['window'][0]} to {data['window'][1]}; pool {data['pool']['symbol_days']} "
        f"symbol-days ({data['pool']['symbols']} symbols), replayed "
        f"{data['pool']['replayed_symbol_days']}, skipped {data['pool']['skipped']}.",
        f"Signals: {data['signals_by_outcome']}.",
        f"Pool cap {data['pool']['cap']} (ranks by the day's high, look-ahead if it binds): bound "
        f"on {data['pool']['cap_bound_sessions']} sessions, "
        f"{data['pool']['cap_dropped_symbol_days']} symbol-days dropped; most candidates in a "
        f"session {data['pool']['most_candidates_in_a_session']}.",
        "",
        "| view | trades | win rate | mean R | total R | profit factor |",
        "|---|---|---|---|---|---|",
        _row("every signal", a),
        _row("bot limits", b),
        "",
    ]
    recent = data.get("last_sessions")
    if recent:
        lines += [f"Last {recent['requested']} sessions only: {recent['first']} to "
                  f"{recent['last']}, {recent['sessions_without_replayed_days']} of them without "
                  "a replayed day. Few days: the interval and the hold-out below are wide by "
                  "construction.", ""]
    lines.append(f"Mean R per trade, {a['confidence_percent']}% interval (days resampled): "
                 f"{_interval_text(a)}.")
    h = data.get("holdout")
    if h:
        lines += ["", f"Hold-out from {h['starts']}:", "",
                  "| period | trades | win rate | mean R | total R | profit factor |",
                  "|---|---|---|---|---|---|",
                  _row("before", h["before"]), _row("hold-out", h["holdout"]), "",
                  f"Before: {_interval_text(h['before'])}. "
                  f"Hold-out: {_interval_text(h['holdout'])}."]
    if b.get("trades"):
        lines.append(f"Bot view: total {b['total_usd']:.2f} USD gross over "
                     f"{b['days_with_trades']} days, positive days {b['positive_days']}, worst "
                     f"day {b['worst_day_usd']:.2f}, max drawdown {b['max_drawdown_usd']:.2f} "
                     f"USD; refused {b['refused']}.")
    for title, key in (("Month", "by_month"), ("Setup", "by_setup"), ("Exit", "by_exit"),
                       ("Entry hour (NY)", "by_entry_hour_ny"),
                       ("Entry price (USD)", "by_entry_price_usd")):
        lines += ["", f"## {title}", "",
                  "| group | trades | win rate | mean R | total R | profit factor |",
                  "|---|---|---|---|---|---|"]
        lines += [_row(k, v) for k, v in data[key].items()]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class LastSessions:
    """`run --last-sessions`: the requested count, the calendar window it covers, and how many
    of its sessions had no pool day that could be replayed (no gainer, or no complete data)."""

    requested: int
    first: date
    last: date
    sessions_without_replayed_days: int


def write_report(result: ReplayResult, pool: Sequence[PoolEntry], caps: Sequence[CapRecord],
                 traded_days: int,
                 skipped: Sequence[dict[str, str]], cfg: Config, settings: BacktestSettings,
                 replay_settings: ReplaySettings, out: Path,
                 recent: LastSessions | None = None) -> str:
    data = build(result, pool, caps, traded_days, skipped, cfg, settings)
    if recent is not None:
        data["last_sessions"] = {"requested": recent.requested,
                                 "first": recent.first.isoformat(),
                                 "last": recent.last.isoformat(),
                                 "sessions_without_replayed_days":
                                     recent.sessions_without_replayed_days}
    places = replay_settings.output_decimal_places
    (out / REPORT_JSON).write_text(json.dumps(_plain(data, places), indent=2) + "\n", "utf-8")
    text = markdown(data)
    (out / REPORT_MD).write_text(text, "utf-8")
    return text
