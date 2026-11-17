"""Prior-session history: paging, refusals, per-day cache (fake source, real calendar)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import RateLimited
from tickr.calendar import SessionCalendar
from tickr.data.base import FakeMarketData, SymbolsRefused
from tickr.data.history import (
    HistoryCause,
    HistoryReader,
    HistorySettings,
    HistoryUnavailable,
    SessionHistory,
)
from tickr.types import Bar

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
    values = dict(bars_per_request=1200, max_pages=4, max_lookback_days=14,
                  symbols_per_request=20, memory_symbols=300)
    values.update(overrides)
    return HistoryReader(source, CALENDAR, HistorySettings(**values), tmp_path)


def one(rd: HistoryReader, symbol: str, before: date, sessions: int) -> SessionHistory:
    """One symbol's answer: its history, or its refusal / the call's error raised."""
    read = rd.prior_sessions([symbol], before, sessions)
    if symbol in read.found:
        return read.found[symbol]
    if symbol in read.refused:
        raise read.refused[symbol]
    assert read.error is not None
    raise read.error


def week_source(symbol: str = "AAA") -> FakeMarketData:
    md = FakeMarketData()
    for day in [date(2026, 11, 6), *WEEK, MONDAY]:  # one older and one newer session too
        md.add_bars(session_bars(symbol, day))
    return md


def test_five_prior_sessions_in_two_pages_oldest_first(tmp_path):
    md = week_source()
    history = one(reader(md, tmp_path), "AAA", MONDAY, 5)
    assert history.sessions == tuple(WEEK)
    assert len(history.bars) == 5 * 390 and history.pages == 2
    assert [b.start for b in history.bars] == sorted(b.start for b in history.bars)
    assert history.bars[0].start == CALENDAR.day(WEEK[0]).regular_open
    assert history.bars[-1].start == CALENDAR.day(WEEK[-1]).regular_close - timedelta(minutes=1)
    assert len(history.bars_of(WEEK[2])) == 390
    assert [name for name, _ in md.calls] == ["bars_before", "bars_before"]


def test_cached_in_memory_then_on_disk_for_the_same_day(tmp_path):
    md = week_source()
    first = one(reader(md, tmp_path), "AAA", MONDAY, 5)
    again = reader(md, tmp_path)
    loaded = one(again, "AAA", MONDAY, 5)
    assert loaded.pages == 0 and loaded.bars == first.bars and loaded.sessions == first.sessions
    assert one(again, "AAA", MONDAY, 5) is loaded
    assert len(md.calls) == 2  # only the first reader asked the provider
    assert (tmp_path / "2026-11-16" / "AAA-5.jsonl").exists()


def test_memory_holds_the_most_recently_asked_and_the_rest_come_from_disk(tmp_path):
    """The memory cache is bounded: a history pushed out is read again from its file, never
    from the provider."""
    md = FakeMarketData()
    for symbol in ("AAA", "BBB", "CCC"):
        for day in [date(2026, 11, 6), *WEEK, MONDAY]:
            md.add_bars(session_bars(symbol, day))
    rd = reader(md, tmp_path, memory_symbols=2)
    first = {s: one(rd, s, MONDAY, 5) for s in ("AAA", "BBB")}
    assert one(rd, "AAA", MONDAY, 5) is first["AAA"]  # AAA is now the most recent
    asked = len(md.calls)
    one(rd, "CCC", MONDAY, 5)  # pushes out BBB, the least recently asked
    assert len(rd._memory) == 2
    assert one(rd, "AAA", MONDAY, 5) is first["AAA"]  # still in memory
    again = one(rd, "BBB", MONDAY, 5)  # from its file
    assert again is not first["BBB"] and again.bars == first["BBB"].bars and again.pages == 0
    assert len(md.calls) == asked + 2  # only CCC's two pages went to the provider


def test_a_history_whose_write_failed_is_never_pushed_out(tmp_path, monkeypatch):
    md = FakeMarketData()
    for symbol in ("AAA", "BBB", "CCC"):
        for day in [date(2026, 11, 6), *WEEK, MONDAY]:
            md.add_bars(session_bars(symbol, day))
    rd = reader(md, tmp_path, memory_symbols=1)
    store = HistoryReader._store

    def fail_for_aaa(self, before, sessions, history):
        if history.symbol == "AAA":
            raise HistoryUnavailable("AAA", HistoryCause.CACHE, "disk full")
        store(self, before, sessions, history)

    monkeypatch.setattr(HistoryReader, "_store", fail_for_aaa)
    with pytest.raises(HistoryUnavailable):
        one(rd, "AAA", MONDAY, 5)  # CACHE refusal; the history stays in memory
    one(rd, "BBB", MONDAY, 5)
    one(rd, "CCC", MONDAY, 5)
    asked = len(md.calls)
    assert one(rd, "AAA", MONDAY, 5).pages > 0  # memory serves it: no file to read
    assert len(md.calls) == asked


def test_skips_weekend_holiday_and_handles_a_half_day(tmp_path):
    days = [date(2026, 11, d) for d in (20, 23, 24, 25, 27)]  # 26th Thanksgiving, 27th half day
    md = FakeMarketData()
    for day in days:
        md.add_bars(session_bars("AAA", day))
    history = one(reader(md, tmp_path), "AAA", date(2026, 11, 30), 5)
    assert history.sessions == tuple(days)
    assert len(history.bars_of(date(2026, 11, 27))) == 210  # 09:30-13:00


def test_thin_minutes_are_kept_but_an_empty_session_is_refused(tmp_path):
    md = FakeMarketData()
    for day in [date(2026, 11, 6), *WEEK]:
        md.add_bars(session_bars("AAA", day, minutes=7))  # a thin symbol
    assert len(one(reader(md, None), "AAA", MONDAY, 5).bars) == 35
    gap = FakeMarketData()
    for day in [date(2026, 11, 6), *WEEK[1:]]:  # no trading on Nov 9 (halted all day)
        gap.add_bars(session_bars("AAA", day))
    with pytest.raises(HistoryUnavailable) as caught:
        one(reader(gap, tmp_path), "AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.EMPTY_SESSION and "2026-11-09" in caught.value.detail
    assert not list(tmp_path.rglob("*.jsonl"))  # a refusal is never cached


def test_page_limit(tmp_path):
    with pytest.raises(HistoryUnavailable) as caught:
        one(reader(week_source(), tmp_path, max_pages=1), "AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.PAGE_LIMIT


class IgnoresEnd(FakeMarketData):
    """A provider that does not apply the end time: always the newest bars."""

    def bars_before(self, symbols, end: datetime, count: int, *, budget_reserve=0) -> list[Bar]:
        return [b for s in symbols for b in session_bars(s, MONDAY)[-count:]]


class IgnoresEndFiltered(FakeMarketData):
    """What WebullMarketData does if Webull ignores end_time: the newest `count` bars, then the
    adapter's own filter to bars before `end`."""

    def __init__(self) -> None:
        super().__init__()
        for day in [*WEEK, MONDAY]:  # asked during Monday: today's bars are the newest
            self.add_bars(session_bars("AAA", day))

    def bars_before(self, symbols, end: datetime, count: int, *, budget_reserve=0) -> list[Bar]:
        self.calls.append(("bars_before", tuple(symbols)))
        newest = sorted(self._bars[symbols[0]], key=lambda b: b.start)[-count:]
        return [b for b in newest if b.start < end]


def test_end_time_ignored_through_the_adapter_filter_is_incomplete():
    source = IgnoresEndFiltered()
    rd = reader(source, None)
    with pytest.raises(HistoryUnavailable) as caught:
        one(rd, "AAA", MONDAY, 3)  # first page 1200 bars reaches only 30 into Nov 11
    assert caught.value.cause is HistoryCause.INCOMPLETE
    assert "2026-11-11" in caught.value.detail
    calls = len(source.calls)
    with pytest.raises(HistoryUnavailable):
        one(rd, "AAA", MONDAY, 3)  # the refusal is remembered: no new requests
    assert len(source.calls) == calls


def test_no_older_history_is_incomplete(tmp_path):
    md = FakeMarketData()
    for day in WEEK[2:]:  # listed on Nov 11
        md.add_bars(session_bars("AAA", day))
    with pytest.raises(HistoryUnavailable) as caught:
        one(reader(md, tmp_path), "AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.INCOMPLETE


def test_end_time_not_applied_is_refused():
    with pytest.raises(HistoryUnavailable) as caught:
        one(reader(IgnoresEnd(), None), "AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.NO_PROGRESS


class Scripted(FakeMarketData):
    def __init__(self, page: list[Bar]) -> None:
        super().__init__()
        self.page = page

    def bars_before(self, symbols, end: datetime, count: int, *, budget_reserve=0) -> list[Bar]:
        return self.page


def test_conflicting_and_out_of_session_bars_are_refused():
    bar = session_bars("AAA", WEEK[0], minutes=1)[0]
    revised = Bar(bar.symbol, bar.start, bar.open, bar.high, bar.low, bar.close, Decimal(1))
    with pytest.raises(HistoryUnavailable) as caught:
        one(reader(Scripted([bar, revised]), None), "AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.CONFLICT
    premarket = Bar("AAA", CALENDAR.day(WEEK[2]).premarket_open, bar.open, bar.high, bar.low,
                    bar.close, bar.volume)
    with pytest.raises(HistoryUnavailable) as caught:
        one(reader(Scripted([bar, premarket]), None), "AAA", MONDAY, 5)
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
            one(reader(md, None, **kwargs), *args)
        assert caught.value.cause is cause


def test_budget_refusal_propagates_and_nothing_is_cached(tmp_path):
    md = week_source()
    md.limit("bars_before", 1)
    with pytest.raises(RateLimited):
        one(reader(md, tmp_path), "AAA", MONDAY, 5)
    assert not list(tmp_path.rglob("*.jsonl"))


def test_corrupt_cache_file_is_refused_not_refetched(tmp_path):
    path = tmp_path / "2026-11-16" / "AAA-5.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{not json\n", encoding="utf-8")
    md = week_source()
    with pytest.raises(HistoryUnavailable) as caught:
        one(reader(md, tmp_path), "AAA", MONDAY, 5)
    assert caught.value.cause is HistoryCause.CACHE and md.calls == []


def test_from_config_paths():
    cfg = config.load(ROOT / "config" / "bot.toml")
    built = HistoryReader.from_config(cfg, week_source(), CALENDAR)
    assert isinstance(built, HistoryReader)
    settings = HistorySettings.from_config(cfg)
    assert settings.bars_per_request == cfg.market_data.history_bars_per_request


# --- batches: many symbols per request -------------------------------------------------------


def many_source(symbols, minutes: dict[str, int] | None = None,
                days=(date(2026, 11, 6), *WEEK, MONDAY)) -> FakeMarketData:
    md = FakeMarketData()
    for symbol in symbols:
        for day in days:
            md.add_bars(session_bars(symbol, day, (minutes or {}).get(symbol)))
    return md


def test_a_batch_pages_together_and_a_finished_symbol_drops_out(tmp_path):
    md = many_source(["AAA", "BBB", "CCC"], {"CCC": 7})  # CCC thin: one page reaches back
    read = reader(md, tmp_path).prior_sessions(["AAA", "BBB", "CCC"], MONDAY, 5,
                                               budget_reserve=12)
    assert read.error is None and read.refused == {} and read.requests == 2
    assert md.calls == [("bars_before", ("AAA", "BBB", "CCC")), ("bars_before", ("AAA", "BBB"))]
    assert md.bars_reserves == [12, 12]  # the reserve reaches every page
    assert [len(read.found[s].bars) for s in ("AAA", "BBB", "CCC")] == [1950, 1950, 35]
    assert read.found["CCC"].pages == 1 and read.found["AAA"].pages == 2
    assert (tmp_path / "2026-11-16" / "CCC-5.jsonl").exists()


def test_a_symbol_that_reached_further_back_is_carried_along_without_gaps():
    # MMM trades 300 minutes a day: its first page reaches Nov 10 09:30, further than AAA's
    # (Nov 10 15:30). The second page asks both at AAA's end; MMM's overlap merges.
    md = many_source(["AAA", "MMM"], {"MMM": 300})
    read = reader(md, None, max_pages=2).prior_sessions(["AAA", "MMM"], MONDAY, 5)
    assert read.requests == 2 and read.refused == {}
    assert read.found["MMM"].pages == 2  # one own page, one carried along
    assert len(read.found["MMM"].bars) == 5 * 300 and len(read.found["AAA"].bars) == 1950
    for history in read.found.values():
        assert all(len(history.bars_of(day)) for day in WEEK)


def test_one_symbols_refusal_never_fails_the_others(tmp_path):
    md = many_source(["AAA"])
    for day in [date(2026, 11, 6), *WEEK[1:]]:  # GAP halted all day on Nov 9
        md.add_bars(session_bars("GAP", day))
    for day in WEEK[2:]:  # NEW listed on Nov 11
        md.add_bars(session_bars("NEW", day))
    read = reader(md, tmp_path).prior_sessions(["GAP", "AAA", "NEW", "../x"], MONDAY, 5)
    assert read.found.keys() == {"AAA"} and read.error is None
    assert {s: r.cause for s, r in read.refused.items()} == {
        "GAP": HistoryCause.EMPTY_SESSION, "NEW": HistoryCause.INCOMPLETE,
        "../x": HistoryCause.INVALID_REQUEST}
    calls = len(md.calls)
    again = reader(md, tmp_path)  # a fresh reader: AAA from disk; refusals are per reader
    assert again.prior_sessions(["AAA"], MONDAY, 5).requests == 0 and len(md.calls) == calls


class UnknownSymbol(FakeMarketData):
    """The bars route refuses any request that holds BAD (Webull 417 INVALID_SYMBOL)."""

    def bars_before(self, symbols, end, count, *, budget_reserve=0):
        if "BAD" in symbols:
            self.calls.append(("refused", tuple(symbols)))
            raise SymbolsRefused("BARS", 417, "INVALID_SYMBOL")
        return super().bars_before(symbols, end, count, budget_reserve=budget_reserve)


def test_an_unknown_symbol_is_isolated_and_refused_alone():
    md = UnknownSymbol()
    for symbol in ("AAA", "BBB"):
        for day in [date(2026, 11, 6), *WEEK]:
            md.add_bars(session_bars(symbol, day))
    rd = reader(md, None)
    read = rd.prior_sessions(["AAA", "BAD", "BBB"], MONDAY, 5)
    assert read.found.keys() == {"AAA", "BBB"}
    assert read.refused["BAD"].cause is HistoryCause.SYMBOL_REFUSED
    # page 1: whole, then halves [AAA] and [BAD, BBB], then [BAD] and [BBB]; page 2 without BAD
    assert md.calls == [("refused", ("AAA", "BAD", "BBB")), ("bars_before", ("AAA",)),
                        ("refused", ("BAD", "BBB")), ("refused", ("BAD",)),
                        ("bars_before", ("BBB",)), ("bars_before", ("AAA", "BBB"))]
    assert read.requests == 6
    calls = len(md.calls)
    assert rd.prior_sessions(["BAD"], MONDAY, 5).refused["BAD"].cause is (
        HistoryCause.SYMBOL_REFUSED)
    assert len(md.calls) == calls  # remembered for the day


def test_request_count_is_batches_times_pages():
    symbols = [f"S{i:02d}" for i in range(45)]
    md = many_source(symbols)
    read = reader(md, None).prior_sessions(symbols, MONDAY, 5)
    assert read.requests == 6 and len(md.calls) == 6  # ceil(45 / 20) = 3 batches x 2 pages
    assert [len(args) for _, args in md.calls] == [20, 20, 20, 20, 5, 5]
    assert md.calls[0][1] == tuple(symbols[:20])  # in the order asked
    assert len(read.found) == 45


def test_a_budget_stop_keeps_the_symbols_finished_before_it(tmp_path):
    symbols = [f"S{i:02d}" for i in range(45)]
    md = many_source(symbols)
    md.limit("bars_before", 3)  # batch 1 (two pages), then batch 2's first page only
    rd = reader(md, tmp_path)
    read = rd.prior_sessions(symbols, MONDAY, 5)
    assert isinstance(read.error, RateLimited)
    assert list(read.found) == symbols[:20] and read.refused == {}
    assert read.requests == 3  # a budget refusal sends nothing
    md.limit("bars_before", 100)
    again = rd.prior_sessions(symbols, MONDAY, 5)
    assert again.error is None and len(again.found) == 45
    assert again.requests == 4  # only the 25 not finished: two batches x two pages
