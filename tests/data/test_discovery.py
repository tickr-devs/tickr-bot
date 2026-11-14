from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from md_helpers import NOW, OPEN, bars, quote, row, settings

from aperture import config
from aperture.data.base import FakeMarketData, ScreenerKind
from aperture.data.discovery import (
    Cause,
    DiscoverySettings,
    Stage,
    StopCause,
    completed_session_bars,
    discover,
    merge,
    session_dollar_volume,
)

G, L, A = ScreenerKind.GAINERS, ScreenerKind.LOSERS, ScreenerKind.TOP_ACTIVES
ROOT = Path(__file__).resolve().parents[2]


class Sink:
    def __init__(self) -> None:
        self.screener_rows: list = []
        self.bars_seen: list = []
        self.quotes_seen: list = []

    def screener(self, rows, provider, receipt_time):
        self.screener_rows.extend(rows)

    def bars(self, bars, provider, receipt_time):
        self.bars_seen.extend(bars)

    def quotes(self, quotes, provider, source):
        self.quotes_seen.extend(quotes)


def liquid(md: FakeMarketData, symbol: str, minutes: int = 30) -> None:
    md.set_quote(quote(symbol))
    md.add_bars(bars(symbol, minutes))


def run(md: FakeMarketData, sink=None, **overrides):
    return discover(md, settings(**overrides), OPEN, lambda: NOW, sink)


def test_settings_from_bot_config():
    cfg = config.load(ROOT / "config" / "bot.toml")
    s = DiscoverySettings.from_config(cfg)
    assert "NSQ" in s.allowed_exchanges and s.max_quote_age == timedelta(seconds=5)
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
    c = result.candidates[0]
    # 30 completed minutes x 10 USD x 20000 shares
    assert c.session_dollar_volume == Decimal(6000000)
    assert c.spread_ratio == Decimal("0.02") / Decimal("10.01")
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
    md.set_screener(G, [row(G, i, s) for i, s in enumerate(["NOQ", "OLD", "BAD", "WIDE"], 1)])
    md.set_quote(quote("OLD", age_seconds=6))
    md.set_quote(quote("BAD", bid="10.05", ask="10.00"))
    md.set_quote(quote("WIDE", bid="10.00", ask="10.20"))
    result = run(md)
    causes = {r.symbol: r.cause for r in result.rejections}
    assert causes == {"NOQ": Cause.NO_QUOTE, "OLD": Cause.QUOTE_STALE,
                      "BAD": Cause.QUOTE_INVALID, "WIDE": Cause.SPREAD_TOO_WIDE}
    assert result.evaluated == 4 and not any(c[0] == "bars" for c in md.calls)


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
    assert result.candidates[0].session_dollar_volume == Decimal(30 * 10 * 16667)
    assert len(result.candidates[0].session_bars) == 30
    causes = {r.symbol: r.cause for r in result.rejections}
    assert causes == {"LOW": Cause.DOLLAR_VOLUME_TOO_LOW, "NOB": Cause.NO_BARS}


def test_completed_session_bars_and_volume_helpers():
    series = bars("X", 3, start=OPEN - timedelta(minutes=1))
    kept = completed_session_bars(series, OPEN, OPEN + timedelta(minutes=1))
    assert [b.start for b in kept] == [OPEN]
    assert session_dollar_volume(kept) == Decimal(200000)
    assert session_dollar_volume(()) == Decimal(0)


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
    result = run(md)
    assert result.screeners_read == (G,)
    assert result.summary() == "evaluated 0 of 1 (screeners 1 of 3): screener budget"


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
