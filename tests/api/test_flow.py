"""GET /flow and GET /watching: the Overview's pipeline and closest symbols, read-only."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from aperture.api import contract
from aperture.api.contract import closeness
from aperture.api.history import History, HistoryPaths
from aperture.broker.fake import FakeBroker
from aperture.config import GateResult
from aperture.data.base import ScreenerKind, ScreenerRow
from aperture.data.capture import CaptureWriter
from aperture.engine.core import Engine
from aperture.observers.log import DecisionLog
from aperture.research.records import Admission
from aperture.store import Store
from aperture.strategy.base import Check, Verdict

from .samples import account, status
from .test_decisions import (
    ACTIVE,
    BAR,
    DAY,
    FAIL,
    FLAG,
    PASS,
    _cfg,
    _get,
    _observer,
    _record,
    _serve,
    _write,
)
from .test_history import DAY as HDAY
from .test_history import OPEN, _observed, _run, _timed_store, _trade, _trade_rows
from .test_server import doc, request

PAST = DAY - timedelta(days=3)
NOT_RECORDED = contract.QUOTE_AGE_NOT_RECORDED


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path, "[]")


@pytest.fixture
def api(cfg):
    server = _serve(cfg)
    yield server
    server.shutdown()
    server.server_close()


def check(measured, operator, threshold, rule="r"):
    return Check(rule, None if measured is None else Decimal(measured), operator,
                 None if threshold is None else Decimal(threshold), False)


# ---------------------------------------------------------------- closeness


@pytest.mark.parametrize(("measured", "operator", "threshold", "expected"), [
    ("12.33", ">", "12.35", "0.9983"),  # rises to the threshold: measured / threshold
    ("1.2", ">=", "1.5", "0.8000"),
    ("0", ">=", "1.5", "0.0000"),
    ("5", ">", "5", "1.0000"),  # at the threshold of a strict check
    ("0.0031", "<=", "0.002", "0.6451"),  # falls to the threshold: threshold / measured
    ("4", "<", "1", "0.2500"),
    ("2", "<", "2", "1.0000"),
])
def test_closeness_in_both_directions_rounds_down(measured, operator, threshold, expected):
    assert closeness(check(measured, operator, threshold), 4) == (Decimal(expected), None)


@pytest.mark.parametrize(("decisive", "cause"), [
    (None, "every recorded check passed"),
    (FLAG, "bars_consistent: a yes/no check has no measured value"),
    (check("-1", ">=", "2"), "r: needs threshold > 0 and measured >= 0"),
    (check("1", ">", "0"), "r: needs threshold > 0 and measured >= 0"),
    (check("0", "<=", "1"), "r: needs measured > 0 and threshold >= 0"),
    (check("1", "<", "-1"), "r: needs measured > 0 and threshold >= 0"),
    (check("1", "==", "2"), "r: operator '==' has no direction"),
    (check("3", ">", "2"), "r: recorded as failed but measured meets the threshold"),
    (check("1", "<=", "2"), "r: recorded as failed but measured meets the threshold"),
])
def test_closeness_is_null_with_a_cause_when_it_cannot_be_computed(decisive, cause):
    assert closeness(decisive, 4) == (None, cause)


# ---------------------------------------------------------------- GET /watching


def _watch(symbol, minute, *checks, verdict=Verdict.WATCH, **kw):
    return _record(symbol=symbol, minute=minute, checks=checks, verdict=verdict, **kw)


def test_watching_latest_per_symbol_closest_first_without_enters_or_observers(api, cfg):
    near = check("12.33", ">", "12.35", "ask_above_trigger")
    _write(DecisionLog.from_config(cfg),
           _watch("ABCD", 0, PASS, check("1", ">=", "2")),
           _watch("ABCD", 4, PASS, near),  # latest ABCD
           _watch("WXYZ", 1, FAIL, verdict=Verdict.IGNORE),
           _watch("FLAG", 2, FLAG, verdict=Verdict.IGNORE),
           _watch("PASS", 3, PASS),  # a WATCH without a quote: nothing failed
           _watch("GONE", 1, near),
           _watch("GONE", 5, PASS, verdict=Verdict.ENTER, admission=Admission.TAKEN),
           _watch("HELD", 6, PASS, verdict=Verdict.HOLD),
           _observer(symbol="SHADOW", minute=7, checks=(check("9", ">", "10"),)))
    body = _get(api, "/watching", "watching-v1")
    assert body["day"] == DAY.isoformat() and body["total"] == 4
    assert [(r["symbol"], r["verdict"], r["closeness"], r["closeness_cause"])
            for r in body["rows"]] == [
        ("ABCD", "WATCH", "0.9983", None),
        ("WXYZ", "IGNORE", "0.6451", None),
        ("FLAG", "IGNORE", None, "bars_consistent: a yes/no check has no measured value"),
        ("PASS", "WATCH", None, "every recorded check passed")]
    first = body["rows"][0]
    assert first["decisive_check"] == {"rule": "ask_above_trigger", "measured": "12.33",
                                       "operator": ">", "threshold": "12.35", "passed": False,
                                       "note": ""}
    assert first["at"] == "2026-11-16T15:04:00.000Z"
    assert first["strategy"] == {"name": ACTIVE[0], "version": ACTIVE[1]}
    assert (first["quote_age_seconds"], first["quote_age_cause"]) == (None, NOT_RECORDED)
    assert body["rows"][3]["decisive_check"] is None


def test_watching_is_capped_by_config_and_ties_go_by_symbol(cfg):
    records = tuple(_watch(s, i, check("1", ">=", "2")) for i, s in enumerate("EDCBA"))
    document = contract.watching_document(DAY, records, 3, 4, 4)
    assert document.total == 5
    assert [r.symbol for r in document.rows] == ["A", "B", "C"]
    assert cfg.api.watching_max_rows > 0


@pytest.mark.parametrize(("measured", "operator", "threshold", "shown"), [
    ("32.32228491", ">=", "40", ("32.3222", "40")),    # measured rounded down, away from passing
    ("1.49999", ">=", "1.5", ("1.4999", "1.5")),       # half-up would read 1.5 >= 1.5
    ("2.4", ">", "2.400001", ("2.4", "2.4001")),       # threshold rounded up
    ("0.0050001", "<=", "0.005", ("0.0051", "0.005")),  # measured rounded up for a falling check
    ("0.01", "<", "0.0099999", ("0.01", "0.0099")),    # threshold rounded down
])
def test_watching_decisive_check_rounds_away_from_passing(measured, operator, threshold, shown):
    records = (_watch("ABCD", 0, check(measured, operator, threshold, "rule")),)
    document = contract.watching_document(DAY, records, 3, 4, 4)
    row = document.rows[0]
    assert (str(row.decisive_check.measured), str(row.decisive_check.threshold)) == shown
    unrounded = contract.watching_document(DAY, records, 3, 4, 12).rows[0]
    assert row.closeness == unrounded.closeness  # computed from the recorded values


def test_watching_check_decimal_places_is_configured(cfg):
    assert cfg.api.watching_check_decimal_places >= 0


def test_watching_past_day_is_the_state_at_the_close_and_empty_day(api, cfg):
    log = DecisionLog.from_config(cfg)
    _write(log, _watch("ABCD", 0, FAIL, day_offset=-3),
           _watch("ABCD", 300, check("3", ">=", "4"), day_offset=-3), day=PAST)
    body = _get(api, f"/watching?date={PAST.isoformat()}", "watching-v1")
    assert [(r["symbol"], r["closeness"]) for r in body["rows"]] == [("ABCD", "0.7500")]
    empty = _get(api, "/watching?date=2026-11-10", "watching-v1")
    assert (empty["total"], empty["rows"]) == (0, [])


# ---------------------------------------------------------------- GET /flow


def _row(kind, rank, symbol, *, security="COMMON_STOCK", price="11.00", pre_close="10.00"):
    fields = {"symbol": symbol, "price": price}
    if pre_close is not None:
        fields["pre_close"] = pre_close
    return ScreenerRow(kind, rank, f"id-{symbol}", symbol, "NSQ", "USD", security, fields)


def _capture(cfg):
    """Two discovery passes, as the bot captures them (one read per screener)."""
    writer = CaptureWriter.from_config(cfg, "webull")
    first = BAR - timedelta(minutes=20)
    g, lo, act = ScreenerKind.GAINERS, ScreenerKind.LOSERS, ScreenerKind.TOP_ACTIVES
    writer.screener([_row(g, 1, "ABCD"), _row(g, 2, "WXYZ", pre_close=None)], "webull", first)
    writer.screener([_row(lo, 1, "EFGH", price="9.195")], "webull",
                    first + timedelta(seconds=1))
    writer.screener([_row(act, 1, "SPY", security="ETF"), _row(act, 2, "ABCD")], "webull",
                    first + timedelta(seconds=2))
    second = first + timedelta(minutes=5)
    writer.screener([_row(g, 1, "ABCD", price="11.24"), _row(g, 2, "SPY", security="ETF")],
                    "webull", second)
    writer.close()


def _fake_broker_day(cfg, api):
    """Engine on the fake broker: ABCD filled and its stop seen working, WXYZ filled with its
    stop sent but not yet read, EFGH never filled."""
    settings = api.settings
    store = Store.open(settings.history.database, busy_timeout_seconds=1, now=lambda: BAR)
    run = store.start_run(environment=cfg.environment_name, config_digest="d", strategy="s",
                          strategy_version="1", code_version="c")
    clock = [0.0]
    broker = FakeBroker()
    engine = Engine(store, broker, replace(cfg, risk=replace(cfg.risk, max_open_positions=3)),
                    run, lambda: GateResult(True, "t"), clock=lambda: clock[0])
    engine.recover()
    poll = float(cfg.orders.detail_poll_seconds)

    def opened(symbol):
        trade = engine.open_trade(symbol, Decimal(2), Decimal(10), Decimal("9.80"),
                                  Decimal("0.40"))
        return trade, engine.orders(trade)[0].client_order_id

    verified, entry = opened("ABCD")
    broker.fill(entry, Decimal(2), Decimal(10))
    for _ in range(2):  # fill observed and stop sent; then the stop read working
        clock[0] += poll
        engine.step()
    opened("EFGH")  # rests unfilled
    pending, entry = opened("WXYZ")
    broker.fill(entry, Decimal(2), Decimal(10))
    while not engine.orders(pending)[0].filled_quantity:  # Detail reads have a per-step budget
        clock[0] += poll
        engine.step()  # in the step that observes the fill, the stop is sent; never read
    assert len(engine.orders(pending)) == 2
    store.close()
    return verified, pending


def test_flow_stages_on_a_fake_broker_day(api, cfg):
    _capture(cfg)
    _write(DecisionLog.from_config(cfg),
           _watch("ABCD", 0, PASS, verdict=Verdict.ENTER, admission=Admission.TAKEN),
           _watch("WXYZ", 1, PASS, verdict=Verdict.ENTER, admission=Admission.TAKEN),
           _watch("EFGH", 2, PASS, verdict=Verdict.ENTER, admission=Admission.REFUSED,
                  cause="POSITION_LIMIT_REACHED: 1 open position, limit 1"),
           _watch("SPY", 3, FAIL),
           _observer(symbol="ZZZ", minute=4))
    verified, pending = _fake_broker_day(cfg, api)
    contract.write_status(api.settings.status_path, status(written_at=BAR))
    contract.write_account(api.settings.account_path, account(read_at=BAR))

    body = _get(api, "/flow", "flow-v1")
    assert body["day"] == DAY.isoformat() and body["environment"] == cfg.environment_name
    assert body["chips"] == [
        {"symbol": "ABCD", "sources": ["GAINERS", "TOP_ACTIVES"], "change_pct": "12.40",
         "change_cause": None, "excluded_cause": None},
        {"symbol": "SPY", "sources": ["GAINERS", "TOP_ACTIVES"], "change_pct": "10.00",
         "change_cause": None,
         "excluded_cause": "SECURITY_TYPE_NOT_ALLOWED: ETF"},
        {"symbol": "WXYZ", "sources": ["GAINERS"], "change_pct": None,
         "change_cause": "the captured row has no pre_close", "excluded_cause": None},
        {"symbol": "EFGH", "sources": ["LOSERS"], "change_pct": "-8.05",
         "change_cause": None, "excluded_cause": None}]
    stages = {s["stage"]: (s["count"], s["symbols"]) for s in body["stages"]}
    assert [s["stage"] for s in body["stages"]] == [s.value for s in contract.FlowStage]
    assert stages == {
        "screeners": (4, ["ABCD", "EFGH", "SPY", "WXYZ"]),
        "universe": (3, ["ABCD", "EFGH", "WXYZ"]),
        "strategy": (4, ["ABCD", "EFGH", "SPY", "WXYZ"]),
        "risk": (2, ["ABCD", "WXYZ"]),
        "order": (2, ["ABCD", "WXYZ"]),
        "stop": (1, ["ABCD"])}
    assert body["stops"] == [
        {"trade_id": verified, "symbol": "ABCD", "state": "verified"},
        {"trade_id": pending, "symbol": "WXYZ", "state": "pending"}]
    assert body["reconcile"] == {
        "agreement": "match", "agreement_cause": None, "read_at": "2026-11-16T15:00:00.000Z",
        "mismatch": None, "fences_cause": None,
        "fences": [{"kind": "foreign-position:EFGH", "since": "2026-11-16T15:00:00.123Z",
                    "text": "position not opened by this bot"}]}


def test_flow_reconcile_mismatch_and_missing_or_old_files(api, cfg):
    reconcile = _get(api, "/flow", "flow-v1")["reconcile"]
    assert reconcile == {"agreement": None,
                         "agreement_cause": "the bot has not written an account file",
                         "read_at": None, "mismatch": None, "fences": None,
                         "fences_cause": "the bot has not written a status file"}
    contract.write_account(api.settings.account_path, account(
        read_at=BAR, matches_records=False, mismatch="foreign-position:EFGH: not ours"))
    contract.write_status(api.settings.status_path,
                          status(written_at=BAR - timedelta(days=1), fences=()))
    reconcile = _get(api, "/flow", "flow-v1")["reconcile"]
    assert (reconcile["agreement"], reconcile["mismatch"]) == (
        "mismatch", "foreign-position:EFGH: not ours")
    assert (reconcile["fences"], reconcile["fences_cause"]) == (
        None, "the latest status was written on 2026-11-15")


def test_flow_empty_and_past_day(api, cfg):
    contract.write_account(api.settings.account_path, account(read_at=BAR))
    body = _get(api, f"/flow?date={PAST.isoformat()}", "flow-v1")
    assert (body["chips"], body["stops"]) == ([], [])
    assert all(s["count"] == 0 and s["symbols"] == [] for s in body["stages"])
    assert body["reconcile"]["agreement"] is None
    assert body["reconcile"]["agreement_cause"].startswith("only the latest reconciliation")
    today = _get(api, "/flow", "flow-v1")
    assert today["chips"] == [] and today["reconcile"]["agreement"] == "match"


def test_flow_reads_a_half_written_last_line_later_and_refuses_a_malformed_one(api, cfg):
    _capture(cfg)
    path = (api.settings.history.capture / DAY.isoformat() / "screener.jsonl")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"kind": "GAINERS"')
    assert len(_get(api, "/flow", "flow-v1")["chips"]) == 4
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    resp, payload = request(api, "GET", "/flow")
    assert (resp.status, doc(payload)["error"]) == (503, "unavailable")
    assert "capture file unreadable" in doc(payload)["cause"]


@pytest.mark.parametrize("path", [
    "/flow?date=16.11.2026", "/flow?date=", "/flow?symbol=ABCD",
    "/watching?date=2026-11-16&date=2026-11-16", "/watching?symbol=ABCD"])
def test_bad_parameters(api, path):
    resp, payload = request(api, "GET", path)
    assert (resp.status, doc(payload)["error"]) == (400, "bad_request")


@pytest.mark.parametrize("path", ["/flow", "/watching"])
def test_read_only(api, cfg, path):
    resp, _ = request(api, "POST", path, {})
    assert resp.status == 405
    _write(DecisionLog.from_config(cfg), _record(),
           tail='{"role": "active", "symbol": "ABCD", broken\n')
    resp, payload = request(api, "GET", path)
    assert (resp.status, doc(payload)["error"]) == (503, "unavailable")


# ---------------------------------------------------------------- stop state per trade


def _entries_day(tmp_path):
    """Entries filled (read at 10:00:30 New York); only `seen` has its stop read working.
    `before` opened 23:59:59 New York the evening before, `after` the next morning."""
    paths = HistoryPaths("sandbox", tmp_path / "db.sqlite3", tmp_path / "capture")
    store, clock = _timed_store(paths)
    run = _run(store)
    for trade_id, closed in (("seen", None), ("open", None),
                             ("closed", OPEN + timedelta(minutes=9))):
        _trade(store, run, trade_id, closed, exit_=None if closed is None else "12.30")
        _observed(store, clock, f"{trade_id}-entry", 30, "FILLED", "10")
    _observed(store, clock, "seen-stop", 33, "ACCEPTED", "0")
    with store.transaction():
        for trade_id, opened in (("before", OPEN.replace(hour=4, minute=59, second=59)),
                                 ("after", OPEN.replace(hour=5) + timedelta(days=1))):
            _trade_rows(store, run, trade_id, None, "12.34", None, "11.90", "10")
            store.execute("UPDATE trades SET opened_at = ? WHERE trade_id = ?",
                          (opened.isoformat(), trade_id))
    store.close()
    return History(paths)


def _states(history, day, today):
    return [(s.trade_id, s.state.value) for s in history.entries(day, today)]


def test_stop_is_pending_only_for_an_open_trade_today(tmp_path):
    history = _entries_day(tmp_path)
    assert _states(history, HDAY, HDAY) == [  # same opened_at: by trade_id
        ("closed", "never_verified"), ("open", "pending"), ("seen", "verified")]


def test_stop_never_seen_on_a_past_day_is_never_verified(tmp_path):
    history = _entries_day(tmp_path)
    later = HDAY + timedelta(days=1)
    assert _states(history, HDAY, later) == [
        ("closed", "never_verified"), ("open", "never_verified"), ("seen", "verified")]
    assert _states(history, later, later) == [("after", "pending")]
    assert _states(history, HDAY - timedelta(days=1), later) == [("before", "never_verified")]
