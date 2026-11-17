"""History fetch over a scripted source: universe filter, daily labels, minute paging."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from tickr.backtest import fetch
from tickr.backtest.errors import BacktestError
from tickr.backtest.settings import load_settings
from tickr.backtest.universe import (
    active_screen,
    drop_unpriced,
    read_pool,
    select_all,
    select_liquid,
    select_pool,
    write_pool,
)
from tickr.broker.base import RateLimited
from tickr.broker.budget import BudgetExhausted, RequestBudget, RouteLimit
from tickr.broker.webull import InstrumentPage
from tickr.strategy.price_action_v1 import dollar_volume_check

from .helpers import ROOT, UTC, FakeHistory, calendar, cfg, daily_bar, flat_bars, row

SETTINGS = load_settings(ROOT / "config" / "backtest.toml")


class Floor:
    """Session liquidity rules with only a dollar-volume floor, USD."""

    def __init__(self, floor: int) -> None:
        self.floor = Decimal(floor)

    def session_liquidity(self, session_bars):
        return (dollar_volume_check(session_bars, self.floor),)


SHIPPED = active_screen(cfg())  # the active strategy's own [liquidity] floor


def no_sleep(_seconds: float) -> None:
    return None


def quiet(_text: str) -> None:
    return None


def test_universe_filters_like_discovery_and_refuses_duplicates(tmp_path):
    pages = {
        None: InstrumentPage((row("AAA"), row("ETF1", sub="ETF"), row("OTC1", exchange="OTC"),
                              row("EUR1", currency="EUR")), "k2"),
        "k2": InstrumentPage((row("DUP", iid="1"), row("DUP", iid="2"), row("BBB"),
                              row("GONE", status="NT")), None),
    }
    source = FakeHistory(pages, {}, {})
    kept = fetch.fetch_universe(source, cfg(), SETTINGS, tmp_path, no_sleep, quiet)
    assert kept == ["AAA", "BBB"]
    causes = {(r["symbol"], r["cause"])
              for r in fetch.read_lines(tmp_path / fetch.UNIVERSE_REFUSED_FILE)}
    assert causes == {("ETF1", "SECURITY_TYPE_NOT_ALLOWED"), ("OTC1", "EXCHANGE_NOT_ALLOWED"),
                      ("EUR1", "CURRENCY_NOT_ALLOWED"), ("DUP", "DUPLICATE_SYMBOL"),
                      ("GONE", "STATUS_NOT_ALLOWED")}
    # A rerun reads the file and sends nothing.
    again = FakeHistory({}, {}, {})
    assert fetch.fetch_universe(again, cfg(), SETTINGS, tmp_path, no_sleep, quiet) == kept
    assert again.requests == 0


def test_universe_refuses_a_repeated_pagination_key(tmp_path):
    pages = {None: InstrumentPage((row("AAA"),), "k"), "k": InstrumentPage((), "k")}
    with pytest.raises(BacktestError, match="repeated"):
        fetch.fetch_universe(FakeHistory(pages, {}, {}), cfg(), SETTINGS, tmp_path,
                             no_sleep, quiet)


def test_patiently_waits_for_the_budget():
    waits: list[float] = []
    calls = iter([BudgetExhausted("/bars", "window full", 2.5), "ok"])

    def call():
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    assert fetch.patiently(call, waits.append, quiet) == "ok"
    assert waits == [2.5]


DAYS = [date(2026, 11, d) for d in (9, 10, 11, 12, 13)]


def test_daily_maps_labels_to_sessions_and_pool_selects_gainers(tmp_path):
    daily = {
        # +12% high on the 13th, plenty of dollar volume.
        "UP": [daily_bar("UP", d, "10", "10", "10", "10", "1000000") for d in DAYS[:-1]]
        + [daily_bar("UP", DAYS[-1], "10", "11.2", "10", "11", "1000000")],
        # +12% but thin: below the bot.toml dollar-volume floor.
        "THIN": [daily_bar("THIN", d, "10", "10", "10", "10", "100") for d in DAYS[:-1]]
        + [daily_bar("THIN", DAYS[-1], "10", "11.2", "10", "11", "100")],
        "FLAT": [daily_bar("FLAT", d, "10", "10.1", "10", "10", "1000000") for d in DAYS],
    }
    source = FakeHistory({}, daily, {})
    directory = fetch.fetch_daily(source, calendar(), cfg(), ["UP", "THIN", "FLAT"],
                                  DAYS[1], DAYS[-1], tmp_path, no_sleep, quiet)
    rows = fetch.load_daily(directory)
    assert {(r["symbol"], r["date"]) for r in rows} >= {("UP", "2026-11-09")}
    window = [d.ny_date for d in fetch.daily_window(calendar(), DAYS[1], DAYS[-1], 14)]
    pool, caps = select_pool(rows, window, Decimal(5), SHIPPED, 200)
    assert [(c.candidates, c.dropped) for c in caps][-1] == (1, 0)
    assert [(e.symbol, e.session_date) for e in pool] == [("UP", DAYS[-1])]
    assert pool[0].day_high_change_percent == Decimal("12.0")
    write_pool(pool, tmp_path / "pool.csv")
    assert read_pool(tmp_path / "pool.csv") == pool


def test_daily_refuses_a_label_outside_the_sessions():
    sessions = fetch.daily_window(calendar(), DAYS[1], DAYS[-1], 14)
    saturday = datetime(2026, 11, 14, 12, tzinfo=UTC)
    bad = daily_bar("X", DAYS[0], "1", "1", "1", "1", "1")
    bad = type(bad)(**{**bad.__dict__, "time": saturday})
    with pytest.raises(BacktestError, match="not a session"):
        fetch.daily_records([bad], sessions)


def test_needed_runs_merge_history_sessions():
    window = fetch.sessions_between(calendar(), date(2026, 11, 2), date(2026, 11, 13))
    runs = fetch.needed_runs([date(2026, 11, 10), date(2026, 11, 13)], window, 2)
    assert [[d.ny_date.day for d in run] for run in runs] == [[6, 9, 10, 11, 12, 13]]
    runs = fetch.needed_runs([date(2026, 11, 5), date(2026, 11, 13)], window, 1)
    assert [[d.ny_date.day for d in run] for run in runs] == [[4, 5], [12, 13]]
    with pytest.raises(BacktestError, match="sessions before"):
        fetch.needed_runs([date(2026, 11, 3)], window, 2)


def test_minutes_page_back_in_batches_and_record_status(tmp_path):
    window = fetch.sessions_between(calendar(), date(2026, 11, 9), date(2026, 11, 13))
    history = {s: [b for d in DAYS for b in flat_bars(s, d)] for s in ("AAA", "BBB")}
    history["NEW"] = flat_bars("NEW", DAYS[-1])  # listed on the last day only
    source = FakeHistory({}, {}, history)
    runs = {s: fetch.needed_runs([DAYS[-1]], window, 4) for s in history}
    fetch.fetch_minutes(source, cfg(), SETTINGS, runs, tmp_path, no_sleep, quiet)
    statuses = {s["symbol"]: s for s in fetch.load_statuses(tmp_path)}
    assert statuses["AAA"]["complete"] and statuses["AAA"]["bars"] == 5 * 390
    assert not statuses["NEW"]["complete"]
    assert statuses["NEW"]["cause"].startswith("INCOMPLETE")
    assert statuses["NEW"]["empty_sessions"] == [d.isoformat() for d in DAYS[:-1]]
    # The three symbols share every request while their ends agree.
    first = [c for c in source.calls if c[0] == "minutes"][0]
    assert set(first[1]) == {"AAA", "BBB", "NEW"}
    lines = (tmp_path / "minutes" / "AAA.jsonl").read_text().splitlines()
    assert len(lines) == 5 * 390 and json.loads(lines[0])["symbol"] == "AAA"
    # A rerun sends nothing.
    again = FakeHistory({}, {}, history)
    fetch.fetch_minutes(again, cfg(), SETTINGS, runs, tmp_path, no_sleep, quiet)
    assert again.requests == 0


def test_minutes_skip_a_symbol_the_route_refuses_and_keep_the_others(tmp_path):
    window = fetch.sessions_between(calendar(), date(2026, 11, 9), date(2026, 11, 13))
    history = {s: [b for d in DAYS for b in flat_bars(s, d)] for s in ("AAA", "BBB")}
    source = FakeHistory({}, {}, history, invalid=frozenset({"ODD"}))
    runs = {s: fetch.needed_runs([DAYS[-1]], window, 4) for s in ("AAA", "BBB", "ODD")}
    fetch.fetch_minutes(source, cfg(), SETTINGS, runs, tmp_path, no_sleep, quiet)
    statuses = {s["symbol"]: s for s in fetch.load_statuses(tmp_path)}
    assert statuses["AAA"]["complete"] and statuses["BBB"]["complete"]
    assert not statuses["ODD"]["complete"]
    assert statuses["ODD"]["cause"] == fetch.REFUSED_CAUSE
    assert (tmp_path / "minutes" / "ODD.jsonl").read_text() == ""


def test_minutes_ignore_bars_outside_the_run(tmp_path):
    window = fetch.sessions_between(calendar(), date(2026, 11, 9), date(2026, 11, 13))
    bars = [b for d in DAYS for b in flat_bars("AAA", d)]
    source = FakeHistory({}, {}, {"AAA": bars})
    runs = {"AAA": fetch.needed_runs([DAYS[2]], window, 1)}  # 10th and 11th only
    fetch.fetch_minutes(source, cfg(), SETTINGS, runs, tmp_path, no_sleep, quiet)
    lines = (tmp_path / "minutes" / "AAA.jsonl").read_text().splitlines()
    starts = {json.loads(x)["start"][:10] for x in lines}
    assert starts == {"2026-11-10", "2026-11-11"} and len(lines) == 2 * 390


def test_daily_splits_a_chunk_to_isolate_refused_symbols(tmp_path):
    symbols = [f"S{i:02d}" for i in range(8)]
    daily = {s: [daily_bar(s, d, "10", "10", "10", "10", "1") for d in DAYS] for s in symbols}
    source = FakeHistory({}, daily, {}, invalid=frozenset({"S02", "S05"}))
    directory = fetch.fetch_daily(source, calendar(), cfg(), symbols, DAYS[1], DAYS[-1],
                                  tmp_path, no_sleep, quiet)
    got = {r["symbol"] for r in fetch.load_daily(directory)}
    assert got == set(symbols) - {"S02", "S05"}
    refused = [r["symbol"] for r in fetch.read_lines(directory / "00000.refused")]
    assert sorted(refused) == ["S02", "S05"]


def test_pool_cap_records_dropped_candidates():
    rows = []
    for name, high in (("A", "12"), ("B", "11"), ("C", "10.6")):
        rows.append({"symbol": name, "date": "2026-11-12", "open": "10", "high": "10",
                     "low": "10", "close": "10", "volume": "1000000"})
        rows.append({"symbol": name, "date": "2026-11-13", "open": "10", "high": high,
                     "low": "10", "close": "10", "volume": "1000000"})
    pool, caps = select_pool(rows, [date(2026, 11, 12), date(2026, 11, 13)], Decimal(5),
                             Floor(1), 2)
    assert [e.symbol for e in pool] == ["A", "B"]
    assert (caps[0].candidates, caps[0].kept, caps[0].dropped) == (3, 2, 1)


def test_liquid_ranks_by_previous_day_only():
    def bar(symbol, day, close, volume, high=None):
        return {"symbol": symbol, "date": day, "open": close, "high": high or close,
                "low": close, "close": close, "volume": volume}
    rows = [
        bar("A", "2026-11-12", "20", "1000"),            # 20k: rank 2
        bar("A", "2026-11-13", "20", "1", high="30"),
        bar("B", "2026-11-12", "50", "1000"),            # 50k: rank 1
        bar("B", "2026-11-13", "50", "1"),
        bar("C", "2026-11-12", "9", "100000"),           # below the price floor
        bar("C", "2026-11-13", "9", "100000"),
        bar("D", "2026-11-12", "11", "100"),             # 1.1k: outside top 2
        bar("D", "2026-11-13", "11", "9999999"),         # the day's own volume must not count
        bar("E", "2026-11-12", "10", "1500"),            # 15k: rank 3, no bar on the day
    ]
    pool, caps = select_liquid(rows, [date(2026, 11, 12), date(2026, 11, 13)], 3, Decimal(10))
    assert [(e.rank, e.symbol) for e in pool] == [(1, "B"), (2, "A")]
    assert pool[1].day_high_change_percent == Decimal(50)
    assert (caps[0].candidates, caps[0].kept, caps[0].dropped) == (3, 2, 1)


def test_all_keeps_every_symbol_above_the_price_floor_whatever_its_move():
    def bar(symbol, day, close, volume, high=None):
        return {"symbol": symbol, "date": day, "open": close, "high": high or close,
                "low": close, "close": close, "volume": volume}
    rows = [
        bar("A", "2026-11-12", "1", "10"),
        bar("A", "2026-11-13", "1", "10"),               # flat day: kept
        bar("B", "2026-11-12", "2", "1000"),
        bar("B", "2026-11-13", "2", "1", high="3"),      # jumped and faded: kept
        bar("C", "2026-11-12", "0.2", "100000"),         # previous close below the floor
        bar("C", "2026-11-13", "0.5", "100000"),
        bar("E", "2026-11-12", "5", "1"),                # no bar on the day
    ]
    pool, caps = select_all(rows, [date(2026, 11, 12), date(2026, 11, 13)], Decimal("0.30"))
    assert [(e.rank, e.symbol) for e in pool] == [(1, "B"), (2, "A")]
    assert (caps[0].candidates, caps[0].kept, caps[0].dropped) == (3, 2, 1)


def test_patiently_after_a_provider_429_waits_for_the_budget_pause():
    clock = [0.0]
    budget = RequestBudget(RouteLimit(30, 60, 60), clock=lambda: clock[0])
    waits: list[float] = []
    answers = iter(["429", "ok"])

    def call():
        budget.take("/bars")
        answer = next(answers)
        if answer == "429":
            budget.pause("/bars")
            raise RateLimited("HTTP 429")
        return answer

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    assert fetch.patiently(call, sleep, quiet) == "ok"
    assert len(waits) == 1 and waits[0] > 0


def test_previous_close_on_minute_scale_undoes_split_and_dividend_adjustment():
    from tickr.backtest.universe import PoolEntry
    day = date(2026, 8, 17)
    # 2:1 split overnight: adjusted daily previous close 80 (raw 160), day close 82 adjusted = raw
    split = PoolEntry(day, 1, "APH", Decimal(80), Decimal(82), Decimal(3), Decimal(10**8))
    assert split.previous_close_on_minute_scale(Decimal(82)) == Decimal(80)
    # forward-adjusted series before a later dividend: daily 99.9 / 100.8, raw M1 close 101
    dividend = PoolEntry(day, 1, "D", Decimal("99.9"), Decimal("100.8"), Decimal(1), Decimal(10**8))
    scaled = dividend.previous_close_on_minute_scale(Decimal(101))
    assert abs(scaled / Decimal(101) - Decimal("99.9") / Decimal("100.8")) < Decimal("1e-12")
    with pytest.raises(BacktestError):
        dividend.previous_close_on_minute_scale(Decimal(0))


def test_symbols_with_a_non_positive_daily_price_are_left_out():
    def bar(symbol, day, close):
        return {"symbol": symbol, "date": day, "open": close, "high": close, "low": close,
                "close": close, "volume": "1000000"}
    rows = [bar("OK", "2026-11-12", "10"), bar("OK", "2026-11-13", "11"),
            bar("NEG", "2026-11-12", "-84.0548"), bar("NEG", "2026-11-13", "12")]
    kept, refused = drop_unpriced(rows)
    assert {r["symbol"] for r in kept} == {"OK"}
    assert refused == {"NEG": "2026-11-12 open -84.0548 not positive"}
    pool, _ = select_pool(kept, [date(2026, 11, 12), date(2026, 11, 13)], Decimal(5),
                          Floor(1), 10)
    assert [e.symbol for e in pool] == ["OK"]


def test_pool_uses_the_strategy_floor_on_the_whole_day():
    """Day dollar volume = typical price x volume; the floor is the strategy's, not a copy."""
    rows = []
    for name, volume in (("AT", "500000"), ("UNDER", "499999")):
        rows.append({"symbol": name, "date": "2026-11-12", "open": "10", "high": "10",
                     "low": "10", "close": "10", "volume": volume})
        rows.append({"symbol": name, "date": "2026-11-13", "open": "10", "high": "11",
                     "low": "9", "close": "10", "volume": volume})
    days = [date(2026, 11, 12), date(2026, 11, 13)]
    pool, _ = select_pool(rows, days, Decimal(5), Floor(5_000_000), 10)
    assert [e.symbol for e in pool] == ["AT"]
    pool, _ = select_pool(rows, days, Decimal(5), Floor(4_000_000), 10)
    assert [e.symbol for e in pool] == ["AT", "UNDER"]
