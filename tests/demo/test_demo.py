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
from aperture.demo.world import Texture, day_bars, textured

ROOT = Path(__file__).resolve().parents[2]
SUNDAY = date(2026, 11, 15)  # the demo day falls back to Friday 2026-11-13
FRIDAY = date(2026, 11, 13)
HOT = D(30000)


def repo(tmp_path: Path) -> Path:
    for part in ("config", "strategies"):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def small(scenario_path: Path):
    """The real demo.toml, cut down to one past session and two symbols today (fast), on the
    plain planned bars (no texture), so a stop-out's metrics are exact."""
    full = load(scenario_path)
    breakout = SymbolPlan("AAA", D(20), D(3000), ScreenerKind.GAINERS, (
        ("09:30", "09:45", D("24.50"), HOT), ("10:30", "10:32", D(21), HOT)))
    # Today AAA breaks out again inside its 30-minute pause after the stop-out: refused.
    again = replace(breakout, legs=(*breakout.legs, ("10:36", "10:52", D("24.80"), HOT)))
    held = SymbolPlan("BBB", D(30), D(3000), ScreenerKind.GAINERS, (
        ("10:40", "10:55", D("34.50"), HOT),))
    # Breaks out while BBB is held: admission refuses it (one position at a time).
    late = SymbolPlan("CCC", D(40), D(3000), ScreenerKind.GAINERS, (
        ("11:05", "11:20", D("46.00"), HOT),))
    return replace(full, past=((breakout,),), today=(again, held, late), clock="11:30",
                   texture=Texture(*(D(0),) * 6),
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


def test_bad_texture_is_refused(tmp_path):
    path = tmp_path / "demo.toml"
    text = (ROOT / "config" / "demo.toml").read_text("utf-8")
    path.write_text(re.sub(r'texture_pull = "[^"]*"', 'texture_pull = "1"', text), "utf-8")
    with pytest.raises(ScenarioError, match="texture_pull"):
        load(path)


def test_texture_gives_real_looking_bars_the_same_on_every_run():
    scenario = load(ROOT / "config" / "demo.toml")
    plan = scenario.today[0]
    day = SessionCalendar.load(ROOT / "config" / "calendar").day(FRIDAY)
    planned = day_bars(plan.symbol, day, plan.base, plan.volume, plan.legs, scenario.wick)
    bars = textured(planned, scenario.texture)
    assert bars == textured(planned, scenario.texture)
    assert [b.start for b in bars] == [b.start for b in planned]
    assert all(b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in bars)
    # No gaps between minutes: each bar opens at the previous close.
    assert all(a.close == b.open for a, b in zip(bars, bars[1:], strict=False))
    flat = sum(b.open == b.close for b in bars)
    assert flat < len(bars) // 4 < sum(b.open == b.close for b in planned)
    assert len({b.volume for b in bars}) > len(bars) // 2
    # Noise never makes a breakout where the plan holds: no close above the planned close there,
    # and every high reaches it.
    holding = [(b, p) for b, p in zip(bars, planned, strict=True) if p.open == p.close]
    assert holding and all(b.close <= p.close <= b.high for b, p in holding)
    off = Texture(*(D(0),) * 6)
    assert [(b.close, b.volume) for b in textured(planned, off)] == [
        (b.close, b.volume) for b in planned]


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
        for shown in (closed, status.positions[0]):  # entry sent once, then seen filled
            assert shown.entry_cause is None and shown.entry.sends == 1
            assert shown.entry.send_outcome is api.SendOutcome.ACKNOWLEDGED
            assert shown.entry.intent_at <= shown.entry.sent_at <= shown.entry.filled_at
        (day_trade,) = server.history.day(FRIDAY, FRIDAY).trades
        assert day_trade.stop_confirmed_after_seconds == closed.stop_confirmed_after_seconds
        assert day_trade.planned_loss_usd == closed.planned_loss_usd
        assert (day_trade.entry, day_trade.entry_cause) == (closed.entry, None)
        assert day_trade.mae_usd is not None and day_trade.mfe_usd is not None
        # Lower bound: the stop-out shows its 1 R loss, not the fall after the stop filled.
        assert day_trade.mae_usd == day_trade.result_gross_usd == -day_trade.planned_loss_usd
        # The bot's periodic `order-held` rows prove the shares held until shortly before
        # the stop-out, so the bars after the fill count: the rise is shown.
        assert day_trade.mfe_usd > 0
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
                                       for s in skipped["signals"]} == {("AAA", "refused"),
                                                                        ("CCC", "refused")}
        assert all(s["refusal"] and s["entry_price"] for s in skipped["signals"])
        assert {s["refusal"]["code"] for s in skipped["signals"]} == {
            "SYMBOL_COOLDOWN", "OWN_POSITION_OPEN"}
        # The cooldown refusal carries its times: 30 minutes from AAA's stop-out.
        cooldown = next(s["refusal"] for s in skipped["signals"]
                        if s["refusal"]["code"] == "SYMBOL_COOLDOWN")
        since, until = api.parse_time(cooldown["since"]), api.parse_time(cooldown["until"])
        assert (until - since).total_seconds() == cfg.risk.symbol_cooldown_minutes_after_loss * 60
        assert since == day_trade.closed_at.replace(microsecond=since.microsecond)
        # The Overview's live flow and "Watching closely".
        flow = _get(server, "/flow")
        assert {c["symbol"] for c in flow["chips"]} == {"AAA", "BBB", "CCC"}
        assert all(c["change_pct"] is not None and c["excluded_cause"] is None
                   for c in flow["chips"])
        stages = {s["stage"]: s["symbols"] for s in flow["stages"]}
        assert stages["universe"] == stages["screeners"] == ["AAA", "BBB", "CCC"]
        assert stages["risk"] == stages["order"] == stages["stop"] == ["AAA", "BBB"]
        assert {s["state"] for s in flow["stops"]} == {"verified"}
        assert flow["reconcile"]["agreement"] == "match"
        watching = _get(server, "/watching")
        assert 0 < len(watching["rows"]) <= cfg.api.watching_max_rows
        assert all(r["verdict"] in ("WATCH", "IGNORE") for r in watching["rows"])
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
