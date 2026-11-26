"""Tradability reads (backlog T8): the Webull adapter's read-only trade instrument route, the
probe `tickr probe-tradability`, and its CLI.

Every body here is SYNTHETIC (shaped after the SDK's names: `trade_policy`, `forbid_reason`),
not a recorded Webull answer: no trade instrument answer has been recorded yet."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tickr import cli, config
from tickr.broker.base import TradabilityReader
from tickr.broker.budget import RouteLimit
from tickr.broker.webull import (
    ACCOUNT_FREE_ROUTES,
    MARKET_DATA_ROUTES,
    SOURCE_PROFILES,
    SOURCE_SNAPSHOT,
    SOURCE_TRADE_INSTRUMENT,
    AdapterConfigError,
    MalformedResponse,
    Route,
    WireResponse,
    read_trade_instrument,
)
from tickr.data.base import SourceRead
from tickr.engine.tradability_probe import READS_FILE, describe, run

from .test_webull_broker import broker

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)


def ok(body) -> WireResponse:
    return WireResponse(200, body)


def profile(symbol: str, iid: str, status: str = "OC") -> dict:
    return {"symbol": symbol, "instrument_id": iid, "category": "US_STOCK", "status": status,
            "sub_category": "COMMON_STOCK"}


def with_category(*responses):
    b, transport, budget = broker(*responses)
    b._category = "US_STOCK"  # what broker_from_config passes
    return b, transport


def test_route_is_read_only_account_free_and_not_on_the_chart_transport():
    assert Route.TRADE_INSTRUMENT.method == "GET" and not Route.TRADE_INSTRUMENT.mutating
    assert Route.TRADE_INSTRUMENT.path == "/trade/instrument"
    assert Route.TRADE_INSTRUMENT in ACCOUNT_FREE_ROUTES
    assert Route.TRADE_INSTRUMENT not in MARKET_DATA_ROUTES


def test_tradability_reads_the_profile_then_the_instrument_once_each():
    b, transport = with_category(ok({"data": [profile("ABC", "913")]}),
                                 ok({"instrument_id": "913", "trade_policy": "CLOSE_ONLY",
                                     "forbid_reason": "synthetic reason"}))
    assert isinstance(b, TradabilityReader)
    t = b.tradability("ABC")
    assert (t.symbol, t.profile_status, t.trade_policy, t.forbid_reason, t.read_error) == (
        "ABC", "OC", "CLOSE_ONLY", "synthetic reason", None)
    assert transport.sent == [(Route.PROFILES, {"symbols": "ABC", "category": "US_STOCK"}),
                              (Route.TRADE_INSTRUMENT, {"instrument_id": "913"})]


def test_can_read_tradability_needs_both_routes_and_takes_nothing():
    b, transport, budget = broker(limit=RouteLimit(1, 60, 60))
    assert b.can_read_tradability()
    budget.take(Route.TRADE_INSTRUMENT.path)  # the instrument route is spent
    assert not b.can_read_tradability()
    assert transport.sent == []


def test_several_instruments_for_a_symbol_are_refused_before_the_instrument_read():
    b, transport = with_category(ok({"data": [profile("OPENW", "1", "NT"),
                                              profile("OPENW", "2", "CO")]}))
    with pytest.raises(MalformedResponse, match="2 instruments"):
        b.tradability("OPENW")
    assert [r for r, _ in transport.sent] == [Route.PROFILES]


def test_without_a_category_nothing_is_sent():
    b, transport, _ = broker()
    with pytest.raises(AdapterConfigError, match="no instrument category"):
        b.tradability("ABC")
    assert transport.sent == []


@pytest.mark.parametrize("body, match", [
    ({"data": {"trade_policy": "ALL"}}, "missing field 'trade_policy'"),
    ({"trade_policy": ""}, "missing field"),
    ({"trade_policy": "ALL", "instrument_id": "999"}, "names instrument 999"),
    ({"trade_policy": 1}, "must be strings"),
    ([], "expected an object"),
])
def test_read_trade_instrument_refuses_any_other_shape(body, match):
    with pytest.raises(MalformedResponse, match=match):
        read_trade_instrument(body, "913")


def test_read_trade_instrument_takes_an_empty_reason_as_none():
    assert read_trade_instrument({"trade_policy": "ALL", "forbid_reason": ""}, "1") == ("ALL", None)


def test_sources_record_every_answer_refusals_included():
    b, transport = with_category(
        ok([{"symbol": "ABC", "trade_status": "SYNTHETIC"}]),
        ok({"data": [profile("ABC", "913"), profile("XYZ", "924", "NT")]}),
        ok({"trade_policy": "ALL"}),
        WireResponse(417, {"error_code": "SYNTHETIC_REFUSAL"}))
    reads = b.tradability_sources(["ABC", "XYZ"])
    assert [(label, r.name) for label, r in reads] == [
        ("ABC,XYZ", SOURCE_SNAPSHOT), ("ABC,XYZ", SOURCE_PROFILES),
        ("ABC 913", SOURCE_TRADE_INSTRUMENT), ("XYZ 924", SOURCE_TRADE_INSTRUMENT)]
    assert (reads[3][1].status, reads[3][1].error_code) == (417, "SYNTHETIC_REFUSAL")
    assert [r for r, _ in transport.sent] == [Route.SNAPSHOT, Route.PROFILES,
                                              Route.TRADE_INSTRUMENT, Route.TRADE_INSTRUMENT]
    assert not any(r.mutating for r, _ in transport.sent)


def test_sources_stop_at_a_failed_profile_read():
    b, transport = with_category(ok([]), WireResponse(500, {"error_code": "X"}))
    reads = b.tradability_sources(["ABC"])
    assert [r.name for _, r in reads] == [SOURCE_SNAPSHOT, SOURCE_PROFILES]
    assert len(transport.sent) == 2


class Sources:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def tradability_sources(self, symbols):
        self.asked.append(list(symbols))
        return [("ABC", SourceRead("snapshot", 200, None,
                                   [{"symbol": "ABC", "trade_status": "SYNTHETIC"}], NOW)),
                ("ABC", SourceRead("profiles", 200, None, {"data": [profile("ABC", "913")]},
                                   NOW)),
                ("ABC 913", SourceRead("trade-instrument", None, "BudgetExhausted", None, NOW))]


def test_describe_shows_the_probed_fields_and_what_is_absent():
    read = SourceRead("snapshot", 200, None, [{"symbol": "ABC", "trade_status": "S"}], NOW)
    lines = describe("ABC", read)
    assert lines[0] == "  snapshot ABC: HTTP 200"
    assert lines[1].startswith("    symbol=ABC trade_status=S (absent: list_status")


def test_run_records_every_answer_and_says_when_one_was_missing(tmp_path: Path):
    out: list[str] = []
    assert run(Sources(), ["ABC"], tmp_path, out.append) == 3
    rows = [json.loads(x) for x in (tmp_path / READS_FILE).read_text("utf-8").splitlines()]
    assert [(r["label"], r["read"]) for r in rows] == [
        ("ABC", "snapshot"), ("ABC", "profiles"), ("ABC 913", "trade-instrument")]
    assert "  trade-instrument ABC 913: HTTP None BudgetExhausted" in out


def test_cli_reads_only_and_refuses_bad_symbols(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    bot = tmp_path / "config" / "bot.toml"
    bot.write_text((ROOT / "config" / "bot.toml").read_text("utf-8"), "utf-8")
    sources = Sources()
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: (sources, None))
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    assert cli.main(["--config", str(bot), "probe-tradability", "abc"]) == 2
    assert sources.asked == []
    assert cli.main(["--config", str(bot), "probe-tradability", "ABC", "XYZ"]) == 3
    assert sources.asked == [["ABC", "XYZ"]]
    assert "evidence in" in capsys.readouterr().out
    assert config.load(bot).risk.tradability_check_enabled is False
