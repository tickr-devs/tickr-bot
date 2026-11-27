"""The NYSE extension of the pinned training calendar."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import model_inputs as inputs
import pytest

from tickr.calendar import NEW_YORK, MarketDay
from tickr.model import features
from tickr.model.errors import ModelRefused
from tickr.model.features import calendar_ext as cx
from tickr.model.features import full

PINNED = features.PINNED_DIR
CONFIG_CALENDAR = Path(__file__).parents[2] / "config" / "calendar"
TRAINING_MD5 = "3c22591622d1f06ce1611f9fcf5c702d"
TRAINING_LAST = "2026-12-31"
EXT_DAY = "2027-01-04"  # the extension's first session
EXT_EARLY = "2027-11-26"  # an extension early close (13:00 New York), a half day of the bot


def _standard_day(day):
    """A MarketDay with the standard hours, not from the bot calendar (a disagreeing day)."""
    d = datetime.date.fromisoformat(day)

    def at(h, m=0):
        return datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=NEW_YORK)

    return MarketDay(d, at(4), at(9, 30), at(16), at(20), False, "test: standard hours")


def _training():
    text = (PINNED / "trading-days.txt").read_bytes()
    assert hashlib.md5(text).hexdigest() == TRAINING_MD5  # the prefix bytes are the training's
    return [x for x in text.decode().split("\n") if x and not x.startswith("#")]


def _copy(tmp_path):
    out = tmp_path / "pinned"
    out.mkdir()
    for name in (*cx.EXT_PINS, cx.SIDECAR):
        shutil.copy(PINNED / name, out / name)
    return out


def test_prefix_is_preserved_and_extension_follows_once():
    training = _training()
    ext = cx.load()
    got = cx.extend(training)
    assert got[: len(training)] == training and training[-1] == TRAINING_LAST
    assert got[len(training):] == list(ext.days)
    assert got == sorted(set(got)) and ext.days[0] > TRAINING_LAST
    assert all(date.fromisoformat(d).weekday() < 5 for d in ext.days)
    assert not set(ext.closures) & set(ext.days)


def test_overlap_or_gap_before_the_training_end_is_refused():
    training = _training()
    with pytest.raises(ModelRefused) as exc:
        cx.extend([*training, "2027-01-04"])
    assert exc.value.cause == "calendar_ext_overlap"
    with pytest.raises(ModelRefused) as exc:
        cx.extend(cx.extend(training))  # appended twice
    assert exc.value.cause == "calendar_ext_overlap"


def test_a_date_past_the_extension_is_refused():
    cal = cx.extend(_training())
    cx.require_covered(cal[-1], cal)
    with pytest.raises(ModelRefused) as exc:
        cx.require_covered("2029-01-02", cal)
    assert exc.value.cause == "calendar_uncovered"


def test_early_closes_are_sessions_with_their_own_close_time():
    ext = cx.load()
    assert ext.early_close == {"2027-11-26": "13:00", "2028-07-03": "13:00",
                               "2028-11-24": "13:00"}
    assert all(d in ext.days for d in ext.early_close)
    assert cx.early_close("2027-11-26", ext) == "13:00"
    assert cx.early_close("2027-11-29", ext) is None
    assert "2027-11-25" not in ext.days and "2028-01-03" in ext.days


def test_md5_pins_and_sidecar_agree():
    side = json.loads((PINNED / cx.SIDECAR).read_text())
    assert side["md5"] == cx.EXT_PINS and side["source_url"].startswith("https://www.nyse.com/")
    for name, pin in cx.EXT_PINS.items():
        assert hashlib.md5((PINNED / name).read_bytes()).hexdigest() == pin


def test_a_changed_extension_byte_is_refused(tmp_path):
    d = _copy(tmp_path)
    (d / cx.DAYS_FILE).write_bytes((d / cx.DAYS_FILE).read_bytes() + b"2029-01-02\n")
    with pytest.raises(ModelRefused) as exc:
        cx.load(d)
    assert exc.value.cause == "calendar_ext_md5"
    (d / cx.DAYS_FILE).unlink()
    with pytest.raises(ModelRefused) as exc:
        cx.load(d)
    assert exc.value.cause == "calendar_ext_missing"


def test_the_bots_own_calendar_agrees_for_2027():
    cx.reconcile_bot_calendar(CONFIG_CALENDAR)


def test_disagreement_with_the_bots_calendar_is_refused(tmp_path):
    for field, value in (("day", "2027-11-24"), ("regular_close", "12:00")):
        data = json.loads((CONFIG_CALENDAR / "us-equity-2027.json").read_text())
        row = next(e for e in data["exceptions"] if e["day"] == "2027-11-26")
        if field == "day":
            row["day"] = value
        else:
            row[field] = value
        (tmp_path / "us-equity-2027.json").write_text(json.dumps(data))
        with pytest.raises(ModelRefused) as exc:
            cx.reconcile_bot_calendar(tmp_path)
        assert exc.value.cause == "calendar_disagrees"


def test_the_live_context_reads_the_extended_calendar_and_research_the_pinned_one():
    ctx = full.live_context(inputs.market_day(EXT_DAY))
    cal = ctx.code.labels.calendar()
    assert cal[-1] == cx.load().days[-1] and "2027-03-26" not in cal and "2027-11-26" in cal
    assert cal[: len(_training())] == _training()
    with pytest.raises(SystemExit):
        ctx.code.labels.calendar(str(PINNED / cx.DAYS_FILE), TRAINING_MD5)  # pin still checked
    assert ctx.code.labels.calendar(str(PINNED / "trading-days.txt")) == _training()
    # the research import keeps the pinned function and file
    assert inputs.research().labels.calendar() == _training()


def test_an_extension_day_passes_the_live_checks():
    ctx = full.live_context(inputs.market_day(EXT_DAY))
    assert ctx.day == EXT_DAY and ctx.end_minute == 960
    with pytest.raises(ModelRefused) as exc:  # past the extension: still unknown
        full.live_context(inputs.market_day("2029-01-02"))
    assert exc.value.cause == "unknown_trading_day"
    closed = _standard_day("2027-03-26")  # Good Friday: in no calendar's sessions
    with pytest.raises(ModelRefused) as exc:
        full.live_context(closed)
    assert exc.value.cause == "unknown_trading_day"


def test_an_extension_early_close_meets_the_half_day_policy():
    day = inputs.market_day(EXT_EARLY)
    assert day.half_day and day.regular_close.strftime("%H:%M") == cx.load().early_close[EXT_EARLY]
    with pytest.raises(ModelRefused) as exc:
        full.live_context(day)
    assert exc.value.cause == "half_day_outside_session_policy"
    assert full.live_context(day, full.HALF_DAYS_UNTIL_CLOSE).end_minute == 540  # 13:00


def test_a_bot_day_that_disagrees_with_the_extension_is_refused():
    early = inputs.market_day(EXT_EARLY)
    later = dataclasses.replace(early, regular_close=early.regular_close.replace(hour=14))
    for day in (_standard_day(EXT_EARLY), later,
                dataclasses.replace(inputs.market_day(EXT_DAY), half_day=True)):
        with pytest.raises(ModelRefused) as exc:
            full.live_context(day, full.HALF_DAYS_UNTIL_CLOSE)
        assert exc.value.cause == "calendar_disagrees", day
