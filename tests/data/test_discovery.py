from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from md_helpers import NOW, OPEN, Screen, bars, quote, row, settings

from tickr import config
from tickr.data.base import FakeMarketData, ScreenerKind, SymbolsRefused
from tickr.data.discovery import (
    Cause,
    DiscoverySettings,
    RefusedSymbol,
    RefusedSymbols,
    Stage,
    StopCause,
    completed_session_bars,
    discover,
    latest_refusals,
    merge,
    pass_record,
)
from tickr.data.movers import MoverSettings, measure
from tickr.data.todaybars import TodayBars
from tickr.types import Bar

G, L, A = ScreenerKind.GAINERS, ScreenerKind.LOSERS, ScreenerKind.TOP_ACTIVES
ROOT = Path(__file__).resolve().parents[2]


class Sink:
    def __init__(self) -> None:
        self.screener_rows: list = []
        self.bars_seen: list = []
        self.quotes_seen: list = []
        self.skipped: list = []

    def screener(self, rows, provider, receipt_time):
        self.screener_rows.extend(rows)

    def screener_skipped(self, kinds, cause, source, receipt_time):
        self.skipped.extend((kind, cause, source) for kind in kinds)

    def bars(self, bars, provider, receipt_time):
        self.bars_seen.extend(bars)

    def quotes(self, quotes, provider, source):
        self.quotes_seen.extend(quotes)


def liquid(md: FakeMarketData, symbol: str, minutes: int = 30) -> None:
    md.set_quote(quote(symbol))
    md.add_bars(bars(symbol, minutes))


def run(md: FakeMarketData, sink=None, screen=None, **overrides):
    return discover(md, settings(**overrides), screen or Screen(), OPEN, lambda: NOW, sink)


def test_settings_from_bot_config():
    cfg = config.load(ROOT / "config" / "bot.toml")
    s = DiscoverySettings.from_config(cfg)
    assert "NSQ" in s.allowed_exchanges and s.max_quote_age == timedelta(seconds=5)
    assert s.max_clock_skew == timedelta(seconds=float(cfg.risk.max_clock_skew_seconds))
    assert s.bars_symbols_per_request == 20


def test_merge_by_instrument_id_keeps_kinds_and_order():
    rows = [row(G, 1, "AAA"), row(G, 2, "BBB"), row(A, 1, "BBB"), row(L, 1, "CCC"),
            row(A, 2, "AAA")]
    instruments, rejections = merge(rows)
    assert rejections == []
    assert [i.symbol for i in instruments] == ["AAA", "BBB", "CCC"]
    assert instruments[0].kinds == (G, A)


def test_merge_refuses_conflicting_identity():
    rows = [row(G, 1, "AAA", "x"), row(A, 1, "AAB", "x"), row(G, 2, "DUP", "y"),
            row(L, 1, "DUP", "z"), row(G, 3, "OK")]
    instruments, rejections = merge(rows)
    assert [i.symbol for i in instruments] == ["OK"]
    assert {r.instrument_id for r in rejections} == {"x", "y", "z"}
    assert all(r.cause is Cause.CONFLICTING_ROWS for r in rejections)


def test_happy_path_candidate_with_dollar_volume():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "AAA")])
    md.set_screener(A, [row(A, 1, "AAA")])
    liquid(md, "AAA")
    result = run(md)
    assert result.screeners_read == (G, L, A)
    assert [c.instrument.symbol for c in result.candidates] == ["AAA"]
    assert len(result.candidates[0].session_bars) == 30
    assert result.summary() == "evaluated 1 of 1"


def test_universe_refusals():
    md = FakeMarketData()
    md.set_screener(G, [
        row(G, 1, "OTC1", exchange="OTC"),
        row(G, 2, "CAD1", currency="CAD"),
        row(G, 3, "ETF1", security_type="ETF"),
        row(G, 4, "UNK1", security_type=None),
    ])
    result = run(md)
    assert [r.cause for r in result.rejections] == [
        Cause.EXCHANGE_NOT_ALLOWED, Cause.CURRENCY_NOT_ALLOWED,
        Cause.SECURITY_TYPE_NOT_ALLOWED, Cause.SECURITY_TYPE_UNKNOWN]
    assert result.in_universe == 0 and result.summary() == "evaluated 0 of 0"
    assert ("snapshot", ()) not in md.calls and all(c[0] == "screener" for c in md.calls)


def test_quote_refusals():
    md = FakeMarketData()
    symbols = ["NOQ", "OLD", "FUT", "BAD", "WIDE", "SKEW"]
    md.set_screener(G, [row(G, i, s) for i, s in enumerate(symbols, 1)])
    md.set_quote(quote("OLD", age_seconds=6))
    md.set_quote(quote("FUT", age_seconds=-2))
    md.set_quote(quote("SKEW", age_seconds=-1))
    md.set_quote(quote("BAD", bid="10.05", ask="10.00"))
    md.set_quote(quote("WIDE", bid="10.00", ask="10.20"))
    result = run(md)
    causes = {r.symbol: r.cause for r in result.rejections}
    assert causes == {"NOQ": Cause.NO_QUOTE, "OLD": Cause.QUOTE_STALE,
                      "FUT": Cause.QUOTE_FROM_FUTURE, "BAD": Cause.QUOTE_INVALID,
                      "WIDE": Cause.STRATEGY_LIQUIDITY, "SKEW": Cause.NO_BARS}
    assert result.evaluated == 6  # SKEW is 1 s ahead, within the allowed skew: it reached bars
    wide = next(r for r in result.rejections if r.symbol == "WIDE")
    assert wide.check is not None and wide.check.rule == "spread_ratio"
    assert wide.measured.startswith("screen 1 spread_ratio ") and wide.threshold == "<= 0.015"
    # No snapshot spread limit of its own: a strategy with a wider limit keeps WIDE.
    looser = run(md, screen=Screen(max_spread_ratio="0.05"))
    assert {r.symbol: r.cause for r in looser.rejections}["WIDE"] is Cause.NO_BARS


def test_dollar_volume_counts_only_completed_session_minutes():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "AAA"), row(G, 2, "LOW"), row(G, 3, "NOB")])
    md.set_quote(quote("AAA"))
    md.set_quote(quote("LOW"))
    md.set_quote(quote("NOB"))
    # Pre-session bars, 30 completed session minutes and the minute in progress (09:30 + 30).
    md.add_bars(bars("AAA", 10, volume="999999", start=OPEN - timedelta(minutes=10)))
    md.add_bars(bars("AAA", 31, volume="16667"))
    md.add_bars(bars("LOW", 31, volume="16666"))
    result = run(md)
    assert [c.instrument.symbol for c in result.candidates] == ["AAA"]
    assert len(result.candidates[0].session_bars) == 30
    causes = {r.symbol: r.cause for r in result.rejections}
    assert causes == {"LOW": Cause.STRATEGY_LIQUIDITY, "NOB": Cause.NO_BARS}
    low = next(r for r in result.rejections if r.symbol == "LOW")
    assert low.check is not None and low.check.rule == "dollar_volume_usd"
    assert low.check.measured == Decimal(30 * 10 * 16666)
    # The floor is the strategy's: a lower one keeps LOW.
    lower = run(md, screen=Screen(min_dollar_volume_usd="4000000"))
    assert [c.instrument.symbol for c in lower.candidates] == ["AAA", "LOW"]


def test_completed_session_bars():
    series = bars("X", 3, start=OPEN - timedelta(minutes=1))
    kept = completed_session_bars(series, OPEN, OPEN + timedelta(minutes=1))
    assert [b.start for b in kept] == [OPEN]


def test_bars_only_before_the_session_are_no_session_bars():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "PRE")])
    md.set_quote(quote("PRE"))
    md.add_bars(bars("PRE", 5, volume="9999999", start=OPEN - timedelta(minutes=10)))
    (refusal,) = run(md).rejections
    assert refusal.cause is Cause.NO_BARS and refusal.check is None


def test_requests_are_chunked():
    md = FakeMarketData()
    symbols = [f"S{i:02d}" for i in range(5)]
    md.set_screener(G, [row(G, i, s) for i, s in enumerate(symbols, 1)])
    for s in symbols:
        liquid(md, s)
    result = run(md, snapshot_symbols_per_request=2, bars_symbols_per_request=3)
    assert len(result.candidates) == 5
    assert [len(a) for m, a in md.calls if m == "snapshot"] == [2, 2, 1]
    assert [len(a) for m, a in md.calls if m == "bars"] == [3, 2]


def test_bars_budget_is_visible_never_silent():
    md = FakeMarketData()
    symbols = [f"S{i:02d}" for i in range(5)]
    md.set_screener(G, [row(G, i, s) for i, s in enumerate(symbols, 1)])
    for s in symbols:
        liquid(md, s)
    md.limit("bars", 1)
    result = run(md, bars_symbols_per_request=2)
    assert len(result.candidates) == 2
    assert result.stopped is not None
    assert (result.stopped.stage, result.stopped.cause) == (Stage.BARS, StopCause.BUDGET)
    assert result.summary() == "evaluated 2 of 5: bars budget"


def test_snapshot_budget_stops_pass():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "A1"), row(G, 2, "A2")])
    md.limit("snapshot", 0)
    result = run(md)
    assert result.summary() == "evaluated 0 of 2: snapshot budget"


def test_screener_budget_reports_partial_screeners():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "A1")])
    md.limit("screener", 1)
    sink = Sink()
    result = run(md, sink)
    assert result.screeners_read == (G,)
    assert result.summary() == "evaluated 0 of 1 (screeners 1 of 3): screener budget"
    assert sink.skipped == [(L, "budget", "discovery"), (A, "budget", "discovery")]


def test_delayed_bars_stop_the_pass():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "AAA")])
    liquid(md, "AAA")
    md.delay_bars(Decimal(15))
    result = run(md)
    assert result.candidates == ()
    assert result.summary() == "evaluated 0 of 1: bars delayed data"


def test_capture_receives_rows_quotes_and_completed_bars():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "AAA")])
    md.set_screener(A, [row(A, 1, "AAA")])
    md.add_bars(bars("AAA", 31))
    md.set_quote(quote("AAA"))
    md.set_quote(quote("ZZZ"))
    sink = Sink()
    run(md, sink)
    assert len(sink.screener_rows) == 2
    assert [q.symbol for q in sink.quotes_seen] == ["AAA"]
    assert len(sink.bars_seen) == 30


@pytest.mark.parametrize("name", ["price-action-v1", "price-action-v2", "noise-band-v1",
                                  "volume-shock-v1"])
def test_every_shipped_strategy_screens_like_the_former_platform_limits(name):
    """Moving the limits into the strategies changed no candidate (owner 2026-11-15): with each
    shipped strategy, discovery keeps and refuses exactly what spread 1.5% and 5M USD did."""
    import importlib
    import tomllib
    module = importlib.import_module("tickr.strategy." + name.replace("-", "_"))
    values = tomllib.loads((ROOT / "strategies" / name / "strategy.toml").read_text())
    strategy = module.create(values)
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, s) for s in ("OK", "WIDE", "EDGE", "LOW", "FLOOR")])
    md.set_quote(quote("OK"))
    md.set_quote(quote("WIDE", bid="10.00", ask="10.16"))  # 1.587% > 1.5%
    md.set_quote(quote("EDGE", bid="9.925", ask="10.075"))  # exactly 1.5%: kept
    md.set_quote(quote("LOW"))
    md.set_quote(quote("FLOOR"))
    md.add_bars(bars("OK", 31))
    md.add_bars(bars("WIDE", 31))
    md.add_bars(bars("EDGE", 31))
    md.add_bars(bars("LOW", 31, volume="16666"))  # 4,999,800 USD
    md.add_bars(bars("FLOOR", 25, volume="20000") + bars(
        "FLOOR", 6, volume="0", start=OPEN + timedelta(minutes=25)))  # exactly 5M by 10:00
    result = run(md, screen=strategy)
    assert [c.instrument.symbol for c in result.candidates] == ["OK", "EDGE", "FLOOR"]
    refused = {r.symbol: (r.cause, r.check.rule if r.check else None) for r in result.rejections}
    assert refused == {"WIDE": (Cause.STRATEGY_LIQUIDITY, "spread_ratio"),
                       "LOW": (Cause.STRATEGY_LIQUIDITY, "dollar_volume_usd")}


class Unknown(FakeMarketData):
    """A route refuses the whole read when it holds a symbol it does not know (Webull HTTP 417
    INVALID_SYMBOL, 2026-11-16: the snapshot of a chunk holding DJT)."""

    def __init__(self, snapshot: set[str] = frozenset(), bars: set[str] = frozenset()) -> None:
        super().__init__()
        self.unknown = {"snapshot": set(snapshot), "bars": set(bars)}

    def snapshot(self, symbols):
        quotes = super().snapshot(symbols)
        if self.unknown["snapshot"] & set(symbols):
            raise SymbolsRefused("SNAPSHOT", 417, "INVALID_SYMBOL")
        return quotes

    def bars(self, symbols, count, *, budget_reserve=0):
        got = super().bars(symbols, count, budget_reserve=budget_reserve)
        if self.unknown["bars"] & set(symbols):
            raise SymbolsRefused("BARS", 417, "INVALID_SYMBOL")
        return got


def five(md: FakeMarketData) -> list[str]:
    symbols = [f"S{i:02d}" for i in range(5)]
    md.set_screener(G, [row(G, i, s) for i, s in enumerate(symbols, 1)])
    for s in symbols:
        liquid(md, s)
    return symbols


def test_symbol_unknown_to_snapshot_is_refused_alone_and_the_rest_is_read():
    md = Unknown(snapshot={"S02"})
    five(md)
    result = run(md)
    assert [c.instrument.symbol for c in result.candidates] == ["S00", "S01", "S03", "S04"]
    (refusal,) = [r for r in result.rejections if r.cause is Cause.SYMBOL_REFUSED]
    assert refusal.symbol == "S02" and "snapshot" in refusal.measured
    assert result.stopped is None and result.summary() == "evaluated 5 of 5"
    assert all("S02" not in a for m, a in md.calls if m == "bars")


def test_symbol_unknown_to_bars_is_refused_alone_and_the_rest_is_read():
    md = Unknown(bars={"S04"})
    five(md)
    result = run(md, bars_symbols_per_request=3)
    assert [c.instrument.symbol for c in result.candidates] == ["S00", "S01", "S02", "S03"]
    (refusal,) = [r for r in result.rejections if r.cause is Cause.SYMBOL_REFUSED]
    assert refusal.symbol == "S04" and "bars" in refusal.measured
    assert result.stopped is None


def test_refused_symbol_is_not_asked_again_this_session_but_is_next_session():
    md = Unknown(snapshot={"S02"})
    five(md)
    memory = RefusedSymbols()
    discover(md, settings(), Screen(), OPEN, lambda: NOW, None, memory)
    md.calls.clear()
    second = discover(md, settings(), Screen(), OPEN, lambda: NOW, None, memory)
    assert [len(a) for m, a in md.calls if m == "snapshot"] == [4]  # one request, no split
    assert all("S02" not in a for _, a in md.calls)
    (refusal,) = [r for r in second.rejections if r.cause is Cause.SYMBOL_REFUSED]
    assert "earlier this session" in refusal.measured
    md.calls.clear()
    discover(md, settings(), Screen(), OPEN + timedelta(days=1), lambda: NOW, None, memory)
    assert any("S02" in a for m, a in md.calls if m == "snapshot")


def test_refused_symbol_is_asked_again_after_the_recheck_period():
    md = Unknown(snapshot={"S02"})
    five(md)
    memory = RefusedSymbols()
    recheck = timedelta(seconds=900)
    discover(md, settings(refused_symbol_recheck=recheck), Screen(), OPEN, lambda: NOW, None,
             memory)
    md.calls.clear()
    just_before = NOW + recheck - timedelta(seconds=1)
    discover(md, settings(refused_symbol_recheck=recheck), Screen(), OPEN, lambda: just_before,
             None, memory)
    assert all("S02" not in a for _, a in md.calls)  # still remembered
    md.calls.clear()
    md.unknown["snapshot"] = set()  # the provider knows the symbol now
    third = discover(md, settings(refused_symbol_recheck=recheck), Screen(), OPEN,
                     lambda: NOW + recheck, None, memory)
    assert any("S02" in a for m, a in md.calls if m == "snapshot")  # asked again
    assert not [r for r in third.rejections if r.cause is Cause.SYMBOL_REFUSED]


def test_budget_during_a_split_still_stops_the_pass_visibly():
    md = Unknown(snapshot={"S02"})
    five(md)
    md.limit("snapshot", 1)
    result = run(md)
    assert result.stopped is not None
    assert (result.stopped.stage, result.stopped.cause) == (Stage.SNAPSHOT, StopCause.BUDGET)


def test_symbols_isolated_before_a_budget_stop_are_kept_so_the_next_pass_progresses():
    md = Unknown(snapshot={"S01", "S02", "S03"})
    five(md)
    md.limit("snapshot", 4)
    memory = RefusedSymbols()
    first = discover(md, settings(), Screen(), OPEN, lambda: NOW, None, memory)
    assert first.stopped is not None and first.stopped.stage is Stage.SNAPSHOT
    kept = {r.symbol for r in first.rejections if r.cause is Cause.SYMBOL_REFUSED}
    assert kept  # what the split isolated before the budget stopped it
    md.limit("snapshot", 1000)
    second = discover(md, settings(), Screen(), OPEN, lambda: NOW, None, memory)
    assert second.stopped is None
    assert [c.instrument.symbol for c in second.candidates] == ["S00", "S04"]


def test_every_discovery_bars_read_leaves_the_configured_reserve():
    md = FakeMarketData()
    five(md)
    run(md, bars_symbols_per_request=2)
    assert md.bars_reserves == [3, 3, 3]


def test_every_bars_read_is_kept_with_the_time_it_was_sent():
    md = Unknown(bars={"S04"})
    five(md)
    times = iter(NOW + timedelta(seconds=i) for i in range(100))
    kept = TodayBars()
    discover(md, settings(bars_symbols_per_request=3), Screen(), OPEN, lambda: next(times),
             None, None, kept)
    # The clock is read at the pass start, for the refused-symbol memory, by the snapshot
    # stage and then before the first bars request.
    minute_end = NOW + timedelta(seconds=3)
    for symbol in ("S00", "S01", "S02", "S03"):
        assert len(kept.complete(OPEN, symbol, minute_end) or ()) == 30
    assert kept.complete(OPEN, "S00", minute_end + timedelta(seconds=1)) is None  # sent before
    assert kept.complete(OPEN, "S04", minute_end) is None  # refused: nothing kept
    assert kept.complete(OPEN + timedelta(days=1), "S00", minute_end) is None  # next session


# ---------------------------------------------------------------- outcomes and fast movers


class MoverScreen(Screen):
    """Screen plus the fast-mover rule of price-action-v3, with small test thresholds."""

    def fast_mover(self, symbol, session_bars):
        return measure(symbol, session_bars, MoverSettings(5, Decimal(3), Decimal(2),
                                                           Decimal(3), Decimal(1)))


def spiking(symbol: str) -> list:
    """29 sideways minutes, then one heavy upward bar."""
    out = []
    for i in range(29):
        close = Decimal("10.00") + Decimal("0.02") * (i % 2)
        out.append(Bar(symbol, OPEN + timedelta(minutes=i), close, close + Decimal("0.05"),
                       close - Decimal("0.05"), close, Decimal(20000)))
    out.append(Bar(symbol, OPEN + timedelta(minutes=29), Decimal("10.02"), Decimal("10.60"),
                   Decimal("10.00"), Decimal("10.50"), Decimal(200000)))
    return out


def test_symbols_a_stopped_pass_never_reached_are_named():
    md = FakeMarketData()
    symbols = [f"S{i:02d}" for i in range(5)]
    md.set_screener(G, [row(G, i, s) for i, s in enumerate(symbols, 1)])
    for s in symbols:
        liquid(md, s)
    md.limit("bars", 1)
    result = run(md, bars_symbols_per_request=2)
    assert result.not_reached == ("S02", "S03", "S04")
    finished = run(FakeMarketData())
    assert finished.not_reached == ()


def test_instruments_keep_their_screeners():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "AAA")])
    md.set_screener(A, [row(A, 1, "AAA"), row(A, 2, "BBB")])
    result = run(md)
    assert [(i.symbol, i.kinds) for i in result.instruments] == [("AAA", (G, A)), ("BBB", (A,))]


def test_fast_movers_are_measured_on_the_bars_already_read():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "AAA"), row(G, 2, "BBB")])
    md.set_quote(quote("AAA"))
    md.set_quote(quote("BBB"))
    md.add_bars(spiking("AAA"))
    md.add_bars(bars("BBB", 30))
    before = len(md.calls)
    result = run(md, screen=MoverScreen("0.015", "1"))
    assert [m.symbol for m in result.movers] == ["AAA"] and result.movers[0].spike_up
    assert [m for m, _ in md.calls[before:]].count("bars") == 1  # no read of its own
    plain = run(md, screen=Screen("0.015", "1"))  # a strategy without the rule
    assert plain.movers == () and not plain.movers_measured


def test_pass_record_is_plain_json_with_every_outcome():
    md = FakeMarketData()
    md.set_screener(G, [row(G, 1, "AAA"), row(G, 2, "ETF", security_type="ETF"),
                        row(G, 3, "THIN")])
    md.set_quote(quote("AAA"))
    md.set_quote(quote("THIN"))
    md.add_bars(spiking("AAA"))
    md.add_bars(bars("THIN", 30, volume="1"))
    result = run(md, screen=MoverScreen("0.015", "100000"))
    record = json.loads(json.dumps(pass_record(result, NOW)))
    assert record["finished_at"] == "2026-11-13T15:00:10+00:00"
    assert record["candidates"] == ["AAA"] and record["not_reached"] == []
    assert [i["kinds"] for i in record["instruments"]] == [["GAINERS"]] * 3
    causes = {r["symbol"]: (r["cause"], r["check"]) for r in record["rejections"]}
    stages = {r["symbol"]: r["stage"] for r in record["rejections"]}
    assert causes["ETF"] == ("SECURITY_TYPE_NOT_ALLOWED", None) and stages["ETF"] == "screener"
    assert causes["THIN"][0] == "STRATEGY_LIQUIDITY" and stages["THIN"] == "bars"
    assert causes["THIN"][1]["rule"] == "dollar_volume_usd" and not causes["THIN"][1]["passed"]
    assert record["movers_measured"] is True
    mover = record["movers"][0]
    assert mover["symbol"] == "AAA" and mover["spike_up"] is True
    assert mover["volume_ratio"]["fired"] is True and mover["volume_ratio"]["threshold"] == "3"


def test_a_restart_seeds_the_refusals_from_the_last_pass_record_and_splits_nothing():
    """2026-11-16: the first pass after each restart split DJT's chunk again and stopped on the
    snapshot budget. The pass record carries the remembered refusals; a new memory seeded from
    the day's latest record asks the rest in one request."""
    md = Unknown(snapshot={"S02"})
    five(md)
    first = discover(md, settings(), Screen(), OPEN, lambda: NOW, None, RefusedSymbols())
    lines = [json.dumps(pass_record(first, NOW)) + "\n"]
    seeded = RefusedSymbols()
    seeded.seed(*latest_refusals(lines, "discovery.jsonl"))
    md.calls.clear()
    later = NOW + timedelta(seconds=30)
    second = discover(md, settings(), Screen(), OPEN, lambda: later, None, seeded)
    assert [len(a) for m, a in md.calls if m == "snapshot"] == [4]  # no split
    (refusal,) = [r for r in second.rejections if r.cause is Cause.SYMBOL_REFUSED]
    assert refusal.symbol == "S02" and "earlier this session" in refusal.measured


def test_seeded_refusals_keep_their_own_time_and_session():
    md = Unknown(snapshot={"S02"})
    five(md)
    memory = RefusedSymbols()
    memory.seed(OPEN, (RefusedSymbol("S02", Stage.SNAPSHOT, NOW - timedelta(seconds=899)),))
    assert memory.active(OPEN, NOW, timedelta(seconds=900)) == {"S02": Stage.SNAPSHOT}
    assert memory.active(OPEN, NOW + timedelta(seconds=1), timedelta(seconds=900)) == {}
    memory.seed(OPEN, (RefusedSymbol("S02", Stage.SNAPSHOT, NOW),))
    assert memory.active(OPEN + timedelta(days=1), NOW, timedelta(seconds=900)) == {}


def test_latest_refusals_reads_the_newest_complete_record():
    def line(symbols, at="2026-11-13T15:00:00+00:00"):
        return json.dumps({"session_open": "2026-11-13T14:30:00+00:00", "refused_symbols": [
            {"symbol": s, "stage": "snapshot", "refused_at": at} for s in symbols]}) + "\n"
    found = latest_refusals([line(["A"]), line(["A", "B"]), line(["C"])[:-1]], "f")
    assert found is not None
    session, refused = found
    assert session == OPEN and [r.symbol for r in refused] == ["A", "B"]  # half line skipped
    assert latest_refusals([json.dumps({"candidates": []}) + "\n"], "f") is None  # older format
    assert latest_refusals([], "f") is None
    with pytest.raises(ValueError, match="f:1"):
        latest_refusals(["{not json\n"], "f")
    with pytest.raises(ValueError, match="UTC offset"):
        latest_refusals([line(["A"], at="2026-11-13T15:00:00")], "f")
