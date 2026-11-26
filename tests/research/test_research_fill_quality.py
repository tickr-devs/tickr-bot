"""M4: fill quality of real fills (entry/exit/stop slippage, fees, fill delay) per record set.

Every record here is synthetic (built by the helpers below), never a real fill.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta

import pytest
from research_helpers import HERE

from tickr.research.__main__ import main
from tickr.store import Store

SETTINGS = str(HERE / "research_settings.toml")
REPLAY = str(HERE / "replay_settings.toml")  # slippage_percent = 0.1
CONFIG = str(HERE.parent.parent / "config" / "bot.toml")
DAY_ONE = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # 10:00 New York
DAY_TWO = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)


def _journal(store: Store, kind: str, key: str, at: datetime, body: dict | None = None) -> None:
    store.execute("INSERT INTO journal(at_utc, run_id, kind, key, body) VALUES (?, NULL, ?, ?, ?)",
                  (at.isoformat(), kind, key, json.dumps(body or {})))


def _order(store: Store, trade: str, leg: str, side: str, order_type: str, *, fill: str,
           fees: str | None, at: datetime, limit: str | None = None, stop: str | None = None,
           delay: int = 2, quantity: str = "10") -> None:
    cid = f"{trade}-{leg}"
    store.execute(
        "INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, order_type, quantity, "
        "limit_price, stop_price, send_state, filled_quantity, average_fill_price, fees_usd, "
        "updated_at) VALUES (?, ?, ?, 'ABCD', ?, ?, ?, ?, ?, 'acknowledged', ?, ?, ?, ?)",
        (cid, trade, leg, side, order_type, quantity, limit, stop, quantity, fill, fees,
         at.isoformat()))
    _journal(store, "order-intent", cid, at)
    _journal(store, "order-attempted", cid, at + timedelta(seconds=1))
    _journal(store, "order-observed", cid, at + timedelta(seconds=1 + delay),
             {"filled_quantity": quantity})


def _trade(store: Store, run: str, trade: str, day: datetime, *, entry_fill: str, fees: str | None,
           exit_leg: str = "stop", exit_fill: str = "9.70", delay: int = 2) -> None:
    closed = day + timedelta(minutes=30)
    with store.transaction():
        store.execute(
            "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
            "entry_limit_price, stop_price, planned_loss_usd, opened_at, closed_at, close_cause) "
            "VALUES (?, ?, 'ABCD', 'closed', '10', '10.00', '9.80', '2.00', ?, ?, 'x')",
            (trade, run, day.isoformat(), closed.isoformat()))
        _order(store, trade, "entry", "BUY", "LIMIT", fill=entry_fill, fees=fees, at=day,
               limit="10.00", delay=delay)
        _order(store, trade, exit_leg, "SELL", "LIMIT" if exit_leg == "stop" else "MARKET",
               fill=exit_fill, fees=fees, at=closed, stop="9.80" if exit_leg == "stop" else None,
               delay=delay)


def _database(path, environment: str, *, extra_slip: str = "0.00"):
    store = Store.open(path, busy_timeout_seconds=1, now=lambda: DAY_ONE)
    run = store.start_run(environment=environment, config_digest="d", strategy="s",
                          strategy_version="1", code_version="c")
    entry = "9.98" if extra_slip == "0.00" else "10.03"  # better than the limit, or worse
    _trade(store, run, "t1", DAY_ONE, entry_fill=entry, fees="0.30")
    _trade(store, run, "t2", DAY_TWO, entry_fill="10.00", fees="0.10", exit_leg="exit",
           exit_fill="10.10")
    _trade(store, run, "t3", DAY_TWO, entry_fill="10.00", fees=None)  # fees not recorded yet
    store.close()
    return path


def _quotes(path):
    def line(symbol, at, bid, ask, source="stream"):
        return json.dumps({"symbol": symbol, "bid": bid, "ask": ask, "bid_size": "1",
                           "ask_size": "1", "provider_time": at.isoformat(),
                           "receipt_time": at.isoformat(), "provider": "synthetic",
                           "source": source})

    path.write_text("\n".join([
        line("ABCD", DAY_ONE - timedelta(seconds=5), "9.95", "9.99"),
        line("ABCD", DAY_ONE + timedelta(seconds=30), "1", "1"),  # after the decision: unused
        line("ABCD", DAY_TWO - timedelta(seconds=3), "9.98", "9.99"),
        line("ABCD", DAY_TWO + timedelta(minutes=30) - timedelta(seconds=1), "10.20", "10.30"),
    ]) + "\n")
    return path


def _rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def _run(tmp_path, *extra: str) -> dict:
    out = tmp_path / "out"
    assert main(["fill-quality", "--settings", SETTINGS, "--replay-settings", REPLAY,
                 "--config", CONFIG,
                 "--out", str(out), *extra]) == 0
    return json.loads((out / "fill_quality_summary.json").read_text())


def test_one_set_per_day_and_overall(tmp_path):
    db = _database(tmp_path / "a.sqlite3", "sandbox")
    summary = _run(tmp_path, "--database", str(db), "--quotes", str(_quotes(tmp_path / "q.jsonl")),
                   "--environment", "sandbox")
    total = summary["sets"]["records"]
    assert total["trades"] == 3
    # (9.98 - 10.00) x 10 over 10.00 x 30 shares across the three entries
    assert total["entry_slippage_vs_limit_percent"] == "-0.0667"
    # t1: 9.98 against ask 9.99; t2 and t3: 10.00 against ask 9.99
    assert total["entry_quote_trades"] == 3
    assert total["entry_slippage_vs_quote_percent"] == "0.0334"
    assert total["stop_slippage_percent"] == "1.0204"  # R5: (9.80 - 9.70) over 9.80 (t1, t3)
    assert total["exit_slippage_vs_quote_percent"] == "0.9804"  # bid 10.20 against fill 10.10
    assert total["fee_trades"] == 2 and total["trades_without_fees"] == 1
    assert total["fees_per_trade_usd"] == "0.4000"  # (0.60 + 0.20) over the two with fees
    assert total["fees_per_share_usd"] == "0.0200"  # 0.80 over their 40 shares filled
    assert total["entry_delay_median_s"] == "2.0000"
    assert "gross of fees" in summary["fees_assumption"]
    days = _rows(tmp_path / "out" / "records" / "fill_quality_days.csv")
    assert [d["day"] for d in days] == ["2026-11-16", "2026-11-17", "all"]
    assert days[0]["trades"] == "1" and days[1]["trades"] == "2"
    trades = _rows(tmp_path / "out" / "records" / "fill_quality_trades.csv")
    assert trades[2]["cause"] == "fees not recorded"
    assert trades[0]["entry_vs_limit_usd"] == "-0.0200"


def test_compare_shows_assumption_and_difference(tmp_path):
    a = _database(tmp_path / "a.sqlite3", "sandbox")
    b = _database(tmp_path / "b.sqlite3", "production", extra_slip="0.05")
    quotes = _quotes(tmp_path / "q.jsonl")
    summary = _run(tmp_path, "--database", str(a), "--quotes", str(quotes), "--label", "sandbox",
                   "--compare-database", str(b), "--compare-quotes", str(quotes),
                   "--compare-label", "production")
    assert set(summary["sets"]) == {"sandbox", "production"}
    rows = {r["metric"]: r for r in _rows(tmp_path / "out" / "fill_quality_compare.csv")}
    quote_row = rows["entry_slippage_vs_quote_percent"]
    assert quote_row["assumed"] == "0.1000"  # the replay's slippage_percent
    assert float(quote_row["difference"]) == pytest.approx(
        float(quote_row["production"]) - float(quote_row["sandbox"]), abs=1e-3)
    assert rows["fees_per_trade_usd"]["assumed"] == ""  # the replay has no fee model


def test_without_quotes_the_quote_figures_are_empty_with_a_cause(tmp_path):
    db = _database(tmp_path / "a.sqlite3", "sandbox")
    summary = _run(tmp_path, "--database", str(db))
    assert summary["sets"]["records"]["entry_slippage_vs_quote_percent"] is None
    assert summary["sets"]["records"]["entry_slippage_vs_limit_percent"] is not None


def test_bad_inputs_are_refused(tmp_path, capsys):
    db = _database(tmp_path / "a.sqlite3", "sandbox")
    base = ["fill-quality", "--settings", SETTINGS, "--replay-settings", REPLAY, "--config", CONFIG,
            "--out", str(tmp_path / "o")]
    assert main([*base, "--database", str(tmp_path / "none.sqlite3")]) == 2
    assert "database not found" in capsys.readouterr().err
    assert main([*base, "--database", str(db), "--compare-database", str(db),
                 "--compare-label", "records"]) == 2
    assert "labels" in capsys.readouterr().err
