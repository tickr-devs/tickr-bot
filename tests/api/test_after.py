"""Prices after an exit or a skipped signal, overshoot (day-v1) and what-if (skipped-v1)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.after import AfterSettings, Closes, SkippedAfter, overshoot, prices_after
from tickr.api.history import History, HistoryPaths, closed_trades
from tickr.api.observer_results import SimulationPaths
from tickr.api.server import settings_from_config
from tickr.calendar import SessionCalendar
from tickr.config import ConfigError
from tickr.data.capture import read_captured_bars
from tickr.observers.log import DecisionLog
from tickr.replay.clock import calendar_clock
from tickr.replay.settings import load_settings
from tickr.research.records import Admission
from tickr.research.whatif import simulate_one
from tickr.strategy.base import Role

from .schema_check import errors
from .test_decisions import ACTIVE, ROOT, _write
from .test_history import DAY, LATER, OPEN, _bars_file, _run, _store, _trade
from .test_skipped import M1, _capture, _enter, cfg  # noqa: F401 - pytest fixture

CALENDAR = ROOT / "config" / "calendar"
CLOSE = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)  # 16:00 New York
AFTER = AfterSettings((1, 5, 15, 60), 4, CALENDAR)


def _bar(minute: int, high: str, close: str) -> contract.BarV1:
    return contract.BarV1(OPEN + minute * M1, Decimal("12.00"), Decimal(high),
                          Decimal("11.90"), Decimal(close), Decimal(1000))


def _points(bars, anchor, close=CLOSE, minutes=(1, 5)):
    return prices_after("ABCD", bars, anchor, Decimal("12.00"), None, minutes, close)


# ---------------------------------------------------------------- prices after


def test_the_close_of_the_bar_covering_the_moment():
    bars = [_bar(m, "12.10", f"12.0{m}") for m in range(0, 7)]
    anchor = OPEN + timedelta(seconds=20)  # inside minute 0: +1 min is in minute 1
    one, five = _points(bars, anchor)
    assert (one.minutes, one.at, one.price, one.change_usd, one.cause) == (
        1, anchor + M1, Decimal("12.01"), Decimal("0.01"), None)
    assert (five.price, five.change_usd) == (Decimal("12.05"), Decimal("0.05"))


@pytest.mark.parametrize("bars, close, cause", [
    ([], CLOSE, "no bar recorded for ABCD that day"),
    ([_bar(0, "12", "12"), _bar(1, "12", "12")], CLOSE, "the bars end at 2026-11-16T15:01:00.000Z"),
    ([_bar(0, "12", "12"), _bar(6, "12", "12")], CLOSE,
     "no bar recorded for the minute 2026-11-16T15:05:00.000Z"),
    ([_bar(m, "12", "12") for m in range(7)], OPEN + 3 * M1,
     "the session ended at 2026-11-16T15:03:00.000Z"),
    ([_bar(m, "12", "12") for m in range(7)], "session calendar unreadable: x",
     "session calendar unreadable: x"),
])
def test_a_point_without_its_bar_is_null_with_the_cause(bars, close, cause):
    *_, five = _points(bars, OPEN, close)
    assert (five.price, five.change_usd, five.cause) == (None, None, cause)


def test_an_unknown_reference_keeps_the_price_and_says_why_the_change_is_null():
    (one,) = prices_after("ABCD", [_bar(1, "12", "12.30")], OPEN, None, "no reference", (1,),
                          CLOSE)
    assert (one.price, one.change_usd, one.cause) == (Decimal("12.30"), None, "no reference")


# ---------------------------------------------------------------- overshoot


def _closed(at: datetime, exit_price: str = "12.00") -> contract.DayTrade:
    return contract.DayTrade(
        "t1", "ABCD", Decimal(10), Decimal("11.50"), Decimal(exit_price), OPEN, at,
        "strategy_exit", None, Decimal(50), Decimal(5), None, None, None,
        contract.EntrySend(None, None, None, None, None, None, None), None, (), None, None,
        None, None)


def test_overshoot_is_the_highest_high_after_the_exit_minute_within_the_longest_offset():
    bars = [_bar(0, "13.00", "12"),  # the exit's minute: may be before the exit, left out
            _bar(1, "12.10", "12"), _bar(3, "12.40", "12"), _bar(5, "12.20", "12"),
            _bar(6, "14.00", "12")]  # after the window
    usd, r, cause = overshoot(_closed(OPEN + timedelta(seconds=30)), bars, 5, CLOSE, 4)
    assert (usd, r, cause) == (Decimal("0.40"), Decimal("0.0800"), None)  # 0.40 x 10 / 50


def test_overshoot_is_signed_when_the_price_never_came_back():
    usd, r, _ = overshoot(_closed(OPEN), [_bar(1, "11.80", "11"), _bar(2, "11.90", "11")],
                          1, CLOSE, 4)
    assert (usd, r) == (Decimal("-0.20"), Decimal("-0.0400"))


@pytest.mark.parametrize("bars, close, cause", [
    ([], CLOSE, "no bar recorded for ABCD that day"),
    ([_bar(0, "12", "12"), _bar(3, "12", "12")], CLOSE,
     "the bars end at 2026-11-16T15:03:00.000Z, before 2026-11-16T15:05:00.000Z"),
    ([_bar(0, "12", "12"), _bar(6, "12", "12")], CLOSE,
     "no bar recorded after the exit minute up to 2026-11-16T15:05:00.000Z"),
    ([_bar(0, "12", "12")], OPEN + M1,
     "no minute after the exit before the close 2026-11-16T15:01:00.000Z"),
])
def test_overshoot_without_bars_is_null_with_the_cause(bars, close, cause):
    assert overshoot(_closed(OPEN), bars, 5, close, 4) == (None, None, cause)


def test_the_overshoot_window_stops_at_the_regular_close():
    bars = [_bar(0, "12", "12"), _bar(1, "12.30", "12"), _bar(2, "15.00", "12")]
    assert overshoot(_closed(OPEN), bars, 60, OPEN + 2 * M1, 4)[0] == Decimal("0.30")


def test_closes_come_from_the_calendar_and_name_a_refused_day():
    closes = Closes(CALENDAR)
    assert closes.close(DAY) == CLOSE
    assert closes.close(DAY - timedelta(days=1)).startswith("calendar refuses 2026-11-15")
    assert Closes(CALENDAR / "missing").close(DAY).startswith("session calendar unreadable")


# ---------------------------------------------------------------- day-v1


@pytest.fixture
def paths(tmp_path) -> HistoryPaths:
    return HistoryPaths("sandbox", tmp_path / "tickr.sqlite3", tmp_path / "capture" / "sandbox",
                        CALENDAR, Decimal(900))


def _line(minute: int, high: str, close: str) -> str:
    start = OPEN + minute * M1
    return json.dumps({"symbol": "ABCD", "start": start.isoformat(), "open": "12.30",
                       "high": high, "low": "12.00", "close": close, "volume": "1000",
                       "provider": "webull", "receipt_time": start.isoformat()}) + "\n"


def test_day_trades_carry_prices_after_and_overshoot(paths):
    store = _store(paths)
    _trade(store, _run(store), "t1", OPEN + 30 * M1)  # exit 12.75, 10 shares, 1 R = 50
    store.close()
    _bars_file(paths, DAY, [_line(m, "13.05" if m == 40 else "12.95", f"12.{m}")
                            for m in range(30, 91)])
    document = History(paths, AFTER).day(DAY, LATER)
    (trade,) = document.trades
    assert [(p.minutes, p.price, p.change_usd, p.cause) for p in trade.prices_after] == [
        (1, Decimal("12.31"), Decimal("-0.44"), None),
        (5, Decimal("12.35"), Decimal("-0.40"), None),
        (15, Decimal("12.45"), Decimal("-0.30"), None),
        (60, Decimal("12.90"), Decimal("0.15"), None)]
    assert trade.prices_after_cause is None
    assert (trade.overshoot_usd, trade.overshoot_r, trade.overshoot_cause) == (
        Decimal("0.30"), Decimal("0.0600"), None)
    body = contract.to_jsonable(document)
    assert errors(contract.json_schema(contract.DayV1), body) == []


def test_the_daily_loss_path_never_computes_them(paths):
    store = _store(paths)
    _trade(store, _run(store), "t1", OPEN + 30 * M1)
    (trade,) = closed_trades(store, "sandbox")
    assert (trade.prices_after, trade.prices_after_cause, trade.overshoot_cause) == (
        (), "not read on this path", "not read on this path")


# ---------------------------------------------------------------- skipped-v1


def test_skipped_rows_carry_prices_after_and_the_whatif_simulation(cfg):  # noqa: F811
    log = DecisionLog.from_config(cfg)
    refused = _enter(ACTIVE, Role.ACTIVE, minute=5, admission=Admission.REFUSED,
                     cause="POSITION_LIMIT_REACHED: 1 open position, limit 1")
    _write(log, refused)
    path = _capture(cfg)
    skipped = SkippedAfter(SimulationPaths.from_config(cfg), (1, 5, 60), 4)
    records = log.records(DAY, role=Role.ACTIVE)
    document = skipped.document(DAY, DAY + timedelta(days=1), records, log.path(DAY))
    (signal,) = document.signals
    # The decision bar (10:05 New York) closes at 12.00; minute 1 after trades at 12.05 and the
    # price falls through the stop to 11.75 from minute 5.
    assert [(p.minutes, p.price, p.change_usd, p.cause) for p in signal.prices_after] == [
        (1, Decimal("12.05"), Decimal("0.05"), None),
        (5, Decimal("11.75"), Decimal("-0.25"), None),
        (60, None, None, "the bars end at 2026-11-16T15:59:00.000Z")]
    settings = load_settings(cfg.root / cfg.api.replay_settings_file)
    clock = calendar_clock(SessionCalendar.load(CALENDAR), cfg.session)
    bars = [b for (s, _), b in sorted(read_captured_bars(path).bars.items()) if s == "ABCD"]
    expected = simulate_one(refused, bars, settings, clock)
    assert signal.what_if.exit_reason == expected.trade.exit_reason == "stop"
    assert signal.what_if.result_gross_usd == expected.result_usd.quantize(
        Decimal(1).scaleb(-settings.output_decimal_places))
    assert signal.what_if.result_gross_usd < 0 and signal.what_if.cause is None
    body = contract.to_jsonable(document)
    assert errors(contract.json_schema(contract.SkippedV1), body) == []
    # Same files: the memo answers without simulating again.
    assert skipped.document(DAY, DAY + timedelta(days=1), records, log.path(DAY)) is document


def test_a_signal_that_cannot_be_simulated_says_why(cfg):  # noqa: F811
    log = DecisionLog.from_config(cfg)
    _write(log, _enter(ACTIVE, Role.ACTIVE, minute=5, admission=Admission.REFUSED,
                       cause="POSITION_LIMIT_REACHED: x", quantity=None))
    _capture(cfg)
    skipped = SkippedAfter(SimulationPaths.from_config(cfg), (1,), 4)
    (signal,) = skipped.document(DAY, DAY + timedelta(days=1), log.records(DAY, role=Role.ACTIVE),
                                 log.path(DAY)).signals
    assert signal.what_if == contract.WhatIfV1(
        None, None, None, "not_simulated: no quantity: none recorded and none given")
    assert signal.prices_after[0].price == Decimal("12.05")


def test_an_unreadable_calendar_is_a_cause_and_not_remembered(cfg):  # noqa: F811
    log = DecisionLog.from_config(cfg)
    _write(log, _enter(ACTIVE, Role.ACTIVE, minute=5, admission=Admission.REFUSED,
                       cause="POSITION_LIMIT_REACHED: x"))
    _capture(cfg)
    paths = SimulationPaths.from_config(cfg)
    skipped = SkippedAfter(replace(paths, calendar=paths.calendar.parent / "missing"), (1,), 4)
    records = log.records(DAY, role=Role.ACTIVE)
    first = skipped.document(DAY, DAY, records, log.path(DAY))
    (signal,) = first.signals
    assert signal.prices_after[0].cause.startswith("session calendar unreadable")
    assert signal.what_if.cause.startswith("session calendar unreadable")
    assert skipped.document(DAY, DAY, records, log.path(DAY)) is not first


# ---------------------------------------------------------------- config


@pytest.mark.parametrize("minutes", ["[]", "[0, 5]", "[5, 1]", "[1, 1]"])
def test_price_after_minutes_must_be_strictly_increasing_and_positive(tmp_path, minutes):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    assert text.count("\nprice_after_minutes = [1, 5, 15, 60]\n") == 1
    path = tmp_path / "bot.toml"
    path.write_text(text.replace("\nprice_after_minutes = [1, 5, 15, 60]\n",
                                 f"\nprice_after_minutes = {minutes}\n"), "utf-8")
    with pytest.raises(ConfigError, match="price_after_minutes"):
        settings_from_config(config.load(path, root=ROOT))


def test_settings_carry_the_offsets_and_the_calendar():
    after = settings_from_config(config.load(ROOT / "config" / "bot.toml", root=ROOT)).after
    assert after.minutes == (1, 5, 15, 60) and after.calendar == CALENDAR
    assert isinstance(after.calendar, Path)
