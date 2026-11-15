"""Advisor backtest mode: evidence from the fitting period only, ideas, and the hold-out judge."""

from __future__ import annotations

import csv
import json
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aperture import config as config_module
from aperture.advisor.__main__ import main
from aperture.advisor.errors import AdvisorError
from aperture.advisor.judge import judge
from aperture.advisor.judge import main as judge_main
from aperture.advisor.proposal import IDEA_KEYS, parse_reply
from aperture.advisor.run import TUNING_FILE, Status, run_backtest_advisor
from aperture.replay.settings import load_settings

from .test_advisor import BASE, FakeClient, answer

ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime(2026, 11, 18, 1, 0, tzinfo=UTC)
M1 = timedelta(minutes=1)
FIT_DAYS = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
HOLD_DAY = date(2026, 10, 1)
TRADE_COLUMNS = ("label", "strategy", "version", "symbol", "session_date", "setup",
                 "signal_bar_start", "entry_time", "entry_price", "stop", "exit_time",
                 "exit_price", "exit_reason", "bars_held", "r_multiple", "return_percent")
SIGNAL_COLUMNS = ("label", "strategy", "version", "symbol", "session_date", "bar_start", "setup",
                  "entry_low", "entry_high", "stop", "outcome", "cause")
IDEA = {"name": "pullback-v1", "hypothesis": "gainers that hold the VWAP continue",
        "entry_rule": "buy the first higher low above VWAP after 10:00",
        "exit_rule": "stop under the higher low; flatten at the close", "filters": "price > 5",
        "evidence": "stops after +0.6R, then 2.4R after the stop", "test_plan": "backtest"}


def holdout(trades, low, high):
    return {"trades": trades, "win_rate_percent": "50.0", "mean_r": "0.10", "median_r": "0.0",
            "total_r": "1.0", "profit_factor": "1.2", "mean_r_interval": [low, high]}


def write_results(folder: Path, strategy="price-action-v1", version="1", hold_low="-0.30",
                  hold_high="0.10", starts=HOLD_DAY, symbols=2) -> Path:
    folder.mkdir(parents=True)
    report = {"label": "simulated", "strategy": strategy, "version": version,
              "window": [FIT_DAYS[0].isoformat(), HOLD_DAY.isoformat()],
              "pool": {"symbol_days": 4, "sessions": 4, "symbols": symbols,
                       "replayed_symbol_days": 4, "skipped": {}},
              "holdout": {"starts": starts.isoformat(),
                          "before": holdout(6, "-0.5", "0.4"),
                          "holdout": holdout(2, hold_low, hold_high)}}
    (folder / "report.json").write_text(json.dumps(report))
    trades, signals = [], []
    for day, symbol in [(d, "ABC") for d in FIT_DAYS] + [(HOLD_DAY, "HLD")]:
        entry = datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)
        for exit_reason, minutes, price in (("stop", 5, "9.50"), ("flatten", 60, "11.00")):
            trades.append(dict(zip(TRADE_COLUMNS, (
                "simulated", strategy, version, symbol, day.isoformat(), "breakout",
                (entry - M1).isoformat(), entry.isoformat(), "10.00", "9.50",
                (entry + minutes * M1).isoformat(), price, exit_reason, str(minutes), "", ""),
                strict=True)))
        for outcome, cause in (("filled", ""), ("missed", "WINDOW_LEFT: top 10.1")):
            signals.append(dict(zip(SIGNAL_COLUMNS, (
                "simulated", strategy, version, symbol, day.isoformat(), entry.isoformat(),
                "breakout", "9.9", "10.1", "9.5", outcome, cause), strict=True)))
    for name, columns, rows in (("trades.csv", TRADE_COLUMNS, trades),
                                ("signals.csv", SIGNAL_COLUMNS, signals)):
        with open(folder / name, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    return folder


def write_minutes(data: Path) -> Path:
    (data / "minutes").mkdir(parents=True)
    for symbol, days in (("ABC", FIT_DAYS), ("HLD", [HOLD_DAY])):
        lines = []
        for day in days:
            first = datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)
            for i in range(70):
                high = "10.30" if i < 5 else "11.20"  # +0.6R while open, +2.4R after the stop
                lines.append(json.dumps({"symbol": symbol, "start": (first + i * M1).isoformat(),
                                         "open": "10", "high": high, "low": "9.4",
                                         "close": "10", "volume": "1000"}))
        (data / "minutes" / f"{symbol}.jsonl").write_text("\n".join(lines) + "\n")
    return data


def make_root(tmp_path, min_trades=4):
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    shutil.copytree(ROOT / "config", tmp_path / "config")
    bot = tmp_path / "config" / "bot.toml"
    text = bot.read_text().replace("backtest_min_fitting_trades = 30",
                                   f"backtest_min_fitting_trades = {min_trades}")
    bot.write_text(text)
    return config_module.load(bot)


def run(tmp_path, client, cfg=None, results=None):
    cfg = cfg or make_root(tmp_path)
    results = results or write_results(tmp_path / "results")
    data = write_minutes(tmp_path / "data")
    return run_backtest_advisor(cfg, results, data, client,
                                load_settings(ROOT / "config" / "replay.toml"), NOW,
                                out_dir=tmp_path / "out")


def test_evidence_holds_only_the_fitting_period(tmp_path):
    client = FakeClient(answer([("breakout", "lookback_bars", "8")]))
    result = run(tmp_path, client)
    assert result.status is Status.PROPOSED
    evidence = json.loads((result.folder / "evidence.json").read_text())
    assert evidence["trades"]["trades"] == 6  # the two hold-out trades are not in it
    assert evidence["fitting_period"] == ["2026-09-01", "2026-09-03"]
    assert evidence["signals_by_outcome"] == {"filled": 3, "missed": 3}
    assert evidence["not_filled_signal_causes"] == {"missed: WINDOW_LEFT": 3}
    assert evidence["by_exit_reason"]["stop"]["mean_r"] == "-1.0000"
    assert evidence["by_stop_distance_percent"].keys() == {"4..8"}
    assert evidence["excursions"]["best_high_while_open_r"] == {"0.5..1": 3, "2..3": 3}
    assert evidence["excursions"]["best_high_after_stop_exit_until_session_end_r"] == {"2..3": 3}
    system, user, _ = client.calls[0]
    assert "2026-10" not in user and "HLD" not in user  # nothing of the hold-out is sent
    assert "SIMULATED backtest" in system and "new_strategy_idea" in system
    assert "def create(" in user  # the strategy source is sent
    tuning = json.loads((result.folder / TUNING_FILE).read_text())
    assert tuning["holdout_starts"] == "2026-10-01" and tuning["proposed"] == "price-action-v1 v2"
    report = (result.folder / "report.md").read_text()
    assert "Judge it only on the backtest hold-out from 2026-10-01" in report
    assert "advisor.judge --run" in report
    toml = (result.proposal_folder / "strategy.toml").read_text()
    assert "backtest sessions 2026-09-01 .. 2026-09-03 (before the hold-out from 2026-10-01)" \
        in toml


def test_idea_without_changes_writes_idea_md(tmp_path):
    result = run(tmp_path, FakeClient(answer(idea=IDEA)))
    assert result.status is Status.IDEA and result.proposal_folder is None
    idea = (result.folder / "idea.md").read_text()
    assert "# Strategy idea: pullback-v1" in idea
    assert "src/aperture/strategy/pullback_v1.py" in idea
    assert "New strategy idea: pullback-v1" in (result.folder / "report.md").read_text()
    assert json.loads((result.folder / TUNING_FILE).read_text())["idea"] == "pullback-v1"


@pytest.mark.parametrize("idea, cause", [
    ({**dict.fromkeys(IDEA_KEYS, ""), "name": "x-v1"}, "lacks hypothesis, entry_rule, evidence"),
    ({**IDEA, "name": "Pullback"}, "is not like 'name-v1'"),
])
def test_partial_or_misnamed_idea_is_refused(idea, cause):
    with pytest.raises(AdvisorError, match=cause):
        parse_reply(answer(idea=idea), BASE, 3)


def test_refusals(tmp_path):
    with pytest.raises(AdvisorError, match="backtest_min_fitting_trades is 7"):
        run(tmp_path / "a", FakeClient(answer()), cfg=make_root(tmp_path / "a", min_trades=7))
    other = write_results(tmp_path / "b" / "results", version="9")
    with pytest.raises(AdvisorError, match="backtest of price-action-v1 v9"):
        run(tmp_path / "b", FakeClient(answer()), results=other)
    folder = write_results(tmp_path / "c" / "results")
    report = json.loads((folder / "report.json").read_text())
    report["holdout"] = None
    (folder / "report.json").write_text(json.dumps(report))
    with pytest.raises(AdvisorError, match="holdout.starts"):
        run(tmp_path / "c", FakeClient(answer()), results=folder)


def test_cli_backtest_mode(tmp_path, capsys):
    make_root(tmp_path)
    results = write_results(tmp_path / "results")
    data = write_minutes(tmp_path / "data")
    code = main(["--backtest", str(results), "--backtest-data", str(data),
                 "--config", str(tmp_path / "config" / "bot.toml"),
                 "--replay-settings", str(ROOT / "config" / "replay.toml"),
                 "--out", str(tmp_path / "out")],
                client_factory=lambda _: FakeClient(answer(no_change="thin")))
    assert code == 0 and "no_change" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--backtest", str(results)])


def test_judge_compares_only_the_holdout(tmp_path):
    result = run(tmp_path, FakeClient(answer([("breakout", "lookback_bars", "8")])))
    candidate = write_results(tmp_path / "candidate", version="2", hold_low="0.05",
                              hold_high="0.40")
    text = judge(result.folder, candidate)
    assert "price-action-v1 v2: interval 0.05 to 0.40 is above 0" in text
    assert "price-action-v1 v1: interval -0.30 to 0.10 includes 0" in text
    assert (result.folder / "judge-price-action-v1-v2.md").exists()
    with pytest.raises(AdvisorError, match="is the base"):
        judge(result.folder, tmp_path / "results")
    shifted = write_results(tmp_path / "shifted", version="3", starts=date(2026, 9, 3))
    with pytest.raises(AdvisorError, match="splits its hold-out at 2026-09-03"):
        judge(result.folder, shifted)
    other_data = write_results(tmp_path / "other-data", version="4", symbols=3)
    with pytest.raises(AdvisorError, match="ran on other data"):
        judge(result.folder, other_data)
    assert judge_main(["--run", str(tmp_path / "nowhere"), "--results", str(candidate)]) == 2


def test_bucket_edges_must_increase(tmp_path):
    cfg_path = make_root(tmp_path).root / "config" / "bot.toml"
    cfg_path.write_text(cfg_path.read_text().replace(
        "backtest_excursion_edges_r = [0, 0.5, 1, 2, 3]", "backtest_excursion_edges_r = [1, 0]"))
    with pytest.raises(config_module.ConfigError, match="strictly increasing"):
        config_module.load(cfg_path)


def test_reads_what_the_real_backtest_writes(tmp_path):
    """Contract with aperture.backtest: its own writers produce the files the advisor reads."""
    from aperture.backtest.report import write_report
    from aperture.backtest.settings import load_settings as load_backtest
    from aperture.backtest.universe import PoolEntry
    from aperture.replay.engine import ExitReason, ReplayResult, SimulatedTrade
    from aperture.replay.report import write_all

    days = [*FIT_DAYS, date(2026, 9, 4), HOLD_DAY, date(2026, 10, 2)]
    trades = []
    for day in days:
        entry = datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)
        for minutes, price, reason in ((5, "9.5", ExitReason.STOP),
                                       (60, "11", ExitReason.FLATTEN)):
            trades.append(SimulatedTrade(
                "price-action-v1", "1", "ABC", day, "breakout", entry - M1, entry,
                Decimal(10), Decimal("9.5"), entry + minutes * M1, Decimal(price), reason,
                minutes, Decimal(10), Decimal("10.1")))
    result = ReplayResult("price-action-v1", "1", tuple(trades), (), {}, len(days), 0)
    pool = [PoolEntry(d, 1, "ABC", Decimal(9), Decimal(10), Decimal(20), Decimal(10**8))
            for d in days]
    cfg = make_root(tmp_path)
    replay = load_settings(ROOT / "config" / "replay.toml")
    results = tmp_path / "results"
    write_all(result, results, replay.output_decimal_places)
    write_report(result, pool, [], len(days), [], cfg,
                 load_backtest(ROOT / "config" / "backtest.toml"), replay, results)
    starts = json.loads((results / "report.json").read_text())["holdout"]["starts"]
    advised = run_backtest_advisor(cfg, results, write_minutes(tmp_path / "data"),
                                   FakeClient(answer(no_change="thin")), replay, NOW,
                                   out_dir=tmp_path / "out")
    evidence = json.loads((advised.folder / "evidence.json").read_text())
    fitting = sum(2 for d in days if d.isoformat() < starts)
    assert evidence["trades"]["trades"] == fitting < len(trades)


def test_notes_reach_the_request(tmp_path):
    make_root(tmp_path)
    results = write_results(tmp_path / "results")
    data = write_minutes(tmp_path / "data")
    notes = tmp_path / "rejected.md"
    notes.write_text("ORB 5-min: fit +1%, validation -1%. Rejected.")
    client = FakeClient(answer(no_change="thin"))
    code = main(["--backtest", str(results), "--backtest-data", str(data),
                 "--config", str(tmp_path / "config" / "bot.toml"),
                 "--replay-settings", str(ROOT / "config" / "replay.toml"),
                 "--out", str(tmp_path / "out"), "--notes", str(notes)],
                client_factory=lambda _: client)
    assert code == 0
    user = client.calls[0][1]
    assert user.startswith("<already_tested>") and "ORB 5-min: fit +1%" in user
