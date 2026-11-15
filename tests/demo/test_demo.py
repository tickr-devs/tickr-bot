"""`tickr demo`: the real bot on a simulated exchange, in its own folder, never Webull."""

from __future__ import annotations

import http.client
import json
import re
import shutil
import sys
import threading
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

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

    def stop(cid, symbol):
        exchange.place(OrderRequest(cid, symbol, Side.SELL, OrderType.STOP_LOSS, D(10),
                                    TimeInForce.DAY, TradingSession.CORE, None, D(19)))

    stop("a-1", "AAA")
    stop("b-1", "BBB")
    exchange.unverify_stops("AAA")
    stop("a-2", "AAA")  # a later stop of the symbol is unverified too
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


def test_live_demo_day_ends_at_live_until(tmp_path):
    """The live demo day stops itself at [demo] live_until (exit 0), so the launchd job starts
    a fresh day in the same window."""
    root = repo(tmp_path)
    scenario = load(root / "config" / "demo.toml")
    cfg = config.load(demo.build_folder(root, scenario))
    scheduled: dict[datetime, object] = {}

    class FakeWorld:
        def at(self, when, action):
            scheduled[when] = action

    class Server:
        def shutdown(self):
            pass

    class Session:
        def __init__(self):
            self.scenario, self.day = scenario, FRIDAY

        def run(self, world):
            return 0

    assert demo._demo_day(Session(), FakeWorld(), cfg, Server(), lambda _: None) == 0
    assert scheduled[ny(FRIDAY, scenario.live_until)] is demo._stop_self
    assert ny(FRIDAY, scenario.clock) in scheduled  # going live comes first


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
    loop = LiveLoop(5, (LoopRole("up", ("AAA",), legs),),
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
    ('[[0, 2, "0.015", "40000"], [2, 4,', '[[0, 3, "0.015", "40000"], [2, 4,', "overlaps"),
    ('[4, 5, "-0.01", "15000"]', '[4, 11, "-0.01", "15000"]', "whole minute from 0 to 10"),
    ('[4, 5, "-0.01", "15000"]', '[4, 5, "-1", "15000"]', "above -1"),
    ("at_seconds = 285", "at_seconds = 600", "inside the cycle"),
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
    assert {"OWN_POSITION_OPEN", "ENTRY_STOP_SET"} <= refused
