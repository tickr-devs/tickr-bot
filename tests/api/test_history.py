"""Read-only history: GET /days, /days/<date>, /bars against a real database and capture files."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from aperture.api import contract
from aperture.api.history import (
    History,
    HistoryPaths,
    HistoryUnavailable,
    closed_trades,
    trade_metrics,
)
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


# ---------------------------------------------------------------- stop timing, MAE / MFE

LATER = DAY + timedelta(days=1)  # "today" for History: the DAY file is complete


class Clock:
    def __init__(self) -> None:
        self.now = OPEN

    def __call__(self) -> datetime:
        return self.now


def _timed_store(paths: HistoryPaths) -> tuple[Store, Clock]:
    clock = Clock()
    return Store.open(paths.database, busy_timeout_seconds=1, now=clock), clock


def _observed(store: Store, clock: Clock, cid: str, seconds: float, state: str,
              filled: str, run_id: str | None = None) -> None:
    """The engine's `order-observed` row for one Detail read, `seconds` after OPEN."""
    clock.now = OPEN + timedelta(seconds=seconds)
    with store.transaction():
        store.append("order-observed", {"state": state, "raw_status": state,
                                        "filled_quantity": Decimal(filled),
                                        "average_fill_price": None}, key=cid, run_id=run_id)


def _intent(store: Store, clock: Clock, cid: str, seconds: float) -> None:
    """The engine's `order-intent` row, written before the order is sent."""
    clock.now = OPEN + timedelta(seconds=seconds)
    with store.transaction():
        store.append("order-intent", {"leg": "exit"}, key=cid)


def _sold_by_stop(store: Store, trade_id: str, quantity: str, price: str) -> None:
    with store.transaction():
        store.execute("UPDATE orders SET filled_quantity = ?, average_fill_price = ? "
                      "WHERE client_order_id = ?", (quantity, price, f"{trade_id}-stop"))


def _range_bar(minute: int, low: str, high: str) -> str:
    start = OPEN + timedelta(minutes=minute)
    return json.dumps({"symbol": "ABCD", "start": start.isoformat(), "open": "12.34",
                       "high": high, "low": low, "close": "12.34", "volume": "1000",
                       "provider": "webull", "receipt_time": start.isoformat()}) + "\n"


def _journaled_trade(paths: HistoryPaths) -> None:
    """Entry 12.34 x 10, fill read at 0:40.5, stop working at 0:42.75; a close sent at 3:05,
    filled at 12.75, read at 3:10."""
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30))
    _observed(store, clock, "t1-entry", 10, "ACCEPTED", "0")
    _observed(store, clock, "t1-entry", 40.5, "FILLED", "10")
    _observed(store, clock, "t1-stop", 42.75, "ACCEPTED", "0")
    _intent(store, clock, "t1-exit", 185)
    _observed(store, clock, "t1-exit", 189, "ACCEPTED", "0")
    _observed(store, clock, "t1-exit", 190, "FILLED", "10")
    _observed(store, clock, "t1-stop", 191, "CANCELLED", "0")
    store.close()


WILD = ("10.00", "20.00")  # a bar that must never count
BARS = {-1: _range_bar(-1, *WILD),  # before the fill's minute
        0: _range_bar(0, *WILD),  # the fill's minute: partial
        1: _range_bar(1, "12.30", "12.60"),
        2: _range_bar(2, "12.25", "12.70"),
        3: _range_bar(3, *WILD),
        4: _range_bar(4, *WILD),
        5: _range_bar(5, *WILD),
        6: _range_bar(6, *WILD)}


def _bars(*minutes: int, **override: str) -> list[str]:
    """Bars of `minutes`; `m3="low,high"` replaces the bar of minute 3."""
    out = []
    for m in minutes:
        given = override.get(f"m{m}")
        out.append(_range_bar(m, *given.split(",")) if given else BARS[m])
    return out


def test_stop_confirmation_time_and_excursions_from_journal_and_capture(paths):
    _journaled_trade(paths)
    _bars_file(paths, DAY, _bars(-1, 0, 1, 2, 3, 4))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.stop_confirmed_after_seconds == Decimal("2.250")
    assert trade.planned_loss_usd == Decimal("50")
    # Minutes 1 and 2 only: 0 is the fill's, 3 the close's send minute.
    assert trade.mae_usd == (Decimal("12.25") - Decimal("12.34")) * 10  # inner low: -0.90
    assert trade.mfe_usd == (Decimal("12.75") - Decimal("12.34")) * 10  # exit fill: 4.10


def test_a_market_close_is_bounded_by_its_send_minute_not_its_slow_fill_read(paths):
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30))
    _observed(store, clock, "t1-entry", 30, "FILLED", "10")
    _intent(store, clock, "t1-exit", 150)  # sent in minute 2
    _observed(store, clock, "t1-exit", 320, "FILLED", "10")  # read only in minute 5
    store.close()
    _bars_file(paths, DAY, _bars(0, 1, 2, 3, 4, 5))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.mae_usd == (Decimal("12.30") - Decimal("12.34")) * 10  # minute 1 only
    assert trade.mfe_usd == (Decimal("12.75") - Decimal("12.34")) * 10


def _stopped_out(paths: HistoryPaths, *, restart: bool = False) -> None:
    """Fill read at 10:00:30, stop working at 10:00:33 and read unfilled again at 10:04:20;
    it filled at 11.80 at 10:05:58, and the read that showed it came at 10:06:01."""
    store, clock = _timed_store(paths)
    first_run = _run(store)
    _trade(store, first_run, "t1", OPEN + timedelta(minutes=30), exit_=None)
    _sold_by_stop(store, "t1", "10", "11.80")
    _observed(store, clock, "t1-entry", 30, "FILLED", "10", first_run)
    _observed(store, clock, "t1-stop", 33, "ACCEPTED", "0", first_run)
    if not restart:
        _observed(store, clock, "t1-stop", 260, "ACCEPTED", "0", first_run)
    later_run = _run(store) if restart else first_run
    _observed(store, clock, "t1-stop", 361, "FILLED", "10", later_run)
    store.close()


def test_a_stop_fill_read_in_the_next_minute_never_counts_the_bar_after_the_exit(paths):
    _stopped_out(paths)
    # Minute 5 (10:05) holds the fall after the 10:05:58 fill; minute 4 is the last unfilled
    # read's own minute. Only minutes 1-3 count.
    _bars_file(paths, DAY, _bars(0, 1, 2, 3, 4, 5, 6, m3="12.20,12.95"))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.mae_usd == trade.result_gross_usd == Decimal("-5.40")  # the stop fill
    assert trade.mfe_usd == (Decimal("12.95") - Decimal("12.34")) * 10


def test_a_restart_between_the_last_unfilled_read_and_the_fill_read_keeps_the_proven_window(
        paths):
    _stopped_out(paths, restart=True)
    _bars_file(paths, DAY, _bars(0, 1, 2, 3, 4, 5, 6))
    store = Store.open_readonly(paths.database)
    runs = {r["run_id"] for r in store.query(
        "SELECT run_id FROM journal WHERE kind = 'order-observed' AND key = 't1-stop'")}
    store.close()
    assert len(runs) == 2
    (trade,) = History(paths).day(DAY, LATER).trades
    # Held is proven only up to 10:00:33: no inner bar, the fill prices alone.
    assert trade.mae_usd == Decimal("-5.40") and trade.mfe_usd == Decimal(0)


def _held(store: Store, clock: Clock, cid: str, seconds: float, filled: str = "0",
          state: str = "ACCEPTED") -> None:
    """The engine's periodic `order-held` row: an unchanged read of a working stop."""
    clock.now = OPEN + timedelta(seconds=seconds)
    with store.transaction():
        store.append("order-held", {"state": state, "raw_status": state,
                                    "filled_quantity": Decimal(filled)}, key=cid)


def _stop_out_with_held_rows(paths: HistoryPaths, *held: tuple[float, str]) -> None:
    """Fill read at 10:00:30, stop working at 10:00:33 (the only change before its fill),
    the given `order-held` rows, the stop's fill at 11.80 read at 10:06:01."""
    store, clock = _timed_store(paths)
    run_id = _run(store)
    _trade(store, run_id, "t1", OPEN + timedelta(minutes=30), exit_=None)
    _sold_by_stop(store, "t1", "10", "11.80")
    _observed(store, clock, "t1-entry", 30, "FILLED", "10", run_id)
    _observed(store, clock, "t1-stop", 33, "ACCEPTED", "0", run_id)
    for seconds, filled in held:
        _held(store, clock, "t1-stop", seconds, filled)
    _observed(store, clock, "t1-stop", 361, "FILLED", "10", run_id)
    store.close()


def test_held_rows_extend_the_window_of_a_stop_out(paths):
    _stop_out_with_held_rows(paths, (93, "0"), (153, "0"), (213, "0"))
    _bars_file(paths, DAY, _bars(0, 1, 2, 3, 4, 5, 6, m2="12.20,12.95"))
    (trade,) = History(paths).day(DAY, LATER).trades
    # The last held row is at 10:03:33: minutes 1 and 2 count, 3 (its own) does not.
    assert trade.mae_usd == trade.result_gross_usd == Decimal("-5.40")  # the stop fill
    assert trade.mfe_usd == (Decimal("12.95") - Decimal("12.34")) * 10
    assert trade.stop_confirmed_after_seconds == Decimal("3.000")  # held rows never confirm


def test_without_held_rows_a_stop_out_keeps_only_its_fill_prices(paths):
    _stop_out_with_held_rows(paths)
    _bars_file(paths, DAY, _bars(0, 1, 2, 3, 4, 5, 6, m2="12.20,12.95"))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.mae_usd == Decimal("-5.40") and trade.mfe_usd == Decimal(0)


def test_a_held_row_showing_or_following_a_fill_proves_nothing(paths):
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30), exit_=None)
    _sold_by_stop(store, "t1", "10", "11.80")
    _observed(store, clock, "t1-entry", 30, "FILLED", "10")
    _observed(store, clock, "t1-stop", 33, "ACCEPTED", "0")
    _held(store, clock, "t1-stop", 93, filled="4", state="PARTIALLY_FILLED")
    _observed(store, clock, "t1-stop", 100, "PARTIALLY_FILLED", "4")
    _held(store, clock, "t1-stop", 200)  # after a read showing a fill: not full quantity
    _observed(store, clock, "t1-stop", 361, "FILLED", "10")
    store.close()
    _bars_file(paths, DAY, _bars(0, 1, 2, 3, 4, 5, 6))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.mae_usd == Decimal("-5.40") and trade.mfe_usd == Decimal(0)


def test_a_malformed_held_row_shows_null_metrics(paths):
    _stop_out_with_held_rows(paths, (93, "0"))
    store, clock = _timed_store(paths)
    clock.now = OPEN + timedelta(seconds=150)
    with store.transaction():
        store.append("order-held", {"state": "ACCEPTED"}, key="t1-stop")
    store.close()
    _bars_file(paths, DAY, _bars(0, 1, 2, 3, 4, 5, 6))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert (trade.stop_confirmed_after_seconds, trade.mae_usd, trade.mfe_usd) == (
        None, None, None)


def test_a_partial_entry_fill_counts_only_the_shares_first_seen_filled(paths):
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30))
    _observed(store, clock, "t1-entry", 30, "PARTIALLY_FILLED", "4")
    _observed(store, clock, "t1-entry", 90, "FILLED", "10")
    _intent(store, clock, "t1-exit", 200)
    _observed(store, clock, "t1-exit", 202, "FILLED", "10")
    store.close()
    _bars_file(paths, DAY, _bars(0, 1, 2, 3))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.quantity == 10
    assert trade.mae_usd == (Decimal("12.25") - Decimal("12.34")) * 4
    assert trade.mfe_usd == (Decimal("12.75") - Decimal("12.34")) * 4


def test_several_exit_legs_end_the_window_at_the_earliest_proof(paths):
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30))
    with store.transaction():
        store.execute("UPDATE orders SET filled_quantity = '6' WHERE client_order_id = 't1-exit'")
    _sold_by_stop(store, "t1", "4", "12.20")
    _observed(store, clock, "t1-entry", 5, "FILLED", "10")
    _observed(store, clock, "t1-stop", 6, "ACCEPTED", "0")  # the stop's last unfilled read
    _observed(store, clock, "t1-stop", 70, "PARTIALLY_FILLED", "4")
    _intent(store, clock, "t1-exit", 140)
    _observed(store, clock, "t1-exit", 150, "FILLED", "6")  # the last shares: minute 2
    _observed(store, clock, "t1-stop", 250, "CANCELLED", "4")  # no more sold: not the end
    store.close()
    _bars_file(paths, DAY, _bars(0, 1, 2))  # minute 4 not needed: the end is minute 2
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.stop_confirmed_after_seconds == Decimal("1.000")
    assert trade.mae_usd == (Decimal("12.20") - Decimal("12.34")) * 10  # stop fill, no bar
    assert trade.mfe_usd == (Decimal("12.75") - Decimal("12.34")) * 10


def test_a_trade_within_two_minutes_has_only_its_fill_prices(paths):
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30), exit_="12.30")
    _observed(store, clock, "t1-entry", 50, "FILLED", "10")
    _intent(store, clock, "t1-exit", 65)
    _observed(store, clock, "t1-exit", 70, "FILLED", "10")  # next minute: no inner bar
    store.close()
    _bars_file(paths, DAY, _bars(0, 1))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.mae_usd == (Decimal("12.30") - Decimal("12.34")) * 10
    assert trade.mfe_usd == Decimal(0)  # never certainly above the entry: no guess


def test_a_sale_with_no_proof_of_holding_leaves_only_the_fill_prices(paths):
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30))
    _observed(store, clock, "t1-entry", 30, "FILLED", "10")
    _observed(store, clock, "t1-exit", 200, "FILLED", "10")  # no intent row journaled
    store.close()
    _bars_file(paths, DAY, _bars(0, 1, 2, 3))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.mae_usd == Decimal(0)
    assert trade.mfe_usd == (Decimal("12.75") - Decimal("12.34")) * 10


def test_the_daily_loss_path_reads_no_journal_and_no_capture(paths):
    _journaled_trade(paths)
    _bars_file(paths, DAY, _bars(0, 1, 2, 3))
    store = Store.open_readonly(paths.database)
    (trade,) = closed_trades(store, closed_on=DAY)
    shown = trade_metrics(store, trade, None)  # what the status writer adds, for display
    store.close()
    assert (trade.stop_confirmed_after_seconds, trade.mae_usd, trade.mfe_usd) == (
        None, None, None)
    assert shown.stop_confirmed_after_seconds == Decimal("2.250")
    assert (shown.mae_usd, shown.mfe_usd) == (None, None)


@pytest.mark.parametrize("minutes", [(), (0, 1, 2), (1, 2, 3)],
                         ids=["no-capture", "no-exit-minute", "no-fill-minute"])
def test_excursions_are_null_without_capture_of_both_edge_minutes(paths, minutes):
    _journaled_trade(paths)
    if minutes:
        _bars_file(paths, DAY, _bars(*minutes))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert (trade.mae_usd, trade.mfe_usd) == (None, None)
    assert trade.stop_confirmed_after_seconds == Decimal("2.250")


def test_a_malformed_journal_row_shows_null_metrics_not_an_error(paths):
    _journaled_trade(paths)
    store, clock = _timed_store(paths)
    _observed(store, clock, "t1-stop", 200, "ACCEPTED", "0")
    with store.transaction():
        store.append("order-observed", {"state": "ACCEPTED"}, key="t1-stop")
    store.close()
    _bars_file(paths, DAY, _bars(0, 1, 2, 3))
    (trade,) = History(paths).day(DAY, LATER).trades
    assert (trade.stop_confirmed_after_seconds, trade.mae_usd, trade.mfe_usd) == (
        None, None, None)
    assert trade.result_gross_usd == Decimal("4.10")


def test_day_trade_carries_its_entry_send(paths):
    _journaled_trade(paths)
    store, clock = _timed_store(paths)
    for seconds, kind in ((1, "order-intent"), (1.25, "order-attempted"),
                          (1.5, "order-acknowledged")):
        clock.now = OPEN + timedelta(seconds=seconds)
        with store.transaction():
            store.append(kind, {}, key="t1-entry")
    store.close()
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.entry == contract.EntrySend(
        "t1-entry", OPEN + timedelta(seconds=1), OPEN + timedelta(seconds=1.25),
        contract.SendOutcome.ACKNOWLEDGED, contract.SendOutcome.ACKNOWLEDGED, 1,
        OPEN + timedelta(seconds=40.5))
    assert trade.entry_cause is None


def test_an_unreadable_entry_row_shows_null_entry_and_keeps_the_metrics(paths):
    _journaled_trade(paths)
    store, clock = _timed_store(paths)
    with store.transaction():
        store.execute("INSERT INTO journal(at_utc, run_id, kind, key, body) "
                      "VALUES ('not a time', NULL, 'order-attempted', 't1-entry', '{}')")
    store.close()
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.entry == contract.EntrySend(None, None, None, None, None, None, None)
    assert trade.entry_cause.startswith("journal unreadable: journal row ")
    assert trade.stop_confirmed_after_seconds == Decimal("2.250")


@pytest.mark.parametrize("rows", [
    [("t1-entry", 5, "FILLED", "10"), ("t1-stop", 6, "REJECTED", "0")],
    [("t1-entry", 5, "FILLED", "10")],
    [("t1-stop", 3, "ACCEPTED", "0"), ("t1-entry", 5, "FILLED", "10")],
    [("t1-stop", 6, "ACCEPTED", "0")],
], ids=["stop-rejected", "stop-never-read", "working-before-fill", "fill-never-observed"])
def test_stop_never_confirmed_after_a_fill_is_null(paths, rows):
    store, clock = _timed_store(paths)
    _trade(store, _run(store), "t1", OPEN + timedelta(minutes=30))
    for cid, seconds, state, filled in rows:
        _observed(store, clock, cid, seconds, state, filled)
    store.close()
    (trade,) = History(paths).day(DAY, LATER).trades
    assert trade.stop_confirmed_after_seconds is None


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
