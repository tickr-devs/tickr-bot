"""GET /chart: candles of any US stock from market data, for the apps' chart.

Reads go through a `MarketData.history_bars` source owned by the API process: Webull
(`[chart] webull_enabled`, its own transport and request budget, never the bot's) or the demo
world. Nothing here talks to the bot or to the broker's trading routes, and nothing is retried:
a refused or failed read is one typed answer.

How an answer is built:
- Ranges 1d and 5d (intervals 1m, 5m, 30m, 1h): the latest 1 or 5 sessions of the calendar whose
  pre-market has begun. Each session's M1 bars come from one request (end_time = the session's
  extended close, or now while it runs; `[chart] bars_per_request` covers the 960 minutes of a
  full day). Every bar must lie inside its day's sessions and carry the provider's label for that
  part of the day (PRE, RTH, ATH), else the answer is refused; `extended` comes from the calendar.
  5m/30m/1h candles are cut from M1, anchored at the start of each part of the day (pre-market
  open, regular open, regular close), so no candle spans a session edge: the last candle of a part
  may be shorter, and a bucket without bars has no candle.
- Ranges 1m, 3m and 1y (intervals 1d, 1w, 1mo): D bars from one request, each mapped to its New
  York date; weeks (from Monday) and months are merged from them. The range starts that many
  months before today, moved back to the start of its week or month so the first candle is whole.
- Merging: open of the first bar, close of the last, highest high, lowest low, summed volume,
  exact Decimal.
- Cache: a session's M1 bars are written to disk once `[chart] closed_day_settle_seconds` have
  passed after its extended close (minute bars are unadjusted, so a settled day never changes),
  and only if the day has bars; until then, and for a day without bars, they stay in memory for
  `[chart] live_ttl_seconds`. The daily series stays in memory for the live TTL while today's
  session is not over, then until the next session's pre-market opens. Nothing prunes the disk
  cache yet (a known gap).
- Concurrency: the provider is read outside the shared state lock. Answers for one symbol run one
  at a time (a per-symbol lock), so a second request for the same symbol waits and then finds the
  first one's cache instead of spending budget twice; other symbols are not held up.
"""

from __future__ import annotations

import calendar as gregorian
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from aperture.api import contract
from aperture.api.contract import ChartBarV1, ChartInterval, ChartRange, ChartV1
from aperture.broker.base import BrokerError, RateLimited
from aperture.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
    ny_date_of,
)
from aperture.config import Config
from aperture.data.base import BarSpan, DelayedData, MarketData, MarketDataError, SessionBar
from aperture.types import Bar

# Ticker letters and digits, one optional "." or "-" class suffix (BRK.B); never a path part.
CHART_SYMBOL = re.compile(r"^[A-Z0-9]+([.-][A-Z0-9]+)?$")
# Minutes per candle and sessions or months per range: what the names mean (unit factors).
INTRADAY_MINUTES = {ChartInterval.M1: 1, ChartInterval.M5: 5, ChartInterval.M30: 30,
                    ChartInterval.H1: 60}
RANGE_SESSIONS = {ChartRange.D1: 1, ChartRange.D5: 5}
MONTHS_PER_YEAR = 12
RANGE_MONTHS = {ChartRange.M1: 1, ChartRange.M3: 3, ChartRange.Y1: MONTHS_PER_YEAR}
DAILY_INTERVALS = (ChartInterval.D1, ChartInterval.W1, ChartInterval.MO1)
# Provider session labels of M1 bars (probe M4).
PRE, REGULAR, POST = "PRE", "RTH", "ATH"
ONE_DAY = timedelta(days=1)
SKIPPED_DAYS = (CalendarCause.WEEKEND, CalendarCause.HOLIDAY)
OFF_CAUSE = "chart market data is off ([chart] webull_enabled = false)"


class ChartRefused(Exception):
    """One typed answer: `error` is an ErrorV1 code (bad_request, rate_limited, unavailable)."""

    def __init__(self, error: str, cause: str, retry_after: float | None = None) -> None:
        super().__init__(cause)
        self.error = error
        self.cause = cause
        self.retry_after = retry_after


@dataclass(frozen=True)
class ChartSettings:
    webull_enabled: bool
    bars_per_request: int
    live_ttl: timedelta
    closed_day_settle: timedelta
    cache_dir: Path  # <data_dir>/<cache_dir>/<environment>
    calendar_dir: Path
    session_lookback_days: int

    @classmethod
    def from_config(cls, cfg: Config) -> ChartSettings:
        chart = cfg.chart
        return cls(
            webull_enabled=chart.webull_enabled,
            bars_per_request=chart.bars_per_request,
            live_ttl=timedelta(seconds=float(chart.live_ttl_seconds)),
            closed_day_settle=timedelta(seconds=float(chart.closed_day_settle_seconds)),
            cache_dir=cfg.root / cfg.run.data_dir / chart.cache_dir / cfg.environment_name,
            calendar_dir=(cfg.root / cfg.session.calendar_file).parent,
            session_lookback_days=chart.session_lookback_days,
        )


def parse_query(params: Mapping[str, str]) -> tuple[str, ChartInterval, ChartRange]:
    """Symbol, interval and range of a GET /chart query, or ChartRefused(bad_request)."""
    symbol = params["symbol"]
    if not CHART_SYMBOL.match(symbol):
        raise ChartRefused("bad_request", f"symbol {symbol!r} is not an upper-case US ticker")
    try:
        interval = ChartInterval(params["interval"])
    except ValueError:
        allowed = ", ".join(i.value for i in ChartInterval)
        raise ChartRefused("bad_request", f"interval must be one of: {allowed}") from None
    try:
        span = ChartRange(params["range"])
    except ValueError:
        allowed = ", ".join(r.value for r in ChartRange)
        raise ChartRefused("bad_request", f"range must be one of: {allowed}") from None
    intraday_pair = interval in INTRADAY_MINUTES and span in RANGE_SESSIONS
    daily_pair = interval in DAILY_INTERVALS and span in RANGE_MONTHS
    if not (intraday_pair or daily_pair):
        raise ChartRefused("bad_request", f"interval {interval.value} is not served for range "
                           f"{span.value}: 1m/5m/30m/1h take 1d or 5d, 1d/1w/1mo take 1m, 3m "
                           "or 1y")
    return symbol, interval, span


# ---------------------------------------------------------------- aggregation (pure)


def merge(bars: Sequence[Bar]) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """(open, high, low, close, volume) of bars in time order, exact."""
    return (bars[0].open, max(b.high for b in bars), min(b.low for b in bars), bars[-1].close,
            sum((b.volume for b in bars), Decimal(0)))


@dataclass(frozen=True)
class PlacedBar:
    """An M1 bar placed in its day by the calendar: the start of its part of the day and
    whether that part is pre- or post-market."""

    bar: Bar
    part_start: datetime
    extended: bool


def place(day: MarketDay, item: SessionBar) -> PlacedBar:
    """Check an M1 bar against the calendar's sessions of `day`; ChartRefused if it lies outside
    them or the provider labels it as another session."""
    start = item.bar.start
    if not day.premarket_open <= start < day.extended_close:
        raise ChartRefused("unavailable", f"{item.bar.symbol} bar at "
                           f"{contract.format_time(start)} is outside the calendar's sessions "
                           f"of {day.ny_date.isoformat()}")
    if start < day.regular_open:
        part, part_start = PRE, day.premarket_open
    elif start < day.regular_close:
        part, part_start = REGULAR, day.regular_open
    else:
        part, part_start = POST, day.regular_close
    if item.session != part:
        raise ChartRefused("unavailable", f"{item.bar.symbol} bar at "
                           f"{contract.format_time(start)}: the provider says {item.session}, "
                           f"the calendar says {part}")
    return PlacedBar(item.bar, part_start, part != REGULAR)


def aggregate_intraday(placed: Sequence[PlacedBar], minutes: int) -> list[ChartBarV1]:
    """Candles of `minutes` from placed M1 bars in time order, anchored at each part's start."""
    width = timedelta(minutes=minutes)
    buckets: dict[datetime, list[PlacedBar]] = {}
    previous: datetime | None = None
    for item in placed:
        if previous is not None and item.bar.start <= previous:
            raise ChartRefused("unavailable", f"{item.bar.symbol} bars are not in strictly "
                               "increasing time order")
        previous = item.bar.start
        offset = (item.bar.start - item.part_start) // width
        buckets.setdefault(item.part_start + offset * width, []).append(item)
    return [ChartBarV1(start, *merge([i.bar for i in items]), extended=items[0].extended)
            for start, items in buckets.items()]


def bucket_day(day: date, interval: ChartInterval) -> date:
    """First calendar day of the 1d/1w/1mo candle holding `day`."""
    if interval is ChartInterval.W1:
        return day - timedelta(days=day.weekday())
    if interval is ChartInterval.MO1:
        return day.replace(day=1)
    return day


def aggregate_daily(days: Sequence[tuple[date, Bar]], interval: ChartInterval) -> list[ChartBarV1]:
    """1d/1w/1mo candles from (New York date, D bar) in date order. A candle starts at 00:00 New
    York time of its first calendar day."""
    buckets: dict[date, list[Bar]] = {}
    for day, bar in days:
        buckets.setdefault(bucket_day(day, interval), []).append(bar)
    return [ChartBarV1(datetime.combine(first, time(), NEW_YORK), *merge(bars), extended=False)
            for first, bars in buckets.items()]


def months_back(day: date, months: int) -> date:
    """The same day `months` calendar months earlier (the month's last day if it is shorter)."""
    years, index = divmod(day.month - 1 - months, MONTHS_PER_YEAR)
    year, month = day.year + years, index + 1
    return date(year, month, min(day.day, gregorian.monthrange(year, month)[1]))


# ---------------------------------------------------------------- reader


@dataclass
class _Live:
    expires: datetime
    read_at: datetime
    rows: list[SessionBar]


class Chart:
    """Answers GET /chart from a `MarketData` source. Thread safe: shared state under one
    lock, never held across a provider read; one answer at a time per symbol."""

    def __init__(self, settings: ChartSettings, source: MarketData | None = None,
                 off_cause: str = OFF_CAUSE) -> None:
        self.settings = settings
        self._source = source
        self._off_cause = off_cause
        self._lock = threading.Lock()  # source, calendar, live cache, symbol locks
        self._symbol_locks: dict[str, threading.Lock] = {}
        self._calendar: SessionCalendar | None = None
        self._live: dict[tuple[str, str, date | None], _Live] = {}

    def use(self, source: MarketData | None, off_cause: str = OFF_CAUSE) -> None:
        """Serve from `source` from now on; None = refuse every chart with `off_cause`."""
        with self._lock:
            self._source, self._off_cause = source, off_cause
            self._live.clear()

    def answer(self, symbol: str, interval: ChartInterval, span: ChartRange,
               now: datetime) -> ChartV1:
        """The chart document, or ChartRefused (rate_limited, unavailable)."""
        with self._lock:
            source = self._source
            off_cause = self._off_cause
            symbol_lock = self._symbol_locks.setdefault(symbol, threading.Lock())
        if source is None:
            raise ChartRefused("unavailable", off_cause)
        with symbol_lock:
            try:
                if interval in INTRADAY_MINUTES:
                    read_at, bars = self._intraday(source, symbol, interval,
                                                   RANGE_SESSIONS[span], now)
                else:
                    read_at, bars = self._daily(source, symbol, interval, RANGE_MONTHS[span], now)
            except RateLimited as exc:
                raise ChartRefused("rate_limited", f"chart read budget: {exc}",
                                   getattr(exc, "retry_after_seconds", None)) from None
            except DelayedData as exc:
                raise ChartRefused("unavailable", f"market data is delayed: {exc}") from None
            except (MarketDataError, BrokerError) as exc:
                raise ChartRefused("unavailable", f"market data read failed: "
                                   f"{type(exc).__name__}: {exc}") from None
            except CalendarRefused as exc:
                raise ChartRefused("unavailable", f"calendar: {exc}") from None
        cause = None if bars else (f"no {interval.value} bars from {source.provider} for "
                                   f"{symbol} in range {span.value}")
        return ChartV1(symbol, interval, span, source.provider, read_at, cause, tuple(bars))

    # ------------------------------------------------------------ intraday

    def _calendar_(self) -> SessionCalendar:
        with self._lock:
            if self._calendar is None:
                self._calendar = SessionCalendar.load(self.settings.calendar_dir)
            return self._calendar

    def _cached(self, key: tuple[str, str, date | None], now: datetime) -> _Live | None:
        with self._lock:
            live = self._live.get(key)
        return live if live is not None and now < live.expires else None

    def _sessions(self, now: datetime, count: int) -> list[MarketDay]:
        """The latest `count` sessions whose pre-market has begun, oldest first."""
        found: list[MarketDay] = []
        day = ny_date_of(now)
        for _ in range(self.settings.session_lookback_days + 1):  # today and the days before
            try:
                market_day = self._calendar_().day(day)
            except CalendarRefused as exc:
                if exc.cause not in SKIPPED_DAYS:
                    raise
            else:
                if market_day.premarket_open <= now:
                    found.append(market_day)
                    if len(found) == count:
                        return list(reversed(found))
            day -= ONE_DAY
        raise ChartRefused("unavailable", f"fewer than {count} sessions in the "
                           f"{self.settings.session_lookback_days} days before "
                           f"{ny_date_of(now).isoformat()} ([chart] session_lookback_days)")

    def _intraday(self, source: MarketData, symbol: str, interval: ChartInterval,
                  sessions: int, now: datetime) -> tuple[datetime, list[ChartBarV1]]:
        placed: list[PlacedBar] = []
        read_times: list[datetime] = []
        for day in self._sessions(now, sessions):
            read_at, rows = self._minutes(source, symbol, day, now)
            read_times.append(read_at)
            placed.extend(place(day, row) for row in rows)
        return min(read_times), aggregate_intraday(placed, INTRADAY_MINUTES[interval])

    def _minutes(self, source: MarketData, symbol: str, day: MarketDay,
                 now: datetime) -> tuple[datetime, list[SessionBar]]:
        """M1 bars of one session day: from disk once the day has settled, from memory within
        the live TTL, else one provider read. A day is written to disk only once settled (its
        last minutes are final) and only if it has bars, so a read just after the close or an
        empty or transient answer is never kept for good."""
        settled = now >= day.extended_close + self.settings.closed_day_settle
        path = self.settings.cache_dir / symbol / f"{day.ny_date.isoformat()}.json"
        key = (symbol, BarSpan.M1.value, day.ny_date)
        if settled:
            cached = read_day_file(path, symbol)
            if cached is not None:
                return cached
        live = self._cached(key, now)
        if live is not None:
            return live.read_at, live.rows
        end = min(now, day.extended_close)
        got = source.history_bars(symbol, BarSpan.M1, end, self.settings.bars_per_request)
        rows = [r for r in got if r.bar.symbol == symbol and ny_date_of(r.bar.start) == day.ny_date]
        if len(got) >= self.settings.bars_per_request and len(rows) == len(got):
            raise ChartRefused("unavailable", f"one request of {len(got)} M1 bars did not reach "
                               f"the start of {day.ny_date.isoformat()} "
                               "([chart] bars_per_request)")
        for row in rows:
            place(day, row)  # refused before anything is cached
        if settled and rows:
            write_day_file(path, symbol, day.ny_date, now, rows)
        else:
            self._remember(key, now + self.settings.live_ttl, now, rows)
        return now, rows

    def _remember(self, key: tuple[str, str, date | None], expires: datetime, read_at: datetime,
                  rows: list[SessionBar]) -> None:
        with self._lock:
            self._live = {k: v for k, v in self._live.items() if v.expires > read_at}
            self._live[key] = _Live(expires, read_at, rows)

    # ------------------------------------------------------------ daily

    def _daily(self, source: MarketData, symbol: str, interval: ChartInterval, months: int,
               now: datetime) -> tuple[datetime, list[ChartBarV1]]:
        today = ny_date_of(now)
        first = bucket_day(months_back(today, months), interval)
        read_at, rows = self._daily_rows(source, symbol, now)
        days: list[tuple[date, Bar]] = []
        for row in rows:
            day = ny_date_of(row.bar.start)
            if days and day <= days[-1][0]:
                raise ChartRefused("unavailable", f"{symbol} daily bars: two bars or a step back "
                                   f"at {day.isoformat()}")
            days.append((day, row.bar))
        return read_at, aggregate_daily([(d, b) for d, b in days if d >= first], interval)

    def _daily_rows(self, source: MarketData, symbol: str,
                    now: datetime) -> tuple[datetime, list[SessionBar]]:
        """D bars from the start of the week or month a year back (enough for every daily
        range and interval), shared by all of them. Kept across midnight until it expires: a
        series read earlier starts earlier, so it still covers every range."""
        today = ny_date_of(now)
        live = self._cached((symbol, BarSpan.D.value, None), now)
        if live is not None:
            return live.read_at, live.rows
        back = months_back(today, max(RANGE_MONTHS.values()))
        first = min(bucket_day(back, ChartInterval.W1), bucket_day(back, ChartInterval.MO1))
        count = (today - first).days + 1  # calendar days: never fewer than the sessions
        if count > self.settings.bars_per_request:
            raise ChartRefused("unavailable", f"a year of daily bars needs up to {count} bars, "
                               "more than [chart] bars_per_request")
        got = source.history_bars(symbol, BarSpan.D, now, count)
        rows = [r for r in got if r.bar.symbol == symbol]
        self._remember((symbol, BarSpan.D.value, None), self._daily_expiry(now), now, rows)
        return now, rows

    def _daily_expiry(self, now: datetime) -> datetime:
        """While today's session is not over: the live TTL. After it, on a weekend or holiday:
        the next session's pre-market open (the live TTL when the calendar cannot say)."""
        calendar = self._calendar_()
        day = ny_date_of(now)
        try:
            if now < calendar.day(day).extended_close:
                return now + self.settings.live_ttl
        except CalendarRefused as exc:
            if exc.cause not in SKIPPED_DAYS:
                return now + self.settings.live_ttl
        for _ in range(self.settings.session_lookback_days):
            day += ONE_DAY
            try:
                return calendar.day(day).premarket_open
            except CalendarRefused as exc:
                if exc.cause not in SKIPPED_DAYS:
                    break
        return now + self.settings.live_ttl


# ---------------------------------------------------------------- disk cache of closed days


def write_day_file(path: Path, symbol: str, day: date, read_at: datetime,
                   rows: Sequence[SessionBar]) -> None:
    contract.write_atomic(path, contract.dumps({
        "symbol": symbol, "day": day, "read_at": read_at,
        "bars": [{"start": r.bar.start, "open": r.bar.open, "high": r.bar.high,
                  "low": r.bar.low, "close": r.bar.close, "volume": r.bar.volume,
                  "session": r.session} for r in rows]}))


def read_day_file(path: Path, symbol: str) -> tuple[datetime, list[SessionBar]] | None:
    """A cached closed day, or None when there is none or it cannot be read (then the day is
    read from the provider again and the file replaced)."""
    try:
        raw: Any = json.loads(path.read_text("utf-8"))
        if raw["symbol"] != symbol:
            return None
        rows = [SessionBar(Bar(symbol, contract.parse_time(b["start"]), Decimal(b["open"]),
                               Decimal(b["high"]), Decimal(b["low"]), Decimal(b["close"]),
                               Decimal(b["volume"])), b["session"]) for b in raw["bars"]]
        return contract.parse_time(raw["read_at"]), rows
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, contract.ContractError):
        return None
