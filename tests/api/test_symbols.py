"""GET /symbols and GET /movers: every symbol through the pipeline, and the fast movers."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tickr.api import contract, symbols
from tickr.data.capture import CaptureWriter
from tickr.observers.loader import read_values
from tickr.observers.log import DecisionLog
from tickr.research.records import Admission
from tickr.strategy.base import Verdict

from .test_decisions import BAR, DAY, FAIL, PASS, _cfg, _get, _serve, _write
from .test_flow import _capture, _watch
from .test_server import doc, request

STARTED = BAR - timedelta(minutes=5)


def _iso(at):
    return at.isoformat()


def _mover(symbol, minute, spike_up=True, move="2.5"):
    def signal(value, threshold, fired):
        return {"value": value, "threshold": threshold, "fired": fired}
    return {"symbol": symbol, "bar_start": _iso(BAR + timedelta(minutes=minute)),
            "close": "10.50", "low": "10.00", "bar_dollar_volume": "820000",
            "volume_ratio": signal("13.3", "5", True), "move_atr": signal(move, "2", True),
            "dollar_volume_ratio": signal("4.1", "5", False), "spike_up": spike_up}


def _pass(minute, *, rejections=(), candidates=(), not_reached=(), stopped=None, movers=(),
          listed=("ABCD", "SPY", "THIN", "SLOW", "LATE")):
    start = STARTED + timedelta(minutes=minute)
    return {
        "started_at": _iso(start), "finished_at": _iso(start + timedelta(seconds=20)),
        "screeners_read": ["GAINERS", "LOSERS", "TOP_ACTIVES"], "stopped": stopped,
        "instruments": [{"symbol": s, "instrument_id": f"id-{s}", "kinds": ["GAINERS"]}
                        for s in listed],
        "rejections": list(rejections), "candidates": list(candidates),
        "not_reached": list(not_reached), "movers_measured": True, "movers": list(movers)}


def _refused(symbol, stage, cause, measured="m", threshold="t", check=None):
    return {"symbol": symbol, "instrument_id": f"id-{symbol}", "cause": cause, "stage": stage,
            "measured": measured, "threshold": threshold, "check": check}


DV_CHECK = {"rule": "dollar_volume_usd", "measured": "812345.123456", "operator": ">=",
            "threshold": "5000000", "passed": False, "note": ""}


def _discovery(cfg, *passes):
    writer = CaptureWriter.from_config(cfg, "webull")
    for record in passes:
        writer.discovery(record, symbols._time(record["finished_at"], "t"))
    writer.close()


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path, "[]")


@pytest.fixture
def api(cfg):
    server = _serve(cfg)
    yield server
    server.shutdown()
    server.server_close()


def test_every_symbol_with_its_furthest_stage_and_where_it_stands_now(api, cfg):
    _capture(cfg)
    _discovery(cfg,
               _pass(0, candidates=("ABCD", "THIN"),
                     rejections=(_refused("SPY", "screener", "SECURITY_TYPE_NOT_ALLOWED",
                                          "ETF", "COMMON_STOCK"),),
                     not_reached=("SLOW",), stopped={"stage": "bars", "cause": "budget",
                                                     "detail": "bars route window full"}),
               _pass(10, candidates=("ABCD",),
                     rejections=(_refused("SPY", "screener", "SECURITY_TYPE_NOT_ALLOWED",
                                          "ETF", "COMMON_STOCK"),
                                 _refused("THIN", "bars", "STRATEGY_LIQUIDITY",
                                          check=DV_CHECK),
                                 _refused("LATE", "snapshot", "QUOTE_STALE", "7.2s",
                                          "<= 5.0s"))))
    _write(DecisionLog.from_config(cfg), _watch("ABCD", 0, PASS, FAIL),
           _watch("THIN", 0, PASS, verdict=Verdict.ENTER, admission=Admission.REFUSED,
                  cause="POSITION_LIMIT_REACHED: 1 open position, limit 1"))
    body = _get(api, "/symbols", "symbols-v1")
    assert body["day"] == DAY.isoformat() and body["as_of"] is not None
    rows = {r["symbol"]: r for r in body["rows"]}
    assert set(rows) == {"ABCD", "SPY", "THIN", "SLOW", "LATE", "EFGH", "WXYZ"}

    def where(symbol):
        r = rows[symbol]
        return r["furthest"], r["stage"], r["state"], r["cause"]

    assert where("ABCD") == ("strategy", "strategy", "waiting", None)  # WATCH
    assert rows["ABCD"]["verdict"] == "WATCH"
    assert rows["ABCD"]["sources"] == [{"kind": "GAINERS", "rank": 1},
                                       {"kind": "TOP_ACTIVES", "rank": 2}]
    assert where("SPY") == ("universe", "universe", "dropped", "SECURITY_TYPE_NOT_ALLOWED")
    assert rows["SPY"]["detail"] == "ETF (needs COMMON_STOCK)"
    # Reached the strategy (and risk refused it) earlier; the newer pass dropped it at bars.
    assert where("THIN") == ("strategy", "bars", "dropped", "STRATEGY_LIQUIDITY")
    assert rows["THIN"]["check"]["measured"] == "812345.1234"  # rounded like every check
    assert where("SLOW") == ("bars", "bars", "not_reached", "REQUEST_BUDGET")
    assert rows["SLOW"]["detail"] is None  # the route's own text never reaches a screen
    assert where("LATE") == ("quote", "quote", "dropped", "QUOTE_STALE")
    # An age to a tenth of a second, the signs as signs.
    assert rows["LATE"]["detail"] == "7.2 s (needs \u2264 5.0 s)"
    assert rows["LATE"]["first_seen"] == "2026-11-16T14:55:00.000Z"
    # The detail panel: the price, when each stage was reached and the last decision's checks.
    assert rows["ABCD"]["price"] == "11.24"  # the latest screener row
    assert [(r["stage"], r["at"]) for r in rows["ABCD"]["reached"]] == [
        ("screeners", "2026-11-16T14:55:00.000Z"), ("universe", "2026-11-16T14:55:20.000Z"),
        ("quote", "2026-11-16T14:55:20.000Z"), ("bars", "2026-11-16T14:55:20.000Z"),
        ("strategy", "2026-11-16T14:55:20.000Z")]  # a candidate: handed to the strategy
    assert rows["ABCD"]["strategy"].startswith("price-action")
    assert [c["rule"] for c in rows["ABCD"]["checks"]] == [PASS.rule, FAIL.rule]
    assert rows["SPY"]["decided_at"] is None and rows["SPY"]["checks"] == []
    assert [r["stage"] for r in rows["SPY"]["reached"]] == ["screeners", "universe"]
    counts = [(s["stage"], s["count"]) for s in body["stages"]]
    assert [stage for stage, _ in counts] == [s.value for s in contract.SymbolStage]
    assert dict(counts)["screeners"] == 7 and dict(counts)["strategy"] == 2


def test_risk_refusal_and_ignore_are_drops_with_their_cause(cfg):
    folded = symbols.DiscoveryFolded()
    folded.fold(_pass(0, candidates=("ABCD", "EFGH")))
    records = (_watch("ABCD", 30, PASS, verdict=Verdict.ENTER, admission=Admission.REFUSED,
                      cause="DAILY_LOSS_CEILING: 0 USD left"),
               _watch("EFGH", 30, PASS, FAIL, verdict=Verdict.IGNORE))
    document = symbols.symbols_document(DAY, folded, (), records, (), 4)
    rows = {r.symbol: r for r in document.rows}
    assert (rows["ABCD"].stage, rows["ABCD"].state, rows["ABCD"].cause) == (
        contract.SymbolStage.RISK, contract.SymbolState.DROPPED, "refused")
    assert rows["ABCD"].detail == "DAILY_LOSS_CEILING: 0 USD left"
    assert (rows["EFGH"].stage, rows["EFGH"].cause, rows["EFGH"].check.rule) == (
        contract.SymbolStage.STRATEGY, "IGNORE", FAIL.rule)


def test_a_filled_trade_is_at_the_stop_stage():
    folded = symbols.DiscoveryFolded()
    folded.fold(_pass(0, candidates=("ABCD", "EFGH")))
    stops = (contract.FlowStopV1("t1", "ABCD", contract.FlowStopState.VERIFIED),
             contract.FlowStopV1("t2", "EFGH", contract.FlowStopState.PENDING))
    records = (_watch("ABCD", 0, PASS, verdict=Verdict.ENTER, admission=Admission.TAKEN),
               _watch("EFGH", 0, PASS, verdict=Verdict.ENTER, admission=Admission.TAKEN))
    rows = {r.symbol: r for r in symbols.symbols_document(DAY, folded, (), records, stops,
                                                          4).rows}
    assert (rows["ABCD"].furthest, rows["ABCD"].state) == (contract.SymbolStage.STOP,
                                                           contract.SymbolState.PASSED)
    # Filled, its stop not seen working yet: it got to the stop stage and waits there.
    assert (rows["EFGH"].furthest, rows["EFGH"].stage, rows["EFGH"].state) == (
        contract.SymbolStage.STOP, contract.SymbolStage.STOP, contract.SymbolState.WAITING)


def test_the_order_and_stop_stages_carry_the_journal_times_not_a_later_decisions():
    folded = symbols.DiscoveryFolded()
    folded.fold(_pass(0, candidates=("ABCD", "EFGH")))
    filled, confirmed = BAR + timedelta(minutes=1), BAR + timedelta(minutes=1, seconds=4)
    stops = (contract.FlowStopV1("t1", "ABCD", contract.FlowStopState.VERIFIED, filled, confirmed),
             contract.FlowStopV1("t2", "EFGH", contract.FlowStopState.PENDING, filled))
    # A HOLD long after the entry: never the time the stop stage shows.
    records = (_watch("ABCD", 0, PASS, verdict=Verdict.ENTER, admission=Admission.TAKEN),
               _watch("ABCD", 30, PASS, verdict=Verdict.HOLD),
               _watch("EFGH", 0, PASS, verdict=Verdict.ENTER, admission=Admission.TAKEN))
    rows = {r.symbol: r for r in symbols.symbols_document(DAY, folded, (), records, stops,
                                                          4).rows}
    reached = {r.stage: r.at for r in rows["ABCD"].reached}
    assert reached[contract.SymbolStage.ORDER] == filled
    assert reached[contract.SymbolStage.STOP] == confirmed
    assert rows["ABCD"].at == confirmed
    assert rows["EFGH"].at == filled
    assert {r.stage: r.at for r in rows["EFGH"].reached}[contract.SymbolStage.ORDER] == filled


def test_no_pass_yet_says_so(api):
    body = _get(api, "/symbols", "symbols-v1")
    assert body["as_of"] is None and body["as_of_cause"] == symbols.NO_PASS
    assert body["rows"] == []


def test_movers_newest_first_with_watching_and_the_strategy_rules(api, cfg):
    _discovery(cfg, _pass(0, movers=(_mover("ABCD", 0), _mover("EFGH", 0, False, "-2.4"))),
               _pass(1, movers=(_mover("QRST", 1),)))
    _write(DecisionLog.from_config(cfg), _watch("ABCD", 0, PASS, FAIL))
    body = _get(api, "/movers", "movers-v1")
    assert [m["symbol"] for m in body["movers"]] == ["QRST", "ABCD", "EFGH"]
    first = {m["symbol"]: m for m in body["movers"]}
    assert first["ABCD"]["watching"] is True and first["QRST"]["watching"] is False
    assert first["EFGH"]["move_atr"] == "-2.4" and first["EFGH"]["spike_up"] is False
    assert first["ABCD"]["fired"] == ["volume_ratio", "move_atr"]
    # The thresholds are the active strategy's own [spike] section, read from its strategy.toml.
    expected = symbols.mover_rules(read_values(cfg.root, cfg.strategy.active))
    assert expected is not None and body["rules_cause"] is None
    assert body["rules"]["lookback_bars"] == expected.lookback_bars
    assert body["rules"]["min_move_atr"] == str(expected.min_move_atr)


def test_a_strategy_without_spike_has_no_rules_and_says_why():
    body = symbols.movers_document(DAY, symbols.DiscoveryFolded(), None, ())
    assert body.rules is None and body.rules_cause == symbols.NO_RULES


def test_rules_come_from_the_strategy_spike_section():
    rules = symbols.mover_rules({"spike": {
        "lookback_bars": 10, "min_volume_ratio": 5.0, "min_move_atr": 2.0,
        "min_dollar_volume_ratio": 5.0, "min_bar_dollar_volume_usd": 250000,
        "signal_valid_bars": 3}})
    assert rules is not None and rules.lookback_bars == 10 and str(rules.min_move_atr) == "2.0"
    assert symbols.mover_rules({}) is None


def test_the_capture_is_read_incrementally(cfg):
    day = symbols.DiscoveryDay()
    path = CaptureWriter.from_config(cfg, "webull").path(DAY, "discovery.jsonl")
    _discovery(cfg, _pass(0, candidates=("ABCD",)))
    assert day.read(path, open_tail=True).latest["ABCD"].state is contract.SymbolState.WAITING
    _discovery(cfg, _pass(1, rejections=(_refused("ABCD", "snapshot", "NO_QUOTE"),)))
    assert day.read(path, open_tail=True).latest["ABCD"].state is contract.SymbolState.DROPPED


def test_a_malformed_mover_is_a_clean_refusal(api, cfg):
    bad = _mover("ABCD", 0)
    bad["move_atr"] = {"value": "not a number", "threshold": "2", "fired": True}
    _discovery(cfg, _pass(0, movers=(bad,)))
    for path in ("/movers", "/symbols"):
        resp, payload = request(api, "GET", path)
        body = doc(payload)
        assert resp.status == 503 and body["error"] == "unavailable"
        assert "capture file unreadable" in body["cause"] and "move_atr" in body["cause"]


def test_an_unknown_stop_cause_is_a_clean_refusal(api, cfg):
    _discovery(cfg, _pass(0, not_reached=("SLOW",),
                          stopped={"stage": "bars", "cause": "tired", "detail": "x"}))
    resp, payload = request(api, "GET", "/symbols")
    body = doc(payload)
    assert resp.status == 503 and "unknown stop cause" in body["cause"]


def test_a_read_hands_out_a_snapshot_a_later_fold_leaves_alone(cfg):
    day = symbols.DiscoveryDay()
    path = CaptureWriter.from_config(cfg, "webull").path(DAY, "discovery.jsonl")
    _discovery(cfg, _pass(0, candidates=("ABCD",), movers=(_mover("ABCD", 0),)))
    first = day.read(path, open_tail=True)
    _discovery(cfg, _pass(1, rejections=(_refused("ABCD", "snapshot", "NO_QUOTE"),),
                          movers=(_mover("EFGH", 1),)))
    second = day.read(path, open_tail=True)
    assert first.latest["ABCD"].state is contract.SymbolState.WAITING and "EFGH" not in first.movers
    assert second.latest["ABCD"].state is contract.SymbolState.DROPPED and "EFGH" in second.movers


def test_each_row_carries_the_time_of_its_last_check():
    """Backlog T16: under a lasting budget deficit a `not_reached` row says how old its last
    check is; a record written before the times were kept leaves the earlier ones."""
    folded = symbols.DiscoveryFolded()
    checked_at = BAR - timedelta(minutes=2)
    first = _pass(0, candidates=("ABCD",), not_reached=("SPY",))
    first["checked_symbols"] = [{"symbol": "ABCD", "checked_at": _iso(checked_at)},
                                {"symbol": "SPY", "checked_at": _iso(checked_at)}]
    folded.fold(first)
    folded.fold(_pass(1, not_reached=("SPY",)))
    rows = {r.symbol: r for r in symbols.symbols_document(DAY, folded, (), (), (), 4).rows}
    assert rows["SPY"].state is contract.SymbolState.NOT_REACHED
    assert rows["SPY"].checked_at == checked_at and rows["ABCD"].checked_at == checked_at
    assert rows["THIN"].checked_at is None
