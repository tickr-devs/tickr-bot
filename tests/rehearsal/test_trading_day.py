"""Dress rehearsal: a whole trading day through the real `tickr run` on a simulated exchange.

Monday 2026-11-16. Two symbols appear on the gainers screener:
- AAA breaks out at the open, is bought, and later collapses through its stop (stopped out);
- BBB breaks out late morning, is bought, survives a bot restart while held (stop resting at
  the broker), and is closed by the session flatten at 15:50.
The operator stops and re-allows entries through the command inbox in between.
"""

from __future__ import annotations

from decimal import Decimal as D

from rehearsal.harness import DAY, Rehearsal, history
from rehearsal.world import build_day
from tickr.api import contract as api

HOT = D(30000)  # shares per minute while a symbol moves: ten times its prior sessions


def scenario(r: Rehearsal) -> None:
    w = r.world
    w.add_symbol("AAA", build_day("AAA", DAY, D(20), D(3000), [
        ("09:30", "09:45", D("24.50"), HOT),   # breakout ramp; flat afterwards
        ("10:30", "10:32", D(21), HOT),        # collapse through the stop
    ]), history("AAA", D(20)))
    w.add_symbol("BBB", build_day("BBB", DAY, D(30), D(3000), [
        ("11:00", "11:15", D("34.50"), HOT),   # breakout ramp; holds until the close
        ("13:00", "13:30", D("35.50"), D(3000)),
    ]), history("BBB", D(30)))


def test_full_day(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch)
    scenario(r)
    seen: dict[str, api.StatusV1] = {}
    r.at("10:40", lambda: r.command(api.CommandKind.ENTRY_STOP_ON))
    r.at("10:45", lambda: seen.setdefault("entry stop", r.status()))
    r.at("10:50", lambda: r.command(api.CommandKind.ENTRY_STOP_OFF))
    r.at("11:30", lambda: seen.setdefault("holding", r.status()))

    assert r.run_until("12:00") == 0   # SIGTERM while BBB is held: its stop stays resting
    r.bot_down_until("12:05")
    assert r.run_until("16:05") == 0

    aaa, bbb = r.trades()
    assert (aaa["symbol"], aaa["close_cause"]) == ("AAA", "stopped-out")
    assert (bbb["symbol"], bbb["close_cause"]) == ("BBB", "session-flatten")
    assert bbb["closed_at"] >= "2026-11-16T20:50:00"  # 15:50 New York

    # The broker saw exactly: entry, stop (filled) for AAA; entry, stop, close for BBB.
    assert [(side, kind) for _, side, kind, _, _ in r.fills("AAA")] == [
        ("BUY", "LIMIT"), ("SELL", "STOP_LOSS")]
    assert [(side, kind) for _, side, kind, _, _ in r.fills("BBB")] == [
        ("BUY", "LIMIT"), ("SELL", "MARKET")]
    kinds = [(o.side, o.order_type) for o in r.world.exchange.sent]
    assert kinds == [("BUY", "LIMIT"), ("SELL", "STOP_LOSS"),
                     ("BUY", "LIMIT"), ("SELL", "STOP_LOSS"), ("SELL", "MARKET")]
    assert r.world.exchange.holdings == {}

    # Two runs: the restart adopted the held position and its resting stop, sent nothing new.
    runs = r.journal("run-ended")
    assert [row.body["cause"] for row in runs] == ["stopped (SIGTERM)"] * 2
    assert len(r.journal("shutdown-clean")) == 2

    holding = seen["holding"]
    assert holding.position is not None and holding.position.symbol == "BBB"
    assert holding.position.stop.state is api.StopState.VERIFIED
    assert holding.freshness.stream is api.StreamHealth.FRESH
    assert seen["entry stop"].entry_stop
    assert seen["entry stop"].entry_block == api.EntryBlock(
        "entry_stop", "Entries stopped by the operator.")

    final = r.status()
    assert final.bot_state is api.BotState.STOPPED and final.position is None
    assert [t.symbol for t in final.trades_today] == ["AAA", "BBB"]
    assert final.result_today_gross_usd == sum(t.result_gross_usd for t in final.trades_today)
    assert [c.kind for c in final.recent_commands] == [api.CommandKind.ENTRY_STOP_OFF,
                                                       api.CommandKind.ENTRY_STOP_ON]

    taken = [d for d in r.decisions() if d["admission"] == "taken"]
    assert [d["symbol"] for d in taken] == ["AAA", "BBB"]
    assert r.notifier.shown == []  # a normal day raises no alert
    assert (r.config.root / r.config.run.data_dir / r.config.run.backups_dir
            / f"tickr-{DAY.isoformat()}.sqlite3").exists()
