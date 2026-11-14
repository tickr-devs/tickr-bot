from __future__ import annotations

import csv
import json
from decimal import Decimal

from conftest import FLATTEN, HERE, LAST_ENTRY, enter, series

from aperture.replay.__main__ import main
from aperture.replay.engine import replay
from aperture.replay.report import SIGNAL_COLUMNS, TRADE_COLUMNS, summarize, write_all
from aperture.strategy.base import Verdict

FLAT = ("10.00", "10.05", "9.95", "10.00")
UP = ("10.05", "10.12", "10.02", "10.08")


def two_trades(settings, scripted):
    """One stopped-out loser, then one winner exited by the strategy."""
    rows = [
        FLAT,  # 0 ENTER
        UP,  # 1 fill
        ("10.00", "10.00", "9.70", "9.75"),  # 2 stop
        FLAT,  # 3 ENTER
        UP,  # 4 fill
        ("10.40", "10.50", "10.35", "10.45"),  # 5 EXIT
        ("10.50", "10.55", "10.45", "10.50"),  # 6 exit at open
    ]
    strategy = scripted(
        {
            0: enter("10.00", "10.10", "9.80"),
            3: enter("10.00", "10.10", "9.80"),
            5: {"verdict": Verdict.EXIT},
        }
    )
    return replay(lambda: strategy, {"ABC": series("10:00", rows)}, settings, LAST_ENTRY, FLATTEN)


def test_summary_in_r_and_percent(settings, scripted):
    result = two_trades(settings, scripted)
    loser, winner = result.trades
    summary = summarize(result)
    assert summary.trades == 2 and summary.wins == 1 and summary.losses == 1
    assert summary.win_rate_percent == 50
    assert summary.total_r == loser.r_multiple + winner.r_multiple
    assert summary.max_drawdown_r == -loser.r_multiple
    assert summary.total_return_percent == loser.return_percent + winner.return_percent
    assert summary.signals_by_outcome["filled"] == 2
    assert summary.exits_by_reason == {"stop": 1, "strategy_exit": 1}


def test_empty_summary_has_no_averages(settings, scripted):
    strategy = scripted({})
    result = replay(
        lambda: strategy, {"ABC": series("10:00", [FLAT])}, settings, LAST_ENTRY, FLATTEN
    )
    summary = summarize(result)
    assert summary.trades == 0 and summary.average_r is None and summary.total_r == 0


def test_outputs_are_labelled_simulated(settings, scripted, tmp_path):
    result = two_trades(settings, scripted)
    write_all(result, tmp_path, settings.output_decimal_places)
    with open(tmp_path / "trades.csv") as handle:
        trades = list(csv.DictReader(handle))
    with open(tmp_path / "signals.csv") as handle:
        signals = list(csv.DictReader(handle))
    assert tuple(trades[0]) == TRADE_COLUMNS and tuple(signals[0]) == SIGNAL_COLUMNS
    assert {row["label"] for row in trades + signals} == {"simulated"}
    assert trades[0]["exit_reason"] == "stop"
    assert Decimal(trades[0]["r_multiple"]).as_tuple().exponent == -4
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["label"] == "simulated" and summary["trades"] == 2


def test_cli_end_to_end_with_stub_strategy(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(HERE))
    rows = [
        FLAT,
        FLAT,
        FLAT,
        ("10.00", "10.20", "10.00", "10.15"),
        UP,
        ("10.10", "10.30", "10.10", "10.25"),
        ("10.25", "10.30", "9.60", "9.70"),
        FLAT,
    ]
    bars_path = tmp_path / "bars.jsonl"
    bars_path.write_text(
        "".join(
            json.dumps(
                {
                    "symbol": b.symbol,
                    "start": b.start.isoformat(),
                    "open": str(b.open),
                    "high": str(b.high),
                    "low": str(b.low),
                    "close": str(b.close),
                    "volume": str(b.volume),
                }
            )
            + "\n"
            for b in series("10:00", rows)
        )
    )
    out = tmp_path / "out"
    code = main(
        [
            "--bars",
            str(bars_path),
            "--strategy",
            "stub_strategy",
            "--values",
            str(HERE / "stub_strategy.toml"),
            "--settings",
            str(HERE / "replay_settings.toml"),
            "--last-entry",
            "15:45",
            "--flatten",
            "15:55",
            "--out",
            str(out),
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert printed.startswith("SIMULATED replay of stub-breakout v0")
    with open(out / "trades.csv") as handle:
        (trade,) = list(csv.DictReader(handle))
    # Breakout of 10.05 at bar 3: fill at 10.15 + slippage on bar 4, stop 9.95 hit on bar 6.
    assert trade["setup"] == "stub-breakout"
    assert trade["exit_reason"] == "stop"
    assert Decimal(trade["r_multiple"]) < -1


def test_cli_refuses_bad_settings(tmp_path, capsys):
    bad = tmp_path / "s.toml"
    bad.write_text("[replay]\n")
    code = main(
        [
            "--bars",
            str(tmp_path / "none.jsonl"),
            "--strategy",
            "x",
            "--values",
            str(bad),
            "--settings",
            str(bad),
            "--last-entry",
            "15:45",
            "--flatten",
            "15:55",
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert code == 2
    assert "replay refused" in capsys.readouterr().err


def test_cli_refuses_unknown_strategy_module(tmp_path, capsys):
    code = main(
        [
            "--bars",
            str(tmp_path / "none.jsonl"),
            "--strategy",
            "no_such_strategy_module",
            "--values",
            str(HERE / "stub_strategy.toml"),
            "--settings",
            str(HERE / "replay_settings.toml"),
            "--last-entry",
            "15:45",
            "--flatten",
            "15:55",
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert code == 2
    assert "cannot be imported" in capsys.readouterr().err
