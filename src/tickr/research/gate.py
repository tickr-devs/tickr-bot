"""R23 Step 0 gate report: an offline, DESCRIPTIVE report on one model version's closed trades.

Nothing is promoted from it and it never says a gate is passed: binding promotion criteria are
set with the owner (docs/promotion.md). The full contract is in docs/research.md ("gate").

Frozen before evaluation: an explicit --start and --end and the repository's session calendar
(its files are hashed into the summary). Every month the report looks at is anchored to --end.

Inputs:
- trades CSV, one row per actual sized trade (columns in TRADE_COLUMNS). Results are AFTER the
  costs the row lists; the report charges nothing more, except the bid/ask spread of a row whose
  result does not include it (`spread_in_result` false), once. An empty cost cell is UNKNOWN,
  `0` is a valid zero; an unknown cost makes the line inconclusive, never a guessed zero.
- covered-days CSV (`day`): the calendar trading days on which the producer had data and ran the
  model. A calendar trading day missing from it is missing coverage; a covered day without a
  trade is an observed day with zero trades. No threshold: a month (or the period) is observed
  only when every one of its trading days inside --start..--end is covered; otherwise the report
  is inconclusive.

Cost sign: every cost column is in the row's return units and positive when it reduced the result
(fees and spread are never negative; slippage below 0 is a price improvement).

Same-trade spread cost (a DIAGNOSTIC, not a baseline): the spread paid on the model's own trades,
and the net before it, over the matched trades (known net and known spread); the net before spread
is suppressed unless every trade on the line is matched. A paired baseline needs an independent
selection from the same pre-outcome candidates each minute, which trade rows cannot reconstruct,
so it is reported as unavailable.

Timing: the trade's day is the New York date of its entry; the exit must be on that same day
(same-day flatten) by its extended close, inside --start..--end; `session` must match where the
entry falls in that day's calendar sessions.
"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tickr.calendar import (
    FILE_PATTERN,
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
    ny_date_of,
)
from tickr.research.errors import ResearchError
from tickr.research.settings import ResearchSettings

TRADE_COLUMNS = ("trade_id", "symbol", "session", "entry_time", "exit_time", "notional_usd",
                 "return_units", "result", "fees", "slippage", "spread", "spread_in_result",
                 "cost_provenance", "cost_version")
SESSIONS = ("regular", "extended")
RETURN_UNITS = ("pct_of_notional", "usd")
# broker_fills: real fills, the spread is inside the fill prices; simulated: a model's fills.
PROVENANCES = ("broker_fills", "simulated")
BOOLEANS = {"true": True, "false": False}
COST_SIGN = "positive reduces the result (slippage below 0 is a price improvement)"
PERCENT = Decimal(100)  # unit factor: percent to fraction
MONTHS_PER_YEAR = 12  # calendar constant
ZERO = Decimal(0)
MANUAL = ("every feature available live (R33 parity)", "stop chosen", "decision frequency chosen")
HOLDS, NOT_HOLDS, INCONCLUSIVE = "holds", "does not hold", "inconclusive"
UNAVAILABLE = "unavailable"
PAIRED_BASELINE_UNAVAILABLE = (
    "a paired baseline needs an independent selection from the same pre-outcome candidates each "
    "minute (same execution, dates, costs, quota, constraints); trade rows cannot reconstruct it")
OBSERVED, INSUFFICIENT, OUTSIDE = "observed", "insufficient_coverage", "outside_period"


class GateRefused(ResearchError):
    """The report cannot be computed. `cause` is one of the class constants."""

    MISSING_COLUMN = "missing_column"
    NOT_FINITE = "not_finite"
    BAD_VALUE = "bad_value"
    OUTSIDE_PERIOD = "outside_period"
    NOT_COVERED = "not_covered"
    LIMIT = "limit_breach"
    DUPLICATE = "duplicate"
    NOT_SAME_DAY = "not_same_day"
    SESSION_MISMATCH = "session_mismatch"

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(f"gate report refused ({cause}): {detail}")
        self.cause = cause


@dataclass(frozen=True)
class Trade:
    trade_id: str
    symbol: str
    session: str
    entry_time: datetime
    exit_time: datetime
    notional_usd: Decimal
    return_units: str
    result: Decimal
    fees: Decimal | None  # None = unknown
    slippage: Decimal | None
    spread: Decimal | None
    spread_in_result: bool
    cost_provenance: str
    cost_version: str

    @property
    def day(self) -> date:
        return ny_date_of(self.entry_time)

    def usd(self, value: Decimal | None, scale: Decimal = Decimal(1)) -> Decimal | None:
        """A value in this row's return units as dollars, times `scale` (hypothetical size)."""
        if value is None:
            return None
        dollars = value if self.return_units == "usd" else self.notional_usd * value / PERCENT
        return dollars * scale

    @property
    def spread_charged(self) -> Decimal | None:
        """Spread the report charges: none when the result already includes it."""
        return ZERO if self.spread_in_result else self.spread

    @property
    def known_costs(self) -> bool:
        return None not in (self.fees, self.slippage, self.spread_charged)

    def net(self, scale: Decimal = Decimal(1)) -> Decimal | None:
        if not self.known_costs:
            return None
        return self.usd(self.result - self.spread_charged, scale)


@dataclass(frozen=True)
class MonthCoverage:
    month: str  # YYYY-MM
    trading_days: int | None  # calendar trading days of the whole month; None outside the period
    period_days: int  # of those, the days inside --start..--end
    covered_days: int  # of those, the days in --covered-days
    period_fraction: Decimal  # period_days / trading_days: how much of the month the period holds
    coverage: Decimal  # covered_days / period_days
    status: str  # OBSERVED (every in-period trading day covered), INSUFFICIENT or OUTSIDE


@dataclass(frozen=True)
class Month:
    month: str
    status: str
    trades: int
    net: Decimal
    in_plus: bool


@dataclass(frozen=True)
class Tail:
    percent: Decimal
    trades: int
    net: Decimal
    share_pct: Decimal | None  # of the line's net; None when the net is not above 0


@dataclass(frozen=True)
class Line:
    name: str
    label: str  # "descriptive" or "hypothetical"
    sizing: str
    sessions: str
    trades: int
    unknown_cost_trades: int
    days: int
    days_in_plus: int
    earned: Decimal
    lost: Decimal
    net: Decimal
    fees: Decimal | None  # None: a cost the producer did not report (unknown)
    slippage: Decimal | None
    spread_in_results: Decimal | None  # spread already inside the results, not charged again
    spread_charged: Decimal | None  # spread this report charged (rows without it in the result)
    same_trade_matched: int  # trades with a known net AND a known spread
    same_trade_spread_cost: Decimal | None  # spread paid on the matched trades, USD
    net_before_spread: Decimal | None  # matched net + spread cost; None unless all trades match
    months: tuple[Month, ...]
    months_in_plus: int
    dropped: int
    net_without_best: Decimal
    tails: tuple[Tail, ...]
    checks: dict[str, str]  # check name -> HOLDS, NOT_HOLDS or INCONCLUSIVE
    causes: tuple[str, ...]  # why the line is inconclusive, empty otherwise


@dataclass(frozen=True)
class Gate:
    start: date
    end: date
    trading_days: int
    covered_days: int
    coverage: Decimal
    window: tuple[MonthCoverage, ...]
    lines: tuple[Line, ...]
    provenance: dict[str, int]  # "provenance version (spread in result: x)" -> trades

    @property
    def conclusive(self) -> bool:
        return not any(line.causes for line in self.lines)


def _text(path: Path, n: int, row: dict, column: str) -> str:
    value = (row[column] or "").strip()
    if not value:
        raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: {column} is empty")
    return value


def _number(path: Path, n: int, row: dict, column: str, *, optional: bool = False
            ) -> Decimal | None:
    raw = (row[column] or "").strip()
    if optional and not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: {column} is not a number") from exc
    if not value.is_finite():
        raise GateRefused(GateRefused.NOT_FINITE, f"{path}:{n}: {column} is not finite")
    return value


def _choice(path: Path, n: int, row: dict, column: str, allowed: Iterable[str]) -> str:
    value = (row[column] or "").strip()
    if value not in allowed:
        raise GateRefused(GateRefused.BAD_VALUE,
                          f"{path}:{n}: {column} must be one of {', '.join(allowed)}")
    return value


def _time(path: Path, n: int, row: dict, column: str) -> datetime:
    try:
        value = datetime.fromisoformat(_text(path, n, row, column))
    except ValueError as exc:
        raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: {column} is not ISO 8601") \
            from exc
    if value.tzinfo is None:
        raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: {column} has no UTC offset")
    return value


def _rows(path: Path, columns: Iterable[str]) -> Iterable[tuple[int, dict]]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise GateRefused(GateRefused.MISSING_COLUMN, f"{path}: {', '.join(missing)}")
        yield from enumerate(reader, start=2)


def load_trades(path: Path) -> list[Trade]:
    """Trades CSV (docs/research.md). An empty file is allowed: the report says inconclusive."""
    out: list[Trade] = []
    seen: set[str] = set()
    for n, row in _rows(path, TRADE_COLUMNS):
        trade_id = _text(path, n, row, "trade_id")
        if trade_id in seen:
            raise GateRefused(GateRefused.DUPLICATE, f"{path}:{n}: trade_id {trade_id} repeats")
        seen.add(trade_id)
        costs = {c: _number(path, n, row, c, optional=True) for c in ("fees", "slippage", "spread")}
        for column in ("fees", "spread"):
            if costs[column] is not None and costs[column] < 0:
                raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: {column} is below 0")
        trade = Trade(
            trade_id=trade_id, symbol=_text(path, n, row, "symbol"),
            session=_choice(path, n, row, "session", SESSIONS),
            entry_time=_time(path, n, row, "entry_time"),
            exit_time=_time(path, n, row, "exit_time"),
            notional_usd=_number(path, n, row, "notional_usd"),
            return_units=_choice(path, n, row, "return_units", RETURN_UNITS),
            result=_number(path, n, row, "result"), **costs,
            spread_in_result=BOOLEANS[_choice(path, n, row, "spread_in_result", BOOLEANS)],
            cost_provenance=_choice(path, n, row, "cost_provenance", PROVENANCES),
            cost_version=_text(path, n, row, "cost_version"),
        )
        if trade.notional_usd <= 0:
            raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: notional_usd is not above 0")
        if trade.exit_time < trade.entry_time:
            raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: exit_time is before entry_time")
        if trade.cost_provenance == "broker_fills" and not trade.spread_in_result:
            raise GateRefused(GateRefused.BAD_VALUE,
                              f"{path}:{n}: broker fills always include the spread")
        out.append(trade)
    return out


def load_covered_days(path: Path) -> set[date]:
    out: set[date] = set()
    for n, row in _rows(path, ("day",)):
        try:
            day = date.fromisoformat(_text(path, n, row, "day"))
        except ValueError as exc:
            raise GateRefused(GateRefused.BAD_VALUE, f"{path}:{n}: day is not YYYY-MM-DD") \
                from exc
        if day in out:
            raise GateRefused(GateRefused.DUPLICATE, f"{path}:{n}: day {day} repeats")
        out.add(day)
    return out


def market_day_lookup(calendar: SessionCalendar) -> Callable[[date], MarketDay | None]:
    """The day's sessions on a calendar trading day (half days included), None on a weekend or
    closure; any other calendar refusal (out of coverage, invalid file) propagates."""

    def market_day(day: date) -> MarketDay | None:
        try:
            return calendar.day(day)
        except CalendarRefused as refused:
            if refused.cause in (CalendarCause.WEEKEND, CalendarCause.HOLIDAY):
                return None
            raise

    return market_day


def _check_timing(t: Trade, start: date, end: date,
                  market_day: Callable[[date], MarketDay | None]) -> None:
    exit_day = ny_date_of(t.exit_time)
    if not start <= t.day <= end or not start <= exit_day <= end:
        raise GateRefused(GateRefused.OUTSIDE_PERIOD, f"trade {t.trade_id} {t.day} to {exit_day} "
                          f"is not inside {start}..{end}")
    if exit_day != t.day:
        raise GateRefused(GateRefused.NOT_SAME_DAY, f"trade {t.trade_id} exits on {exit_day}, "
                          f"not on its entry day {t.day} (same-day flatten)")
    day = market_day(t.day)
    if day is None:
        raise GateRefused(GateRefused.BAD_VALUE,
                          f"trade {t.trade_id} on {t.day}, not a trading day")
    if t.exit_time > day.extended_close:
        raise GateRefused(GateRefused.NOT_SAME_DAY, f"trade {t.trade_id} exits after the "
                          f"extended close of {t.day}")
    if day.regular_open <= t.entry_time < day.regular_close:
        actual = "regular"
    elif day.premarket_open <= t.entry_time < day.extended_close:
        actual = "extended"
    else:
        raise GateRefused(GateRefused.SESSION_MISMATCH, f"trade {t.trade_id} enters outside "
                          f"the sessions of {t.day}")
    if actual != t.session:
        raise GateRefused(GateRefused.SESSION_MISMATCH, f"trade {t.trade_id} says {t.session} "
                          f"but enters in the {actual} session")


def calendar_fingerprint(directory: Path) -> dict[str, str]:
    """sha256 of each calendar file, frozen into the summary."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.iterdir()) if FILE_PATTERN.match(p.name)}


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _month_index(day: date) -> int:
    return day.year * MONTHS_PER_YEAR + day.month - 1


def _month_name(index: int) -> str:
    year, month = divmod(index, MONTHS_PER_YEAR)
    return f"{year:04d}-{month + 1:02d}"


def _month_days(index: int) -> tuple[date, date]:
    year, month = divmod(index, MONTHS_PER_YEAR)
    first = date(year, month + 1, 1)
    nxt = date(year + (month + 1) // MONTHS_PER_YEAR, (month + 1) % MONTHS_PER_YEAR + 1, 1)
    return first, nxt - timedelta(days=1)


def _fraction(part: int, whole: int) -> Decimal:
    return Decimal(part) / Decimal(whole) if whole else ZERO


def _complete(period: list[date], covered: set[date]) -> bool:
    """Structural coverage rule, no threshold: every in-period trading day has data."""
    return bool(period) and all(d in covered for d in period)


def _coverage(start: date, end: date, covered: set[date], months: int,
              is_trading_day: Callable[[date], bool]) -> tuple[MonthCoverage, ...]:
    out = []
    for index in range(_month_index(end) - months + 1, _month_index(end) + 1):
        first, last = _month_days(index)
        if last < start:
            out.append(MonthCoverage(_month_name(index), None, 0, 0, ZERO, ZERO, OUTSIDE))
            continue
        days = [d for d in _days(first, last) if is_trading_day(d)]
        period = [d for d in days if start <= d <= end]
        hits = sum(1 for d in period if d in covered)
        out.append(MonthCoverage(_month_name(index), len(days), len(period), hits,
                                 _fraction(len(period), len(days)), _fraction(hits, len(period)),
                                 OBSERVED if _complete(period, covered) else INSUFFICIENT))
    return tuple(out)


def _outcome(value: bool, causes: list[str]) -> str:
    if causes:
        return INCONCLUSIVE
    return HOLDS if value else NOT_HOLDS


def _line(name: str, label: str, sizing: str, sessions: str,
          rows: list[tuple[Trade, Decimal]], window: tuple[MonthCoverage, ...],
          period_ok: bool, settings: ResearchSettings) -> Line:
    known = [(t, s) for t, s in rows if t.known_costs]
    nets = [(t.day, t.net(s)) for t, s in known]
    per_day: dict[date, Decimal] = defaultdict(Decimal)
    per_month: dict[str, list[Decimal]] = defaultdict(list)
    for day, value in nets:
        per_day[day] += value
        per_month[_month_name(_month_index(day))].append(value)
    earned = sum((v for _, v in nets if v > 0), ZERO)
    lost = -sum((v for _, v in nets if v < 0), ZERO)
    net = earned - lost
    ranked = sorted((v for _, v in nets), reverse=True)

    def top(percent: Decimal) -> tuple[int, Decimal]:
        count = math.ceil(len(ranked) * percent / PERCENT)
        return count, sum(ranked[:count], ZERO)

    tails = []
    for percent in settings.gate_tail_percents:
        count, value = top(percent)
        tails.append(Tail(percent, count, value, value / net * PERCENT if net > 0 else None))
    dropped, best = top(settings.gate_drop_best_percent)
    matched = [(t, s) for t, s in known if t.spread is not None]
    spread_cost = sum((t.usd(t.spread, s) for t, s in matched), ZERO) if matched else None
    complete = bool(rows) and len(matched) == len(rows)
    months = tuple(Month(m.month, m.status, len(per_month[m.month]), sum(per_month[m.month], ZERO),
                         sum(per_month[m.month], ZERO) > 0) for m in window)
    months_in_plus = sum(1 for m in months if m.in_plus)

    causes = []
    if not period_ok:
        causes.append("insufficient coverage of the period (a trading day without data)")
    if not known:
        causes.append("zero assessable trades")
    if len(known) < len(rows):
        causes.append(f"{len(rows) - len(known)} trades with an unknown cost")
    month_causes = [*causes, *(f"{m.month} {m.status}" for m in window if m.status != OBSERVED)]
    checks = {
        "net_after_costs_above_0": _outcome(net > 0, causes),
        "sensitivity_k_of_m_months": _outcome(
            months_in_plus >= settings.gate_positive_months, month_causes),
        "sensitivity_without_best": _outcome(net - best > 0, causes),
    }

    def total(field: str, only_in_result: bool | None = None) -> Decimal | None:
        values = [t.usd(getattr(t, field), s) for t, s in rows
                  if only_in_result is None or t.spread_in_result == only_in_result]
        return None if None in values else sum(values, ZERO)

    return Line(
        name=name, label=label, sizing=sizing, sessions=sessions, trades=len(rows),
        unknown_cost_trades=len(rows) - len(known), days=len(per_day),
        days_in_plus=sum(1 for v in per_day.values() if v > 0), earned=earned, lost=lost,
        net=net, fees=total("fees"), slippage=total("slippage"),
        spread_in_results=total("spread", True), spread_charged=total("spread", False),
        same_trade_matched=len(matched), same_trade_spread_cost=spread_cost,
        net_before_spread=net + spread_cost if complete else None,
        months=months, months_in_plus=months_in_plus, dropped=dropped, net_without_best=net - best,
        tails=tuple(tails), checks=checks, causes=tuple(causes),
    )


def gate(trades: list[Trade], covered: set[date], start: date, end: date,
         market_day: Callable[[date], MarketDay | None], cap_usd: Decimal,
         all_sessions_usd: Decimal, settings: ResearchSettings) -> Gate:
    if start > end:
        raise GateRefused(GateRefused.BAD_VALUE, f"--start {start} is after --end {end}")
    if not all_sessions_usd.is_finite():
        raise GateRefused(GateRefused.NOT_FINITE, "the all-sessions size is not finite")
    if all_sessions_usd <= 0:
        raise GateRefused(GateRefused.BAD_VALUE, "the all-sessions size is not above 0")
    def is_trading_day(day: date) -> bool:
        return market_day(day) is not None

    period = [d for d in _days(start, end) if is_trading_day(d)]
    for day in sorted(covered):
        if not start <= day <= end:
            raise GateRefused(GateRefused.OUTSIDE_PERIOD, f"covered day {day} is outside "
                              f"{start}..{end}")
        if not is_trading_day(day):
            raise GateRefused(GateRefused.BAD_VALUE, f"covered day {day} is not a trading day")
    for t in trades:
        _check_timing(t, start, end, market_day)
        if t.day not in covered:
            raise GateRefused(GateRefused.NOT_COVERED,
                              f"trade {t.trade_id} on {t.day}, a day not in the covered days")
        if t.notional_usd > cap_usd:
            raise GateRefused(GateRefused.LIMIT, f"trade {t.trade_id} notional {t.notional_usd} "
                              f"is above the position cap {cap_usd}")
    share = _fraction(len(covered), len(period))
    period_ok = _complete(period, covered)
    window = _coverage(start, end, covered, settings.gate_window_months, is_trading_day)
    extended = any(t.session != "regular" for t in trades)
    one = Decimal(1)
    lines = (
        _line("main_session", "descriptive", "actual sized notional", "regular",
              [(t, one) for t in trades if t.session == "regular"], window, period_ok, settings),
        _line("all_sessions", "hypothetical", f"every trade rescaled to {all_sessions_usd} USD",
              "all" if extended else "all (regular only: no extended trades)",
              [(t, all_sessions_usd / t.notional_usd) for t in trades], window, period_ok,
              settings),
    )
    provenance: dict[str, int] = defaultdict(int)
    for t in trades:
        provenance[f"{t.cost_provenance} {t.cost_version} (spread in result: "
                   f"{str(t.spread_in_result).lower()})"] += 1
    return Gate(start, end, len(period), len(covered), share, window, lines,
                dict(sorted(provenance.items())))
