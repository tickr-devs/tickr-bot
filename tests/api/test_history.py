"""Read-only history: GET /days, /days/<date>, /bars against a real database and capture files."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from aperture.api import contract
from aperture.api.history import History, HistoryPaths, HistoryUnavailable, closed_trades
from aperture.data.capture import BARS_FILE
from aperture.store import Store

from .schema_check import errors
from .test_server import api, doc, request, schema, settings  # noqa: F401 - pytest fixtures

DAY = date(2026, 11, 16)
OPEN = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # 10:00 New York
PREVIOUS = date(2026, 11, 13)


def _store(paths: HistoryPaths) -> Store:
    return Store.open(paths.database, busy_timeout_seconds=1, now=lambda: OPEN)


def _run(store: Store, environment: str = "sandbox") -> str:
    return store.start_run(environment=environment, config_digest="d", strategy="s",
                           strategy_version="1", code_version="c")


def _order(store: Store, trade_id: str, leg: str, side: str, order_type: str, filled: str,
           average: str | None, stop: str | None = None) -> None:
    store.execute(
        "INSERT INTO orders(client_order_id, trade_id, leg, symbol, side, order_type, quantity, "
        "stop_price, send_state, filled_quantity, average_fill_price, updated_at) "
        "VALUES (?, ?, ?, 'ABCD', ?, ?, '10', ?, 'acknowledged', ?, ?, ?)",
        (f"{trade_id}-{leg}", trade_id, leg, side, order_type, stop, filled, average,
         OPEN.isoformat()))


def _trade(store: Store, run_id: str, trade_id: str, closed_at: datetime | None,
           entry: str = "12.34", exit_: str | None = "12.75", stop: str | None = "11.90",
           filled: str = "10") -> None:
    with store.transaction():
        _trade_rows(store, run_id, trade_id, closed_at, entry, exit_, stop, filled)


def _trade_rows(store: Store, run_id: str, trade_id: str, closed_at: datetime | None,
                entry: str, exit_: str | None, stop: str | None, filled: str) -> None:
    store.execute(
        "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, entry_limit_price, "
        "stop_price, planned_loss_usd, opened_at, closed_at, close_cause) "
        "VALUES (?, ?, 'ABCD', ?, '10', '12.35', '11.80', '50', ?, ?, ?)",
        (trade_id, run_id, "closed" if closed_at else "open", OPEN.isoformat(),
         closed_at.isoformat() if closed_at else None, "strategy_exit" if closed_at else None))
    _order(store, trade_id, "entry", "BUY", "LIMIT", filled, entry if filled != "0" else None)
    if stop is not None:
        _order(store, trade_id, "stop", "SELL", "STOP_LOSS", "0", None, stop)
    if exit_ is not None:
        _order(store, trade_id, "exit", "SELL", "MARKET", filled, exit_)


def _bars_file(paths: HistoryPaths, day: date, lines: list[str]) -> None:
    path = paths.capture / day.isoformat() / BARS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), "utf-8")


def _bar(symbol: str, minute: int, close: str = "12.40", end: str = "\n") -> str:
    start = OPEN + timedelta(minutes=minute)
    return json.dumps({"symbol": symbol, "start": start.isoformat(), "open": "12.30",
                       "high": "12.50", "low": "12.20", "close": close, "volume": "1000",
                       "provider": "webull", "receipt_time": start.isoformat()}) + end


@pytest.fixture
def paths(tmp_path) -> HistoryPaths:
    return HistoryPaths("sandbox", tmp_path / "aperture.sqlite3", tmp_path / "capture" / "sandbox")


# ---------------------------------------------------------------- closed trades


def test_closed_trade_has_fill_prices_first_stop_and_result(paths):
    store = _store(paths)
    run = _run(store)
    _trade(store, run, "t1", OPEN + timedelta(minutes=30))
    (trade,) = closed_trades(store, "sandbox")
    assert (trade.symbol, trade.quantity, trade.entry_price, trade.exit_price) == (
        "ABCD", Decimal("10"), Decimal("12.34"), Decimal("12.75"))
    assert trade.stop_price == Decimal("11.90")
    assert trade.planned_loss_usd == Decimal("50")
    assert trade.result_gross_usd == Decimal("4.10")


def test_unfilled_and_open_trades_are_not_history(paths):
    store = _store(paths)
    run = _run(store)
    _trade(store, run, "never-filled", OPEN, filled="0", exit_=None)
    _trade(store, run, "still-open", None, exit_=None)
    assert closed_trades(store, "sandbox") == []


def test_closed_on_keeps_one_new_york_date(paths):
    store = _store(paths)
    run = _run(store)
    _trade(store, run, "today", OPEN + timedelta(minutes=5))
    _trade(store, run, "before", OPEN - timedelta(days=3))
    assert [t.trade_id for t in closed_trades(store, closed_on=DAY)] == ["today"]


def test_trades_of_another_environment_are_left_out(paths):
    store = _store(paths)
    _trade(store, _run(store, "production"), "prod", OPEN + timedelta(minutes=5))
    _trade(store, _run(store), "sand", OPEN + timedelta(minutes=6))
    assert [t.trade_id for t in closed_trades(store, "sandbox")] == ["sand"]
    assert len(closed_trades(store)) == 2


# ---------------------------------------------------------------- History


def test_days_join_trades_runs_and_capture_newest_first(paths):
    store = _store(paths)
    run = _run(store)
    _trade(store, run, "win", OPEN + timedelta(minutes=30))
    _trade(store, run, "loss", OPEN + timedelta(minutes=50), exit_="12.00")
    store.close()
    _bars_file(paths, PREVIOUS, [_bar("ABCD", 0)])
    days = History(paths).days()
    assert [d.day for d in days.days] == [DAY, PREVIOUS]
    today, previous = days.days
    assert (today.trades, today.wins, today.losses) == (2, 1, 1)
    assert today.result_gross_usd == Decimal("4.10") + Decimal("-3.40")
    assert not today.has_bars and previous.has_bars and previous.trades == 0


def test_no_database_and_no_capture_is_an_empty_history(paths):
    history = History(paths)
    assert history.days().days == ()
    day = history.day(DAY, DAY)
    assert day.trades == () and day.bar_symbols == () and day.result_gross_usd == 0


def test_trade_belongs_to_its_new_york_close_date(paths):
    store = _store(paths)
    # 01:30 UTC on the 17th is still the 16th in New York.
    _trade(store, _run(store), "late", datetime(2026, 11, 17, 1, 30, tzinfo=UTC))
    store.close()
    history = History(paths)
    assert len(history.day(DAY, DAY).trades) == 1
    assert history.day(date(2026, 11, 17), DAY).trades == ()


def test_database_of_another_schema_version_is_refused(paths):
    _store(paths).close()
    db = sqlite3.connect(paths.database)
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    with pytest.raises(HistoryUnavailable, match="schema v1"):
        History(paths).days()


def test_history_never_writes_the_database(paths):
    _store(paths).close()
    before = paths.database.read_bytes()
    History(paths).days()
    assert paths.database.read_bytes() == before


def test_bars_one_per_start_last_revision_wins(paths):
    _bars_file(paths, PREVIOUS, [_bar("ABCD", 1), _bar("ABCD", 0), _bar("KLMN", 0),
                                 _bar("ABCD", 1, close="12.45"), _bar("ABCD", 1, close="12.45")])
    history = History(paths)
    bars = history.bars("ABCD", PREVIOUS, DAY)
    assert [b.start for b in bars.bars] == [OPEN, OPEN + timedelta(minutes=1)]
    assert bars.bars[1].close == Decimal("12.45")
    assert history.day(PREVIOUS, DAY).bar_symbols == ("ABCD", "KLMN")
    assert history.bars("NONE", PREVIOUS, DAY).bars == ()


def test_todays_half_written_last_line_waits_other_days_refuse_it(paths):
    lines = [_bar("ABCD", 0), _bar("ABCD", 1, end="")[:40]]
    _bars_file(paths, DAY, lines)
    assert len(History(paths).bars("ABCD", DAY, DAY).bars) == 1
    with pytest.raises(HistoryUnavailable, match="capture file unreadable"):
        History(paths).bars("ABCD", DAY, today=DAY + timedelta(days=1))


def test_bars_file_is_read_again_when_it_grows(paths):
    _bars_file(paths, DAY, [_bar("ABCD", 0)])
    history = History(paths)
    assert len(history.bars("ABCD", DAY, DAY).bars) == 1
    _bars_file(paths, DAY, [_bar("ABCD", 0), _bar("ABCD", 1)])
    assert len(history.bars("ABCD", DAY, DAY).bars) == 2


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def served(api, settings):  # noqa: F811 - pytest fixture
    server, clock = api
    clock.now = OPEN + timedelta(hours=1)
    return server, settings.history


def test_days_endpoint(served):
    server, paths = served
    _bars_file(paths, PREVIOUS, [_bar("ABCD", 0)])
    resp, payload = request(server, "GET", "/days")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("days-v1"), body) == []
    assert [d["day"] for d in body["days"]] == ["2026-11-13"]


def test_day_endpoint(served):
    server, paths = served
    _bars_file(paths, PREVIOUS, [_bar("ABCD", 0)])
    resp, payload = request(server, "GET", "/days/2026-11-13")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("day-v1"), body) == []
    assert body["bar_symbols"] == ["ABCD"]


def test_bars_endpoint(served):
    server, paths = served
    _bars_file(paths, PREVIOUS, [_bar("ABCD", 0), _bar("ABCD", 1)])
    resp, payload = request(server, "GET", "/bars?symbol=ABCD&date=2026-11-13")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("bars-v1"), body) == []
    assert [b["close"] for b in body["bars"]] == ["12.40", "12.40"]
    assert contract.from_jsonable(contract.BarsV1, body).symbol == "ABCD"


@pytest.mark.parametrize("path", [
    "/days/2026-13-01", "/days/yesterday", "/days?x=1", "/days/2026-11-13?x=1",
    "/bars", "/bars?symbol=ABCD", "/bars?symbol=ABCD&date=2026-11-13&x=1",
    "/bars?symbol=ABCD&symbol=KLMN&date=2026-11-13", "/bars?symbol=&date=2026-11-13",
    "/bars?symbol=ab%2F..&date=2026-11-13", "/bars?symbol=ABCD&date=13.11.2026",
])
def test_bad_history_requests_are_refused(served, path):
    server, _ = served
    resp, payload = request(server, "GET", path)
    assert resp.status == 400
    body = doc(payload)
    assert errors(schema("error-v1"), body) == [] and body["error"] == "bad_request"


def test_unreadable_history_is_503_with_its_cause(served):
    server, paths = served
    _bars_file(paths, PREVIOUS, ["not json\n"])
    resp, payload = request(server, "GET", "/bars?symbol=ABCD&date=2026-11-13")
    assert resp.status == 503
    assert "capture file unreadable" in doc(payload)["cause"]


@pytest.mark.parametrize("path", ["/days", "/days/2026-11-13", "/bars"])
def test_history_is_get_only(served, path):
    server, _ = served
    resp, _ = request(server, "POST", path, {})
    assert resp.status == 405


def test_settings_point_history_at_the_environment(settings):  # noqa: F811
    assert settings.history.environment == "sandbox"
    assert settings.history.capture.parts[-2:] == ("capture", "sandbox")
    assert replace(settings.history, environment="x").environment == "x"
