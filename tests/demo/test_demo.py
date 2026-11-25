"""`tickr demo`: the real bot on a simulated exchange, in its own folder, never Webull."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import sys
import tarfile
import threading
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path
from time import monotonic

import pytest
from baseline import write_baseline

from tickr import config
from tickr.api import contract as api
from tickr.api.server import make_server, settings_from_config
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.data.base import BarSpan, ScreenerKind
from tickr.demo import run as demo
from tickr.demo.scenario import (
    STOP_UNVERIFIED,
    DemoCommand,
    DemoFault,
    LiveLoop,
    LoopCommand,
    LoopRole,
    ScenarioError,
    SymbolPlan,
    load,
    loop_cycles,
    unroll,
)
from tickr.demo.world import (
    UNVERIFIED_STOP_STATUS,
    Texture,
    World,
    daily_history,
    day_bars,
    ny,
    textured,
    weekdays_before,
)
from tickr.store import MIGRATIONS
from tickr.types import (
    OrderRequest,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
    TradingSession,
)

ROOT = Path(__file__).resolve().parents[2]
SUNDAY = date(2026, 11, 15)  # the demo day falls back to Friday 2026-11-13
FRIDAY = date(2026, 11, 13)
HOT = D(30000)


def repo(tmp_path: Path) -> Path:
    for part in ("config", "strategies"):
        shutil.copytree(ROOT / part, tmp_path / part)
    write_baseline(tmp_path / "config" / "bot.toml")  # one slot: the demo scripts one trade at once
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
    api = config.load(ROOT / "config" / "bot.toml").api
    assert api.demo_port not in (0, api.port, api.remote_port)


def test_every_fall_on_the_demo_day_ends_below_its_base():
    """A fall on the demo day is a stop-out (or a loser drifting), and the stop is placed from the
    noisy tape, which is seeded by the day: a fall that ends above the base can stay above the
    stop on some dates, and the day is then not flat at the clock (KXTR on 2026-11-25)."""
    scenario = load(ROOT / "config" / "demo.toml")
    for plan in scenario.today:
        level = plan.base
        for first, _last, price, _volume in plan.legs:
            if first >= scenario.clock:  # the live loop starts a fresh simulated exchange
                break
            if price < level:
                assert price < plan.base, (plan.symbol, price, plan.base)
            level = price


def test_live_until_must_follow_the_clock(tmp_path):
    path = tmp_path / "demo.toml"
    path.write_text((ROOT / "config" / "demo.toml").read_text("utf-8").replace(
        'live_until = "15:40"', 'live_until = "14:30"'), "utf-8")
    with pytest.raises(ScenarioError, match="live_until"):
        load(path)


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
    real = config.load(root / "config" / "bot.toml").api
    assert cfg.api.port == real.demo_port
    assert cfg.api.demo_port == 0  # the demo's API forwards nowhere, never to itself
    assert cfg.strategy.observers == scenario.observers
    assert (cfg.root / cfg.run.data_dir).is_relative_to(root / scenario.root_dir)
    assert config.trading_gate(cfg).open
    demo.build_folder(root, scenario)  # rebuilt over its own marker
    (root / scenario.root_dir / demo.MARKER).unlink()
    with pytest.raises(demo.DemoRefused, match="not made by"):
        demo.build_folder(root, scenario)


def test_a_second_demo_on_the_same_folder_is_refused_before_touching_it(tmp_path):
    root = repo(tmp_path)
    scenario = small(root / "config" / "demo.toml")
    folder = demo.build_folder(root, scenario).parents[1]
    (folder / "running.txt").write_text("the first demo's files")
    with demo.running_lock(root, scenario):  # the first demo
        with pytest.raises(demo.DemoRefused, match="another `tickr demo` is running"):
            demo.run_demo(root, scenario, SUNDAY, live=False, log=lambda line: None)
        assert (folder / "running.txt").exists()  # not wiped
    with demo.running_lock(root, scenario):  # released when the first one ends
        pass


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
        # Prices after the exit come from the same capture (later ones not captured yet).
        assert [p.minutes for p in day_trade.prices_after] == list(cfg.api.price_after_minutes)
        assert day_trade.prices_after[0].price is not None
        room = status.daily_room_usd
        assert room is not None and room < cfg.risk.max_daily_loss_usd
        assert [c.kind for c in status.recent_commands] == [
            api.CommandKind.ENTRY_STOP_OFF, api.CommandKind.ENTRY_STOP_ON]
        days = server.history.days(FRIDAY)
        assert [d.day for d in days.days][:2] == [FRIDAY, date(2026, 11, 12)]
        assert all(d.trades == 1 for d in days.days[:2])
        # Clean-day marker: the past session's reconcile rows agreed and its day end was clean;
        # the demo day is not over.
        demo_day, past_day = (d.clean for d in days.days[:2])
        assert (demo_day.verdict, demo_day.cause) == (api.CleanVerdict.UNKNOWN, "the day is "
                                                      "not over")
        assert (past_day.verdict, past_day.causes) == (api.CleanVerdict.CLEAN, ())
        assert past_day.reconcile_rows > 1 and past_day.day_end_seq is not None
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
        # Each skipped signal has its prices after and its simulated what-if.
        assert all(s["prices_after"][0]["price"] and s["what_if"]["result_gross_usd"]
                   and s["what_if"]["cause"] is None for s in skipped["signals"])
        assert {s["refusal"]["code"] for s in skipped["signals"]} == {
            "SYMBOL_COOLDOWN", "POSITION_LIMIT_REACHED"}
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
        assert 0 < len(watching["rows"]) == watching["total"]
        assert all(r["verdict"] == "WATCH" for r in watching["rows"])
        observers = _get(server, "/observers")["observers"]
        assert [(o["name"], o["result_cause"]) for o in observers] == [
            ("price-action-v2", None)]
        assert any(d["action"] == "ENTER" for d in observers[0]["decisions"])
        assert D(observers[0]["result_gross_usd"]) != 0
        # The Lab chart's simulated trades add up to the day result (each rounded on its own:
        # at most one unit of the last place apart per trade).
        trades = observers[0]["trades"]
        assert observers[0]["trades_cause"] is None and not observers[0]["trades_truncated"]
        filled = [t for t in trades if t["cause"] is None]
        assert filled and all(t["opened_at"] and t["exit_reason"] for t in filled)
        total = D(observers[0]["result_gross_usd"])
        unit = D(1).scaleb(total.as_tuple().exponent)
        assert abs(sum(D(t["result_gross_usd"]) for t in filled) - total) <= len(filled) * unit
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


def test_faults_are_checked_when_loading(tmp_path):
    text = (ROOT / "config" / "demo.toml").read_text("utf-8")
    assert load(ROOT / "config" / "demo.toml").faults == ()  # off unless the owner turns it on
    path = tmp_path / "demo.toml"
    for fault, cause in (
            ('{ at = "14:31", kind = "stop-missing", symbol = "MVLD" }', "is not one of"),
            ('{ at = "14:31", kind = "stop-unverified", symbol = "NOPE" }',
             "not a symbol of today"),
            ('{ at = "2pm", kind = "stop-unverified", symbol = "MVLD" }', "is not a time")):
        path.write_text(text.replace("\nfaults = []\n", f"\nfaults = [{fault}]\n"), "utf-8")
        with pytest.raises(ScenarioError, match=cause):
            load(path)
    path.write_text(text.replace(
        "\nfaults = []\n", '\nfaults = [{ at = "14:31", kind = "stop-unverified", '
        'symbol = "MVLD" }]\n'), "utf-8")
    assert load(path).faults == (DemoFault("14:31", STOP_UNVERIFIED, "MVLD"),)


def test_an_unverified_stop_is_not_working_and_never_fills():
    calendar = SessionCalendar.load(ROOT / "config" / "calendar")
    market_day = calendar.day(FRIDAY)
    world = World(ny(FRIDAY, "10:00"), market_day, D("0.01"), D(500), "demo")
    for symbol in ("AAA", "BBB"):
        world.add_symbol(symbol, day_bars(symbol, market_day, D(20), D(3000), (
            ("10:10", "10:12", D(15), HOT),), D("0.05")), [])
    exchange = world.exchange

    def stop(cid, symbol, quantity=D(10)):
        exchange.place(OrderRequest(cid, symbol, Side.SELL, OrderType.STOP_LOSS, quantity,
                                    TimeInForce.DAY, TradingSession.CORE, None, D(19)))

    stop("a-1", "AAA")
    stop("b-1", "BBB")
    exchange.unverify_stops("AAA")
    stop("a-2", "AAA", D(5))  # a later stop of the symbol is unverified too (another
    # quantity: Webull refuses a stop identical to a working one)
    assert {cid: exchange.order_detail(cid).state for cid in ("a-1", "a-2", "b-1")} == {
        "a-1": OrderState.UNKNOWN, "a-2": OrderState.UNKNOWN, "b-1": OrderState.ACCEPTED}
    assert exchange.order_detail("a-1").raw_status == UNVERIFIED_STOP_STATUS
    world.advance(15 * 60)  # both symbols fall through 19
    assert [(f[1], f[3]) for f in exchange.fills] == [("BBB", OrderType.STOP_LOSS)]
    assert exchange.order_detail("a-1").state is OrderState.UNKNOWN
    exchange.cancel("a-1")  # the bot can still cancel it before closing the position
    assert exchange.order_detail("a-1").state is OrderState.CANCELLED


def test_demo_fault_shows_the_open_position_unprotected(tmp_path):
    root = repo(tmp_path)
    bot = root / "config" / "bot.toml"
    # Long enough that the bot has not closed the position yet when the demo day stops.
    text, count = re.subn(r"(?m)^max_unprotected_seconds = \d+$",
                          "max_unprotected_seconds = 600", bot.read_text("utf-8"))
    assert count == 1
    bot.write_text(text, "utf-8")
    scenario = replace(small(root / "config" / "demo.toml"), past=(),  # the demo day only
                       faults=(DemoFault("11:25", STOP_UNVERIFIED, "BBB"),))
    # The real bot refuses a clean shutdown while a position has no verified stop.
    assert demo.run_demo(root, scenario, SUNDAY, live=False,
                         log=lambda line: None) == 5  # `tickr run`: work pending

    cfg = config.load(root / scenario.root_dir / "config" / "bot.toml")
    server = make_server(replace(settings_from_config(cfg), port=0))
    try:
        status = server.status_response().status
        assert status is not None
        (position,) = status.positions
        assert position.symbol == "BBB"
        assert position.stop.state is not api.StopState.VERIFIED
        fences = {f.kind.split(":")[0] for f in status.fences}
        assert {"unprotected", "status-unknown"} <= fences
        assert status.entry_block is not None
    finally:
        server.server_close()


def test_chart_days_give_the_d_chart_a_long_history_the_bot_never_reads():
    scenario = load(ROOT / "config" / "demo.toml")
    assert scenario.chart_days >= 30  # 1M, 3M and 1Y show real candles, RSI 14 has data
    days = weekdays_before(date(2026, 11, 9), 3)  # a Monday: the Wednesday to Friday before it
    assert days == [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
    walk = scenario.chart_walk
    bars = daily_history("AAA", weekdays_before(FRIDAY, 60), D(20), D(1000), walk)
    assert bars == daily_history("AAA", weekdays_before(FRIDAY, 60), D(20), D(1000), walk)
    assert len(bars) == 60 and len({b.close for b in bars}) > 30  # the price moves
    assert all(b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in bars)
    assert bars[-1].start == ny(date(2026, 11, 12), "09:30")
    assert abs(bars[-1].close - D(20)) / D(20) < 10 * walk.gap  # the walk ends near `base`

    calendar = SessionCalendar.load(ROOT / "config" / "calendar")
    market_day = calendar.day(FRIDAY)
    world = World(ny(FRIDAY, "12:00"), market_day, D("0.01"), D(500), "demo")
    world.add_symbol("AAA", day_bars("AAA", market_day, D(20), D(3000), (), D("0.05")), [],
                     daily=bars)
    d_rows = world.feed.history_bars("AAA", BarSpan.D, world.now, 1000)
    assert [r.bar for r in d_rows[:-1]] == bars and len(d_rows) == 61  # 60 days and today
    minutes = world.feed.history_bars("AAA", BarSpan.M1, world.now, 100000)
    assert minutes and all(r.bar.start >= market_day.regular_open for r in minutes)
    assert all(b.start >= market_day.regular_open
               for b in world.feed.bars_before("AAA", world.now, 100000))


# --- the live loop ([live] in demo.toml) -------------------------------------------------------


def _loop_file(tmp_path: Path, old: str, new: str) -> Path:
    path = tmp_path / "demo.toml"
    text = (ROOT / "config" / "demo.toml").read_text("utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), "utf-8")
    return path


def test_live_loop_unrolls_the_same_way_every_time():
    scenario = load(ROOT / "config" / "demo.toml")
    assert scenario == load(ROOT / "config" / "demo.toml")  # deterministic, no randomness
    loop = scenario.live
    assert loop is not None
    cycles = loop_cycles(scenario.clock, scenario.live_until, loop.cycle_minutes)
    assert cycles >= 1
    clock = datetime.combine(FRIDAY, datetime.strptime(scenario.clock, "%H:%M").time())
    plans = {p.symbol: p for p in scenario.today}
    for cycle in range(cycles):
        start = clock + cycle * timedelta(minutes=loop.cycle_minutes)
        used = [role.symbols[cycle % len(role.symbols)] for role in loop.roles]
        assert len(set(used)) == len(used)  # one symbol, one role, in every cycle
        for role, symbol in zip(loop.roles, used, strict=True):
            for first, last, _change, volume in role.legs:
                begin = (start + timedelta(minutes=first)).time().isoformat()
                end = (start + timedelta(minutes=last)).time().isoformat()
                assert [leg for leg in plans[symbol].legs if leg[0] == begin] == [
                    next(leg for leg in plans[symbol].legs if leg[0] == begin)]
                (leg,) = [leg for leg in plans[symbol].legs if leg[0] == begin]
                assert (leg[1], leg[3]) == (end, volume)
        for command in loop.commands:
            at = (start + timedelta(seconds=command.at_seconds)).time().isoformat()
            assert any(c.at == at and c.kind is command.kind for c in scenario.commands)
    # Every cycle fits before live_until, and the commands are in time order.
    last_end = max(leg[1] for p in scenario.today for leg in p.legs)
    assert last_end <= time.fromisoformat(scenario.live_until).isoformat()
    ats = [time.fromisoformat(c.at) for c in scenario.commands]
    assert ats == sorted(ats)


def test_live_loop_prices_compound_from_where_each_symbol_was_left():
    base = load(ROOT / "config" / "demo.toml")
    plan = SymbolPlan("AAA", D(20), D(3000), ScreenerKind.GAINERS,
                      (("09:30", "09:40", D(22), HOT),))
    legs = ((0, 2, D("0.10"), HOT), (3, 4, D("-0.5"), HOT))
    loop = LiveLoop(5, 3, (LoopRole("up", ("AAA",), legs),),
                    (LoopCommand(90, api.CommandKind.FLATTEN, "up"),
                     LoopCommand(150, api.CommandKind.ENTRY_STOP_ON, None)))
    scenario = unroll(replace(base, today=(plan,), commands=(), clock="10:00",
                              live_until="10:12", live=loop))
    (aaa,) = scenario.today
    assert aaa.legs == (("09:30", "09:40", D(22), HOT),
                        ("10:00:00", "10:02:00", D("24.20"), HOT),
                        ("10:03:00", "10:04:00", D("12.10"), HOT),
                        ("10:05:00", "10:07:00", D("13.31"), HOT),
                        ("10:08:00", "10:09:00", D("6.66"), HOT))  # two whole cycles only
    assert [(c.at, c.kind, c.symbol) for c in scenario.commands] == [
        ("10:01:30", api.CommandKind.FLATTEN, "AAA"),
        ("10:02:30", api.CommandKind.ENTRY_STOP_ON, None),
        ("10:06:30", api.CommandKind.FLATTEN, "AAA"),
        ("10:07:30", api.CommandKind.ENTRY_STOP_ON, None)]


@pytest.mark.parametrize(("old", "new", "cause"), [
    ('{ role = "busy", symbols = ["TRVX"', '{ role = "busy", symbols = ["NOPE"', "not a symbol"),
    ('kind = "flatten", role = ""', 'kind = "flatten", role = "nobody"', "not a role"),
    ('kind = "entry-stop-on", role = ""', 'kind = "entry-stop-on", role = "win"', "only flatten"),
    ("cycle_minutes = 10", "cycle_minutes = 0", "cycle_minutes"),
    ('[[0, 3, "0.018", "40000"], [3, 5,', '[[0, 4, "0.018", "40000"], [3, 5,', "overlaps"),
    ('[5, 6, "-0.03", "15000"]', '[5, 11, "-0.03", "15000"]', "whole minute from 0 to 10"),
    ('[5, 6, "-0.03", "15000"]', '[5, 6, "-1", "15000"]', "above -1"),
    ("at_seconds = 305", "at_seconds = 600", "inside the cycle"),
])
def test_bad_live_loop_is_refused(tmp_path, old, new, cause):
    with pytest.raises(ScenarioError, match=cause):
        load(_loop_file(tmp_path, old, new))


def test_a_loop_symbol_moving_again_before_its_leg_ends_is_refused(tmp_path):
    # FYRE is the "loss" symbol of the first cycle and must not also be its "cooldown" symbol.
    with pytest.raises(ScenarioError, match="FYRE.*before its leg"):
        load(_loop_file(tmp_path, 'symbols = ["MVLD", "FYRE"', 'symbols = ["FYRE", "FYRE"'))


def test_live_loop_makes_the_real_bot_trade_and_refuse_every_cycle(tmp_path):
    """Two cycles of the real loop on the real bot (fast clock): each cycle the bot buys a
    breakout, is flattened for a win, buys another and is stopped out, and refuses breakouts
    while it holds one and under the operator's entry stop."""
    root = repo(tmp_path)
    full = load(root / "config" / "demo.toml")
    assert full.live is not None
    roles = {s for role in full.live.roles for s in role.symbols}
    quiet = tuple(replace(p, legs=()) for p in full.today if p.symbol in roles)
    scenario = unroll(replace(full, past=(), today=quiet, commands=(), clock="10:00",
                              live_until="10:20"))
    scenario = replace(scenario, clock=scenario.live_until)  # run the loop fast, no API
    assert demo.run_demo(root, scenario, SUNDAY, live=False, log=lambda line: None) == 0
    cfg = config.load(root / scenario.root_dir / "config" / "bot.toml")
    server = make_server(replace(settings_from_config(cfg), port=0))
    try:
        trades = server.history.day(FRIDAY, FRIDAY).trades
    finally:
        server.server_close()
    for cycle in ("10:0", "10:1"):
        reasons = {t.exit_reason for t in trades if t.opened_at.astimezone(
            NEW_YORK).strftime("%H:%M").startswith(cycle)}
        assert {"operator-flatten", "stopped-out"} <= reasons, (cycle, reasons)
    decisions = (cfg.root / cfg.run.data_dir / cfg.observers.decisions_dir
                 / scenario.environment / FRIDAY.isoformat() / "decisions.jsonl")
    refused = {json.loads(line)["refusal_cause"].split(":")[0]
               for line in decisions.read_text("utf-8").splitlines()
               if json.loads(line)["admission"] == "refused"}
    assert {"POSITION_LIMIT_REACHED", "ENTRY_STOP_SET"} <= refused


def test_symbols_only_the_loop_moves_are_listed_shortly_before_the_clock():
    """The fast part of the day does not evaluate the loop's idle symbols: the screeners list
    them only `listed_minutes_before_clock` before the clock (startup time of the demo)."""
    scenario = load(ROOT / "config" / "demo.toml")
    loop = scenario.live
    assert loop is not None
    listed = dict(scenario.listed)
    morning = {p.symbol for p in load(ROOT / "config" / "demo.toml").today
               if p.legs and p.legs[0][0] < scenario.clock}
    role_symbols = {s for role in loop.roles for s in role.symbols}
    assert set(listed) == role_symbols - morning and "MVLD" not in listed
    at = (datetime.combine(FRIDAY, time.fromisoformat(scenario.clock))
          - timedelta(minutes=loop.listed_minutes_before_clock)).time().isoformat()
    assert set(listed.values()) == {at}

    calendar = SessionCalendar.load(ROOT / "config" / "calendar")
    market_day = calendar.day(FRIDAY)
    world = World(ny(FRIDAY, "12:00"), market_day, D("0.01"), D(500), "demo")
    bars = day_bars("AAA", market_day, D(20), D(3000), (), D("0.05"))
    world.add_symbol("AAA", bars, [], listed_from=ny(FRIDAY, "12:05"))
    world.add_symbol("BBB", day_bars("BBB", market_day, D(30), D(3000), (), D("0.05")), [])
    assert [r.symbol for r in world.feed.screener(ScreenerKind.GAINERS)] == ["BBB"]
    world.advance(5 * 60)
    assert {r.symbol for r in world.feed.screener(ScreenerKind.GAINERS)} == {"AAA", "BBB"}


def _demo_port(root: Path, port: int) -> Path:
    bot = root / "config" / "bot.toml"
    text, count = re.subn(r"(?m)^demo_port = \d+$", f"demo_port = {port}",
                          bot.read_text("utf-8"))
    assert count == 1
    bot.write_text(text, "utf-8")
    return root


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- the fixture: built from the committed inputs, a start replays nothing --------------------


def _short_day(root: Path):
    """`small` cut to AAA's breakout and stop-out on the demo day only, flat at the clock, live
    for seconds: one fixture build replays half an hour, not two sessions."""
    scenario = small(root / "config" / "demo.toml")
    aaa = replace(scenario.today[0], legs=(("09:30", "09:45", D("24.50"), HOT),
                                           ("09:46", "09:48", D(21), HOT)))
    return replace(scenario, past=(), today=(aaa,), commands=(), clock="09:50",
                   live_until="09:50:04")


def _no_build(repo_root: Path) -> demo.Builder:
    raise AssertionError("no background build expected: the fixture is current")


def _archive(root: Path, inputs: str, **manifest: object) -> Path:
    """A fixture archive holding only its manifest (for the checks before anything is read)."""
    path = demo.fixture_path(root, inputs)
    path.parent.mkdir(exist_ok=True)
    _pack(path, {"inputs": inputs, "demo_day": FRIDAY.isoformat(), "clock": "09:50",
                 "schema": 1, **manifest})
    return path


def _pack(path: Path, manifest: dict, data_from: Path | None = None) -> None:
    raw = json.dumps(manifest).encode()
    with tarfile.open(path, "w:xz") as tar:
        if data_from is not None:
            with tarfile.open(data_from, "r:xz") as source:
                for member in source.getmembers():
                    if member.name != demo.FIXTURE_MANIFEST:
                        tar.addfile(member, source.extractfile(member))
        info = tarfile.TarInfo(demo.FIXTURE_MANIFEST)
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))


@pytest.fixture(scope="module")
def first_start(tmp_path_factory):
    """The one fixture build of this module: the first `tickr demo` on a Mac (no fixture yet)
    builds it in the foreground, then serves it and runs live until live_until."""
    root = _demo_port(repo(tmp_path_factory.mktemp("first")), _free_port())
    scenario = _short_day(root)
    lines: list[str] = []
    code = demo.run_demo(root, scenario, SUNDAY, log=lines.append, start_build=_no_build)
    return root, scenario, code, lines


def _with_fixture(tmp_path: Path, first_start) -> tuple[Path, int, Path]:
    """A fresh repo holding a copy of the module's fixture; returns it, its port and the copy."""
    built, scenario, _, _ = first_start
    root = _demo_port(repo(tmp_path), port := _free_port())
    archive = demo.fixture_path(built, demo.fixture_inputs(built, scenario))
    (root / demo.FIXTURE_DIR).mkdir()
    return root, port, Path(shutil.copy2(archive, root / demo.FIXTURE_DIR / archive.name))


def test_first_start_builds_the_fixture_then_serves(first_start):
    root, scenario, code, lines = first_start
    assert code == 0
    assert any("building it first" in line for line in lines)
    assert any("serving on" in line for line in lines)
    (archive,) = (root / demo.FIXTURE_DIR).iterdir()  # one build, no partial file left
    assert archive == demo.fixture_path(root, demo.fixture_inputs(root, scenario))
    manifest = demo.read_fixture(archive, scenario)
    assert (manifest["demo_day"], manifest["clock"]) == (FRIDAY.isoformat(), scenario.clock)
    assert manifest["schema"] == len(MIGRATIONS)
    assert archive.stat().st_size < 20 * 1024 * 1024  # compact
    assert (root / (scenario.root_dir + demo.BUILD_SUFFIX)).is_dir()  # built beside, not in, demo/
    assert not (root / "data").exists()  # the real data_dir is never created


def test_find_fixture_takes_the_current_build_else_the_latest_usable_one(tmp_path):
    root = repo(tmp_path)
    scenario = _short_day(root)
    assert demo.find_fixture(root, scenario, log=lambda line: None) is None
    inputs = demo.fixture_inputs(root, scenario)
    (root / demo.FIXTURE_DIR).mkdir()
    (root / demo.FIXTURE_DIR / "data.tar.xz").write_bytes(b"")  # not a build: never read
    latest, earlier = "a" * 64, "b" * 64
    _archive(root, latest)
    os.utime(_archive(root, earlier), (1, 1))  # written long ago
    found = demo.find_fixture(root, scenario, log=lambda line: None)
    assert found is not None and (found.archive.name, found.current) == (
        latest + demo.FIXTURE_SUFFIX, False)
    current = _archive(root, inputs)
    found = demo.find_fixture(root, scenario, log=lambda line: None)
    assert found is not None and (found.archive, found.current) == (current, True)
    # Any change of the inputs makes it a previous build.
    changed = replace(scenario, today=(replace(scenario.today[0], base=D(19)),
                                       *scenario.today[1:]))
    assert not demo.find_fixture(root, changed, log=lambda line: None).current
    (root / "strategies" / "price-action-v1" / "strategy.toml").write_text("# changed", "utf-8")
    assert demo.fixture_inputs(root, scenario) != inputs


@pytest.mark.parametrize(("manifest", "cause"), [
    ({"schema": 10_000}, "newer than this code"),
    ({"clock": "09:59"}, "ends at 09:59"),
    ({"inputs": "c" * 64}, "other inputs"),
])
def test_a_fixture_a_start_cannot_use_is_skipped(tmp_path, manifest, cause):
    root = repo(tmp_path)
    scenario = _short_day(root)
    inputs = demo.fixture_inputs(root, scenario)
    path = demo.fixture_path(root, inputs)
    path.parent.mkdir()
    _pack(path, {"inputs": inputs, "demo_day": FRIDAY.isoformat(), "clock": scenario.clock,
                 "schema": 1, **manifest})
    with pytest.raises(demo.DemoRefused, match=cause):
        demo.read_fixture(path, scenario)
    lines: list[str] = []
    assert demo.find_fixture(root, scenario, log=lines.append) is None
    assert len(lines) == 1 and cause in lines[0]


def _poll(port: int, started: float, done: threading.Event,
          answers: list[tuple[float, dict]], failures: list[tuple[type, int]]) -> None:
    """GET /status every 0.1 s: (seconds since start, status) per answer, (error, answers
    before it) per failure."""
    while not done.is_set():
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/status", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            if resp.status == 200 and body["status"] is not None:
                answers.append((monotonic() - started, body["status"]))
        except (OSError, http.client.HTTPException) as exc:
            failures.append((type(exc), len(answers)))
        done.wait(0.1)


def test_demo_starts_from_its_fixture_and_answers_within_seconds(tmp_path, first_start):
    """`tickr demo` copies the fixture, answers within [demo] answer_within_seconds (nothing
    replayed) and runs the real bot live from the clock. Before it serves, nothing holds the
    port: a client is refused at once, never reset or left hanging. Here the inputs changed
    since the build (a `git pull`): the start serves that previous build all the same, builds
    the new one beside it, and exits 0 once it lands (launchd starts a fresh demo on it)
    instead of serving the old one until live_until. A start from a current build is the same
    without the build (`first_start`, after its build)."""
    root, port, previous = _with_fixture(tmp_path, first_start)
    # A later live_until: changed inputs, and the swap must stop the demo well before it.
    scenario = replace(_short_day(root), live_until="09:50:30")
    current = demo.fixture_path(root, demo.fixture_inputs(root, scenario))
    answers: list[tuple[float, dict]] = []  # (seconds since start, status)
    failures: list[tuple[type[BaseException], int]] = []  # (error, answers before it)
    done = threading.Event()

    def live_written() -> bool:  # the live bot has written a status after the clock
        return any(api.parse_time(s["written_at"]).astimezone(NEW_YORK).strftime("%H:%M:%S")
                   >= scenario.clock + ":01" for _, s in list(answers))

    class Build:  # stands in for `tickr demo --build-fixture`: lands once the live bot wrote
        def __init__(self) -> None:
            self.terminated = False

        def wait(self) -> int:
            while not live_written() and not done.is_set():
                done.wait(0.05)
            manifest = demo.read_fixture(previous, scenario)
            _pack(current, {**manifest, "inputs": demo.fixture_inputs(root, scenario)},
                  data_from=previous)
            return 0

        def terminate(self) -> None:
            self.terminated = True

    builds: list[Build] = []

    def start_build(repo_root: Path) -> Build:
        assert repo_root == root
        builds.append(Build())
        return builds[-1]

    lines: list[str] = []
    started = monotonic()
    poller = threading.Thread(target=_poll, args=(port, started, done, answers, failures),
                              daemon=True)
    poller.start()
    try:
        assert demo.run_demo(root, scenario, SUNDAY, log=lines.append,
                             start_build=start_build) == 0
    finally:
        done.set()
        poller.join(timeout=5)
    ended = monotonic() - started
    assert answers, "the demo never answered"
    assert answers[0][0] <= scenario.answer_within_seconds
    assert all(error is ConnectionRefusedError for error, before in failures if before == 0)
    # The day up to the clock is there at once (the fixture), and the live bot then writes it.
    first = answers[0][1]
    assert first["environment"] == scenario.environment
    assert [t["symbol"] for t in first["trades_today"]][:1] == ["AAA"]
    assert live_written()
    # The new build landed: the demo stopped on it, not at live_until.
    assert len(builds) == 1 and builds[0].terminated  # a build still running would be stopped
    assert any("restarting onto it" in line for line in lines)
    live_seconds = (datetime.combine(FRIDAY, time.fromisoformat(scenario.live_until))
                    - datetime.combine(FRIDAY, time.fromisoformat(scenario.clock)))
    assert ended < live_seconds.total_seconds() / 2
    found = demo.find_fixture(root, scenario, log=lambda line: None)
    assert found is not None and found.current


def test_a_busy_demo_port_is_refused_at_start(tmp_path):
    root = _demo_port(repo(tmp_path), port := _free_port())
    scenario = _short_day(root)
    _archive(root, demo.fixture_inputs(root, scenario))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", port))
        busy.listen()
        with pytest.raises(demo.DemoRefused, match="cannot listen"):
            demo.run_demo(root, scenario, SUNDAY, log=lambda line: None, start_build=_no_build)


def _stale_repo(tmp_path: Path, first_start) -> tuple[Path, Path]:
    """A repo whose inputs changed since the module's build: that build is the previous one."""
    root, _, previous = _with_fixture(tmp_path, first_start)
    strategy = root / "strategies" / "price-action-v1" / "strategy.toml"
    strategy.write_text(strategy.read_text("utf-8") + "# changed\n", "utf-8")
    return root, previous


def test_a_build_stopped_from_outside_stores_nothing(tmp_path, monkeypatch, first_start):
    """SIGTERM (launchd, the demo ending its background build, Ctrl-C) stops the bot the same
    clean way as the build's own stop, only earlier: the cut-short day is never stored, the
    previous build stays the one served, and nothing is recorded as refused (the next start
    builds again). A partial archive a killed writer left is never read and is cleaned up."""
    root, previous = _stale_repo(tmp_path, first_start)
    scenario = _short_day(root)
    leftover = root / demo.FIXTURE_DIR / f".{'d' * 64}.tar.xz{demo.PARTIAL_SUFFIX}"
    leftover.write_bytes(b"cut short")
    assert demo.find_fixture(root, scenario, log=lambda line: None).archive == previous
    world = demo.Session.world

    def interrupted(self, plans, listed=(), start=None):
        made = world(self, plans, listed, start)
        made.at(ny(self.day, "09:40"), lambda: os.kill(os.getpid(), signal.SIGTERM))
        return made

    monkeypatch.setattr(demo.Session, "world", interrupted)
    with pytest.raises(demo.DemoRefused, match="interrupted.*before 09:50") as refused:
        demo.build_fixture(root, scenario, SUNDAY, log=lambda line: None)
    assert not isinstance(refused.value, demo.FixtureRefused)
    assert list((root / demo.FIXTURE_DIR).iterdir()) == [previous]  # leftover cleaned
    found = demo.find_fixture(root, scenario, log=lambda line: None)
    assert found is not None and (found.archive, found.current) == (previous, False)


def test_a_build_killed_while_writing_its_archive_leaves_none(tmp_path, monkeypatch,
                                                               first_start):
    built, _, _, _ = first_start
    root, previous = _stale_repo(tmp_path, first_start)
    scenario = _short_day(root)
    inputs = demo.fixture_inputs(root, scenario)
    cfg = config.load(built / (scenario.root_dir + demo.BUILD_SUFFIX) / "config" / "bot.toml")
    written: list[str] = []

    def killed(info):
        written.append(info.name)
        if len(written) > 3:
            raise KeyboardInterrupt
        return demo_filter(info)

    demo_filter = demo._tar_filter
    monkeypatch.setattr(demo, "_tar_filter", killed)
    with pytest.raises(KeyboardInterrupt):
        demo._store_fixture(root, scenario, inputs, cfg, FRIDAY, 0)
    assert list((root / demo.FIXTURE_DIR).iterdir()) == [previous]  # no archive, no partial


def test_a_build_the_start_would_refuse_is_not_stored_and_not_tried_again(
        tmp_path, monkeypatch, first_start):
    """The build checks its archive exactly as a start will (extract, migrate). Refused: no
    archive, the previous builds kept, the cause recorded under the inputs' digest; a start then
    serves the previous build without building again (no build-and-restart loop), and with no
    previous build it is refused at once."""
    built, _, _, _ = first_start
    root, previous = _stale_repo(tmp_path, first_start)
    scenario = _short_day(root)
    inputs = demo.fixture_inputs(root, scenario)
    cfg = config.load(built / (scenario.root_dir + demo.BUILD_SUFFIX) / "config" / "bot.toml")
    monkeypatch.setattr(demo, "replay", lambda *args: (cfg, FRIDAY, 0))  # the module's replay

    def newer(*args, **kwargs):
        raise RuntimeError("database schema is newer than this code")

    with monkeypatch.context() as patch:
        patch.setattr(demo.Store, "open", newer)
        with pytest.raises(demo.FixtureRefused, match="start checks refuse.*newer"):
            demo.build_fixture(root, scenario, SUNDAY, log=lambda line: None)
    assert "newer than this code" in demo.recorded_refusal(root, inputs)
    assert sorted(p.name for p in (root / demo.FIXTURE_DIR).iterdir()) == sorted(
        [previous.name, demo.refusal_path(root, inputs).name])

    lines: list[str] = []
    assert demo.run_demo(root, scenario, SUNDAY, log=lines.append, start_build=_no_build) == 0
    assert sum("was refused" in line for line in lines) == 1
    previous.unlink()
    with pytest.raises(demo.DemoRefused, match="refused.*no previous fixture"):
        demo.run_demo(root, scenario, SUNDAY, log=lambda line: None, start_build=_no_build)
    # An explicit build tries again: once it is stored, the record is gone.
    demo.build_fixture(root, scenario, SUNDAY, log=lambda line: None)
    assert demo.recorded_refusal(root, inputs) is None
    assert [p.name for p in (root / demo.FIXTURE_DIR).iterdir()] == [
        demo.fixture_path(root, inputs).name]


def test_a_bot_failing_on_a_past_day_is_a_recorded_refusal(tmp_path, monkeypatch, first_start):
    """A non-zero exit of the bot on a past session (here before its stop) is the build's
    refusal, not an interruption: recorded like the others, so no start builds it again."""
    root, previous = _stale_repo(tmp_path, first_start)
    short = _short_day(root)
    scenario = replace(short, past=(short.today,))
    inputs = demo.fixture_inputs(root, scenario)
    monkeypatch.setattr(demo.Session, "run", lambda self, world: 3)  # `tickr run` refused
    with pytest.raises(demo.FixtureRefused, match="exit code 3"):
        demo.build_fixture(root, scenario, SUNDAY, log=lambda line: None)
    assert "exit code 3" in demo.recorded_refusal(root, inputs)
    assert sorted(p.name for p in (root / demo.FIXTURE_DIR).iterdir()) == sorted(
        [previous.name, demo.refusal_path(root, inputs).name])
