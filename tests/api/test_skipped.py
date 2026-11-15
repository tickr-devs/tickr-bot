"""GET /skipped and the observers' simulated day result at GET /observers, read-only."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from aperture import config
from aperture.api import contract
from aperture.api.observer_results import ObserverResults, SimulationPaths
from aperture.api.server import settings_from_config
from aperture.archive import compress
from aperture.calendar import SessionCalendar
from aperture.data.capture import BARS_FILE, read_captured_bars
from aperture.observers.log import DecisionLog
from aperture.replay.clock import calendar_clock
from aperture.replay.settings import load_settings
from aperture.research import observer_report
from aperture.research.records import Admission, DecisionRecord
from aperture.strategy.base import Decision, Role, Verdict

from .test_decisions import (
    ACTIVE,
    BAR,
    DAY,
    FAIL,
    M1,
    OBSERVER,
    PASS,
    ROOT,
    _get,
    _observer,
    _record,
    _serve,
    _write,
)
from .test_server import doc, request

PAST = DAY - timedelta(days=3)  # Friday 2026-11-13, a session
OPEN = BAR.replace(hour=14, minute=30)  # 09:30 in New York


@pytest.fixture
def cfg(tmp_path):
    """A full copy of config/ (calendar and replay settings too) with one observer."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    path = tmp_path / "config" / "bot.toml"
    text = path.read_text("utf-8")
    assert text.count("\nobservers = []\n") == 1
    path.write_text(text.replace("\nobservers = []\n", '\nobservers = ["price-action-v2"]\n'),
                    "utf-8")
    return config.load(path)


@pytest.fixture
def api(cfg):
    server = _serve(cfg)
    yield server
    server.shutdown()
    server.server_close()


def _enter(identity=OBSERVER, role=Role.OBSERVER, minute=5, quantity=Decimal(10),
           admission=Admission.NOT_EVALUATED, cause=None, day_offset=0, symbol="ABCD"):
    bar = BAR + minute * M1 + timedelta(days=day_offset)
    decision = Decision(*identity, symbol, Verdict.ENTER, (PASS,), entry_low=Decimal("12.00"),
                        entry_high=Decimal("12.10"), stop=Decimal("11.80"), quantity=quantity)
    return DecisionRecord(bar + M1, bar, role, decision, admission, cause)


def _bars_file(cfg, day):
    return (cfg.root / cfg.run.data_dir / cfg.market_data.capture_dir / cfg.environment_name
            / day.isoformat() / BARS_FILE)


def _capture(cfg, day_offset=0, symbol="ABCD", tail=""):
    """Flat at 12.00 from the open; after 10:05 New York it trades inside the entry window, then
    falls through the 11.80 stop at 10:10."""
    lines = []
    start = OPEN + timedelta(days=day_offset)
    for n in range(90):
        at = start + n * M1
        minute = n - 35  # minutes after 10:05 New York
        if minute == 1:
            o, h, lo, c = "12.05", "12.08", "12.02", "12.05"
        elif minute >= 5:
            o, h, lo, c = "11.75", "11.78", "11.70", "11.75"
        else:
            o, h, lo, c = "12.00", "12.00", "12.00", "12.00"
        lines.append(json.dumps({"symbol": symbol, "start": at.isoformat(), "open": o, "high": h,
                                 "low": lo, "close": c, "volume": "1000", "provider": "test",
                                 "receipt_time": (at + M1).isoformat()}) + "\n")
    path = _bars_file(cfg, start.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines) + tail, "utf-8")
    return path


def _research_total(cfg, records, path):
    """The research observers report on the same inputs: the number the API must serve."""
    read = read_captured_bars(path)
    bars: dict[str, list] = {}
    for (symbol, _), bar in sorted(read.bars.items()):
        bars.setdefault(symbol, []).append(bar)
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    report = observer_report.report(records, bars, load_settings(
        cfg.root / cfg.api.replay_settings_file), calendar_clock(calendar, cfg.session))
    (row,) = report.identities
    assert row.trades == 1 and row.usd_trades == 1
    return row.total_usd


# ---------------------------------------------------------------- GET /skipped


def test_skipped_lists_refused_and_not_evaluated_enters_only(api, cfg):
    refused = _enter(ACTIVE, Role.ACTIVE, minute=7, admission=Admission.REFUSED,
                     cause="POSITION_LIMIT_REACHED: 1 open position, limit 1")
    late = _enter(ACTIVE, Role.ACTIVE, minute=2, admission=Admission.NOT_EVALUATED,
                  symbol="WXYZ")
    taken = _enter(ACTIVE, Role.ACTIVE, minute=1, admission=Admission.TAKEN)
    watch = _record(minute=3, checks=(PASS, FAIL))
    shadow = _enter(minute=4)  # an observer ENTER is never a skipped signal of the bot
    _write(DecisionLog.from_config(cfg), refused, late, taken, watch, shadow)
    body = _get(api, "/skipped", "skipped-v1")
    assert body["day"] == DAY.isoformat() and body["api_version"] == 1
    assert body["signals"] == [
        {"at": "2026-11-16T15:02:00.000Z", "symbol": "WXYZ",
         "strategy": {"name": "price-action-v1", "version": "1"},
         "admission": "not_evaluated", "refusal_cause": None,
         "entry_price": "12.10", "stop_price": "11.80",
         "checks": [{"rule": "relative_volume", "measured": "2.4", "operator": ">=",
                     "threshold": "1.5", "passed": True, "note": ""}]},
        {"at": "2026-11-16T15:07:00.000Z", "symbol": "ABCD",
         "strategy": {"name": "price-action-v1", "version": "1"},
         "admission": "refused",
         "refusal_cause": "POSITION_LIMIT_REACHED: 1 open position, limit 1",
         "entry_price": "12.10", "stop_price": "11.80",
         "checks": [{"rule": "relative_volume", "measured": "2.4", "operator": ">=",
                     "threshold": "1.5", "passed": True, "note": ""}]},
    ]
    # /decisions carries the same admission and cause
    decisions = _get(api, "/decisions?symbol=ABCD&date=2026-11-16", "decisions-v1")
    assert [(d["at"][11:16], d["admission"], d["refusal_cause"])
            for d in decisions["decisions"]] == [
        ("15:01", "taken", None), ("15:03", "not_applicable", None),
        ("15:04", "not_evaluated", None),
        ("15:07", "refused", "POSITION_LIMIT_REACHED: 1 open position, limit 1")]


def test_skipped_archived_day_and_empty_day(api, cfg):
    log = DecisionLog.from_config(cfg)
    _write(log, _enter(ACTIVE, Role.ACTIVE, admission=Admission.REFUSED,
                       cause="SYMBOL_COOLDOWN: ABCD", day_offset=-3), day=PAST)
    compress(log.path(PAST))
    assert not log.path(PAST).exists()
    body = _get(api, f"/skipped?date={PAST.isoformat()}", "skipped-v1")
    assert [(s["symbol"], s["refusal_cause"]) for s in body["signals"]] == [
        ("ABCD", "SYMBOL_COOLDOWN: ABCD")]
    assert _get(api, "/skipped?date=2026-11-10", "skipped-v1")["signals"] == []


@pytest.mark.parametrize("path", [
    "/skipped?date=2026-11-16&date=2026-11-16", "/skipped?date=16.11.2026", "/skipped?date=",
    "/skipped?symbol=ABCD"])
def test_skipped_bad_parameters(api, path):
    resp, payload = request(api, "GET", path)
    assert (resp.status, doc(payload)["error"]) == (400, "bad_request")


def test_skipped_malformed_active_line_is_unavailable(api, cfg):
    _write(DecisionLog.from_config(cfg), _record(),
           tail='{"role": "active", "symbol": "ABCD", broken\n')
    resp, payload = request(api, "GET", "/skipped")
    assert (resp.status, doc(payload)["error"]) == (503, "unavailable")
    resp, _ = request(api, "POST", "/skipped", {})
    assert resp.status == 405


# ---------------------------------------------------------------- observer result


def _result(api, day=None):
    path = "/observers" + (f"?date={day.isoformat()}" if day else "")
    (entry,) = _get(api, path, "observers-v1")["observers"]
    return entry["result_gross_usd"], entry["result_cause"]


def test_observer_result_is_the_research_report_simulation(api, cfg):
    records = (_enter(), _observer(minute=8, checks=(PASS, FAIL)))
    _write(DecisionLog.from_config(cfg), *records)
    bars = _capture(cfg)
    expected = _research_total(cfg, records, bars)
    assert expected < 0  # filled in the window, then stopped out
    places = load_settings(cfg.root / cfg.api.replay_settings_file).output_decimal_places
    assert _result(api) == (str(expected.quantize(Decimal(1).scaleb(-places))), None)


def test_observer_result_of_an_archived_day(api, cfg):
    log = DecisionLog.from_config(cfg)
    records = (_enter(day_offset=-3),)
    _write(log, *records, day=PAST)
    bars = _capture(cfg, day_offset=-3)
    expected = _research_total(cfg, records, bars)
    compress(log.path(PAST))
    compress(bars)
    gross, cause = _result(api, PAST)
    assert cause is None and Decimal(gross) == expected.quantize(Decimal(gross))


def test_observer_without_enter_simulates_no_trade(api, cfg):
    _write(DecisionLog.from_config(cfg), _observer(checks=(PASS, FAIL)))
    assert _result(api) == ("0", None)  # nothing to trade: no capture file is needed


@pytest.mark.parametrize("case, cause", [
    ("no capture", "no captured bars for 2026-11-16"),
    ("refused capture", "capture file refused: "),
    ("no quantity", "1 simulated trade(s) have no recorded whole quantity"),
    ("other symbol", "1 ENTER(s) could not be simulated"),
    ("no replay settings", "replay settings unreadable: "),
    ("no decisions", contract.NO_DECISIONS),
])
def test_observer_result_null_with_cause(api, cfg, case, cause):
    log = DecisionLog.from_config(cfg)
    if case != "no decisions":
        _write(log, _enter(quantity=None if case == "no quantity" else Decimal(10)))
    if case not in ("no capture", "no decisions"):
        _capture(cfg, symbol="WXYZ" if case == "other symbol" else "ABCD",
                 tail="not json\n" if case == "refused capture" else "")
    if case == "no replay settings":
        (cfg.root / cfg.api.replay_settings_file).unlink()
    gross, got = _result(api)
    assert gross is None and got.startswith(cause), got


def test_observer_result_is_recomputed_only_when_its_files_change(cfg):
    log = DecisionLog.from_config(cfg)
    _write(log, _enter())
    results = ObserverResults(SimulationPaths.from_config(cfg))
    records = log.records(DAY, role=Role.OBSERVER)
    first = results.results(DAY, DAY, records, log.path(DAY))
    assert first[OBSERVER][0] is None  # no capture file yet
    assert results.results(DAY, DAY, records, log.path(DAY)) is first  # same files: memo
    _capture(cfg)
    second = results.results(DAY, DAY, records, log.path(DAY))
    assert second[OBSERVER][0] is not None and second[OBSERVER][1] is None


def test_changed_replay_settings_trigger_a_recompute(cfg):
    log = DecisionLog.from_config(cfg)
    _write(log, _enter())
    _capture(cfg)
    results = ObserverResults(SimulationPaths.from_config(cfg))
    records = log.records(DAY, role=Role.OBSERVER)
    first = results.results(DAY, DAY, records, log.path(DAY))
    assert results.results(DAY, DAY, records, log.path(DAY)) is first
    settings = cfg.root / cfg.api.replay_settings_file
    text = settings.read_text("utf-8")
    assert text.count("slippage_min_usd = 0.01\n") == 1
    settings.write_text(text.replace("slippage_min_usd = 0.01\n", "slippage_min_usd = 0.05\n"),
                        "utf-8")
    second = results.results(DAY, DAY, records, log.path(DAY))
    assert second is not first
    assert second[OBSERVER][0] < first[OBSERVER][0]  # more slippage, a worse result
    # the same day seen as a past day is a different key too
    assert results.results(DAY, DAY + timedelta(days=1), records, log.path(DAY)) is not second


def test_transient_failure_is_not_remembered(cfg):
    log = DecisionLog.from_config(cfg)
    _write(log, _enter())
    _capture(cfg)
    paths = SimulationPaths.from_config(cfg)
    results = ObserverResults(replace(paths, calendar=paths.calendar.parent / "missing"))
    records = log.records(DAY, role=Role.OBSERVER)
    failed = results.results(DAY, DAY, records, log.path(DAY))
    assert failed[OBSERVER][1].startswith("session calendar unreadable: ")
    results.paths = paths  # the calendar is back, no data file changed
    assert results.results(DAY, DAY, records, log.path(DAY))[OBSERVER][1] is None


def test_a_recorded_observer_without_result_is_refused():
    record = _enter()
    with pytest.raises(contract.ContractError, match="no simulated result"):
        contract.observers_document(DAY, (OBSERVER,), (record,), {})


def test_observer_result_never_writes(api, cfg):
    _write(DecisionLog.from_config(cfg), _enter())
    _capture(cfg)
    data = cfg.root / cfg.run.data_dir
    before = sorted(p.relative_to(data) for p in data.rglob("*"))
    _result(api)
    assert sorted(p.relative_to(data) for p in data.rglob("*")) == before


def test_settings_carry_the_simulation_inputs(cfg):
    paths = replace(settings_from_config(cfg), port=0).simulation
    assert paths.replay_settings == cfg.root / "config" / "replay.toml"
    assert paths.replay_settings.is_file() and paths.calendar.is_dir()
