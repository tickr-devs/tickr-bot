"""Screener recorder (runtime/screeners.py) and the capture events it and the bot write."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from baseline import load_baseline

from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.data.base import FakeMarketData, ScreenerKind, ScreenerRow
from tickr.data.capture import (
    BOT_START,
    EVENTS_FILE,
    SCREENER_FILE,
    SCREENER_RECORDED_FILE,
    CaptureWriter,
)
from tickr.runtime.screeners import ScreenerRecorder

ROOT = Path(__file__).resolve().parents[2]
CFG = load_baseline()
DAY = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent).day(date(2026, 11, 16))


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 11, 16, hh, mm, ss, tzinfo=NEW_YORK)


def rig(tmp_path: Path) -> tuple[FakeMarketData, CaptureWriter, ScreenerRecorder]:
    source = FakeMarketData()
    for kind in ScreenerKind:
        source.set_screener(kind, [ScreenerRow(kind, 1, f"id-{kind}", f"S{len(kind)}", "NSQ",
                                               "USD", None, {"price": "2.5"})])
    capture = CaptureWriter(tmp_path, source.provider)
    return source, capture, ScreenerRecorder(CFG, source, capture)


def lines(capture: CaptureWriter, name: str) -> list[dict]:
    path = capture.path(DAY.ny_date, name)
    return [json.loads(x) for x in path.read_text("utf-8").splitlines()] if path.exists() else []


def test_records_every_list_once_a_minute_in_the_pre_and_post_market(tmp_path: Path) -> None:
    source, capture, recorder = rig(tmp_path)
    assert recorder.tick(DAY, at(3, 59, 59)) is None  # before the pre-market: nothing
    rows = recorder.tick(DAY, at(4, 0, 5))
    assert rows is not None and [r.kind for r in rows] == list(ScreenerKind)
    assert recorder.tick(DAY, at(4, 0, 50)) is None  # not due before the next minute
    assert recorder.tick(DAY, at(4, 1, 0)) is not None
    assert recorder.tick(DAY, at(10, 0)) is None  # the regular session is discovery's
    assert recorder.tick(DAY, at(16, 0, 1)) is not None
    assert recorder.tick(DAY, at(20, 0)) is None  # the post-market is over
    recorded = lines(capture, SCREENER_RECORDED_FILE)
    assert len(recorded) == 3 * 3 and {r["kind"] for r in recorded} == set(ScreenerKind)
    assert recorded[0]["fields"] == {"price": "2.5"}
    assert [r["session"] for r in recorded] == ["PRE"] * 6 + ["POST"] * 3
    assert lines(capture, SCREENER_FILE) == []  # discovery's file stays discovery's
    assert source.calls == [("screener", (k.value, s)) for s in ("PRE", "PRE", "POST")
                            for k in ScreenerKind]


def test_a_kind_not_read_is_marked_with_the_ones_after_it(tmp_path: Path) -> None:
    source, capture, recorder = rig(tmp_path)
    source.limit("screener", 1)
    assert recorder.tick(DAY, at(17, 0)) is None
    events = lines(capture, EVENTS_FILE)
    assert [(e["event"], e["kind"], e["cause"], e["source"]) for e in events] == [
        ("screener-skipped", "LOSERS", "budget", "recorder"),
        ("screener-skipped", "TOP_ACTIVES", "budget", "recorder")]
    assert len(lines(capture, SCREENER_RECORDED_FILE)) == 1  # the one list it got is kept


def test_events_carry_their_fields_and_time(tmp_path: Path) -> None:
    _, capture, _ = rig(tmp_path)
    capture.event(BOT_START, at(4, 0), run_id="7", code_version="abc")
    (event,) = lines(capture, EVENTS_FILE)
    assert event["event"] == "bot-start" and event["run_id"] == "7"
    assert datetime.fromisoformat(event["receipt_time"]) == at(4, 0)
