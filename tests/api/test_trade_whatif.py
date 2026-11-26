"""GET /trades/whatif: another strategy version on one trade's symbol and day, simulated."""

from __future__ import annotations

import json
import shutil
import sys
import threading
import types
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.server import make_server, settings_from_config
from tickr.api.trade_whatif import TradeWhatIfRefused, TradeWhatIfSettings, what_if
from tickr.data.capture import CapturedBarsDays, bar_record
from tickr.strategy.base import Decision, Verdict
from tickr.types import Bar

from .samples import T0
from .schema_check import errors
from .test_decisions import M1, PASS, ROOT
from .test_server import Clock, doc, request
from .test_skipped import OPEN, PAST, _capture

SATURDAY = T0 + timedelta(days=5)  # T0 is 10:00 New York on Monday 2026-11-16
PRIOR = [PAST - timedelta(days=d) for d in (7, 4, 3, 2, 1)]  # the five sessions before PAST
SIGNAL = OPEN + 35 * M1 + timedelta(days=-3)  # the 10:05 New York bar of PAST
MODULE = "scripted_whatif_strategy"


class Scripted:
    """ENTER once on PAST's 10:05 bar inside 12.00-12.10, stop 11.80, 10 shares."""

    name, version = "scripted", "1"
    prior_sessions_needed = 5

    def __init__(self, values):
        self.values = values

    def evaluate(self, context):
        bar = context.bars[-1]
        if bar.start == SIGNAL and len(context.prior_sessions) == self.prior_sessions_needed:
            return Decision(self.name, self.version, context.symbol, Verdict.ENTER, (PASS,),
                            entry_low=Decimal("12.00"), entry_high=Decimal("12.10"),
                            stop=Decimal("11.80"), quantity=Decimal(10))
        return Decision(self.name, self.version, context.symbol, Verdict.IGNORE, (PASS,))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    folder = tmp_path / "strategies" / "scripted-v1"
    folder.mkdir()
    (folder / "strategy.toml").write_text('[strategy]\nname = "scripted"\nversion = "1"\n')
    module = types.ModuleType(MODULE)
    module.create = Scripted
    monkeypatch.setitem(sys.modules, MODULE, module)
    path = tmp_path / "config" / "bot.toml"
    text = path.read_text("utf-8")
    marker = 'strategy_modules = ["'
    assert text.count(marker) == 1
    path.write_text(text.replace(marker, f'{marker}scripted={MODULE}", "'), "utf-8")
    return config.load(path)


def flat_bars(symbol, day, price):
    """A full regular session flat at `price`."""
    p = Decimal(price)
    start = OPEN.replace(year=day.year, month=day.month, day=day.day)
    return [Bar(symbol, start + i * M1, p, p, p, p, Decimal(1000)) for i in range(390)]


def _settings(cfg):
    return TradeWhatIfSettings.from_config(cfg)


def _history(cfg, symbol="ABCD"):
    settings = _settings(cfg)
    path = settings.history / PAST.isoformat() / f"{symbol}-{settings.history_sessions}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    bars = [b for d in PRIOR for b in flat_bars(symbol, d, "12")]
    path.write_text("".join(json.dumps(bar_record(b)) + "\n" for b in bars))
    return path


def test_the_other_version_trades_the_day_from_capture_and_cached_history(cfg):
    _capture(cfg, day_offset=-3)
    _history(cfg)
    found = what_if(_settings(cfg), "scripted-v1", "ABCD", PAST, SATURDAY, CapturedBarsDays())
    assert found.cause is None, found.cause
    assert (found.strategy, found.version, found.folder) == ("scripted", "1", "scripted-v1")
    assert found.signals == contract.TradeWhatIfSignalsV1(1, 0, 0, 0)
    (trade,) = found.trades
    assert trade.signal_at == SIGNAL and trade.exit_reason.value == "stop"
    assert trade.result_r < 0 and trade.quantity == Decimal(10)
    assert trade.result_gross_usd == (trade.exit_price - trade.entry_price) * 10
    assert found.result_r == trade.result_r and found.result_gross_usd == trade.result_gross_usd
    assert errors(contract.json_schema(contract.TradeWhatIfV1),
                  json.loads(contract.dumps(found))) == []


def test_a_version_with_no_signal_answers_zero(cfg):
    _capture(cfg, day_offset=-3)
    _history(cfg)
    found = what_if(_settings(cfg), "price-action-v1", "ABCD", PAST, SATURDAY, CapturedBarsDays())
    assert found.cause is None and found.trades == ()
    assert found.result_r == 0 and found.result_gross_usd == 0


@pytest.mark.parametrize("prepare,folder,match", [
    ((), "scripted-v1", "no captured bars for 2026-11-13"),
    (("capture",), "scripted-v1", "no cached prior sessions for ABCD"),
    (("capture", "history"), "nope", "nope: cannot read strategies/nope/strategy.toml"),
])
def test_missing_inputs_are_one_cause_without_numbers(cfg, prepare, folder, match):
    if "capture" in prepare:
        _capture(cfg, day_offset=-3)
    if "history" in prepare:
        _history(cfg)
    found = what_if(_settings(cfg), folder, "ABCD", PAST, SATURDAY, CapturedBarsDays())
    assert found.cause is not None and match in found.cause, found.cause
    assert str(cfg.root) not in found.cause
    assert found.trades == () and found.result_r is None and found.signals is None


def test_another_symbol_has_no_bars(cfg):
    _capture(cfg, day_offset=-3)
    found = what_if(_settings(cfg), "scripted-v1", "WXYZ", PAST, SATURDAY, CapturedBarsDays())
    assert found.cause == "no captured bars for WXYZ on 2026-11-13"


def test_refused_in_session_unless_switched_off(cfg):
    with pytest.raises(TradeWhatIfRefused, match="regular session"):
        what_if(_settings(cfg), "scripted-v1", "ABCD", PAST, T0, CapturedBarsDays())
    anytime = replace(cfg, api=replace(cfg.api, trade_whatif_outside_session_only=False))
    found = what_if(_settings(anytime), "scripted-v1", "ABCD", PAST, T0, CapturedBarsDays())
    assert found.cause is not None


def test_route_queries_and_refusal(cfg):
    _capture(cfg, day_offset=-3)
    _history(cfg)
    clock = Clock(SATURDAY)
    server = make_server(replace(settings_from_config(cfg), port=0), clock)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        path = "/trades/whatif?date=2026-11-13&symbol=ABCD&version=scripted-v1"
        resp, payload = request(server, "GET", path)
        body = doc(payload)
        assert resp.status == 200 and len(body["trades"]) == 1
        assert errors(contract.json_schema(contract.TradeWhatIfV1), body) == []
        for bad in ("/trades/whatif?date=2026-11-13&symbol=ABCD",
                    "/trades/whatif?date=2026-11-13&symbol=A%2F..&version=scripted-v1",
                    "/trades/whatif?date=2026-11-13&symbol=ABCD&version=..",
                    "/trades/whatif?date=13-11-2026&symbol=ABCD&version=scripted-v1"):
            assert request(server, "GET", bad)[0].status == 400, bad
        assert request(server, "POST", path.split("?")[0], {})[0].status == 405
        clock.now = T0
        resp, payload = request(server, "GET", path)
        assert resp.status == 409 and "regular session" in doc(payload)["cause"]
        assert doc(payload)["cause_codes"] == [
            {"code": "outside_session_only", "params": {"until": "16:00"}}]
    finally:
        server.shutdown()
        server.server_close()


class Broken(Scripted):
    def evaluate(self, context):
        raise KeyError("lookback")


def test_a_version_that_raises_is_a_cause_not_a_crash(cfg, monkeypatch):
    _capture(cfg, day_offset=-3)
    _history(cfg)
    monkeypatch.setattr(sys.modules[MODULE], "create", Broken)
    found = what_if(_settings(cfg), "scripted-v1", "ABCD", PAST, SATURDAY, CapturedBarsDays())
    assert found.cause.startswith("simulation failed: KeyError") and found.trades == ()


def test_an_empty_cache_is_refused(cfg):
    _capture(cfg, day_offset=-3)
    _history(cfg).write_text("")
    found = what_if(_settings(cfg), "scripted-v1", "ABCD", PAST, SATURDAY, CapturedBarsDays())
    assert found.cause == "cached prior sessions hold no bar of ABCD"
