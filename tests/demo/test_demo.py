"""`aperture demo`: the real bot on a simulated exchange, in its own folder, never Webull."""

from __future__ import annotations

import http.client
import json
import re
import shutil
import sys
import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from aperture import config
from aperture.api import contract as api
from aperture.api.server import make_server, settings_from_config
from aperture.calendar import SessionCalendar
from aperture.data.base import ScreenerKind
from aperture.demo import run as demo
from aperture.demo.scenario import DemoCommand, ScenarioError, SymbolPlan, load

ROOT = Path(__file__).resolve().parents[2]
SUNDAY = date(2026, 11, 15)  # the demo day falls back to Friday 2026-11-13
FRIDAY = date(2026, 11, 13)
HOT = D(30000)


def repo(tmp_path: Path) -> Path:
    for part in ("config", "strategies"):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def small(scenario_path: Path):
    """The real demo.toml, cut down to one past session and two symbols today (fast)."""
    full = load(scenario_path)
    breakout = SymbolPlan("AAA", D(20), D(3000), ScreenerKind.GAINERS, (
        ("09:30", "09:45", D("24.50"), HOT), ("10:30", "10:32", D(21), HOT)))
    held = SymbolPlan("BBB", D(30), D(3000), ScreenerKind.GAINERS, (
        ("10:40", "10:55", D("34.50"), HOT),))
    # Breaks out while BBB is held: admission refuses it (one position at a time).
    late = SymbolPlan("CCC", D(40), D(3000), ScreenerKind.GAINERS, (
        ("11:05", "11:20", D("46.00"), HOT),))
    return replace(full, past=((breakout,),), today=(breakout, held, late), clock="11:30",
                   commands=(DemoCommand("10:35", api.CommandKind.ENTRY_STOP_ON, None),
                             DemoCommand("10:38", api.CommandKind.ENTRY_STOP_OFF, None)))


def _get(server, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{server.server_port}"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 200, body
    return body


def test_real_demo_file_loads_and_is_commented():
    text = (ROOT / "config" / "demo.toml").read_text("utf-8")
    assert config.uncommented_keys(text) == []
    scenario = load(ROOT / "config" / "demo.toml")
    assert scenario.today and scenario.past
    assert scenario.api_port != config.load(ROOT / "config" / "bot.toml").api.port


def test_unknown_key_is_refused(tmp_path):
    path = tmp_path / "demo.toml"
    path.write_text((ROOT / "config" / "demo.toml").read_text("utf-8").replace(
        "[market]\n", "[market]\n# x\nsurprise = 1\n"), "utf-8")
    with pytest.raises(ScenarioError, match="surprise"):
        load(path)


def test_sessions_skip_weekends():
    calendar = SessionCalendar.load(ROOT / "config" / "calendar")
    assert demo.sessions(calendar, SUNDAY, 2) == [date(2026, 11, 12), FRIDAY]


def test_folder_is_separate_and_only_a_demo_folder_is_replaced(tmp_path):
    root = repo(tmp_path)
    scenario = load(root / "config" / "demo.toml")
    path = demo.build_folder(root, scenario)
    cfg = config.load(path)
    assert cfg.environment_name == scenario.environment
    assert cfg.api.port == scenario.api_port
    assert cfg.strategy.observers == scenario.observers
    assert (cfg.root / cfg.run.data_dir).is_relative_to(root / scenario.root_dir)
    assert config.trading_gate(cfg).open
    demo.build_folder(root, scenario)  # rebuilt over its own marker
    (root / scenario.root_dir / demo.MARKER).unlink()
    with pytest.raises(demo.DemoRefused, match="not made by"):
        demo.build_folder(root, scenario)


def test_demo_day_runs_the_real_bot_and_the_api_serves_it(tmp_path):
    root = repo(tmp_path)
    scenario = small(root / "config" / "demo.toml")
    before = set(sys.modules)
    assert demo.run_demo(root, scenario, SUNDAY, live=False, log=lambda line: None) == 0
    assert not any("webull" in name for name in set(sys.modules) - before)
    assert not (root / "data").exists()  # the real data_dir is never created

    cfg = config.load(root / scenario.root_dir / "config" / "bot.toml")
    server = make_server(replace(settings_from_config(cfg), port=0))
    serving = None
    try:
        status = server.status_response().status
        assert status is not None and status.environment == scenario.environment
        assert [p.symbol for p in status.positions] == ["BBB"]
        assert status.positions[0].stop.state is api.StopState.VERIFIED
        assert [(t.symbol, t.exit_reason) for t in status.trades_today] == [
            ("AAA", "stopped-out")]
        (closed,) = status.trades_today
        assert closed.planned_loss_usd > 0
        assert closed.stop_confirmed_after_seconds is not None
        (day_trade,) = server.history.day(FRIDAY, FRIDAY).trades
        assert day_trade.stop_confirmed_after_seconds == closed.stop_confirmed_after_seconds
        assert day_trade.planned_loss_usd == closed.planned_loss_usd
        assert day_trade.mae_usd is not None and day_trade.mfe_usd is not None
        # Lower bound: the stop-out shows its 1 R loss, not the fall after the stop filled.
        assert day_trade.mae_usd == day_trade.result_gross_usd == -day_trade.planned_loss_usd
        # The journal proves the shares held only until the stop's working read, seconds
        # after the fill (reads are journaled on change), so no bar counts: the rise before
        # the stop-out is not proven, and MFE is the entry itself.
        assert day_trade.mfe_usd == 0
        room = status.daily_room_usd
        assert room is not None and room < cfg.risk.max_daily_loss_usd
        assert [c.kind for c in status.recent_commands] == [
            api.CommandKind.ENTRY_STOP_OFF, api.CommandKind.ENTRY_STOP_ON]
        days = server.history.days()
        assert [d.day for d in days.days][:2] == [FRIDAY, date(2026, 11, 12)]
        assert all(d.trades == 1 for d in days.days[:2])
        assert server.history.bars("BBB", FRIDAY, FRIDAY).bars
        account = server.account_response()
        assert isinstance(account, api.AccountResponseV1)
        assert [p.symbol for p in account.account.positions] == ["BBB"]
        # The apps' "Not taken" list and the observers' simulated results are not empty.
        server.clock = lambda: status.written_at
        serving = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        serving.start()
        skipped = _get(server, "/skipped")
        assert skipped["day"] == FRIDAY.isoformat()
        assert skipped["signals"] and {(s["symbol"], s["admission"])
                                       for s in skipped["signals"]} == {("CCC", "refused")}
        assert all(s["refusal_cause"] and s["entry_price"] for s in skipped["signals"])
        observers = _get(server, "/observers")["observers"]
        assert [(o["name"], o["result_cause"]) for o in observers] == [
            ("price-action-v2", None)]
        assert any(d["action"] == "ENTER" for d in observers[0]["decisions"])
        assert D(observers[0]["result_gross_usd"]) != 0
        # Performance over both demo sessions: one stop-out on each.
        perf = _get(server, f"/performance?from=2026-11-12&to={FRIDAY.isoformat()}")
        assert (perf["trades"], perf["losses"], perf["exclusions"]) == (2, 2, [])
        assert [(d["day"], d["trades"]) for d in perf["days"]] == [
            ("2026-11-12", 1), (FRIDAY.isoformat(), 1)]
        assert D(perf["result_gross_r"]) == D(perf["days"][-1]["cumulative_gross_r"])
        assert D(perf["max_drawdown_gross_usd"]) == -D(perf["result_gross_usd"]) > 0
    finally:
        if serving is not None:
            server.shutdown()
        server.server_close()
    decisions = (cfg.root / cfg.run.data_dir / cfg.observers.decisions_dir
                 / scenario.environment / FRIDAY.isoformat() / "decisions.jsonl")
    roles = {json.loads(line)["role"] for line in decisions.read_text("utf-8").splitlines()}
    assert roles == {"active", "observer"}


def test_demo_copy_can_never_reach_a_broker_account(tmp_path):
    root = repo(tmp_path)
    base = (root / "config" / "bot.toml").read_text("utf-8")
    built = demo.build_folder(root, load(root / "config" / "demo.toml")).read_text("utf-8")
    assert "webull.com" not in built
    real_names = re.findall(r'^\w+_variable = "(.+)"$', base, re.MULTILINE)
    assert real_names and not any(f'"{name}"' in built for name in real_names)


def test_absolute_real_data_dir_stays_out_of_the_demo(tmp_path):
    root = repo(tmp_path)
    path = root / "config" / "bot.toml"
    real = tmp_path / "real-data"
    path.write_text(path.read_text("utf-8").replace('data_dir = "data"', f'data_dir = "{real}"', 1),
                    "utf-8")
    scenario = load(root / "config" / "demo.toml")
    cfg = config.load(demo.build_folder(root, scenario))
    assert (cfg.root / cfg.run.data_dir).resolve().is_relative_to(root / scenario.root_dir)
