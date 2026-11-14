"""Prior-session history: paging, refusals, per-day cache (fake source, real calendar)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aperture import config
from aperture.broker.base import RateLimited
from aperture.calendar import SessionCalendar
from aperture.data.base import FakeMarketData
from aperture.data.history import (
    HistoryCause,
    HistoryReader,
    HistorySettings,
    HistoryUnavailable,
)
from aperture.types import Bar

ROOT = Path(__file__).resolve().parents[2]
CALENDAR = SessionCalendar.load(ROOT / "config" / "calendar")
MONDAY = date(2026, 11, 16)
WEEK = [date(2026, 11, d) for d in (9, 10, 11, 12, 13)]  # the five sessions before MONDAY


def session_bars(symbol: str, day: date, minutes: int | None = None,
                 volume: int = 1000) -> list[Bar]:
    market = CALENDAR.day(day)
    length = int((market.regular_close - market.regular_open).total_seconds() // 60)
    one = Decimal(10)
    return [Bar(symbol, market.regular_open + timedelta(minutes=i), one, one, one, one,
                Decimal(volume)) for i in range(minutes if minutes is not None else length)]


def reader(source, tmp_path: Path | None, **overrides: int) -> HistoryReader:
    values = dict(bars_per_request=1200, max_pages=4, max_lookback_days=14)
    values.update(overrides)
    return HistoryReader(source, CALENDAR, HistorySettings(**values), tmp_path)


def week_source(symbol: str = "AAA") -> FakeMarketData:
    md = FakeMarketData()
    for day in [date(2026, 11, 6), *WEEK, MONDAY]:  # one older and one newer session too
        md.add_bars(session_bars(symbol, day))
    return md


def test_five_prior_sessions_in_two_pages_oldest_first(tmp_path):
    md = week_source()
    history = reader(md, tmp_path).prior_sessions("AAA", MONDAY, 5)
    assert history.sessions == tuple(WEEK)
    assert len(history.bars) == 5 * 390 and history.pages == 2
    assert [b.start for b in history.bars] == sorted(b.start for b in history.bars)
    assert history.bars[0].start == CALENDAR.day(WEEK[0]).regular_open
    assert history.bars[-1].start == CALENDAR.day(WEEK[-1]).regular_close - timedelta(minutes=1)
    assert len(history.bars_of(WEEK[2])) == 390
    assert [name for name, _ in md.calls] == ["bars_before", "bars_before"]


def test_cached_in_memory_then_on_disk_for_the_same_day(tmp_path):
    md = week_source()
    first = reader(md, tmp_path).prior_sessions("AAA", MONDAY, 5)
    again = reader(md, tmp_path)
    loaded = again.prior_sessions("AAA", MONDAY, 5)
    assert loaded.pages == 0 and loaded.bars == first.bars and loaded.sessions == first.sessions
    assert again.prior_sessions("AAA", MONDAY, 5) is loaded
    assert len(md.calls) == 2  # only the first reader asked the provider
    assert (tmp_path / "2026-11-16" / "AAA-5.jsonl").exists()


def test_skips_weekend_holiday_and_handles_a_half_day(tmp_path):
    days = [date(2026, 11, d) for d in (20, 23, 24, 25, 27)]  # 26th Thanksgiving, 27th half day
    md = FakeMarketData()
    for day in days:
        md.add_bars(session_bars("AAA", day))
    history = reader(md, tmp_path).prior_sessions("AAA", date(2026, 11, 30), 5)
    assert history.sessions == tuple(days)
    assert len(history.bars_of(date(2026, 11, 27))) == 210  # 09:30-13:00


def test_thin_minutes_are_kept_but_an_empty_session_is_refused(tmp_path):
    md = FakeMarketData()
    for day in [date(2026, 11, 6), *WEEK]:
        md.add_bars(session_bars("AAA", day, minutes=7))  # a thin symbol
    assert len(reader(md, None).prior_sessions("AAA", MONDAY, 5).bars) == 35
    gap = FakeMarketData()
    for day in [date(2026, 11, 6), *WEEK[1:]]:  # no trading on Nov 9 (halted all day)
        gap.add_bars(session_bars("AAA", day))
    with pytest.raises(HistoryUnavailable) as caught:
        reader(gap, tmp_path).prior_sessions("AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.EMPTY_SESSION and "2026-11-09" in caught.value.detail
    assert not list(tmp_path.rglob("*.jsonl"))  # a refusal is never cached


def test_page_limit(tmp_path):
    with pytest.raises(HistoryUnavailable) as caught:
        reader(week_source(), tmp_path, max_pages=1).prior_sessions("AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.PAGE_LIMIT


class IgnoresEnd(FakeMarketData):
    """A provider that does not apply the end time: always the newest bars."""

    def bars_before(self, symbol: str, end: datetime, count: int) -> list[Bar]:
        return session_bars(symbol, MONDAY)[-count:]


class IgnoresEndFiltered(FakeMarketData):
    """What WebullMarketData does if Webull ignores end_time: the newest `count` bars, then the
    adapter's own filter to bars before `end`."""

    def __init__(self) -> None:
        super().__init__()
        for day in [*WEEK, MONDAY]:  # asked during Monday: today's bars are the newest
            self.add_bars(session_bars("AAA", day))

    def bars_before(self, symbol: str, end: datetime, count: int) -> list[Bar]:
        newest = sorted(self._bars[symbol], key=lambda b: b.start)[-count:]
        return [b for b in newest if b.start < end]


def test_end_time_ignored_through_the_adapter_filter_is_incomplete():
    source = IgnoresEndFiltered()
    rd = reader(source, None)
    with pytest.raises(HistoryUnavailable) as caught:
        rd.prior_sessions("AAA", MONDAY, 3)  # first page 1200 bars reaches only 30 into Nov 11
    assert caught.value.cause is HistoryCause.INCOMPLETE
    assert "2026-11-11" in caught.value.detail
    calls = len(source.calls)
    with pytest.raises(HistoryUnavailable):
        rd.prior_sessions("AAA", MONDAY, 3)  # the refusal is remembered: no new requests
    assert len(source.calls) == calls


def test_no_older_history_is_incomplete(tmp_path):
    md = FakeMarketData()
    for day in WEEK[2:]:  # listed on Nov 11
        md.add_bars(session_bars("AAA", day))
    with pytest.raises(HistoryUnavailable) as caught:
        reader(md, tmp_path).prior_sessions("AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.INCOMPLETE


def test_end_time_not_applied_is_refused():
    with pytest.raises(HistoryUnavailable) as caught:
        reader(IgnoresEnd(), None).prior_sessions("AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.NO_PROGRESS


class Scripted(FakeMarketData):
    def __init__(self, page: list[Bar]) -> None:
        super().__init__()
        self.page = page

    def bars_before(self, symbol: str, end: datetime, count: int) -> list[Bar]:
        return self.page


def test_conflicting_and_out_of_session_bars_are_refused():
    bar = session_bars("AAA", WEEK[0], minutes=1)[0]
    revised = Bar(bar.symbol, bar.start, bar.open, bar.high, bar.low, bar.close, Decimal(1))
    with pytest.raises(HistoryUnavailable) as caught:
        reader(Scripted([bar, revised]), None).prior_sessions("AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.CONFLICT
    premarket = Bar("AAA", CALENDAR.day(WEEK[2]).premarket_open, bar.open, bar.high, bar.low,
                    bar.close, bar.volume)
    with pytest.raises(HistoryUnavailable) as caught:
        reader(Scripted([bar, premarket]), None).prior_sessions("AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.OUTSIDE_SESSION


def test_lookback_calendar_and_request_refusals():
    md = week_source()
    for kwargs, args, cause in [
        ({"max_lookback_days": 3}, ("AAA", MONDAY, 5), HistoryCause.LOOKBACK),
        ({}, ("AAA", date(2025, 1, 6), 5), HistoryCause.CALENDAR),  # before calendar coverage
        ({}, ("../x", MONDAY, 5), HistoryCause.INVALID_REQUEST),
        ({}, ("AAA", MONDAY, 0), HistoryCause.INVALID_REQUEST),
    ]:
        with pytest.raises(HistoryUnavailable) as caught:
            reader(md, None, **kwargs).prior_sessions(*args)
        assert caught.value.cause is cause


def test_budget_refusal_propagates_and_nothing_is_cached(tmp_path):
    md = week_source()
    md.limit("bars_before", 1)
    with pytest.raises(RateLimited):
        reader(md, tmp_path).prior_sessions("AAA", MONDAY, 5)
    assert not list(tmp_path.rglob("*.jsonl"))


def test_corrupt_cache_file_is_refused_not_refetched(tmp_path):
    path = tmp_path / "2026-11-16" / "AAA-5.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{not json\n", encoding="utf-8")
    md = week_source()
    with pytest.raises(HistoryUnavailable) as caught:
        reader(md, tmp_path).prior_sessions("AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.CACHE and md.calls == []


def test_from_config_paths():
    cfg = config.load(ROOT / "config" / "bot.toml")
    built = HistoryReader.from_config(cfg, week_source(), CALENDAR)
    assert isinstance(built, HistoryReader)
    settings = HistorySettings.from_config(cfg)
    assert settings.bars_per_request == cfg.market_data.history_bars_per_request
