"""Stream-built day files: written at the day's end from the feed-arrival captures, compared with
the published flat file. Fake stream, synthetic files; no network, no credentials (canary)."""

from __future__ import annotations

import dataclasses
import datetime
import gzip
import threading

import pytest
import test_arrival as ta  # the arrival tests' fake stream, clock and synthetic capture

from tickr import cli, config
from tickr.data.base import FakeMarketData
from tickr.model import arrival, massive, streamday
from tickr.model.errors import ModelRefused

ROOT, CFG, CAL, NS = ta.ROOT, ta.CFG, ta.CAL, ta.NS
HALF_DAY = CAL.day(datetime.date(2026, 11, 27))  # early close: 13:00, extended 17:00
TAIL = CFG.massive.arrival_report_points_seconds[-1]


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv(CFG.massive.api_key_variable, "api-canary-1")


def record(cfg, day, batches, stop=None):
    start = day.extended_close - datetime.timedelta(seconds=90)
    clock = ta.Clock(ta.ns(start))
    sock = ta.Socket([[{"ev": "status", "status": "auth_success"}], *batches])

    def wait(seconds):
        sock.drained.wait(5)
        clock.now += int(seconds * NS)

    said: list[str] = []
    code = arrival.record(cfg, day, FakeMarketData(), stop or threading.Event(), said.append,
                          connect=lambda url, max_size: sock, wall_ns=clock.wall,
                          mono_ns=clock.mono, wait=wait)
    return code, said, start


def test_the_day_is_kept_when_the_stream_reaches_an_early_close(tmp_path, creds):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    s_ms = ta.ns(HALF_DAY.extended_close - datetime.timedelta(minutes=3)) // 10**6
    bar = {"ev": "AM", "sym": "AAA", "v": 10, "o": 1.0, "c": 1.1, "h": 1.2, "l": 0.9, "s": s_ms}
    other = dict(bar, sym="BBB", c=2.0)
    code, said, _ = record(cfg, HALF_DAY, [[bar, other], [dict(bar, c=1.15)]])
    assert code == 0
    day_path, gaps_path = streamday.paths(cfg, "2026-11-27")
    assert any(str(day_path) in line for line in said)
    assert not list(day_path.parent.glob("*.part"))
    kept = streamday.read(cfg, "2026-11-27")
    rows = kept.rows.set_index("ticker")
    assert list(kept.rows.columns) == list(streamday.COLUMNS)
    # DayBars' rule: the later message of a symbol-minute replaces the earlier one
    assert rows.loc["AAA", "close"] == 1.15 and rows.loc["AAA", "corrections"] == 1
    assert rows.loc["BBB", "corrections"] == 0
    assert rows.loc["AAA", "first_receipt_ns"] <= rows.loc["AAA", "last_receipt_ns"]
    assert rows.loc["AAA", "window_start"] == s_ms * 10**6
    # the recorder ran only from 16:58:30: the day before it is a gap
    [(lo, hi, cause)] = kept.gaps
    assert cause == "not running" and lo == ta.ns(HALF_DAY.premarket_open)


def test_a_run_stopped_before_the_end_keeps_no_day(tmp_path, creds):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    stop = threading.Event()
    stop.set()
    code, _, _ = record(cfg, HALF_DAY, [], stop)
    assert code == 0
    assert not streamday.paths(cfg, "2026-11-27")[0].exists()


def kept_scenario(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    path, flat, expect, corrected = ta.scenario(tmp_path)
    built = streamday.build([path], ta.DAY)
    streamday.write(cfg, built)
    return cfg, built, flat, expect, corrected


def test_build_replays_the_captures_through_daybars(tmp_path):
    cfg, built, flat, _, corrected = kept_scenario(tmp_path)
    assert built.other_day == 1 and built.unreadable == 0
    assert int(built.rows.corrections.sum()) == 1
    row = built.rows[(built.rows.ticker == corrected.ticker)
                     & (built.rows.window_start == corrected.window_start)].iloc[0]
    assert row.close == float(corrected.close) + 1 and row.corrections == 1
    [(_, _, cause)] = built.gaps
    assert cause == "ConnectionClosed"
    again = streamday.read(cfg, "2026-11-18")
    assert again.rows.equals(built.rows) and again.gaps == built.gaps


def test_compare_counts_each_outcome_and_keeps_gapped_rows_apart(tmp_path):
    _, built, flat, expect, _ = kept_scenario(tmp_path)
    frame = massive.read_day_file(flat.read_bytes(), "2026-11-18")
    result = streamday.compare(built, frame, ta.DAY, TAIL)
    reg = result["sessions"]["regular"]
    clean, gapped = reg["clean"], reg["gapped"]
    assert clean["different"] == 1 and reg["different_corrected"] == 1
    assert reg["columns"]["close"]["rows"] == 1
    assert reg["columns"]["close"]["abs"]["max"] == pytest.approx(1)
    assert all(reg["columns"][c]["rows"] == 0 for c in ("open", "high", "low", "volume"))
    assert clean["stream_only"] == 1 and gapped["stream_only"] == 0
    assert clean["file_only"] + gapped["file_only"] == expect["never"] + expect["gap"]
    assert gapped["file_only"] >= expect["gap"] > 0 and clean["file_only"] > 0
    assert gapped["equal"] > 0  # streamed next to the gap: counted apart, not as clean
    paired = sum(reg[k]["equal"] + reg[k]["different"] for k in ("clean", "gapped"))
    assert paired + clean["file_only"] + gapped["file_only"] == expect["regular"]
    for name in ("pre", "post"):
        s = result["sessions"][name]
        assert s["clean"]["equal"] > 0 and s["gapped"] == dict.fromkeys(streamday.OUTCOMES, 0)
        assert s["clean"]["different"] == s["clean"]["file_only"] == 0
    assert any("close differs in 1" in line for line in streamday.render(result))


def test_the_compare_command_prints_per_session(tmp_path, capsys):
    toml = tmp_path / "config" / "bot.toml"
    toml.parent.mkdir()
    toml.write_text((ROOT / "config" / "bot.toml").read_text())
    (tmp_path / "config" / "calendar").symlink_to(ROOT / "config" / "calendar")
    _, _, flat, _, _ = kept_scenario(tmp_path)
    args = ["--config", str(toml), "compare-stream-day", "2026-11-18"]
    assert cli.main([*args, "--flat-file", str(flat)]) == 0
    out = capsys.readouterr().out
    assert "[pre]" in out and "[regular]" in out and "[post]" in out and "gapped:" in out
    assert cli.main([*args[:-1], "2026-11-17", "--flat-file", str(flat)]) == 3
    assert "stream_day_missing" in capsys.readouterr().err


def test_a_day_without_captures_and_a_folder_outside_refuse(tmp_path):
    with pytest.raises(ModelRefused) as exc:
        streamday.build([], ta.DAY)
    assert exc.value.cause == "capture_missing"
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    for value in ("", ".", "/abs", "../up"):
        bad = dataclasses.replace(cfg, massive=dataclasses.replace(cfg.massive,
                                                                   stream_day_dir=value))
        with pytest.raises(ModelRefused) as exc:
            streamday.paths(bad, "2026-11-18")
        assert exc.value.cause == "stream_day_dir_outside"


def test_an_unreadable_day_file_refuses(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    day_path, _ = streamday.paths(cfg, "2026-11-18")
    day_path.parent.mkdir(parents=True)
    day_path.write_bytes(gzip.compress(b"ticker,volume\nAAA,1\n"))
    with pytest.raises(ModelRefused) as exc:
        streamday.read(cfg, "2026-11-18")
    assert exc.value.cause == "stream_day_unreadable"
