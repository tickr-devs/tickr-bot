from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from md_helpers import NOW, OPEN, bars, quote, row, settings

from aperture import config
from aperture.data.base import FakeMarketData, ScreenerKind
from aperture.data.capture import CaptureError, CaptureWriter, export_replay_bars
from aperture.data.discovery import discover
from aperture.replay.bars import load_bars
from aperture.types import Bar

ROOT = Path(__file__).resolve().parents[2]
DAY = "2026-11-13"


def lines(path: Path) -> list[dict]:
    return [json.loads(t) for t in path.read_text("utf-8").splitlines()]


def test_layout_from_config(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    writer = CaptureWriter.from_config(cfg, "fake")
    path = writer.path(NOW.date(), "bars.jsonl")
    assert path == (tmp_path / cfg.run.data_dir / cfg.market_data.capture_dir
                    / cfg.environment_name / DAY / "bars.jsonl")


def test_records_are_strings_with_provider_and_receipt_time(tmp_path):
    w = CaptureWriter(tmp_path, "fake")
    w.screener([row(ScreenerKind.GAINERS, 1, "AAA")], "fake", NOW)
    w.quotes([quote("AAA")], "fake", "snapshot")
    w.on_quote(quote("AAA", bid="10.01"))
    w.bars(bars("AAA", 2), "fake", NOW)
    w.close()
    day = tmp_path / DAY
    s = lines(day / "screener.jsonl")[0]
    assert s["kind"] == "GAINERS" and s["fields"]["close"] == "10.00"
    assert s["provider"] == "fake" and s["receipt_time"] == NOW.isoformat()
    q = lines(day / "quotes.jsonl")
    assert [x["source"] for x in q] == ["snapshot", "stream"]
    assert q[1]["bid"] == "10.01" and q[0]["provider_time"].endswith("+00:00")
    b = lines(day / "bars.jsonl")
    assert b[0]["close"] == "10" and b[0]["provider"] == "fake"
    assert all(isinstance(v, str) for rec in b for v in rec.values())


def test_ny_date_decides_the_folder(tmp_path):
    w = CaptureWriter(tmp_path, "fake")
    late = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)  # 20:00 New York on the 13th
    w.quotes([quote("AAA", at=late)], "fake", "stream")
    w.quotes([quote("AAA", at=late + timedelta(hours=5))], "fake", "stream")
    w.close()
    assert (tmp_path / DAY / "quotes.jsonl").exists()
    assert (tmp_path / "2026-11-14" / "quotes.jsonl").exists()


def test_repeated_bars_written_once_revisions_kept(tmp_path):
    w = CaptureWriter(tmp_path, "fake")
    series = bars("AAA", 3)
    w.bars(series, "fake", NOW)
    w.bars(series, "fake", NOW + timedelta(minutes=1))
    revised = Bar("AAA", series[0].start, series[0].open, series[0].high, series[0].low,
                  series[0].close, series[0].volume + 1)
    w.bars([revised], "fake", NOW + timedelta(minutes=2))
    w.close()
    assert len(lines(tmp_path / DAY / "bars.jsonl")) == 4


def test_naive_time_refused(tmp_path):
    w = CaptureWriter(tmp_path, "fake")
    with pytest.raises(CaptureError):
        w.screener([row(ScreenerKind.GAINERS, 1, "A")], "fake", NOW.replace(tzinfo=None))


def test_export_loads_in_replay_and_last_revision_wins(tmp_path):
    w = CaptureWriter(tmp_path, "fake")
    series = bars("BBB", 2) + bars("AAA", 3)
    w.bars(series, "fake", NOW)
    revised = Bar("AAA", series[2].start, series[2].open, series[2].high, series[2].low,
                  series[2].close, series[2].volume + 5)
    w.bars([revised], "fake", NOW)
    w.close()
    captured = tmp_path / DAY / "bars.jsonl"
    # A restart re-writes an identical bar.
    captured.write_text(captured.read_text("utf-8") + captured.read_text("utf-8").splitlines()[0]
                        + "\n", "utf-8")
    out = tmp_path / "replay" / "day.jsonl"
    result = export_replay_bars(captured, out)
    assert (result.bars, result.duplicates, result.revised) == (5, 1, 1)
    loaded = load_bars([out])
    assert len(loaded["AAA"]) == 3 and len(loaded["BBB"]) == 2
    assert loaded["AAA"][0].volume == revised.volume


@pytest.mark.parametrize("text", ["{not json", '{"symbol": "A"}',
                                  '{"symbol":"A","start":"x","open":"1","high":"1","low":"1",'
                                  '"close":"1","volume":"1","provider":"p","receipt_time":"t"}'])
def test_export_refuses_malformed_lines(tmp_path, text):
    path = tmp_path / "bars.jsonl"
    path.write_text(text + "\n", "utf-8")
    with pytest.raises(CaptureError, match=":1:"):
        export_replay_bars(path, tmp_path / "out.jsonl")


def test_discovery_writes_through_capture(tmp_path):
    md = FakeMarketData("fake")
    md.set_screener(ScreenerKind.GAINERS, [row(ScreenerKind.GAINERS, 1, "AAA")])
    md.set_quote(quote("AAA"))
    md.add_bars(bars("AAA", 31))
    w = CaptureWriter(tmp_path, md.provider)
    result = discover(md, settings(), OPEN, lambda: NOW, w)
    w.close()
    assert [c.instrument.symbol for c in result.candidates] == ["AAA"]
    day = tmp_path / DAY
    assert len(lines(day / "screener.jsonl")) == 1
    assert len(lines(day / "quotes.jsonl")) == 1
    assert len(lines(day / "bars.jsonl")) == 30  # the minute in progress is not captured
