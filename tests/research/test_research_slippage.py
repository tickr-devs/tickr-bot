"""R5: stop slippage measured from the bot's database, and the replay using the measured value."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from research_helpers import HERE

from tickr.replay.errors import MeasuredSlippageError
from tickr.replay.settings import load_settings
from tickr.research.__main__ import main
from tickr.store import Store

SETTINGS = str(HERE / "research_settings.toml")
REPLAY = (HERE / "replay_settings.toml").read_text()
DAY_ONE = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)  # 11:00 New York
DAY_TWO = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)


def _order(store: Store, trade_id: str, leg: str, side: str, filled: str, average: str | None,
           stop: str | None = None, exact: str | None = None) -> None:
    store.execute(
        "INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, order_type, quantity, "
        "stop_price, send_state, filled_quantity, average_fill_price, exact_fill_price, "
        "updated_at) VALUES (?, ?, ?, 'ABCD', ?, 'LIMIT', '10', ?, 'acknowledged', ?, ?, ?, ?)",
        (f"{trade_id}-{leg}", trade_id, leg, side, stop, filled, average, exact,
         DAY_ONE.isoformat()))


def _trade(store: Store, run_id: str, trade_id: str, closed: datetime, *, entry: str = "10.00",
           stop: str = "9.80", stop_fill: str | None = "9.70", stop_filled: str = "10",
           exact: str | None = None, cause: str = "stopped-out") -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
            "entry_limit_price, stop_price, planned_loss_usd, opened_at, closed_at, close_cause) "
            "VALUES (?, ?, 'ABCD', 'closed', '10', '10.00', ?, '2.00', ?, ?, ?)",
            (trade_id, run_id, stop, closed.isoformat(), closed.isoformat(), cause))
        _order(store, trade_id, "entry", "BUY", "10", entry)
        _order(store, trade_id, "stop", "SELL", stop_filled, stop_fill, stop, exact)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "tickr.sqlite3"
    store = Store.open(path, busy_timeout_seconds=1, now=lambda: DAY_ONE)
    run = store.start_run(environment="sandbox", config_digest="d", strategy="s",
                          strategy_version="1", code_version="c")
    _trade(store, run, "t1", DAY_ONE)  # 0.10 below a 9.80 stop
    _trade(store, run, "t2", DAY_TWO, exact="9.75")  # the exact fill price wins: 0.05 below
    _trade(store, run, "t3", DAY_TWO, stop_fill=None)  # no fill price: a cause, not measured
    _trade(store, run, "t4", DAY_TWO, stop_filled="0", stop_fill=None, cause="strategy_exit")
    other = store.start_run(environment="production", config_digest="d", strategy="s",
                            strategy_version="1", code_version="c")
    _trade(store, other, "t5", DAY_TWO)
    store.close()
    return path


def _rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def _measure(database, out, *extra: str) -> dict:
    assert main(["slippage", "--database", str(database), "--settings", SETTINGS,
                 "--out", str(out), *extra]) == 0
    return json.loads((out / "slippage_summary.json").read_text())


def test_stopped_trades_planned_against_realized_per_day(database, tmp_path):
    summary = _measure(database, tmp_path / "s", "--environment", "sandbox")
    assert summary["format"] == "tickr-measured-slippage-v1"
    assert (summary["stopped_trades"], summary["measured_trades"]) == (3, 2)
    # (0.10 + 0.05) x 10 over 9.80 x 20 shares
    assert summary["stop_slippage_percent"] == "0.7653"
    assert summary["planned_loss_usd"] == "4.0000"
    assert summary["realized_loss_usd"] == "5.5000"  # (10 - 9.70) x 10 + (10 - 9.75) x 10
    trades = _rows(tmp_path / "s" / "slippage_trades.csv")
    assert [t["trade_id"] for t in trades] == ["t1", "t2", "t3"]
    assert trades[1]["stop_fill_price"] == "9.7500"
    assert trades[2]["cause"] == "a filled stop order has no fill price"
    days = _rows(tmp_path / "s" / "slippage_days.csv")
    assert [(d["day"], d["stopped_trades"], d["measured_trades"], d["stop_slippage_usd"])
            for d in days] == [("2026-11-16", "1", "1", "1.0000"),
                               ("2026-11-17", "2", "1", "0.5000")]


def test_every_environment_without_a_filter(database, tmp_path):
    assert _measure(database, tmp_path / "s")["stopped_trades"] == 4


def test_missing_database_is_refused(tmp_path, capsys):
    assert main(["slippage", "--database", str(tmp_path / "none.sqlite3"),
                 "--settings", SETTINGS, "--out", str(tmp_path / "s")]) == 2
    assert "database not found" in capsys.readouterr().err


def _replay(tmp_path, file: str, minimum: int):
    path = tmp_path / "replay.toml"
    path.write_text(REPLAY.replace('measured_slippage_file = ""',
                                   f'measured_slippage_file = "{file}"')
                    .replace("measured_slippage_min_trades = 20",
                             f"measured_slippage_min_trades = {minimum}"))
    return load_settings(path, 2)


def test_fixed_slippage_is_the_default():
    settings = load_settings(HERE / "replay_settings.toml", 2)
    assert settings.slippage_percent == Decimal("0.1")
    assert settings.measured_slippage_file is None


def test_replay_uses_the_selected_measurement(database, tmp_path):
    _measure(database, tmp_path / "s", "--environment", "sandbox")
    settings = _replay(tmp_path, "s/slippage_summary.json", 2)  # relative to the settings file
    assert settings.slippage_percent == Decimal("0.7653")
    assert settings.measured_slippage_file == tmp_path / "s" / "slippage_summary.json"
    assert settings.slippage(Decimal("100")) == Decimal("0.7653")


def test_too_few_measured_trades_are_refused(database, tmp_path):
    _measure(database, tmp_path / "s", "--environment", "sandbox")
    with pytest.raises(MeasuredSlippageError, match="2 measured stopped trades"):
        _replay(tmp_path, "s/slippage_summary.json", 3)


def test_a_missing_measurement_is_refused(tmp_path):
    with pytest.raises(MeasuredSlippageError, match="not found"):
        _replay(tmp_path, "nowhere.json", 1)


@pytest.mark.parametrize(("body", "cause"), [
    ({"format": "other"}, "format"),
    ({"format": "tickr-measured-slippage-v1", "measured_trades": 5,
      "stop_slippage_percent": "-0.1"}, "negative"),
    ({"format": "tickr-measured-slippage-v1", "measured_trades": 5,
      "stop_slippage_percent": 0.1}, "decimal string"),
])
def test_a_malformed_measurement_is_refused(tmp_path, body, cause):
    (tmp_path / "m.json").write_text(json.dumps(body))
    with pytest.raises(MeasuredSlippageError, match=cause):
        _replay(tmp_path, "m.json", 1)
