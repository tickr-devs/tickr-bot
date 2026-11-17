"""More rehearsals through the real `tickr run`: partial fills, the exit teardown and its
cancel-vs-fill races, the daily ceiling over a day with a win, the per-symbol pause, the
unprotected-position safeguard and a partial entry at the end of the day. Restarts are in
test_scenarios_restarts.py, odd broker answers in test_scenarios_broker.py.

Every scenario uses the price shapes of tests/rehearsal: a 15-minute hot ramp from 20 to 24.50
that price-action-v1 buys at 24.51 with a stop at 22.65 (26 shares, about 48 USD planned).
The broker is `kit.ScriptedExchange`; nothing in src/ is changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from rehearsal.harness import Rehearsal
from rehearsal.kit import (
    HOT,
    Piece,
    add,
    alert_kinds,
    at,
    breakout,
    clear_fence,
    fence_names,
    gap,
    gap_leg,
    records_agree,
    scripted,
    sent,
    strategy_exits,
    without_fsync,
)
from tickr.types import OrderState, OrderType, Side


@pytest.fixture(autouse=True)
def _fast_files(monkeypatch):
    without_fsync(monkeypatch)


# --- partial fills, stops sized on Detail, strategy EXIT and the teardown -------------------


def test_partial_entry_is_protected_piece_by_piece_and_a_strategy_exit_closes_it(
        tmp_path, monkeypatch):
    """The entry fills 10, then 16 two minutes later: one stop per fill observation, sized on
    Detail. The strategy's EXIT tears down (each stop cancelled and shown CANCELLED before any
    share it protected is sold; the closing orders sell exactly what is held); the win pauses
    nothing."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"), ("10:00", "10:10", D("25.50"), HOT))
    ex.pieces["AAA"] = [Piece(D(10)), Piece(None, at("09:47"))]  # 10 now, 16 two minutes later
    strategy_exits(monkeypatch, {"AAA": "10:05"})
    assert r.run_until("10:08:30") == 0

    first, *later = r.trades()
    assert (first["symbol"], first["close_cause"]) == ("AAA", "strategy-exit")
    # One stop per fill observation, each for exactly what Detail showed filled.
    stops = [o for o in ex.sent if o.order_type is OrderType.STOP_LOSS
             and "-t1-" in o.client_order_id]
    assert [s.quantity for s in stops] == [D(10), D(16)]
    assert all(s.stop_price == D(first["stop_price"]) for s in stops)
    # Teardown: every stop cancelled and shown CANCELLED (its own observation) before the
    # shares it protected are sold, and the closing orders together sell exactly the 26 held.
    rows = r.journal()
    seq = {(row.kind, row.key): row.seq for row in rows}
    closes = [o for o in ex.sent if o.order_type is OrderType.MARKET
              and "-t1-" in o.client_order_id]
    assert sum(o.quantity for o in closes) == D(26)
    for close in closes:
        intent = seq[("order-intent", close.client_order_id)]
        assert any(seq[("cancel-sent", s.client_order_id)] < intent for s in stops)
    shown_cancelled = {}
    for stop in stops:
        own = [row.seq for row in rows if row.kind == "order-observed"
               and row.key == stop.client_order_id and row.body["state"] == "CANCELLED"]
        assert own, stop.client_order_id
        shown_cancelled[stop.client_order_id] = min(own)
    for close in closes:  # sold so far never exceeds what stops shown CANCELLED had covered
        intent = seq[("order-intent", close.client_order_id)]
        freed = sum(s.quantity for s in stops if shown_cancelled[s.client_order_id] < intent)
        sold = sum(c.quantity for c in closes
                   if seq[("order-intent", c.client_order_id)] <= intent)
        assert sold <= freed, (close.client_order_id, sold, freed)
    assert first["exit_requested"] == "strategy-exit"
    # A winning close pauses nothing: AAA is bought again at the next signal.
    assert later and later[0]["symbol"] == "AAA"
    assert alert_kinds(r) == []
    status = r.status()
    (win, *_) = status.trades_today
    assert win.exit_reason == "strategy-exit" and win.result_gross_usd > 0
    sold = sum(q * p for when, _, side, kind, q, p in ex.fills
               if side is Side.SELL and kind is OrderType.MARKET and when <= at("10:06:30"))
    # (the result is computed from average prices: equal to the cent)
    assert abs(win.result_gross_usd - (sold - D(26) * D("24.51"))) < D("0.01")


def test_the_stop_fills_while_the_exit_cancels_it(tmp_path, monkeypatch):
    """Cancel-vs-fill race: the stop fills at Webull just before the cancel arrives; the cancel
    is refused as too late. Nothing else is sold and the trade closes flat."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    strategy_exits(monkeypatch, {"AAA": "09:50"})

    def race(cid: str) -> None:
        if ex.orders[cid].order_type is OrderType.STOP_LOSS:
            ex.fill_now(cid)

    ex.before_cancel.append(race)
    assert r.run_until("09:53") == 0
    (trade,) = r.trades()
    assert trade["state"] == "closed"
    assert sent(ex) == [(Side.BUY, OrderType.LIMIT, D(26)), (Side.SELL, OrderType.STOP_LOSS, D(26))]
    records_agree(r, ex)
    assert r.status().position is None
    assert [f.kind for f in r.status().fences] == ["shutting-down"]  # until the next start


def test_a_cancel_accepted_too_late_and_a_partial_stop_fill_sell_only_the_rest(
        tmp_path, monkeypatch):
    """Two stops (the entry filled 10 then 16). During the exit the 10-share stop fills although
    Webull accepted its cancel, and the 16-share stop had already sold 6 before its cancel
    took. Only the 10 still held are sold."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    ex.pieces["AAA"] = [Piece(D(10)), Piece(None, at("09:47"))]
    ex.late_cancel_accepted = True
    strategy_exits(monkeypatch, {"AAA": "09:50"})

    def race(cid: str) -> None:
        order = ex.orders[cid]
        if order.order_type is not OrderType.STOP_LOSS:
            return
        ex.fill_now(cid, D(10) if order.quantity == D(10) else D(6))

    ex.before_cancel.append(race)
    assert r.run_until("09:54") == 0
    (trade,) = r.trades()
    assert trade["state"] == "closed"
    closes = ex.of("AAA", OrderType.MARKET)
    assert [c.quantity for c in closes] == [D(10)]
    records_agree(r, ex)


# --- the daily loss ceiling over a day with a win ---------------------------------------------


def test_a_win_offsets_losses_then_trades_shrink_and_stop_at_the_ceiling(tmp_path, monkeypatch):
    """Owner decisions 2026-11-14: the daily rule counts the net realized result (a win offsets
    losses); near the ceiling a trade is sized down to the room left; below 10 USD of room
    entries stop and status says why. A gap through a stop may take the realized loss past
    the ceiling (accepted); a planned loss never does."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"), gap_leg("09:50", "09:51", "18"))
    add(r, "CCC", "20", breakout("09:38", "09:53", "24.50"), ("09:53", "09:58", D("26.50"), HOT))
    add(r, "BBB", "20", breakout("09:47", "10:02", "24.50"), gap_leg("10:06", "10:07", "18"))
    add(r, "DDD", "20", breakout("09:53", "10:08", "24.50"), gap_leg("10:12", "10:13", "18"))
    add(r, "EEE", "20", breakout("09:58", "10:13", "24.50"))
    for symbol, clock in (("AAA", "09:50"), ("BBB", "10:06"), ("DDD", "10:12")):
        gap(r, symbol, clock)
    strategy_exits(monkeypatch, {"CCC": "09:58"})
    seen = {}
    r.at("10:14", lambda: seen.setdefault("late", r.status()))
    assert r.run_until("10:16") == 0

    trades = r.trades()
    assert [t["symbol"] for t in trades] == ["AAA", "CCC", "BBB", "DDD"]  # EEE: no room
    results = {t.symbol: t.result_gross_usd for t in r.status().trades_today}
    assert results["CCC"] > 0 and all(results[s] < 0 for s in ("AAA", "BBB", "DDD"))
    risk = r.config.risk
    # Without CCC's win BBB's loss would have left no room for DDD at all.
    assert -(results["AAA"] + results["BBB"]) > risk.max_daily_loss_usd - risk.min_daily_room_usd
    # Each planned loss fitted the room the net result left when it was opened.
    net = D(0)
    for trade in trades:
        room = risk.max_daily_loss_usd - max(-net, D(0))
        assert D(trade["planned_loss_usd"]) <= room
        net += results[trade["symbol"]]
    ddd = trades[-1]
    assert D(ddd["planned_loss_usd"]) < risk.planned_loss_usd - 1  # sized down, not refused
    assert D(ddd["entry_quantity"]) < D(trades[0]["entry_quantity"])
    # The gap through DDD's stop took the realized loss past the ceiling: alert, no entries.
    assert -net > risk.max_daily_loss_usd
    assert alert_kinds(r) == ["daily-ceiling-reached"]
    block = seen["late"].entry_block
    assert block is not None and block.cause == "daily_ceiling"
    eee = [d for d in r.decisions() if d["symbol"] == "EEE" and d["verdict"] == "ENTER"]
    assert eee and all(d["admission"] == "refused" for d in eee)
    assert eee[-1]["refusal_cause"].startswith("DAILY_CEILING")
    assert not ex.of("EEE")
    records_agree(r, ex)


# --- the per-symbol pause after a loss ---------------------------------------------------------


def test_a_symbol_pauses_30_minutes_after_a_loss_others_trade_on(tmp_path, monkeypatch):
    """AAA is stopped out at 09:50, then runs up again all morning. Its signals are refused for
    30 minutes (also across a bot restart) while BBB is bought and stopped out meanwhile; the
    first AAA signal after the pause is taken."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"), ("09:50", "09:51", D(21), HOT),
        ("09:52", "10:30", D("32.40"), HOT))
    add(r, "BBB", "20", breakout("09:49", "10:04", "24.50"), ("10:08", "10:09", D(21), HOT))
    assert r.run_until("10:12") == 0
    assert r.run_until("10:22") == 0

    first, bbb, again = r.trades()
    assert (first["symbol"], first["close_cause"]) == ("AAA", "stopped-out")
    assert (bbb["symbol"], bbb["close_cause"]) == ("BBB", "stopped-out")
    assert again["symbol"] == "AAA"
    loss_at = datetime.fromisoformat(first["closed_at"])
    pause = timedelta(minutes=r.config.risk.symbol_cooldown_minutes_after_loss)
    aaa = [d for d in r.decisions() if d["symbol"] == "AAA" and d["verdict"] == "ENTER"
           and datetime.fromisoformat(d["time"]) > loss_at]
    paused = [d for d in aaa if d["refusal_cause"]
              and d["refusal_cause"].startswith("SYMBOL_COOLDOWN")]
    # Refused at about +10 minutes, while nothing else was held, and on after the restart.
    assert any(datetime.fromisoformat(d["time"]) - loss_at <= timedelta(minutes=10)
               for d in paused)
    assert any(datetime.fromisoformat(d["time"]) > datetime.fromisoformat(bbb["closed_at"])
               for d in paused)
    assert all(datetime.fromisoformat(d["time"]) < loss_at + pause for d in paused)
    taken = [d for d in aaa if d["admission"] == "taken"]
    assert len(taken) == 1 and datetime.fromisoformat(taken[0]["time"]) >= loss_at + pause
    assert datetime.fromisoformat(again["opened_at"]) - (loss_at + pause) < timedelta(minutes=1)
    # BBB was bought while AAA paused: the pause is per symbol.
    assert loss_at < datetime.fromisoformat(bbb["opened_at"]) < loss_at + pause
    assert len(ex.of("AAA", OrderType.LIMIT)) == 2


# --- the unprotected-position safeguard ---------------------------------------------------------


@pytest.mark.parametrize("fault", ["stop-refused", "stop-never-working"])
def test_no_working_stop_exits_alerts_and_blocks_entries_until_the_operator_clears(
        tmp_path, monkeypatch, fault):
    """Spec 9: a rejected stop is retried per config, then the position is exited; a stop that
    Detail never shows working is the same once `max_unprotected_seconds` pass. Either way the
    operator is alerted and entries stay blocked until he clears the fence."""
    r = Rehearsal(tmp_path, monkeypatch, start="09:43")
    ex = scripted(r)
    add(r, "AAA", "20", breakout("09:30", "09:45", "24.50"))
    add(r, "BBB", "20", breakout("09:44", "10:10", "27.80"))  # signals from about 09:56 on
    retries = r.config.orders.stop_retry_attempts
    if fault == "stop-refused":
        ex.script("ack", *["refuse"] * (retries + 1))
        fence, alert = "stop-rejected:t1", "stop-rejected"
    else:
        ex.unverify_stops("AAA")
        fence, alert = "unprotected-timeout:t1", "unprotected-position"
    seen = {}
    r.at("09:58:30", lambda: seen.setdefault("fenced", r.status()))
    assert r.run_until("10:00") == 0

    (aaa,) = r.trades()
    assert aaa["close_cause"] == ("stop-rejected" if fault == "stop-refused"
                                  else "unprotected-too-long")
    closes = ex.of("AAA", OrderType.MARKET)
    assert [c.quantity for c in closes] == [D(26)]
    stops = ex.of("AAA", OrderType.STOP_LOSS)
    assert len(stops) == (retries + 1 if fault == "stop-refused" else 1)
    if fault == "stop-never-working":
        # Exited once the position had no verified stop for max_unprotected_seconds.
        unprotected = float(r.config.orders.max_unprotected_seconds)
        took = (datetime.fromisoformat(r.journal("exit-requested")[0].at_utc)
                - datetime.fromisoformat(aaa["opened_at"])).total_seconds()
        assert unprotected <= took <= unprotected + 2 * float(r.config.orders.detail_poll_seconds)
    assert alert in alert_kinds(r)
    assert fence in fence_names(r)
    block = seen["fenced"].entry_block
    assert block is not None and block.cause == fence.split(":")[0].replace("-", "_")
    bbb = [d for d in r.decisions() if d["symbol"] == "BBB" and d["verdict"] == "ENTER"]
    assert bbb and all(d["admission"] == "refused" for d in bbb)
    assert not ex.of("BBB")

    assert clear_fence(r, monkeypatch, fence) == 0
    assert r.run_until("10:04") == 0
    assert [t["symbol"] for t in r.trades()] == ["AAA", "BBB"]
    assert len(ex.of("BBB", OrderType.LIMIT)) == 1


# --- the end of the day ------------------------------------------------------------------------


def test_a_partial_entry_at_the_last_entry_time_is_protected_then_flattened(tmp_path, monkeypatch):
    """BBB is bought at about 15:34 but only 10 of the shares fill. At the 15:35 last-entry
    time the rest of the entry is cancelled (never re-priced), the 10 stay protected, and the
    15:45 flatten closes exactly those 10. The day ends flat and clean."""
    r = Rehearsal(tmp_path, monkeypatch, start="15:32")  # first signal at the ramp top
    ex = scripted(r)
    # Late in the day only a much hotter run makes the cumulative relative volume pass.
    add(r, "BBB", "20", ("15:18", "15:33", D("24.50"), HOT * 10))
    ex.pieces["BBB"] = [Piece(D(10)), Piece(None, at("16:30"))]  # the rest would never fill
    assert r.run_until("15:54") == 0  # the day-end check reads the broker 500 s after 15:45

    (trade,) = r.trades()
    assert trade["close_cause"] == "session-flatten"
    assert at("15:30").isoformat() < trade["opened_at"] < at("15:35").isoformat()
    (entry,) = ex.of("BBB", OrderType.LIMIT)
    assert ex.orders[entry.client_order_id].state is OrderState.CANCELLED
    (cancel,) = r.journal("entry-cancel")
    assert "last entry time" in cancel.body["cause"]
    assert [s.quantity for s in ex.of("BBB", OrderType.STOP_LOSS)] == [D(10)]
    (close,) = ex.of("BBB", OrderType.MARKET)
    assert close.quantity == D(10)
    assert at("15:45").isoformat() <= trade["closed_at"] < at("15:46").isoformat()
    records_agree(r, ex)
    (day_end,) = r.journal("day-end")
    assert day_end.body["day_clean"] and alert_kinds(r) == []
