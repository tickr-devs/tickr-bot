"""WebullHistory (backtest-only reads) over a scripted transport."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tickr.broker.budget import RequestBudget
from tickr.broker.webull import (
    BrokerReadFailed,
    MalformedResponse,
    Route,
    WebullHistory,
    WireResponse,
    read_daily_bars,
    read_instrument_page,
)
from tickr.data.base import DelayedData

from .test_webull_broker import DEFAULT_LIMIT, ScriptedTransport

END = datetime(2026, 11, 14, 5, tzinfo=UTC)


def instrument(symbol: str, **extra):
    return {"symbol": symbol, "instrument_id": f"id{symbol}", "exchange_code": "NSQ",
            "currency": "USD", "sub_category": "COMMON_STOCK", "status": "OC", **extra}


def daily_body(symbol: str, times: list[str], delay: str = "0"):
    rows = [{"time": t, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"}
            for t in times]
    return {"result": [{"symbol": symbol, "instrument_id": "1", "delay_minutes": delay,
                        "result": rows}]}


def history(*responses, raw_dir=None):
    transport = ScriptedTransport(*responses)
    budget = RequestBudget(DEFAULT_LIMIT, clock=lambda: 0.0)
    return WebullHistory(transport, budget, "US_STOCK", clock=lambda: 0.0,
                         raw_dir=raw_dir), transport


def test_instrument_page_with_and_without_key():
    page = read_instrument_page({"data": [instrument("AAA")], "pagination_key": "p2"})
    assert page.next_key == "p2" and page.rows[0].security_type == "COMMON_STOCK"
    last = read_instrument_page({"data": [instrument("BBB", sub_category=None)]})
    assert last.next_key is None and last.rows[0].security_type is None


def test_instrument_page_refuses_unknown_top_level_keys():
    with pytest.raises(MalformedResponse, match="unknown top-level keys"):
        read_instrument_page({"data": [], "next_page_token": "x"})


def test_daily_bars_are_sorted_and_delay_is_refused():
    bars = read_daily_bars(daily_body("AAA", ["2026-11-13T05:00:00.000+0000",
                                              "2026-11-12T05:00:00.000+0000"]))
    assert [b.time.day for b in bars] == [12, 13] and bars[0].close == Decimal("1.5")
    with pytest.raises(DelayedData):
        read_daily_bars(daily_body("AAA", [], delay="15"))


def test_history_sends_read_requests_and_saves_first_raw_body(tmp_path):
    ok = WireResponse(200, daily_body("AAA", ["2026-11-13T05:00:00.000+0000"]))
    h, transport = history(ok, ok, raw_dir=tmp_path)
    assert len(h.daily_bars(["AAA"], END, 5)) == 1
    h.daily_bars(["AAA"], END, 5)
    route, params = transport.sent[0]
    assert route is Route.BARS and params["timespan"] == "D" and params["count"] == "5"
    assert params["end_time"] == int(END.timestamp() * 1000) and h.requests == 2
    saved = sorted(tmp_path.iterdir())
    assert len(saved) == 1 and json.loads(saved[0].read_text())["status"] == 200


def test_history_instruments_and_failures(tmp_path):
    page = WireResponse(200, {"data": [instrument("AAA")]})
    h, transport = history(page, WireResponse(500, {"error_code": "X"}), raw_dir=tmp_path)
    assert h.instruments("COMMON_STOCK", None).rows[0].symbol == "AAA"
    assert transport.sent[0][1] == {"category": "US_STOCK", "sub_category": "COMMON_STOCK"}
    with pytest.raises(BrokerReadFailed):
        h.instruments("COMMON_STOCK", "p2")
    assert transport.sent[1][1]["pagination_key"] == "p2"
    assert len(list(tmp_path.iterdir())) == 2  # the failed response is kept too


def test_minute_bars_refuse_stray_symbols():
    body = {"result": [{"symbol": "ZZZ", "instrument_id": "1", "delay_minutes": "0",
                        "result": [{"time": "2026-11-13T20:59:00.000+0000", "open": "1",
                                    "high": "1", "low": "1", "close": "1", "volume": "1",
                                    "trading_session": "RTH"}]}]}
    h, _ = history(WireResponse(200, body))
    with pytest.raises(MalformedResponse, match="not requested"):
        h.minute_bars(["AAA"], END, 10)


def test_invalid_symbol_is_a_typed_refusal():
    from tickr.data.base import SymbolsRefused

    h, _ = history(WireResponse(417, {"error_code": "INVALID_SYMBOL", "message": "x"}))
    with pytest.raises(SymbolsRefused):
        h.daily_bars(["1ESC011"], END, 5)
