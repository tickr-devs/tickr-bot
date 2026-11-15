"""GET /observers and GET /decisions: recorded strategy decisions, read-only."""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aperture import config
from aperture.api import contract
from aperture.api.server import make_server, settings_from_config
from aperture.archive import compress
from aperture.observers.log import DecisionLog
from aperture.research.records import Admission, DecisionRecord
from aperture.strategy.base import Check, Decision, Role, Verdict

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]
BAR = T0.replace(microsecond=0)
DAY = BAR.date()  # 10:00 in New York: the same date
M1 = timedelta(minutes=1)
PASS = Check("relative_volume", Decimal("2.4"), ">=", Decimal("1.5"), True)
FAIL = Check("spread_ratio", Decimal("0.0031"), "<=", Decimal("0.002"), False)
FLAG = Check("bars_consistent", None, "is", None, False, "own symbol, increasing start")
OBSERVER = ("price-action-v2", "2")  # what strategies/price-action-v2/strategy.toml declares
ACTIVE = ("price-action-v1", "1")


def _record(identity=ACTIVE, symbol="ABCD", minute=0, verdict=Verdict.WATCH,
            checks=(PASS, FAIL), role=Role.ACTIVE, admission=Admission.NOT_APPLICABLE,
            cause=None, entry_high=None, stop=None, day_offset=0):
    bar = BAR + minute * M1 + timedelta(days=day_offset)
    decision = Decision(*identity, symbol, verdict, checks, entry_low=entry_high,
                        entry_high=entry_high, stop=stop)
    return DecisionRecord(bar + M1 + timedelta(seconds=minute), bar, role, decision, admission,
                          cause)


def _observer(**kw):
    return _record(OBSERVER, role=Role.OBSERVER, **kw)


def _write(log: DecisionLog, *records: DecisionRecord, tail: str = "", day=DAY) -> None:
    for record in records:
        log.append(record)
    log.close()
    if tail:
        with open(log.path(day), "a", encoding="utf-8") as handle:
            handle.write(tail)


def _cfg(tmp_path, observers: str):
    (tmp_path / "config").mkdir()
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    assert text.count("\nobservers = []\n") == 1
    path = tmp_path / "config" / "bot.toml"
    path.write_text(text.replace("\nobservers = []\n", f"\nobservers = {observers}\n"), "utf-8")
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    return config.load(path)


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path, '["price-action-v2"]')


def _serve(cfg):
    server = make_server(replace(settings_from_config(cfg), port=0), Clock(T0 + 30 * M1))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    return server


@pytest.fixture
def api(cfg):
    server = _serve(cfg)
    yield server
    server.shutdown()
    server.server_close()


def _get(api, path, schema):
    resp, payload = request(api, "GET", path)
    body = doc(payload)
    assert resp.status == 200, body
    assert errors(contract.json_schema(contract.SCHEMA_TYPES[schema]), body) == []
    return body


# ---------------------------------------------------------------- reading the decision file


def test_records_filters_by_symbol_and_role(cfg):
    log = DecisionLog.from_config(cfg)
    assert log.records(DAY) == ()  # no file yet
    a, o, other = _record(), _observer(minute=1), _record(symbol="WXYZ", minute=2)
    _write(log, a, o, other, tail='{"half a line')
    assert log.records(DAY) == (a, o, other)
    assert log.records(DAY, symbol="ABCD") == (a, o)
    assert log.records(DAY, role=Role.OBSERVER) == (o,)
    assert log.records(DAY, symbol="WXYZ", role=Role.OBSERVER) == ()


def test_action_is_the_explain_verdict_enum():
    explain = contract.json_schema(contract.ExplainV1)["properties"]["verdict"]
    item = contract.json_schema(contract.DecisionsV1)["properties"]["decisions"]["items"]
    observed = (contract.json_schema(contract.ObserversV1)["properties"]["observers"]["items"]
                ["properties"]["decisions"]["items"])
    assert explain == {"type": "string", "enum": [v.value for v in Verdict]}
    assert item["properties"]["action"] == explain == observed["properties"]["action"]


# ---------------------------------------------------------------- GET /observers


def test_observers_today(api, cfg):
    log = DecisionLog.from_config(cfg)
    late = _observer(minute=5, verdict=Verdict.ENTER, checks=(PASS,),
                     admission=Admission.NOT_EVALUATED, entry_high=Decimal("12.40"),
                     stop=Decimal("12.10"))
    early = _observer(symbol="WXYZ", minute=1, checks=(FLAG, PASS, FAIL))
    unconfigured = _record(("price-action-v1", "9"), role=Role.OBSERVER, minute=2)
    _write(log, _record(minute=3), late, early, unconfigured)
    body = _get(api, "/observers", "observers-v1")
    assert body["day"] == DAY.isoformat() and body["api_version"] == 1
    assert body["observers"] == [{
        "name": "price-action-v2", "version": "2", "result_gross_usd": None,
        "decisions": [
            {"at": "2026-11-16T15:01:00.000Z", "symbol": "WXYZ", "action": "WATCH",
             "reasons": ["bars_consistent", "spread_ratio"]},
            {"at": "2026-11-16T15:05:00.000Z", "symbol": "ABCD", "action": "ENTER",
             "reasons": []},
        ]}]


def test_observer_without_decisions_and_no_observers(api, cfg, tmp_path_factory):
    _write(DecisionLog.from_config(cfg), _record())
    body = _get(api, "/observers?date=2026-11-16", "observers-v1")
    assert [(o["name"], o["version"], o["decisions"]) for o in body["observers"]] == [
        ("price-action-v2", "2", [])]  # the current strategy.toml version
    none = _serve(_cfg(tmp_path_factory.mktemp("bare"), "[]"))
    try:
        assert _get(none, "/observers", "observers-v1")["observers"] == []
    finally:
        none.shutdown()
        none.server_close()


def test_observer_versions_recorded_that_day(api, cfg):
    """A bump from 1 to 2 (and a later 10) mid-day: each recorded version keeps its records,
    in version order, even the ones strategy.toml no longer declares."""
    old = ("price-action-v2", "1")
    _write(DecisionLog.from_config(cfg),
           _record(("price-action-v2", "10"), role=Role.OBSERVER, minute=6),
           _record(old, role=Role.OBSERVER, minute=0),
           _observer(minute=4, verdict=Verdict.ENTER, checks=(PASS,),
                     admission=Admission.NOT_EVALUATED),
           _record(old, role=Role.OBSERVER, symbol="WXYZ", minute=2))
    body = _get(api, "/observers", "observers-v1")
    assert [(o["name"], o["version"], [(d["at"][11:16], d["symbol"]) for d in o["decisions"]])
            for o in body["observers"]] == [
        ("price-action-v2", "1", [("15:00", "ABCD"), ("15:02", "WXYZ")]),
        ("price-action-v2", "2", [("15:04", "ABCD")]),
        ("price-action-v2", "10", [("15:06", "ABCD")]),
    ]


def test_observers_archived_day(api, cfg):
    log = DecisionLog.from_config(cfg)
    past = DAY - timedelta(days=3)
    _write(log, _observer(day_offset=-3), _record(day_offset=-3))
    compress(log.path(past))
    assert not log.path(past).exists()
    body = _get(api, f"/observers?date={past.isoformat()}", "observers-v1")
    assert body["day"] == past.isoformat()
    assert [d["action"] for d in body["observers"][0]["decisions"]] == ["WATCH"]


# ---------------------------------------------------------------- GET /decisions


def test_decisions_of_a_symbol(api, cfg):
    log = DecisionLog.from_config(cfg)
    refused = _record(minute=2, verdict=Verdict.ENTER, checks=(PASS,),
                      admission=Admission.REFUSED, cause="RISK: limit",
                      entry_high=Decimal("12.40"), stop=Decimal("12.10"))
    taken = _record(minute=4, verdict=Verdict.ENTER, checks=(PASS,),
                    admission=Admission.TAKEN, entry_high=Decimal("12.50"),
                    stop=Decimal("12.20"))
    shadow = _observer(minute=2, verdict=Verdict.ENTER, checks=(PASS,),
                       admission=Admission.NOT_EVALUATED, stop=Decimal("12.00"))
    _write(log, taken, _record(minute=1, checks=(FLAG, PASS)), refused, shadow,
           _record(symbol="ABC", minute=0))
    body = _get(api, "/decisions?symbol=ABCD&date=2026-11-16", "decisions-v1")
    assert (body["symbol"], body["day"]) == ("ABCD", "2026-11-16")
    got = [(d["at"][11:16], d["action"], d["strategy"]["name"], d["observer"],
            d["entry_price"], d["stop_price"]) for d in body["decisions"]]
    assert got == [
        ("15:01", "WATCH", "price-action-v1", False, None, None),
        ("15:02", "ENTER", "price-action-v1", False, "12.40", "12.10"),
        ("15:02", "ENTER", "price-action-v2", True, None, "12.00"),
        ("15:04", "ENTER", "price-action-v1", False, None, None),  # taken: not a what-if
    ]
    assert body["decisions"][0]["checks"] == [
        {"rule": "bars_consistent", "measured": None, "operator": "is", "threshold": None,
         "passed": False, "note": "own symbol, increasing start"},
        {"rule": "relative_volume", "measured": "2.4", "operator": ">=", "threshold": "1.5",
         "passed": True, "note": ""}]
    assert body["decisions"][1]["strategy"] == {"name": "price-action-v1", "version": "1"}


def test_decisions_archived_day_and_empty_day(api, cfg):
    log = DecisionLog.from_config(cfg)
    past = DAY - timedelta(days=1)
    _write(log, _record(day_offset=-1), _observer(minute=1, day_offset=-1))
    compress(log.path(past))
    body = _get(api, f"/decisions?symbol=ABCD&date={past.isoformat()}", "decisions-v1")
    assert [d["observer"] for d in body["decisions"]] == [False, True]
    assert _get(api, "/decisions?symbol=ABCD&date=2026-11-10", "decisions-v1")[
        "decisions"] == []


@pytest.mark.parametrize("path", [
    "/observers?date=2026-11-16&date=2026-11-16",
    "/observers?date=16.11.2026",
    "/observers?date=",
    "/observers?symbol=ABCD",
    "/decisions?symbol=ABCD",
    "/decisions?date=2026-11-16",
    "/decisions?symbol=abcd&date=2026-11-16",
    "/decisions?symbol=ABCD&symbol=ABCD&date=2026-11-16",
    "/decisions?symbol=ABCD&date=2026-13-01",
    "/decisions?symbol=ABCD&date=2026-11-16&role=observer",
])
def test_bad_parameters(api, path):
    resp, payload = request(api, "GET", path)
    assert (resp.status, doc(payload)["error"]) == (400, "bad_request")


def test_malformed_records_are_unavailable(api, cfg):
    log = DecisionLog.from_config(cfg)
    _write(log, _record(), _observer(minute=1),
           tail='{"role": "observer", "symbol": "ABCD", broken\n')
    for path in ("/observers", "/decisions?symbol=ABCD&date=2026-11-16"):
        resp, payload = request(api, "GET", path)
        body = doc(payload)
        assert (resp.status, body["error"]) == (503, "unavailable")
        assert "decisions.jsonl:3:" in body["cause"]
    # another symbol's decisions do not depend on that line
    _get(api, "/decisions?symbol=WXYZ&date=2026-11-16", "decisions-v1")


def test_unreadable_observer_folder_is_unavailable(api, cfg):
    (cfg.root / "strategies" / "price-action-v2" / "strategy.toml").write_text(
        "[strategy]\nname = 'price-action-v2'\n", "utf-8")
    resp, payload = request(api, "GET", "/observers")
    body = doc(payload)
    assert (resp.status, body["error"]) == (503, "unavailable")
    assert "version" in body["cause"]
    resp, _ = request(api, "POST", "/observers", {})
    assert resp.status == 405
