"""Gross performance of the trades closed over a range of New York dates (GET /performance).

Display only: reads the trades `history.closed_trades` gives (the same source as day-v1) and
never feeds the daily-loss check or the trading path. All arithmetic is exact Decimal; values
that come from a division are rounded to `places` only when the document is built.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from tickr.api import contract as api
from tickr.calendar import ny_date_of

ZERO = Decimal(0)
NOT_POSITIVE_PLANNED_LOSS = "planned loss is not positive: no 1 R to divide by"
NO_TRADES = "no counted trades in the range"
NO_WINS = "no winning trades in the range"
NO_LOSSES = "no losing trades in the range"


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def _max_drawdown(results: Sequence[Decimal]) -> Decimal:
    """Largest fall of the running total below its running peak; the peak starts at 0, before
    the first trade."""
    total = peak = worst = ZERO
    for result in results:
        total += result
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def performance(environment: str, from_day: date, to_day: date,
                trades: Sequence[api.DayTrade], places: int) -> api.PerformanceV1:
    """`trades`: closed trades, oldest close first (as `closed_trades` returns them); only
    those closed from `from_day` to `to_day` (New York dates, both included) are read."""
    step = Decimal(1).scaleb(-places)

    def rounded(value: Decimal | None) -> Decimal | None:
        return None if value is None else value.quantize(step)

    in_range = [t for t in trades if from_day <= ny_date_of(t.closed_at) <= to_day]
    counted = [t for t in in_range if t.planned_loss_usd > 0]
    excluded = len(in_range) - len(counted)
    usd = [t.result_gross_usd for t in counted]
    r = [t.result_gross_usd / t.planned_loss_usd for t in counted]
    win_r = [x for x in r if x > 0]
    loss_r = [x for x in r if x < 0]
    won_usd = sum((x for x in usd if x > 0), ZERO)
    lost_usd = -sum((x for x in usd if x < 0), ZERO)
    profit_factor = won_usd / lost_usd if lost_usd > 0 else None

    days: list[api.PerformanceDayV1] = []
    cumulative = ZERO
    by_day: dict[date, list[int]] = {}
    for index, trade in enumerate(counted):
        by_day.setdefault(ny_date_of(trade.closed_at), []).append(index)
    for day, indexes in by_day.items():  # insertion order = close order
        day_r = sum((r[i] for i in indexes), ZERO)
        cumulative += day_r
        days.append(api.PerformanceDayV1(
            day=day, trades=len(indexes), result_gross_r=day_r.quantize(step),
            result_gross_usd=sum((usd[i] for i in indexes), ZERO),
            cumulative_gross_r=cumulative.quantize(step)))

    def cause(value: object, why: str) -> str | None:
        return why if value is None else None

    expectancy_r, expectancy_usd = _mean(r), _mean(usd)
    average_win, average_loss = _mean(win_r), _mean(loss_r)
    win_rate = Decimal(len(win_r)) / len(r) if r else None
    return api.PerformanceV1(
        environment=environment, from_day=from_day, to_day=to_day,
        trades=len(counted), wins=len(win_r), losses=len(loss_r),
        scratches=len(r) - len(win_r) - len(loss_r),
        exclusions=(api.ExclusionV1(NOT_POSITIVE_PLANNED_LOSS, excluded),) if excluded else (),
        win_rate=rounded(win_rate), win_rate_cause=cause(win_rate, NO_TRADES),
        expectancy_gross_r=rounded(expectancy_r),
        expectancy_gross_r_cause=cause(expectancy_r, NO_TRADES),
        expectancy_gross_usd=rounded(expectancy_usd),
        expectancy_gross_usd_cause=cause(expectancy_usd, NO_TRADES),
        average_win_gross_r=rounded(average_win),
        average_win_gross_r_cause=cause(average_win, NO_TRADES if not r else NO_WINS),
        average_loss_gross_r=rounded(average_loss),
        average_loss_gross_r_cause=cause(average_loss, NO_TRADES if not r else NO_LOSSES),
        profit_factor_gross=rounded(profit_factor),
        profit_factor_gross_cause=cause(profit_factor, NO_TRADES if not r else NO_LOSSES),
        max_drawdown_gross_r=_max_drawdown(r).quantize(step),
        max_drawdown_gross_usd=_max_drawdown(usd),
        result_gross_r=sum(r, ZERO).quantize(step),
        result_gross_usd=sum(usd, ZERO),
        days=tuple(days))
