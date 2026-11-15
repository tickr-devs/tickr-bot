"""Gainer gate and an end-to-end backtest run over generated bars."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

from aperture.backtest import fetch
from aperture.backtest.gate import RULE_GAINER, RULE_POOL, GainerGate
from aperture.backtest.report import bootstrap_interval, bot_view, price_bucket, stats
from aperture.backtest.run import run_backtest
from aperture.backtest.settings import load_settings
from aperture.backtest.universe import CapRecord, PoolEntry, write_caps, write_pool
from aperture.data.capture import bar_record
from aperture.replay.settings import load_settings as load_replay
from aperture.strategy.base import Decision, SymbolContext, Verdict
from aperture.types import Bar

from .helpers import ROOT, cfg, flat_bars, minute

PRIOR = [date(2026, 11, d) for d in (6, 9, 10, 11, 12)]
TODAY = date(2026, 11, 13)
SETTINGS = replace(load_settings(ROOT / "config" / "backtest.toml"), workers=1)


class Spy:
    name, version = "spy", "1"

    def __init__(self):
        self.asked = 0

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
    gate = GainerGate(spy, [("ABCD", TODAY)], Decimal(5), Decimal(5_000_000))
    verdicts = [gate.evaluate(context(today[: i + 1], prior)) for i in range(10)]
    assert [d.checks[0].rule for d in verdicts[:3]] == [RULE_GAINER] * 3
    assert all(d.verdict is Verdict.WATCH for d in verdicts[3:]) and spy.asked == 7
    assert verdicts[0].strategy == "spy"
    other_day = GainerGate(spy, [("ABCD", PRIOR[-1])], Decimal(5), Decimal(1))
    assert other_day.evaluate(context(today, prior)).checks[0].rule == RULE_POOL


def test_gate_waits_for_the_dollar_volume_floor():
    prior = [flat_bars("ABCD", d, "20") for d in PRIOR]
    today = rising("ABCD", "20", "0.30", 10, "1000")  # up 15% but tiny volume
    gate = GainerGate(Spy(), [("ABCD", TODAY)], Decimal(5), Decimal(5_000_000))
    assert gate.evaluate(context(today, prior)).verdict is Verdict.IGNORE


def test_stats_interval_and_buckets():
    r = [Decimal(x) for x in ("1", "-1", "2", "-1")]
    s = stats(r)
    assert s["mean_r"] == Decimal("0.25") and s["profit_factor"] == Decimal("1.5")
    low, high = bootstrap_interval([r[:2], r[2:]], 500, 1, Decimal(95))
    assert low <= Decimal("0.25") <= high
    # One day per resample unit: two days give at most three distinct means.
    assert bootstrap_interval([r], 500, 1, Decimal(95)) is None


def test_holdout_start_is_the_last_share_of_sessions():
    from aperture.backtest.report import holdout_start

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
                 "aperture.strategy.price_action_v1",
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
    from aperture.replay.engine import ExitReason, SimulatedTrade

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
    view = bot_view(trades, cfg())
    assert view["trades"] == 3 and view["total_usd"] == Decimal(-300)
    assert view["sized_down_to_daily_room"] == 1
    assert view["refused"] == {"DAILY_CEILING": 1, "NOTIONAL_OVER_CAP": 1,
                               "PLANNED_LOSS_OVER_LIMIT": 1, "POSITION_OPEN": 1}


def test_last_sessions_cuts_the_pool_to_the_latest_calendar_sessions():
    from aperture.backtest.errors import BacktestError
    from aperture.backtest.run import last_sessions
    from aperture.calendar import SessionCalendar

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
    for bad in (0, -1):
        try:
            last_sessions(calendar, pool, bad, 30)
        except BacktestError as exc:
            assert "positive" in str(exc)
        else:
            raise AssertionError("a count below 1 must be refused")


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
                 "aperture.strategy.price_action_v1",
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
    from aperture.api.lab_replay import summarize

    summary = summarize(report)
    assert (summary.first_day, summary.last_day, summary.sessions) == (
        date(2026, 11, 11), TODAY, 3)
    assert summary.sessions_without_replayed_days == 2 and summary.replayed_symbol_days == 1
    assert summary.bot_trades == report["bot"]["trades"] <= summary.trades
