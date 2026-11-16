"""Profile recorder (runtime/profiles.py): the screeners' instrument profiles, captured."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from tickr import config
from tickr.calendar import NEW_YORK
from tickr.data.base import FakeMarketData, InstrumentProfile, ScreenerKind, ScreenerRow
from tickr.data.capture import EVENTS_FILE, PROFILES_FILE, CaptureError, CaptureWriter
from tickr.runtime.profiles import ProfileRecorder, move_percent

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
DAY = date(2026, 11, 16)
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=NEW_YORK)
REFRESH = timedelta(seconds=float(CFG.market_data.profile_refresh_seconds))


def row(kind: ScreenerKind, symbol: str, price: str = "1.20", pre_close: str = "1.00",
        ) -> ScreenerRow:
    return ScreenerRow(kind, 1, f"id-{symbol}", symbol, "NSQ", "USD", None,
                       {"price": price, "pre_close": pre_close})


def profile(symbol: str, instrument_id: str | None = None) -> InstrumentProfile:
    return InstrumentProfile(instrument_id or f"id-{symbol}", symbol, "COMMON_STOCK",
                             {"shortable": "true", "easy_to_borrow": "false",
                              "margin_requirement_short": "1.0000000000"})


def rig(tmp_path: Path, symbols: list[str]) -> tuple[FakeMarketData, CaptureWriter,
                                                     ProfileRecorder]:
    source = FakeMarketData()
    for symbol in symbols:
        source.set_profile(profile(symbol))
    capture = CaptureWriter(tmp_path, source.provider)
    return source, capture, ProfileRecorder(CFG, source, capture, lambda: T0, in_place)


def in_place(target):
    target()


def lines(capture: CaptureWriter, name: str, day: date = DAY) -> list[dict]:
    path = capture.path(day, name)
    return [json.loads(x) for x in path.read_text("utf-8").splitlines()] if path.exists() else []


def asked(source: FakeMarketData) -> list[tuple[str, ...]]:
    return [args for name, args in source.calls if name == "profiles"]


def test_first_listed_once_then_losers_and_big_gainers_again(tmp_path: Path) -> None:
    rows = [row(ScreenerKind.GAINERS, "BIG", "1.25", "1.00"),  # +25 %
            row(ScreenerKind.GAINERS, "SMALL", "1.10", "1.00"),  # +10 %
            row(ScreenerKind.LOSERS, "DOWN", "0.80", "1.00"),
            row(ScreenerKind.TOP_ACTIVES, "BUSY")]
    source, capture, recorder = rig(tmp_path, ["BIG", "SMALL", "DOWN", "BUSY"])
    recorder.record(rows, T0)
    recorder.record(rows, T0 + REFRESH - timedelta(seconds=1))  # nothing due
    recorder.record(rows, T0 + REFRESH)
    assert asked(source) == [("BIG", "SMALL", "DOWN", "BUSY"), ("BIG", "DOWN")]
    captured = lines(capture, PROFILES_FILE)
    assert [c["symbol"] for c in captured] == ["BIG", "SMALL", "DOWN", "BUSY", "BIG", "DOWN"]
    assert captured[0]["fields"]["shortable"] == "true"
    assert captured[0]["instrument_id"] == "id-BIG" and captured[0]["provider"] == "fake"


def test_a_budget_refusal_is_marked_and_the_symbols_stay_due(tmp_path: Path) -> None:
    rows = [row(ScreenerKind.LOSERS, "AAA"), row(ScreenerKind.LOSERS, "BBB")]
    source, capture, recorder = rig(tmp_path, ["AAA", "BBB"])
    source.limit("profiles", 0)
    recorder.record(rows, T0)
    (event,) = lines(capture, EVENTS_FILE)
    assert (event["event"], event["symbols"], event["waiting"], event["cause"]) == (
        "profiles-skipped", "AAA,BBB", "0", "budget")
    source.limit("profiles", 1)
    recorder.record(rows, T0 + timedelta(seconds=1))
    assert [c["symbol"] for c in lines(capture, PROFILES_FILE)] == ["AAA", "BBB"]


def test_at_most_the_configured_requests_a_round(tmp_path: Path) -> None:
    chunk = CFG.market_data.profile_symbols_per_request
    most = CFG.market_data.profile_max_requests_per_round
    symbols = [f"S{n}" for n in range(chunk * most + 2)]
    source, capture, recorder = rig(tmp_path, symbols)
    recorder.record([row(ScreenerKind.TOP_ACTIVES, s) for s in symbols], T0)
    assert len(asked(source)) == most
    (event,) = lines(capture, EVENTS_FILE)
    assert (event["symbols"], event["waiting"], event["cause"]) == ("", "2", "round-cap")


def test_only_the_listed_instrument_of_a_symbol_is_kept(tmp_path: Path) -> None:
    source, capture, recorder = rig(tmp_path, ["OPENW"])
    source.set_profile(profile("OPENW", "stale-id"))  # a second, older row of the symbol
    recorder.record([row(ScreenerKind.GAINERS, "OPENW")], T0)
    assert [c["instrument_id"] for c in lines(capture, PROFILES_FILE)] == ["id-OPENW"]


def test_a_new_new_york_day_captures_every_instrument_again(tmp_path: Path) -> None:
    source, capture, recorder = rig(tmp_path, ["AAA"])
    rows = [row(ScreenerKind.TOP_ACTIVES, "AAA")]
    recorder.record(rows, T0)
    tomorrow = T0 + timedelta(days=1)
    recorder.record(rows, tomorrow)
    assert len(asked(source)) == 2
    assert len(lines(capture, PROFILES_FILE, tomorrow.date())) == 1


def test_a_move_not_stated_is_never_a_big_gainer() -> None:
    assert move_percent(row(ScreenerKind.GAINERS, "A", "1.30", "1.00")) == 30
    assert move_percent(row(ScreenerKind.GAINERS, "A", "1.30", "0")) is None
    assert move_percent(row(ScreenerKind.GAINERS, "A", "n/a", "1.00")) is None
    assert move_percent(replace(row(ScreenerKind.GAINERS, "A"), fields={})) is None


def test_without_a_capture_nothing_is_read(tmp_path: Path) -> None:
    source = FakeMarketData()
    ProfileRecorder(CFG, source, None, lambda: T0, in_place).submit(
        [row(ScreenerKind.LOSERS, "AAA")])
    assert asked(source) == []


def test_submitted_rounds_run_on_the_thread_and_failures_wait_for_the_loop(tmp_path: Path,
                                                                          ) -> None:
    source, capture, recorder = rig(tmp_path, ["AAA"])
    rows = [row(ScreenerKind.LOSERS, "AAA")]

    def refuse(*args, **kwargs):
        raise CaptureError("profiles.jsonl: disk full")

    capture.profiles = refuse  # type: ignore[method-assign]
    recorder.submit(rows)
    (error,) = recorder.take_errors()
    assert isinstance(error, CaptureError) and recorder.take_errors() == []


def test_a_stopped_recorder_sends_nothing(tmp_path: Path) -> None:
    source, _, recorder = rig(tmp_path, ["AAA"])
    assert recorder.stop(0)
    recorder.submit([row(ScreenerKind.LOSERS, "AAA")])
    assert asked(source) == []
