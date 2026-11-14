"""WebullMarketData readers and routing over a scripted transport.

Rows are built from the recorded key names and JSON types in tests/fixtures/webull/market_data
(no raw market-data body was ever recorded; values here are synthetic strings).
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from aperture import config
from aperture.broker.base import RateLimited
from aperture.broker.budget import RequestBudget, RouteLimit
from aperture.broker.webull import (
    ACCOUNT_FREE_ROUTES,
    BrokerReadFailed,
    MalformedResponse,
    Route,
    WebullMarketData,
    WireResponse,
    market_data_from_config,
    read_bars,
    read_profiles,
    read_screener,
    read_snapshot,
)
from aperture.data.base import DelayedData, MarketData, MarketDataError, ScreenerKind
from aperture.data.discovery import DiscoverySettings, merge, universe_cause

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webull" / "market_data"
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
QUOTE_MS = 1794431115431  # recorded example value of snapshot quote_time


def screener_keys() -> list[str]:
    data = json.loads((FIXTURES / "screener_gainers_losers_keys.json").read_text("utf-8"))
    return data["observations"][0]["key_names"]


def screener_row(symbol: str, **overrides: str) -> dict[str, str]:
    row = {k: "1.5" for k in screener_keys()}
    row.update(symbol=symbol, instrument_id=iid(symbol), exchange_code="NSQ",
               currency="USD", name=f"{symbol} Inc")
    row.update(overrides)
    return row


def snapshot_row(symbol: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"symbol": symbol, "instrument_id": "1", "bid": "10.00", "ask": "10.02",
                           "bid_size": "300", "ask_size": "200", "price": "10.01",
                           "quote_time": QUOTE_MS, "last_trade_time": QUOTE_MS,
                           "trade_status": "ATH", "list_status": "LISTED"}
    row.update(overrides)
    return row


def bar_row(minute: int) -> dict[str, Any]:
    start = NOW + timedelta(minutes=minute)
    return {"time": start.strftime("%Y-%m-%dT%H:%M:%S.000+0000"), "open": "10", "high": "10.5",
            "low": "9.9", "close": "10.2", "volume": "1200", "trading_session": "RTH"}


def iid(symbol: str) -> str:
    return f"9{len(symbol)}{symbol}"


def profile(symbol: str, sub_category: str | None = "COMMON_STOCK", instrument_id: str = ""):
    row = {"symbol": symbol, "instrument_id": instrument_id or iid(symbol),
           "category": "US_STOCK", "status": "OC"}
    if sub_category is not None:
        row["sub_category"] = sub_category
    return row


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text("utf-8"))["body"]


class Scripted:
    def __init__(self, *responses: WireResponse) -> None:
        self.responses = deque(responses)
        self.sent: list[tuple[Route, dict[str, Any]]] = []

    def send(self, route: Route, params: dict[str, Any]) -> WireResponse:
        self.sent.append((route, dict(params)))
        return self.responses.popleft()

    def is_own_account(self, account_id: str) -> bool:
        return False


def ok(body: Any) -> WireResponse:
    return WireResponse(200, body)


def adapter(*responses: WireResponse, limit: int = 30, now=lambda: NOW, chunk: int = 100):
    transport = Scripted(*responses)
    budget = RequestBudget(RouteLimit(limit, 60, 60), clock=lambda: 0.0)
    md = WebullMarketData(transport, budget, "US_STOCK", "DAY_1", "RELATIVE_VOLUME_10D", chunk,
                          utc_now=now)
    md.budget = budget  # type: ignore[attr-defined]  # test access
    return md, transport


def test_satisfies_protocol_and_market_routes_carry_no_account():
    md, _ = adapter()
    assert isinstance(md, WebullMarketData) and md.provider == "webull"
    _: MarketData = md
    for route in (Route.GAINERS_LOSERS, Route.TOP_ACTIVES, Route.SNAPSHOT, Route.BARS,
                  Route.PROFILES):
        assert route in ACCOUNT_FREE_ROUTES and not route.mutating
    assert Route.PLACE not in ACCOUNT_FREE_ROUTES


def test_read_screener_keeps_every_recorded_field_as_strings():
    rows = read_screener([screener_row("AAA"), screener_row("BB", relative_volume_10d="")],
                         ScreenerKind.GAINERS)
    assert [(r.rank, r.symbol) for r in rows] == [(1, "AAA"), (2, "BB")]
    assert set(rows[0].fields) == set(screener_keys())
    assert rows[0].security_type is None


@pytest.mark.parametrize("missing", ["instrument_id", "symbol", "exchange_code", "currency"])
def test_read_screener_refuses_missing_identity(missing):
    row = screener_row("AAA")
    del row[missing]
    with pytest.raises(MalformedResponse, match=missing):
        read_screener([row], ScreenerKind.LOSERS)


def test_screener_routes_params_and_profile_types():
    md, transport = adapter(
        ok([screener_row("AAA"), screener_row("ETFX")]),
        ok({"data": [profile("AAA"), profile("ETFX", "ETF")]}),
        ok([screener_row("AAA")]),
        ok([screener_row("NEW")]),
        ok({"data": [profile("NEW", None)]}),
        ok([screener_row("NEW")]),
        ok({"data": [profile("NEW")]}),
    )
    gainers = md.screener(ScreenerKind.GAINERS)
    assert [r.security_type for r in gainers] == ["COMMON_STOCK", "ETF"]
    losers = md.screener(ScreenerKind.LOSERS)  # AAA cached: no profile request
    assert losers[0].security_type == "COMMON_STOCK"
    actives = md.screener(ScreenerKind.TOP_ACTIVES)
    assert actives[0].security_type is None  # profile states no type: refused by discovery
    again = md.screener(ScreenerKind.TOP_ACTIVES)  # unknown is not cached: asked again
    assert again[0].security_type == "COMMON_STOCK"
    routes = [(r, p) for r, p in transport.sent]
    assert routes[0] == (Route.GAINERS_LOSERS,
                         {"category": "US_STOCK", "rank_type": "DAY_1", "direction": "DESC"})
    assert routes[1] == (Route.PROFILES, {"symbols": "AAA,ETFX", "category": "US_STOCK"})
    assert routes[2][1]["direction"] == "ASC"
    assert routes[3] == (Route.TOP_ACTIVES,
                         {"category": "US_STOCK", "rank_type": "RELATIVE_VOLUME_10D"})
    assert [r for r, _ in routes].count(Route.PROFILES) == 3


def test_profile_cache_resets_on_a_new_new_york_day_and_chunks():
    clock = {"now": NOW}
    md, transport = adapter(
        ok([screener_row("A"), screener_row("B"), screener_row("C")]),
        ok({"data": [profile("A"), profile("B")]}),
        ok({"data": [profile("C")]}),
        ok([screener_row("A")]),
        ok({"data": [profile("A", "WARRANT")]}),
        now=lambda: clock["now"], chunk=2,
    )
    md.screener(ScreenerKind.GAINERS)
    clock["now"] = NOW + timedelta(days=1)
    assert md.screener(ScreenerKind.GAINERS)[0].security_type == "WARRANT"
    assert [r for r, _ in transport.sent].count(Route.PROFILES) == 3


def test_profile_budget_refusal_is_visible():
    md, transport = adapter(ok([screener_row("AAA")]), limit=1)
    md.budget.take(Route.PROFILES.path)  # the profile route's window is already full
    with pytest.raises(RateLimited):
        md.screener(ScreenerKind.GAINERS)
    assert [r for r, _ in transport.sent] == [Route.GAINERS_LOSERS]


def test_read_profiles_refuses_contradictions():
    with pytest.raises(MalformedResponse, match="twice"):
        read_profiles({"data": [profile("A"), profile("A", "ETF")]})
    with pytest.raises(MalformedResponse, match="instrument_id"):
        read_profiles({"data": [{"symbol": "A", "sub_category": "COMMON_STOCK"}]})


def test_recorded_profiles_types_and_multi_row_symbol():
    profiles = read_profiles(recorded("instruments_stocks_profiles_list_mixed_types.json"))
    by_symbol: dict[str, list[str | None]] = {}
    for p in profiles.values():
        by_symbol.setdefault(p.symbol, []).append(p.security_type)
    assert by_symbol == {"AAPL": ["COMMON_STOCK"], "BABA": ["COMMON_STOCK"],
                         "TSM": ["COMMON_STOCK"], "SPY": ["ETF"], "GRABW": ["WARRANT"],
                         "SOUNW": ["WARRANT"], "OPENW": ["WARRANT", "WARRANT"]}
    assert {"950176123", "951011795"} <= set(profiles)


def test_recorded_profiles_admit_common_stock_and_adrs_only():
    body = recorded("instruments_stocks_profiles_list_mixed_types.json")
    rows = [screener_row(p["symbol"], instrument_id=p["instrument_id"],
                         exchange_code=p["exchange_code"])
            for p in body["data"] if p["status"] != "NT"]
    md, _ = adapter(ok(rows), ok(body))
    typed = md.screener(ScreenerKind.GAINERS)
    cfg = config.load(ROOT / "config" / "bot.toml")
    settings = DiscoverySettings.from_config(cfg)
    instruments, _ = merge(typed)
    admitted = sorted(i.symbol for i in instruments if universe_cause(i, settings) is None)
    assert admitted == ["AAPL", "BABA", "TSM"]  # ETF and warrants refused, ADRs pass


@pytest.mark.parametrize(("security_type", "admitted"), [("COMMON_STOCK", True), ("ETF", False)])
def test_nyse_arca_listing_is_judged_by_type_not_exchange(security_type, admitted):
    md, _ = adapter(ok([screener_row("ARCA", exchange_code="PSE")]),
                    ok({"data": [profile("ARCA", security_type)]}))
    cfg = config.load(ROOT / "config" / "bot.toml")
    settings = DiscoverySettings.from_config(cfg)
    instruments, _ = merge(md.screener(ScreenerKind.GAINERS))
    cause = universe_cause(instruments[0], settings)
    assert (cause is None) is admitted


def test_multi_row_symbol_resolved_by_the_screener_instrument_id():
    md, _ = adapter(
        ok([screener_row("OPENW", instrument_id="951011795"), screener_row("X")]),
        ok({"data": [profile("OPENW", "WARRANT", "950176123"),
                     profile("OPENW", "COMMON_STOCK", "951011795"),
                     profile("X", "COMMON_STOCK", "other-id")]}),
    )
    rows = md.screener(ScreenerKind.GAINERS)
    assert rows[0].security_type == "COMMON_STOCK"  # matched by id, not first symbol hit
    assert rows[1].security_type is None  # profile id differs from the screener id: refused


def test_read_snapshot_uses_quote_time_and_receipt_time():
    quotes = read_snapshot([snapshot_row("AAA")], NOW)
    q = quotes[0]
    assert (q.bid, q.ask, q.bid_size, q.ask_size) == (D("10.00"), D("10.02"), D(300), D(200))
    assert q.provider_time == datetime.fromtimestamp(QUOTE_MS / 1000, tz=UTC)
    assert q.receipt_time == NOW


def test_read_snapshot_skips_rows_without_a_quote_and_refuses_bad_values():
    assert read_snapshot([snapshot_row("H", bid="")], NOW) == []
    without_time = snapshot_row("H")
    del without_time["quote_time"]
    assert read_snapshot([without_time], NOW) == []
    with pytest.raises(MalformedResponse):
        read_snapshot([snapshot_row("X", ask="abc")], NOW)
    with pytest.raises(MalformedResponse):
        read_snapshot([snapshot_row("X", quote_time=1.5)], NOW)
    with pytest.raises(MalformedResponse, match="symbol"):
        read_snapshot([{"bid": "1"}], NOW)


def test_snapshot_request_and_bars_request():
    md, transport = adapter(
        ok([snapshot_row("AAA")]),
        ok({"result": [{"delay_minutes": 0, "instrument_id": "1", "symbol": "AAA",
                        "result": [bar_row(1), bar_row(0)]}]}),
    )
    assert md.snapshot(["AAA", "BBB"])[0].receipt_time == NOW
    bars = md.bars(["AAA"], 390)
    assert [b.start for b in bars] == [NOW, NOW + timedelta(minutes=1)]
    assert bars[0].close == D("10.2") and bars[0].symbol == "AAA"
    assert transport.sent[0] == (Route.SNAPSHOT, {"symbols": "AAA,BBB", "category": "US_STOCK"})
    assert transport.sent[1] == (Route.BARS, {"symbols": ["AAA"], "category": "US_STOCK",
                                              "timespan": "M1", "count": "390"})


def test_snapshot_refuses_symbols_not_requested():
    md, _ = adapter(ok([snapshot_row("AAA"), snapshot_row("ZZZ")]))
    with pytest.raises(MalformedResponse, match="ZZZ"):
        md.snapshot(["AAA"])


def test_recorded_bars_newest_first_become_oldest_first_starts():
    bars = read_bars(recorded("stocks_bars_list_aapl_m1.json"))
    assert [b.start.strftime("%H:%M") for b in bars] == ["20:55", "20:56", "20:57", "20:58",
                                                          "20:59"]
    last = bars[-1]  # 15:59 New York: the bar START, closing auction volume included
    assert last.start == datetime(2026, 11, 13, 20, 59, tzinfo=UTC)
    assert (last.open, last.close, last.volume) == (D("356.19"), D("356.27"), D(6884218))
    assert {b.symbol for b in bars} == {"AAPL"}


def test_read_bars_time_formats():
    for bad in ["2026-11-13T10:00:00", "2026-11-13T15:00:00-05:00", "2026-11-13T20:59:30.000+0000",
                "yesterday", 1794431115431, 1.5]:
        with pytest.raises(MalformedResponse):
            read_bars({"result": [{"delay_minutes": 0, "symbol": "A",
                                   "result": [dict(bar_row(0), time=bad)]}]})


def test_read_bars_refuses_delayed_and_malformed():
    with pytest.raises(DelayedData) as caught:
        read_bars({"result": [{"delay_minutes": 15, "symbol": "A", "result": [bar_row(0)]},
                              {"delay_minutes": 0, "symbol": "B", "result": [bar_row(0)]}]})
    assert caught.value.symbols == ("A",) and caught.value.delay_minutes == 15
    with pytest.raises(MalformedResponse, match="delay_minutes"):
        read_bars({"result": [{"symbol": "A", "result": []}]})
    with pytest.raises(MalformedResponse):
        read_bars([])
    with pytest.raises(MalformedResponse, match="close"):
        read_bars({"result": [{"delay_minutes": 0, "symbol": "A",
                               "result": [{k: v for k, v in bar_row(0).items() if k != "close"}]}]})
    with pytest.raises(MalformedResponse, match="trading_session"):
        read_bars({"result": [{"delay_minutes": 0, "symbol": "A",
                               "result": [dict(bar_row(0), trading_session="PRE")]}]})


def test_http_error_is_a_typed_read_failure():
    md, _ = adapter(WireResponse(500, {"error_code": "INTERNAL"}))
    with pytest.raises(BrokerReadFailed):
        md.snapshot(["A"])


def test_stream_not_implemented_is_refused():
    md, _ = adapter()
    with pytest.raises(MarketDataError):
        md.subscribe(["A"], None)  # type: ignore[arg-type]


def test_from_config_uses_market_data_section():
    cfg = config.load(ROOT / "config" / "bot.toml")
    md = market_data_from_config(cfg, transport=Scripted(
        ok([screener_row("A")]), ok({"data": [profile("A")]})))
    md.screener(ScreenerKind.GAINERS)


def test_bars_before_sends_end_time_and_drops_bars_at_or_after_end():
    end = NOW + timedelta(minutes=1)
    md, transport = adapter(ok({"result": [{"delay_minutes": 0, "instrument_id": "1",
                                            "symbol": "AAA",
                                            "result": [bar_row(1), bar_row(0)]}]}))
    bars = md.bars_before("AAA", end, 1200)
    assert [b.start for b in bars] == [NOW]  # the bar starting at `end` is not "before" it
    assert transport.sent[0] == (Route.BARS, {
        "symbols": ["AAA"], "category": "US_STOCK", "timespan": "M1", "count": "1200",
        "end_time": int(end.timestamp() * 1000)})


def test_bars_before_refuses_other_symbols_and_naive_end():
    md, _ = adapter(ok({"result": [{"delay_minutes": 0, "symbol": "BBB",
                                    "result": [bar_row(0)]}]}))
    with pytest.raises(MalformedResponse, match="BBB"):
        md.bars_before("AAA", NOW + timedelta(hours=1), 10)
    with pytest.raises(ValueError):
        md.bars_before("AAA", NOW.replace(tzinfo=None), 10)


def recorded_meta(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text("utf-8"))["_meta"]


def wire_start(row: dict[str, Any]) -> datetime:
    return datetime.strptime(row["time"], "%Y-%m-%dT%H:%M:%S.%f%z")


def test_m4_end_time_is_exclusive_so_bars_before_drops_nothing():
    name = "stocks_bars_list_aapl_m1_end_time_all_sessions_trimmed.json"
    body, meta = recorded(name), recorded_meta(name)
    end = datetime.fromtimestamp(meta["request"]["end_time"] / 1000, tz=UTC)
    rows = body["result"][0]["result"]
    assert wire_start(rows[0]) == end - timedelta(minutes=1)  # newest first, end excluded
    regular = {"result": [dict(body["result"][0],
                               result=[r for r in rows if r["trading_session"] == "RTH"])]}
    md, _ = adapter(ok(regular))
    bars = md.bars_before("AAPL", end, 1200)
    assert len(bars) == len(regular["result"][0]["result"])  # the end filter drops nothing
    assert bars[-1].start == end - timedelta(minutes=1)


def test_m4_extended_sessions_are_refused_and_start_time_adds_a_bar():
    name = "stocks_bars_list_aapl_m1_window_pre_rth.json"
    body, meta = recorded(name), recorded_meta(name)
    start = datetime.fromtimestamp(meta["request"]["start_time"] / 1000, tz=UTC)
    oldest = wire_start(body["result"][0]["result"][-1])
    assert oldest == start - timedelta(minutes=1)  # why bars_before never sends start_time
    with pytest.raises(MalformedResponse, match="PRE"):
        read_bars(body)  # only RTH is requested; any other session is refused
