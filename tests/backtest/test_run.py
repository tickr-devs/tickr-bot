"""Gainer gate and an end-to-end backtest run over generated bars."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

from tickr.backtest import fetch
from tickr.backtest.gate import RULE_GAINER, RULE_POOL, GainerGate
from tickr.backtest.report import bootstrap_interval, bot_view, price_bucket, stats
from tickr.backtest.run import run_backtest
from tickr.backtest.settings import load_settings
from tickr.backtest.universe import CapRecord, PoolEntry, write_caps, write_pool
from tickr.data.capture import bar_record
from tickr.replay.settings import load_settings as load_replay
from tickr.strategy.base import Decision, SymbolContext, Verdict
from tickr.strategy.price_action_v1 import dollar_volume_check
from tickr.types import Bar

from .helpers import ROOT, cfg, flat_bars, minute

PRIOR = [date(2026, 11, d) for d in (6, 9, 10, 11, 12)]
TODAY = date(2026, 11, 13)
SETTINGS = replace(load_settings(ROOT / "config" / "backtest.toml"), workers=1)


class Spy:
    name, version = "spy", "1"

    def __init__(self, floor: Decimal = Decimal(5_000_000)):
        self.asked = 0
        self.floor = floor

    def session_liquidity(self, session_bars):
        return (dollar_volume_check(session_bars, self.floor),)

    def evaluate(self, context):
        self.asked += 1
        return Decision(self.name, self.version, context.symbol, Verdict.WATCH, ())


def rising(symbol: str, start: str, step: str, count: int, volume: str) -> list[Bar]:
    bars, price = [], Decimal(start)
    for i in range(count):
        close = price + Decimal(step)
        bars.append(Bar(symbol, minute(TODAY, i), price, close + Decimal("0.05"),
                        price - Decimal("0.05"), close, Decimal(volume)))
        price = close
    return bars


def context(bars, prior):
    return SymbolContext("ABCD", tuple(bars), tuple(tuple(p) for p in prior))


def test_gate_asks_the_strategy_only_from_the_gainer_minute():
    prior = [flat_bars("ABCD", d, "20") for d in PRIOR]
    today = rising("ABCD", "20", "0.30", 10, "300000")  # +5% is 21.00: reached on bar 3
    spy = Spy()
    gate = GainerGate(spy, [("ABCD", TODAY)], Decimal(5))
    verdicts = [gate.evaluate(context(today[: i + 1], prior)) for i in range(10)]
    assert [d.checks[0].rule for d in verdicts[:3]] == [RULE_GAINER] * 3
    assert all(d.verdict is Verdict.WATCH for d in verdicts[3:]) and spy.asked == 7
    assert verdicts[0].strategy == "spy"
    other_day = GainerGate(spy, [("ABCD", PRIOR[-1])], Decimal(5))
    assert other_day.evaluate(context(today, prior)).checks[0].rule == RULE_POOL


def test_gate_waits_for_the_dollar_volume_floor():
    prior = [flat_bars("ABCD", d, "20") for d in PRIOR]
    today = rising("ABCD", "20", "0.30", 10, "1000")  # up 15% but tiny volume
    gate = GainerGate(Spy(), [("ABCD", TODAY)], Decimal(5))
    assert gate.evaluate(context(today, prior)).verdict is Verdict.IGNORE
    # The floor is the strategy's own: a strategy with a lower one is asked.
    low = Spy(Decimal(1000))
    assert GainerGate(low, [("ABCD", TODAY)], Decimal(5)).evaluate(
        context(today, prior)).verdict is Verdict.WATCH and low.asked == 1


def test_stats_interval_and_buckets():
    r = [Decimal(x) for x in ("1", "-1", "2", "-1")]
    s = stats(r)
    assert s["mean_r"] == Decimal("0.25") and s["profit_factor"] == Decimal("1.5")
    low, high = bootstrap_interval([r[:2], r[2:]], 500, 1, Decimal(95))
    assert low <= Decimal("0.25") <= high
    # One day per resample unit: two days give at most three distinct means.
    assert bootstrap_interval([r], 500, 1, Decimal(95)) is None


def test_holdout_start_is_the_last_share_of_sessions():
    from tickr.backtest.report import holdout_start

    days = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9)]
    assert holdout_start(days, Decimal(50)) == date(2026, 11, 5)
    assert holdout_start(days, Decimal(33)) == date(2026, 11, 9)
    edges = (Decimal(5), Decimal(20))
    assert [price_bucket(Decimal(p), edges) for p in ("1", "5", "25")] == ["<5", "5-20", ">=20"]


def write_symbol(out, symbol, bars):
    path = out / fetch.MINUTES_DIR / f"{symbol}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(bar_record(b)) + "\n" for b in bars))


def test_run_end_to_end(tmp_path):
    out, results = tmp_path / "data", tmp_path / "results"
    prior = [b for d in PRIOR for b in flat_bars("ABCD", d, "20", "3000")]
    collapse = [Bar("ABCD", minute(TODAY, 30 + i), Decimal(29), Decimal(29), Decimal(15),
                    Decimal(15), Decimal(30000)) for i in range(5)]
    write_symbol(out, "ABCD", prior + rising("ABCD", "20", "0.30", 30, "300000") + collapse)
    write_symbol(out, "GAP", flat_bars("GAP", TODAY))  # no history sessions
    write_symbol(out, "SPLIT", [b for d in PRIOR for b in flat_bars("SPLIT", d, "2", "3000")]
                 + flat_bars("SPLIT", TODAY, "20"))  # 1:10 reverse split overnight
    pool = [PoolEntry(TODAY, 1, "ABCD", Decimal(20), Decimal(15), Decimal(50), Decimal(10**8)),
            PoolEntry(TODAY, 2, "GAP", Decimal(20), Decimal(10), Decimal(50), Decimal(10**8)),
            PoolEntry(TODAY, 3, "NONE", Decimal(20), Decimal(20), Decimal(50), Decimal(10**8)),
            PoolEntry(TODAY, 4, "SPLIT", Decimal(20), Decimal(20), Decimal(50), Decimal(10**8))]
    write_pool(pool, out / "pool.csv")
    write_caps([CapRecord(TODAY, 4, 4)], out / "pool_cap.csv")
    run_backtest(cfg(), SETTINGS, load_replay(ROOT / "config" / "replay.toml"), out, results,
                 "tickr.strategy.price_action_v1",
                 ROOT / "strategies" / "price-action-v1" / "strategy.toml")
    with open(results / "trades.csv") as handle:
        trades = list(csv.DictReader(handle))
    assert trades and trades[0]["symbol"] == "ABCD" and trades[0]["quantity"]
    with open(results / "skipped.csv") as handle:
        causes = {r["symbol"]: r["cause"] for r in csv.DictReader(handle)}
    assert causes["NONE"] == "NO_MINUTE_FILE"
    assert causes["GAP"].startswith("SESSION_WITHOUT_BARS")
    assert causes["SPLIT"].startswith("SPLIT_BETWEEN_SESSIONS")
    report = json.loads((results / "report.json").read_text())
    assert report["label"] == "simulated" and report["bot"]["trades"] == len(trades)
    assert (results / "report.md").read_text().startswith("# SIMULATED backtest")


def test_bot_view_applies_live_sizing_positions_and_the_daily_ceiling():
    from tickr.replay.engine import ExitReason, SimulatedTrade

    def trade(symbol, start, end, entry, exit_, quantity="40"):
        return SimulatedTrade("s", "1", symbol, TODAY, "x", minute(TODAY, start),
                              minute(TODAY, start), Decimal(entry), Decimal(entry) - 1,
                              minute(TODAY, end), Decimal(exit_), ExitReason.STOP, 1,
                              Decimal(quantity), Decimal(entry))

    trades = [trade("A", 0, 10, "10", "5"),        # -200
              trade("B", 5, 8, "10", "11"),        # overlaps A
              trade("C", 11, 12, "10", "8"),       # -80: net -280
              trade("D", 13, 14, "10", "9"),       # room 20: sized down to 20 shares, -20
              trade("E", 15, 16, "10", "9", "60"),  # planned loss 60 > 50
              trade("F", 17, 18, "100", "99", "30"),  # notional 3000 > 2500
              trade("G", 19, 20, "10", "9")]       # room 0 < min_daily_room_usd
    one_slot = cfg()
    one_slot = replace(one_slot, risk=replace(one_slot.risk, max_open_positions=1))
    view = bot_view(trades, one_slot)  # B overlaps A: refused with one slot
    assert view["trades"] == 3 and view["total_usd"] == Decimal(-300)
    assert view["sized_down_to_daily_room"] == 1
    assert view["refused"] == {"DAILY_CEILING": 1, "NOTIONAL_OVER_CAP": 1,
                               "PLANNED_LOSS_OVER_LIMIT": 1, "POSITION_OPEN": 1}


def test_last_sessions_cuts_the_pool_to_the_latest_calendar_sessions():
    from tickr.backtest.errors import BacktestError
    from tickr.backtest.run import last_sessions
    from tickr.calendar import SessionCalendar

    calendar = SessionCalendar.load(ROOT / "config" / "calendar")

    def entry(day, symbol="ABCD"):
        return PoolEntry(day, 1, symbol, Decimal(20), Decimal(21), Decimal(50), Decimal(10**8))

    pool = [entry(date(2026, 10, 30)), entry(date(2026, 11, 6)), entry(TODAY),
            entry(TODAY, "WXYZ")]
    kept, window = last_sessions(calendar, pool, 5, 30)
    # Friday 11-13 back to Monday 11-09: five sessions, 11-06 is the sixth.
    assert window == [date(2026, 11, d) for d in (9, 10, 11, 12, 13)]
    assert kept == pool[2:]
    kept, window = last_sessions(calendar, pool, 1, 30)  # only the pool's last day
    assert window == [TODAY] and kept == pool[2:]
    kept, window = last_sessions(calendar, pool, 6, 30)
    assert window[0] == date(2026, 11, 6) and kept == pool[1:]
    kept, window = last_sessions(calendar, pool, 1, 30, date(2026, 11, 6))  # one chosen day
    assert window == [date(2026, 11, 6)] and kept == pool[1:2]
    try:
        last_sessions(calendar, pool, 1, 30, date(2026, 11, 5))
    except BacktestError as exc:
        assert "not a pool day" in str(exc)
    else:
        raise AssertionError("an end day outside the pool must be refused")
    for bad in (0, -1):
        try:
            last_sessions(calendar, pool, bad, 30)
        except BacktestError as exc:
            assert "positive" in str(exc)
        else:
            raise AssertionError("a count below 1 must be refused")


def test_a_bar_the_engine_refuses_skips_only_its_day(tmp_path):
    out, results = tmp_path / "data", tmp_path / "results"
    first = PRIOR[-1]  # BAD is a pool symbol on two days; only TODAY has the bad print
    history = [date(2026, 11, 5), *PRIOR]
    for symbol in ("ABCD", "BAD"):
        prior = [b for d in history[:-1] for b in flat_bars(symbol, d, "20", "3000")]
        earlier = [replace(b, start=minute(first, i)) for i, b in
                   enumerate(rising(symbol, "20", "0.30", 30, "300000"))]
        today = rising(symbol, "20", "0.30", 30, "300000")
        if symbol == "BAD":  # one 0.01 USD print: its synthetic bid is not positive
            cent = Decimal("0.01")
            today[10] = Bar(symbol, today[10].start, cent, cent, cent, cent, Decimal(100))
        write_symbol(out, symbol, prior + earlier + today)
    write_pool([PoolEntry(first, 1, "BAD", Decimal(20), Decimal(29), Decimal(50), Decimal(10**8)),
                PoolEntry(TODAY, 1, "ABCD", Decimal(29), Decimal(29), Decimal(50), Decimal(10**8)),
                PoolEntry(TODAY, 2, "BAD", Decimal(29), Decimal(29), Decimal(50), Decimal(10**8))],
               out / "pool.csv")
    write_caps([CapRecord(first, 1, 1), CapRecord(TODAY, 2, 2)], out / "pool_cap.csv")
    run_backtest(cfg(), SETTINGS, load_replay(ROOT / "config" / "replay.toml"), out, results,
                 "tickr.strategy.price_action_v1",
                 ROOT / "strategies" / "price-action-v1" / "strategy.toml")
    with open(results / "skipped.csv") as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["symbol"], r["session_date"]) for r in rows] == [("BAD", TODAY.isoformat())]
    assert rows[0]["cause"].startswith("REPLAY_REFUSED") and "synthetic bid" in rows[0]["cause"]
    with open(results / "trades.csv") as handle:
        traded = {(t["symbol"], t["session_date"]) for t in csv.DictReader(handle)}
    assert ("BAD", first.isoformat()) in traded and ("BAD", TODAY.isoformat()) not in traded
    summary = json.loads((results / "summary.json").read_text())
    assert summary["sessions"] == 2 * (len(history) + 1)  # each file once: history + TODAY


def test_run_last_sessions_reports_its_window(tmp_path):
    out, results = tmp_path / "data", tmp_path / "results"
    prior = [b for d in PRIOR for b in flat_bars("ABCD", d, "20", "3000")]
    write_symbol(out, "ABCD", prior + rising("ABCD", "20", "0.30", 30, "300000"))
    old = date(2026, 10, 1)  # outside the window: never replayed, never reported as skipped
    write_pool([PoolEntry(old, 1, "OLD", Decimal(20), Decimal(21), Decimal(50), Decimal(10**8)),
                PoolEntry(TODAY, 1, "ABCD", Decimal(20), Decimal(29), Decimal(50),
                          Decimal(10**8))], out / "pool.csv")
    write_caps([CapRecord(old, 1, 1), CapRecord(TODAY, 1, 1)], out / "pool_cap.csv")
    run_backtest(cfg(), SETTINGS, load_replay(ROOT / "config" / "replay.toml"), out, results,
                 "tickr.strategy.price_action_v1",
                 ROOT / "strategies" / "price-action-v1" / "strategy.toml", 3)
    report = json.loads((results / "report.json").read_text())
    assert report["last_sessions"] == {"requested": 3, "first": "2026-11-11",
                                       "last": TODAY.isoformat(),
                                       "sessions_without_replayed_days": 2}
    assert report["window"] == [TODAY.isoformat(), TODAY.isoformat()]
    assert report["pool"]["symbol_days"] == 1 and report["pool"]["skipped"] == {}
    text = (results / "report.md").read_text()
    assert "Last 3 sessions only: 2026-11-11 to 2026-11-13, 2 of them without" in text
    assert "wide by construction" in text
    # The Lab's summary (GET /lab/replay) reads this report.
    from tickr.api.lab_replay import summarize

    summary = summarize(report)
    assert (summary.first_day, summary.last_day, summary.sessions) == (
        date(2026, 11, 11), TODAY, 3)
    assert summary.sessions_without_replayed_days == 2 and summary.replayed_symbol_days == 1
    assert summary.bot_trades == report["bot"]["trades"] <= summary.trades


def test_an_incomplete_warm_up_or_target_session_refuses_the_symbol(tmp_path):
    from tickr.backtest.run import tradable_days
    from tickr.calendar import SessionCalendar

    out = tmp_path / "data"
    warm = PRIOR[-1]  # PAGE_LIMIT kept one bar of the 12th; later nights fetch only the 13th
    for symbol in ("WARM", "LAST", "GOOD"):
        history = [b for d in PRIOR for b in flat_bars(
            symbol, d, "20", "3000", 1 if symbol == "WARM" and d == warm else 390)]
        write_symbol(out, symbol, history + flat_bars(symbol, TODAY, "20"))
    statuses = [{"symbol": "WARM", "first_session": warm.isoformat(),
                 "last_session": warm.isoformat(), "complete": False,
                 "cause": "PAGE_LIMIT: 1 pages"},
                {"symbol": "WARM", "first_session": TODAY.isoformat(),
                 "last_session": TODAY.isoformat(), "complete": True, "cause": ""},
                {"symbol": "LAST", "first_session": PRIOR[0].isoformat(),
                 "last_session": TODAY.isoformat(), "complete": False,
                 "cause": "PAGE_LIMIT: 3 pages"}]
    fetch._write_lines(out / fetch.MINUTES_STATUS_FILE, statuses)
    pool = [PoolEntry(TODAY, i, s, Decimal(20), Decimal(20), Decimal(50), Decimal(10**8))
            for i, s in enumerate(("WARM", "LAST", "GOOD"), start=1)]
    days, skipped = tradable_days(cfg(), SessionCalendar.load(ROOT / "config" / "calendar"),
                                  pool, out, len(PRIOR), Decimal(1))
    assert days == {"GOOD": [TODAY]}
    causes = {r["symbol"]: r["cause"] for r in skipped}
    assert causes["WARM"] == "FETCH_INCOMPLETE: 2026-11-12 PAGE_LIMIT: 1 pages"
    assert causes["LAST"].startswith("FETCH_INCOMPLETE: 2026-11-06 PAGE_LIMIT")
    assert causes["LAST"].endswith("2026-11-13 PAGE_LIMIT: 3 pages")


def test_the_holdout_splits_on_every_session_not_only_those_with_candidates(tmp_path):
    out, results = tmp_path / "data", tmp_path / "results"
    prior = [b for d in PRIOR for b in flat_bars("ABCD", d, "20", "3000")]
    write_symbol(out, "ABCD", prior + rising("ABCD", "20", "0.30", 30, "300000"))
    write_pool([PoolEntry(PRIOR[0], 1, "OLD", Decimal(20), Decimal(21), Decimal(50),
                          Decimal(10**8)),
                PoolEntry(TODAY, 1, "ABCD", Decimal(20), Decimal(29), Decimal(50),
                          Decimal(10**8))], out / "pool.csv")
    # Ten sessions 11-02 .. 11-13 were selected; only two had a candidate.
    quiet = [date(2026, 11, d) for d in (2, 3, 4, 5, 9, 10, 11, 12)]
    write_caps(sorted([CapRecord(d, 0, 0) for d in quiet]
                      + [CapRecord(PRIOR[0], 1, 1), CapRecord(TODAY, 1, 1)],
                      key=lambda c: c.session_date), out / "pool_cap.csv")
    run_backtest(cfg(), SETTINGS, load_replay(ROOT / "config" / "replay.toml"), out, results,
                 "tickr.strategy.price_action_v1",
                 ROOT / "strategies" / "price-action-v1" / "strategy.toml")
    holdout = json.loads((results / "report.json").read_text())["holdout"]
    # The last 33% of ten sessions is three: 11-11 .. 11-13, whatever days had candidates.
    assert holdout["starts"] == "2026-11-11" and holdout["calendar_sessions"] == 10
