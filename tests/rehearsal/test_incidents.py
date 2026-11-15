"""Rehearsal: the bad moments of a trading day, each through the real `tickr run`.

AAA breaks out at the open and is bought at 09:45 with a stop at 22.65 in every scenario; what
goes wrong afterwards differs.
"""

from __future__ import annotations

from decimal import Decimal as D

from rehearsal.harness import DAY, Rehearsal, history
from rehearsal.world import build_day
from tickr.broker.base import OutcomeUnknown

HOT = D(30000)
RAMP = ("09:30", "09:45", D("24.50"), HOT)  # breakout; bought at 09:45, stop 22.65


class Crash(BaseException):
    """The process dies (kill -9, power): no drain, no orderly shutdown."""


def aaa(r: Rehearsal, *later: tuple[str, str, D, D]) -> None:
    r.world.add_symbol("AAA", build_day("AAA", DAY, D(20), D(3000), [RAMP, *later]),
                       history("AAA", D(20)))


def causes(r: Rehearsal) -> list[str | None]:
    return [t["close_cause"] for t in r.trades()]


def alert_kinds(r: Rehearsal) -> list[str]:
    return [row.body["kind"] for row in r.journal("alert")]


def test_the_stop_fills_while_the_bot_is_down(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch)
    aaa(r, ("10:10", "10:12", D(21), HOT))
    assert r.run_until("10:05") == 0
    r.bot_down_until("10:20")  # the collapse happens now; the resting stop fills at the broker
    assert r.run_until("10:30") == 0
    assert causes(r) == ["stopped-out"]
    assert len(r.world.exchange.sent) == 2  # entry and stop: the restart sent nothing
    assert r.status().trades_today[0].exit_reason == "stopped-out"


def test_a_crash_with_a_position_open(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch)
    aaa(r, ("10:30", "10:32", D(21), HOT))

    def die() -> None:
        raise Crash

    r.at("10:00", die)
    try:
        r.run_until("10:05")
    except Crash:
        pass
    r.world.events.clear()
    r.bot_down_until("10:02")
    assert r.run_until("10:40") == 0  # the lock of the dead process does not block
    assert [row.body["cause"] for row in r.journal("run-ended")] == [
        "crashed", "stopped (SIGTERM)"]
    assert causes(r) == ["stopped-out"]
    assert len(r.world.exchange.sent) == 2


def test_the_stream_is_lost_with_a_position(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch)
    aaa(r)

    def down() -> None:
        r.world.stream_up = False
        r.world.feed.transport(False, r.world.now, "network")

    def up() -> None:
        r.world.stream_up = True
        r.world.feed.transport(True, r.world.now, "back")

    r.at("10:00", down)
    r.at("10:10", up)
    assert r.run_until("10:20") == 0
    (trade,) = r.trades()
    assert trade["close_cause"] == "stream-lost"
    lost = float(r.config.market_data.stream_lost_exit_seconds)
    assert trade["closed_at"] < f"2026-11-16T15:00:{int(lost) + 15}"
    assert [k for _, _, k, _, _ in r.fills("AAA")] == ["LIMIT", "MARKET"]
    assert alert_kinds(r) == ["stream-lost-with-position"]


def test_broker_reads_fail_for_twenty_minutes(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch)
    aaa(r, ("10:10", "10:12", D(21), HOT))
    exchange = r.world.exchange
    working = exchange.positions
    outage = {"on": False}

    def positions():
        if outage["on"]:
            raise OutcomeUnknown("timeout")
        return working()

    monkeypatch.setattr(exchange, "positions", positions)
    r.at("10:00", lambda: outage.update(on=True))
    r.at("10:20", lambda: outage.update(on=False))
    assert r.run_until("10:30") == 0
    assert causes(r) == ["stopped-out"]  # the broker-side stop protected it meanwhile
    assert alert_kinds(r) == ["broker-reads-failing"]
    assert not any(f.kind == "broker-read-failed" for f in r.status().fences)


def test_the_broker_refuses_the_stop_and_the_close(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch)
    aaa(r)
    retries = r.config.orders.stop_retry_attempts
    r.world.exchange.script("ack", *["refuse"] * (retries + 1), "refuse")
    assert r.run_until("09:50") == 5  # the drain cannot leave it protected: exit 5
    (trade,) = r.trades()
    assert trade["close_cause"] is None and r.world.exchange.holdings["AAA"].quantity == 26
    shown = [(row.body["kind"], row.body["text"]) for row in r.journal("alert")]
    (exit_refused,) = [text for kind, text in shown if kind == "exit-refused"]
    assert "NO broker stop" in exit_refused
    (timeout,) = [text for kind, text in shown if text.startswith("unprotected-timeout")]
    assert "closing order was refused" in timeout and "exiting" not in timeout
    assert not any("protection restored" in text for _, text in shown)


def test_near_the_daily_ceiling_trades_shrink_then_stop(tmp_path, monkeypatch):
    """Owner decision 2026-11-14: near the ceiling a trade is sized down to the room left;
    below `min_daily_room_usd` entries stop and status says so."""
    r = Rehearsal(tmp_path, monkeypatch)

    def hm(minute: int) -> str:
        return f"{minute // 60:02d}:{minute % 60:02d}"

    names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]
    for i, symbol in enumerate(names):
        start = 9 * 60 + 30 + 40 * i  # one breakout every 40 minutes, each collapses
        r.world.add_symbol(symbol, build_day(symbol, DAY, D(20), D(3000), [
            (hm(start), hm(start + 15), D("24.50"), D(200000)),
            (hm(start + 25), hm(start + 26), D(19), HOT)]), history(symbol, D(20)))
    seen = {}
    r.at("13:10", lambda: seen.setdefault("status", r.status()))  # FFF held
    assert r.run_until("14:00") == 0
    trades = r.trades()
    assert [t["symbol"] for t in trades] == names[:6]  # GGG: no room left
    assert causes(r) == ["stopped-out"] * 6
    risk = r.config.risk
    full, small = trades[0], trades[5]
    realized_before_fff = sum(t.result_gross_usd for t in r.status().trades_today[:5])
    room = risk.max_daily_loss_usd + realized_before_fff
    assert D(small["planned_loss_usd"]) <= room < risk.planned_loss_usd
    assert D(small["entry_quantity"]) < D(full["entry_quantity"])
    block = seen["status"].entry_block
    assert block is not None and block.cause == "daily_ceiling"  # the app does not say "open"
    assert "GGG" not in {s for _, s, *_ in r.world.exchange.fills}
    # GGG's breakout is still an ENTER; the platform refuses it with the typed cause.
    ggg = [d for d in r.decisions() if d["symbol"] == "GGG" and d["verdict"] == "ENTER"]
    assert ggg and all(d["admission"] == "refused" for d in ggg)
    assert all(d["refusal_cause"].startswith("DAILY_CEILING") for d in ggg)
