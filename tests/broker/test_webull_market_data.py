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

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webull" / "market_data"
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
QUOTE_MS = 1794431115431  # recorded example value of snapshot quote_time


def screener_keys() -> list[str]:
    data = json.loads((FIXTURES / "screener_gainers_losers_keys.json").read_text("utf-8"))
    return data["observations"][0]["key_names"]


def screener_row(symbol: str, **overrides: str) -> dict[str, str]:
    row = {k: "1.5" for k in screener_keys()}
    row.update(symbol=symbol, instrument_id=f"9{len(symbol)}{symbol}", exchange_code="NSQ",
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
    return {"time": int(start.timestamp() * 1000), "open": "10", "high": "10.5", "low": "9.9",
            "close": "10.2", "volume": "1200", "trading_session": "RTH"}


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
        ok([{"symbol": "AAA", "instrument_type": "COMMON_STOCK"},
            {"symbol": "ETFX", "instrument_type": "ETF"}]),
        ok([screener_row("AAA")]),
        ok([screener_row("NEW")]),
        ok({"data": [{"symbol": "NEW"}]}),
        ok([screener_row("NEW")]),
        ok([{"symbol": "NEW", "security_type": "COMMON_STOCK"}]),
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
        ok([{"symbol": "A", "security_type": "COMMON_STOCK"},
            {"symbol": "B", "security_type": "COMMON_STOCK"}]),
        ok([{"symbol": "C", "security_type": "COMMON_STOCK"}]),
        ok([screener_row("A")]),
        ok([{"symbol": "A", "security_type": "WARRANT"}]),
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
        read_profiles([{"symbol": "A", "type": "X"}, {"symbol": "A", "type": "Y"}])


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


def test_read_bars_time_formats():
    iso = dict(bar_row(0), time="2026-11-13T10:00:00-05:00")
    bars = read_bars({"result": [{"delay_minutes": "0", "symbol": "A", "result": [iso]}]})
    assert bars[0].start == NOW
    for bad in ["2026-11-13T10:00:00", "yesterday", 1.5]:
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
        ok([screener_row("A")]), ok([{"symbol": "A", "security_type": "COMMON_STOCK"}])))
    md.screener(ScreenerKind.GAINERS)
