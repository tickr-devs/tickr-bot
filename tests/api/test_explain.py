"""`tickr explain SYMBOL` and GET /explain: the latest active decision recorded today."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import cli, config
from tickr.api import contract
from tickr.api.server import make_server, settings_from_config
from tickr.observers.log import DecisionLog
from tickr.research.errors import RecordFileError
from tickr.research.records import Admission, DecisionRecord
from tickr.strategy.base import Check, Decision, Role, Verdict

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]
BAR = T0.replace(microsecond=0)
DAY = BAR.date()  # 10:00 in New York: the same date
M1 = timedelta(minutes=1)
PASS = Check("relative_volume", Decimal("2.4"), ">=", Decimal("1.5"), True)
FAIL = Check("spread_ratio", Decimal("0.0031"), "<=", Decimal("0.002"), False)
FLAG = Check("bars_consistent", None, "is", None, True, "own symbol, increasing start")


def _record(symbol="ABCD", minute=0, verdict=Verdict.WATCH, checks=(FLAG, PASS, FAIL),
            role=Role.ACTIVE, admission=Admission.NOT_APPLICABLE, cause=None):
    bar = BAR + minute * M1
    decision = Decision("price_action_v1", "1", symbol, verdict, checks, setup="breakout")
    return DecisionRecord(bar + M1, bar, role, decision, admission, cause)


def _write(log: DecisionLog, *records: DecisionRecord, tail: str = "") -> None:
    for record in records:
        log.append(record)
    log.close()
    if tail:
        with open(log.path(DAY), "a", encoding="utf-8") as handle:
            handle.write(tail)


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "bot.toml"
    path.write_text((ROOT / "config" / "bot.toml").read_text("utf-8"), "utf-8")
    return config.load(path)


# ---------------------------------------------------------------- reading the decision file


def test_latest_active_record_of_the_symbol(cfg):
    log = DecisionLog.from_config(cfg)
    assert log.latest_active(DAY, "ABCD") is None  # no file yet
    entered = _record(minute=1, verdict=Verdict.ENTER, checks=(PASS,),
                      admission=Admission.REFUSED, cause="QUOTE_STALE: quote 7 s old > 5 s")
    _write(log, _record(), entered, _record(minute=2, role=Role.OBSERVER),
           _record("WXYZ", minute=3), tail='{"half a line')
    assert log.latest_active(DAY, "ABCD") == entered
    assert log.latest_active(DAY, "KLMN") is None
    assert log.latest_active(DAY + timedelta(days=1), "ABCD") is None


def test_malformed_line_is_refused(cfg):
    log = DecisionLog.from_config(cfg)
    _write(log, _record(), tail='{"symbol": "ABCD", broken\n')
    with pytest.raises(RecordFileError, match=":2: "):
        log.latest_active(DAY, "ABCD")


def test_large_file_filter_keeps_results(cfg):
    """Many symbols in one day's file: only lines carrying the symbol's key are parsed, and the
    answer is the same record a full parse finds. A malformed line of another symbol is not
    read; prefix-sharing tickers (ABC vs ABCD) do not match each other."""
    log = DecisionLog.from_config(cfg)
    symbols = [f"S{i:03d}" for i in range(200)] + ["ABC"]
    records = [_record(s, minute, checks=(PASS,)) for minute in range(20) for s in symbols]
    wanted = _record("ABCD", 19, verdict=Verdict.ENTER, checks=(PASS,),
                     admission=Admission.REFUSED, cause="RISK: limit")
    _write(log, *records, _record("ABCD", 5), wanted, _record("ABCD", 19, role=Role.OBSERVER),
           tail='{"symbol": "S001", broken\n')
    assert log.latest_active(DAY, "ABCD") == wanted
    assert log.latest_active(DAY, "ABC") == records[-1]
    assert log.latest_active(DAY, "S007") == _record("S007", 19, checks=(PASS,))
    with pytest.raises(RecordFileError):
        log.latest_active(DAY, "S001")


# ---------------------------------------------------------------- GET /explain


@pytest.fixture
def api(cfg):
    server = make_server(replace(settings_from_config(cfg), port=0), Clock(T0 + 30 * M1))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_explain_route(api, cfg):
    _write(DecisionLog.from_config(cfg), _record())
    resp, payload = request(api, "GET", "/explain?symbol=ABCD")
    assert resp.status == 200
    body = doc(payload)
    assert errors(contract.json_schema(contract.ExplainV1), body) == []
    assert body["environment"] == cfg.environment_name and body["day"] == DAY.isoformat()
    assert (body["strategy"], body["version"], body["verdict"]) == ("price_action_v1", "1",
                                                                   "WATCH")
    assert body["first_failed_check"] == "spread_ratio" and body["refusal_cause"] is None
    assert body["checks"][2] == {"rule": "spread_ratio", "measured": "0.0031", "operator": "<=",
                                 "threshold": "0.002", "passed": False, "note": ""}


@pytest.mark.parametrize("path, status, error", [
    ("/explain?symbol=KLMN", 404, "not_found"),
    ("/explain", 400, "bad_request"),
    ("/explain?symbol=abc", 400, "bad_request"),
    ("/explain?symbol=ABCD&date=2026-11-16", 400, "bad_request"),
])
def test_explain_refusals(api, cfg, path, status, error):
    _write(DecisionLog.from_config(cfg), _record())
    resp, payload = request(api, "GET", path)
    assert (resp.status, doc(payload)["error"]) == (status, error)


def test_explain_unreadable_file_and_post(api, cfg):
    _write(DecisionLog.from_config(cfg), _record(), tail='{"symbol": "ABCD", broken\n')
    resp, payload = request(api, "GET", "/explain?symbol=ABCD")
    assert (resp.status, doc(payload)["error"]) == (503, "unavailable")
    resp, _ = request(api, "POST", "/explain", {})
    assert resp.status == 405


# ---------------------------------------------------------------- tickr explain


def test_cli_explain(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "NOW", lambda: T0 + 30 * M1)
    bot = str(cfg.root / "config" / "bot.toml")
    assert cli.main(["--config", bot, "explain", "ABCD"]) == 1
    assert "no decision recorded for ABCD on 2026-11-16" in capsys.readouterr().out
    _write(DecisionLog.from_config(cfg), _record(
        verdict=Verdict.ENTER, checks=(FLAG, PASS), admission=Admission.REFUSED,
        cause="QUOTE_STALE: quote 7 s old > 5 s"))
    assert cli.main(["--config", bot, "explain", "ABCD"]) == 0
    out = capsys.readouterr().out
    assert "ABCD 2026-11-16: ENTER by price_action_v1 1" in out
    assert "admission: refused (QUOTE_STALE: quote 7 s old > 5 s)" in out
    assert "  pass bars_consistent [own symbol, increasing start]" in out
    assert "  pass relative_volume: 2.4 >= 1.5" in out
    assert "first failed check: none" in out and "quote age" not in out
