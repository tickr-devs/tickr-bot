"""GET /chart: aggregation, session edges and extended flags from the calendar, the cache, the
chart's own budget, validation, the demo world and the HTTP contract (fake sources only)."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import config
from aperture.api.chart import (
    Chart,
    ChartRefused,
    ChartSettings,
    aggregate_daily,
    aggregate_intraday,
    months_back,
    parse_query,
    place,
)
from aperture.api.contract import ChartInterval, ChartRange
from aperture.api.server import make_server, settings_from_config
from aperture.broker.budget import RequestBudget, RouteLimit
from aperture.broker.webull import Route, WebullMarketData, WireResponse
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.data.base import BarSpan, FakeMarketData, SessionBar
from aperture.demo.world import World, day_bars, flat_bars
from aperture.types import Bar

from .schema_check import errors
from .test_server import doc, request, schema

ROOT = Path(__file__).resolve().parents[2]
CALENDAR_DIR = ROOT / "config" / "calendar"
CALENDAR = SessionCalendar.load(CALENDAR_DIR)
MONDAY = date(2026, 11, 16)
FRIDAY = date(2026, 11, 13)
HALF_DAY = date(2026, 11, 27)
TTL = timedelta(seconds=30)
SETTLE = timedelta(minutes=5)
IV, R = ChartInterval, ChartRange


class Scripted:
    """A recorded transport: answers in order and keeps what was sent."""

    def __init__(self, *responses: WireResponse) -> None:
        self.responses = list(responses)
        self.sent: list[Route] = []

    def send(self, route: Route, params: dict) -> WireResponse:
        self.sent.append(route)
        return self.responses.pop(0)

    def is_own_account(self, account_id: str) -> bool:
        return False


def ny(day: date, clock: str) -> datetime:
    return datetime.combine(day, wall.fromisoformat(clock), NEW_YORK).astimezone(UTC)


def m1(day: date, clock: str, session: str, o="10", h="11", lo="9", c="10.5", v="100",
       symbol: str = "ABCD") -> SessionBar:
    return SessionBar(Bar(symbol, ny(day, clock), D(o), D(h), D(lo), D(c), D(v)), session)


def daily(day: date, o: str, h: str, lo: str, c: str, v: str = "1000") -> SessionBar:
    return SessionBar(Bar("ABCD", ny(day, "00:00"), D(o), D(h), D(lo), D(c), D(v)), None)


def settings(tmp_path: Path, **overrides) -> ChartSettings:
    base = ChartSettings(webull_enabled=False, bars_per_request=1200, live_ttl=TTL,
                         closed_day_settle=SETTLE,
                         cache_dir=tmp_path / "chart", calendar_dir=CALENDAR_DIR,
                         session_lookback_days=14)
    return replace(base, **overrides)


def a_day(tmp_path: Path):
    """A fake source with a full Monday in every session part, and a chart over it."""
    fake = FakeMarketData("fake")
    fake.add_history_bars(BarSpan.M1, [
        m1(MONDAY, "04:00", "PRE", "10", "10.2", "9.9", "10.1", "5"),
        m1(MONDAY, "09:29", "PRE", "10.1", "10.4", "10", "10.3", "7"),
        m1(MONDAY, "09:30", "RTH", "10.3", "10.6", "10.2", "10.5", "300"),
        m1(MONDAY, "09:31", "RTH", "10.5", "10.9", "10.4", "10.8", "200"),
        m1(MONDAY, "15:59", "RTH", "11", "11.2", "10.9", "11.1", "900"),
        m1(MONDAY, "16:00", "ATH", "11.1", "11.1", "11", "11", "40"),
        m1(MONDAY, "19:59", "ATH", "11", "11.05", "10.95", "11.02", "3"),
    ])
    return fake, Chart(settings(tmp_path), fake)


# ---------------------------------------------------------------- aggregation


def test_hour_candles_never_cross_a_session_edge_and_keep_partial_buckets():
    day = CALENDAR.day(MONDAY)
    rows = [m1(MONDAY, t, s) for t, s in (
        ("09:00", "PRE"), ("09:29", "PRE"), ("09:30", "RTH"), ("10:29", "RTH"),
        ("10:30", "RTH"), ("15:30", "RTH"), ("15:59", "RTH"), ("16:00", "ATH"))]
    candles = aggregate_intraday([place(day, r) for r in rows], 60)
    starts = [(c.start.astimezone(NEW_YORK).strftime("%H:%M"), c.extended) for c in candles]
    # 09:00-09:29 is the pre-market's last (short) hour; the regular hours start at 09:30; the
    # last regular hour 15:30-15:59 is half an hour; the post-market starts its own at 16:00.
    assert starts == [("09:00", True), ("09:30", False), ("10:30", False), ("15:30", False),
                      ("16:00", True)]


def test_merge_is_exact_decimal_ohlcv_with_missing_minutes():
    day = CALENDAR.day(MONDAY)
    rows = [m1(MONDAY, "09:35", "RTH", "10.01", "10.20", "9.995", "10.10", "100.5"),
            # 09:36 and 09:37 have no bar (no trades): the candle is built from what exists.
            m1(MONDAY, "09:38", "RTH", "10.10", "10.33", "10.00", "10.30", "0.25"),
            m1(MONDAY, "09:39", "RTH", "10.30", "10.31", "9.99", "10.05", "7")]
    (candle,) = aggregate_intraday([place(day, r) for r in rows], 5)
    assert candle.start == ny(MONDAY, "09:35")
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == (
        D("10.01"), D("10.33"), D("9.99"), D("10.05"), D("107.75"))
    (minute,) = aggregate_intraday([place(day, rows[0])], 1)
    assert (minute.open, minute.volume, minute.extended) == (D("10.01"), D("100.5"), False)


def test_thirty_minute_buckets_anchor_on_each_part_start():
    day = CALENDAR.day(MONDAY)
    rows = [m1(MONDAY, "04:29", "PRE"), m1(MONDAY, "04:30", "PRE"), m1(MONDAY, "09:15", "PRE"),
            m1(MONDAY, "09:45", "RTH"), m1(MONDAY, "16:31", "ATH")]
    starts = [c.start for c in aggregate_intraday([place(day, r) for r in rows], 30)]
    assert starts == [ny(MONDAY, t) for t in ("04:00", "04:30", "09:00", "09:30", "16:30")]


def test_half_day_post_market_starts_at_the_early_close():
    day = CALENDAR.day(HALF_DAY)
    assert day.half_day
    placed = place(day, m1(HALF_DAY, "13:00", "ATH"))
    assert placed.extended and placed.part_start == ny(HALF_DAY, "13:00")
    assert not place(day, m1(HALF_DAY, "12:59", "RTH")).extended
    with pytest.raises(ChartRefused, match="calendar says ATH"):
        place(day, m1(HALF_DAY, "13:30", "RTH"))  # a regular label after the early close
    with pytest.raises(ChartRefused, match="outside the calendar"):
        place(day, m1(HALF_DAY, "17:00", "ATH"))  # post-market ends at 17:00 on a half day


def test_bars_outside_sessions_or_mislabelled_or_repeated_are_refused():
    day = CALENDAR.day(MONDAY)
    with pytest.raises(ChartRefused, match="outside"):
        place(day, m1(MONDAY, "03:59", "PRE"))
    with pytest.raises(ChartRefused, match="outside"):
        place(day, m1(MONDAY, "20:00", "ATH"))
    with pytest.raises(ChartRefused, match="provider says PRE, the calendar says RTH"):
        place(day, m1(MONDAY, "09:30", "PRE"))
    twice = place(day, m1(MONDAY, "09:30", "RTH"))
    with pytest.raises(ChartRefused, match="increasing"):
        aggregate_intraday([twice, twice], 5)


def test_weeks_start_on_monday_and_months_on_the_first():
    rows = [(date(2026, 10, 30), daily(date(2026, 10, 30), "9", "9.5", "8.5", "9.2")),
            (date(2026, 11, 2), daily(date(2026, 11, 2), "9.2", "10", "9", "9.8")),
            (date(2026, 11, 6), daily(date(2026, 11, 6), "9.8", "12", "9.7", "11", "500"))]
    bars = [(d, r.bar) for d, r in rows]
    weeks = aggregate_daily(bars, IV.W1)
    assert [w.start for w in weeks] == [ny(date(2026, 10, 26), "00:00"),
                                        ny(date(2026, 11, 2), "00:00")]
    assert (weeks[1].open, weeks[1].high, weeks[1].low, weeks[1].close, weeks[1].volume) == (
        D("9.2"), D("12"), D("9"), D("11"), D("1500"))
    months = aggregate_daily(bars, IV.MO1)
    assert [m.start for m in months] == [ny(date(2026, 10, 1), "00:00"),
                                         ny(date(2026, 11, 1), "00:00")]
    assert all(not c.extended for c in weeks + months)
    assert len(aggregate_daily(bars, IV.D1)) == 3


def test_months_back_clamps_to_the_shorter_month():
    assert months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_back(date(2026, 11, 15), 12) == date(2025, 11, 15)
    assert months_back(date(2026, 1, 10), 3) == date(2025, 10, 10)


# ---------------------------------------------------------------- reader and cache


def test_one_day_of_five_minute_candles_with_extended_flags(tmp_path):
    fake, chart = a_day(tmp_path)
    now = ny(MONDAY + timedelta(days=1), "03:00")  # Tuesday before its pre-market
    answer = chart.answer("ABCD", IV.M5, R.D1, now)
    flags = [(c.start.astimezone(NEW_YORK).strftime("%H:%M"), c.extended) for c in answer.bars]
    assert flags == [("04:00", True), ("09:25", True), ("09:30", False), ("15:55", False),
                     ("16:00", True), ("19:55", True)]
    assert answer.source == "fake" and answer.cause is None and answer.cached_at == now
    assert fake.calls == [("history_bars", ("ABCD", "M1"))]


def test_closed_day_is_kept_on_disk_and_survives_a_restart(tmp_path):
    fake, chart = a_day(tmp_path)
    now = ny(MONDAY, "21:00")
    first = chart.answer("ABCD", IV.M1, R.D1, now)
    assert (tmp_path / "chart" / "ABCD" / f"{MONDAY.isoformat()}.json").exists()
    restarted = Chart(settings(tmp_path), fake)
    again = restarted.answer("ABCD", IV.M1, R.D1, now + timedelta(minutes=5))
    assert again.bars == first.bars and again.cached_at == now
    assert len(fake.calls) == 1  # the second process read nothing


def test_live_day_is_read_again_only_after_the_ttl(tmp_path):
    fake, chart = a_day(tmp_path)
    now = ny(MONDAY, "19:00")  # the session day is still running (post-market)
    chart.answer("ABCD", IV.M1, R.D1, now)
    chart.answer("ABCD", IV.M30, R.D1, now + TTL - timedelta(seconds=1))
    assert len(fake.calls) == 1
    chart.answer("ABCD", IV.M1, R.D1, now + TTL)
    assert len(fake.calls) == 2
    assert not (tmp_path / "chart").exists()  # an unfinished day is never written to disk


def test_a_day_is_written_only_after_it_settles(tmp_path):
    """A read just after the close may miss the last minute: it stays in memory and is read
    again; only a read after close + settle goes to disk."""
    fake = FakeMarketData("fake")
    fake.add_history_bars(BarSpan.M1, [m1(MONDAY, "10:00", "RTH")])
    chart = Chart(settings(tmp_path), fake)
    path = tmp_path / "chart" / "ABCD" / f"{MONDAY.isoformat()}.json"
    just_closed = ny(MONDAY, "20:00") + timedelta(seconds=5)
    first = chart.answer("ABCD", IV.M1, R.D1, just_closed)
    assert len(first.bars) == 1 and not path.exists()
    fake.add_history_bars(BarSpan.M1, [m1(MONDAY, "19:59", "ATH")])  # finalized late
    assert chart.answer("ABCD", IV.M1, R.D1, just_closed + TTL - timedelta(seconds=1)).bars \
        == first.bars  # memory, within the TTL
    settled = ny(MONDAY, "20:00") + SETTLE
    later = chart.answer("ABCD", IV.M1, R.D1, settled)
    assert len(later.bars) == 2 and path.exists() and len(fake.calls) == 2
    again = Chart(settings(tmp_path), fake).answer("ABCD", IV.M1, R.D1, settled + SETTLE)
    assert again.bars == later.bars and len(fake.calls) == 2  # from disk


def test_a_day_without_bars_is_never_written(tmp_path):
    fake = FakeMarketData("fake")
    chart = Chart(settings(tmp_path), fake)
    now = ny(MONDAY, "21:00")  # settled
    assert chart.answer("ABCD", IV.M1, R.D1, now).bars == ()
    assert not (tmp_path / "chart").exists()
    chart.answer("ABCD", IV.M1, R.D1, now + TTL - timedelta(seconds=1))
    assert len(fake.calls) == 1  # kept in memory for the TTL only
    fake.add_history_bars(BarSpan.M1, [m1(MONDAY, "10:00", "RTH")])
    assert len(chart.answer("ABCD", IV.M1, R.D1, now + TTL).bars) == 1
    assert len(fake.calls) == 2 and (tmp_path / "chart" / "ABCD").exists()


class Blocking(FakeMarketData):
    """A source whose reads of one symbol wait until released."""

    def __init__(self, held: str) -> None:
        super().__init__("fake")
        self.held = held
        self.entered = threading.Event()
        self.release = threading.Event()

    def history_bars(self, symbol, span, end, count):
        if symbol == self.held:
            self.entered.set()
            assert self.release.wait(5)
        return super().history_bars(symbol, span, end, count)


def test_a_slow_read_holds_up_only_its_own_symbol(tmp_path):
    source = Blocking("SLOW")
    chart = Chart(settings(tmp_path), source)
    now = ny(MONDAY, "12:00")
    results: list = []

    def ask(symbol: str) -> None:
        results.append(chart.answer(symbol, IV.D1, R.M1, now))

    slow = threading.Thread(target=ask, args=("SLOW",))
    slow.start()
    assert source.entered.wait(5)
    ask("FAST")  # answered while SLOW's read is still in flight
    second = threading.Thread(target=ask, args=("SLOW",))
    second.start()  # waits for the first SLOW answer, then uses its cache
    source.release.set()
    slow.join(5)
    second.join(5)
    assert [r.symbol for r in results] == ["FAST", "SLOW", "SLOW"]
    assert [c for c in source.calls if c[1][0] == "SLOW"] == [("history_bars", ("SLOW", "D"))]


def test_five_days_skip_the_weekend_and_wait_for_the_pre_market(tmp_path):
    fake = FakeMarketData("fake")
    days = [date(2026, 11, d) for d in (10, 11, 12, 13, 16)]
    fake.add_history_bars(BarSpan.M1, [m1(d, "10:00", "RTH") for d in
                                       [date(2026, 11, 9), *days]])
    chart = Chart(settings(tmp_path), fake)
    answer = chart.answer("ABCD", IV.H1, R.D5, ny(MONDAY, "04:00"))
    assert [c.start for c in answer.bars] == [ny(d, "09:30") for d in days[:-1]]  # no bar yet
    early = Chart(settings(tmp_path / "other"), fake)
    answer = early.answer("ABCD", IV.H1, R.D5, ny(MONDAY, "03:59"))  # Monday not begun yet
    assert [c.start for c in answer.bars] == [ny(d, "09:30") for d in
                                              [date(2026, 11, 9), *days[:-1]]]


def test_an_empty_answer_says_why(tmp_path):
    chart = Chart(settings(tmp_path), FakeMarketData("fake"))
    answer = chart.answer("ZZZZ", IV.M1, R.D1, ny(MONDAY, "12:00"))
    assert answer.bars == () and answer.cause == "no 1m bars from fake for ZZZZ in range 1d"


def test_a_short_page_that_cannot_reach_the_day_start_is_refused(tmp_path):
    fake, _ = a_day(tmp_path)
    chart = Chart(settings(tmp_path, bars_per_request=3), fake)
    with pytest.raises(ChartRefused, match="did not reach the start"):
        chart.answer("ABCD", IV.M1, R.D1, ny(MONDAY, "21:00"))
    assert not (tmp_path / "chart").exists()


def test_daily_series_is_shared_and_kept_until_the_next_pre_market(tmp_path):
    fake = FakeMarketData("fake")
    fake.add_history_bars(BarSpan.D, [
        daily(date(2025, 11, 14), "5", "5", "5", "5"),  # a year back: only in 1y
        daily(date(2026, 10, 30), "9", "9.5", "8.5", "9.2"),
        daily(date(2026, 11, 2), "9.2", "10", "9", "9.8"),
        daily(date(2026, 11, 13), "9.8", "12", "9.7", "11")])
    chart = Chart(settings(tmp_path), fake)
    saturday = ny(date(2026, 11, 14), "12:00")
    days = chart.answer("ABCD", IV.D1, R.M1, saturday)
    # 1m back from 2026-11-14 is 2026-10-14: the 2025 bar is dropped.
    assert [c.start for c in days.bars] == [ny(d, "00:00") for d in (
        date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 13))]
    weeks = chart.answer("ABCD", IV.W1, R.M1, saturday)
    assert [c.start for c in weeks.bars] == [ny(d, "00:00") for d in (
        date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 9))]
    months = chart.answer("ABCD", IV.MO1, R.Y1, ny(date(2026, 11, 16), "03:59"))
    assert [c.start for c in months.bars] == [ny(date(2025, 11, 1), "00:00"),
                                              ny(date(2026, 10, 1), "00:00"),
                                              ny(date(2026, 11, 1), "00:00")]
    assert len(fake.calls) == 1  # one read served all three until Monday's pre-market
    assert fake.calls == [("history_bars", ("ABCD", "D"))]
    chart.answer("ABCD", IV.D1, R.M3, ny(MONDAY, "04:00"))
    assert len(fake.calls) == 2


def test_daily_series_during_a_session_lives_for_the_ttl(tmp_path):
    fake = FakeMarketData("fake")
    chart = Chart(settings(tmp_path), fake)
    now = ny(MONDAY, "11:00")
    chart.answer("ABCD", IV.D1, R.M1, now)
    chart.answer("ABCD", IV.D1, R.M1, now + TTL - timedelta(seconds=1))
    chart.answer("ABCD", IV.D1, R.M1, now + TTL)
    assert len(fake.calls) == 2


def test_two_daily_bars_on_one_date_are_refused(tmp_path):
    fake = FakeMarketData("fake")
    fake.add_history_bars(BarSpan.D, [daily(date(2026, 11, 13), "1", "1", "1", "1"),
                                      daily(date(2026, 11, 13), "1", "1", "1", "1")])
    with pytest.raises(ChartRefused, match="two bars"):
        Chart(settings(tmp_path), fake).answer("ABCD", IV.D1, R.M1, ny(MONDAY, "12:00"))


def test_budget_refusal_is_rate_limited_and_nothing_is_cached(tmp_path):
    fake, chart = a_day(tmp_path)
    fake.limit("history_bars", 0)
    with pytest.raises(ChartRefused) as refused:
        chart.answer("ABCD", IV.M1, R.D1, ny(MONDAY, "21:00"))
    assert refused.value.error == "rate_limited"
    assert not (tmp_path / "chart").exists()


def test_no_source_is_unavailable_with_the_cause(tmp_path):
    with pytest.raises(ChartRefused, match=r"webull_enabled = false") as refused:
        Chart(settings(tmp_path)).answer("ABCD", IV.M1, R.D1, ny(MONDAY, "12:00"))
    assert refused.value.error == "unavailable"


def test_calendar_gap_is_unavailable(tmp_path):
    fake, chart = a_day(tmp_path)
    with pytest.raises(ChartRefused, match="calendar") as refused:
        chart.answer("ABCD", IV.M1, R.D5, ny(date(2026, 1, 3), "12:00"))  # walks into 2025
    assert refused.value.error == "unavailable"


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("query, cause", [
    ({"symbol": "aapl", "interval": "1m", "range": "1d"}, "upper-case"),
    ({"symbol": "A/B", "interval": "1m", "range": "1d"}, "upper-case"),
    ({"symbol": "..", "interval": "1m", "range": "1d"}, "upper-case"),
    ({"symbol": "AAPL", "interval": "2m", "range": "1d"}, "interval must be one of"),
    ({"symbol": "AAPL", "interval": "1m", "range": "2y"}, "range must be one of"),
    ({"symbol": "AAPL", "interval": "1m", "range": "1y"}, "not served for range"),
    ({"symbol": "AAPL", "interval": "1d", "range": "5d"}, "not served for range"),
])
def test_bad_queries_are_refused(query, cause):
    with pytest.raises(ChartRefused, match=cause) as refused:
        parse_query(query)
    assert refused.value.error == "bad_request"


def test_good_queries_parse():
    assert parse_query({"symbol": "BRK.B", "interval": "1mo", "range": "1y"}) == (
        "BRK.B", IV.MO1, R.Y1)
    assert parse_query({"symbol": "AAPL", "interval": "30m", "range": "5d"})[1] is IV.M30


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def served(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    api = settings_from_config(cfg)
    assert api.chart.cache_dir == tmp_path / "data" / "chart" / "sandbox"
    api = replace(api, port=0, chart=replace(api.chart, calendar_dir=CALENDAR_DIR))
    server = make_server(api, lambda: ny(MONDAY, "21:00"))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_http_chart_is_off_by_default(served):
    resp, payload = request(served, "GET", "/chart?symbol=AAPL&interval=1m&range=1d")
    assert resp.status == 503 and doc(payload)["error"] == "unavailable"
    assert "webull_enabled" in doc(payload)["cause"]


@pytest.mark.parametrize("query", [
    "", "?symbol=AAPL&interval=1m", "?symbol=AAPL&interval=1m&range=1d&x=1",
    "?symbol=AAPL&interval=1m&range=1d&range=5d", "?symbol=aapl&interval=1m&range=1d",
    "?symbol=AAPL&interval=1h&range=1y"])
def test_http_bad_queries_are_400(served, query):
    served.chart.use(FakeMarketData("fake"))
    resp, payload = request(served, "GET", "/chart" + query)
    assert resp.status == 400 and doc(payload)["error"] == "bad_request"


def test_http_chart_matches_the_schema(served, tmp_path):
    fake, _ = a_day(tmp_path)
    served.chart.use(fake)
    resp, payload = request(served, "GET", "/chart?symbol=ABCD&interval=5m&range=1d")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("chart-v1"), body) == []
    assert body["interval"] == "5m" and body["range"] == "1d" and body["source"] == "fake"
    assert [b["extended"] for b in body["bars"]] == [True, True, False, False, True, True]
    resp, _ = request(served, "POST", "/chart", {})
    assert resp.status == 405


def test_http_budget_exhaustion_is_429_with_retry_after(served):
    """The real Webull adapter over a recorded transport with a one-request chart budget."""
    budget = RequestBudget(RouteLimit(1, 60, 60), clock=lambda: 0.0)
    transport = Scripted(WireResponse(200, {"result": [{"symbol": "ABCD", "delay_minutes": "0",
                                                         "result": []}]}))
    served.chart.use(WebullMarketData(transport, budget, "US_STOCK", "DAY_1",
                                      "RELATIVE_VOLUME_10D", 100,
                                      utc_now=lambda: ny(MONDAY, "21:00")))
    resp, payload = request(served, "GET", "/chart?symbol=ABCD&interval=1d&range=1m")
    assert resp.status == 200 and doc(payload)["bars"] == []
    resp, payload = request(served, "GET", "/chart?symbol=ABCD&interval=1m&range=1d")
    assert resp.status == 429 and doc(payload)["error"] == "rate_limited"
    assert resp.getheader("Retry-After") == "60"
    assert len(transport.sent) == 1  # nothing queued, nothing retried


# ---------------------------------------------------------------- demo


def test_demo_world_serves_charts(tmp_path):
    market_day = CALENDAR.day(MONDAY)
    world = World(ny(MONDAY, "11:00"), market_day, D("0.01"), D("100"), "demo")
    history = [b for d in (date(2026, 11, 12), FRIDAY)
               for b in flat_bars("ABCD", CALENDAR.day(d), D(20), D(1000))]
    world.add_symbol("ABCD", day_bars("ABCD", market_day, D(20), D(1000),
                                      (("09:30", "10:00", D(22), D(5000)),), D("0.05")), history)
    chart = Chart(settings(tmp_path), world.feed)
    hours = chart.answer("ABCD", IV.H1, R.D5, world.now)
    assert hours.source == "demo" and all(not c.extended for c in hours.bars)
    today = [c for c in hours.bars if c.start >= market_day.regular_open]
    assert [c.start for c in today] == [ny(MONDAY, "09:30"), ny(MONDAY, "10:30")]
    assert today[0].close == D(22)
    assert len(hours.bars) == 7 + 7 + 2  # two full past days, then today until 11:00
    days = chart.answer("ABCD", IV.D1, R.M1, world.now)
    assert [c.start for c in days.bars] == [ny(d, "00:00")
                                            for d in (date(2026, 11, 12), FRIDAY, MONDAY)]
