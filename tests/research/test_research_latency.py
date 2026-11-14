from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from research_helpers import HERE

from aperture.data.base import StreamNotice
from aperture.data.capture import CaptureWriter
from aperture.research.__main__ import main
from aperture.research.latency import stats
from aperture.types import Quote

T0 = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # 10:00 New York
DAY = "2026-11-13"
SETTINGS = ["--settings", str(HERE / "research_settings.toml")]


def quote(symbol: str, at: datetime, latency_ms: int) -> Quote:
    return Quote(symbol=symbol, bid=D("10.00"), ask=D("10.02"), bid_size=D(1), ask_size=D(1),
                 provider_time=at - timedelta(milliseconds=latency_ms), receipt_time=at)


def _rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def test_stats_median_nearest_rank_p90_and_skew():
    s = stats([D(v) for v in range(1, 11)] + [D(-3)])
    assert (s.count, s.median, s.p90, s.max, s.min, s.clock_skew) == (11, 5, 9, 10, -3, 1)
    assert stats([D(7)]).p90 == 7


def test_latency_from_capture_files_end_to_end(tmp_path, capsys):
    w = CaptureWriter(tmp_path / "cap", "webull")
    for ms in (10, 20, 30, 400):
        w.on_quote(quote("AAA", T0, ms))
    w.on_quote(quote("BBB", T0 + timedelta(hours=1), -5))  # receipt before provider: skew
    w.quotes([quote("AAA", T0, 9999)], "webull", "snapshot")  # other source: not counted
    w.on_notice(StreamNotice(T0, 0, 40, 3))
    w.on_notice(StreamNotice(T0 + timedelta(minutes=1), 2, 45, 4))
    w.close()
    day = tmp_path / "cap" / DAY
    with open(day / "quotes.jsonl", "a") as handle:
        handle.write('{"symbol": "AAA", "half written')  # the bot is appending right now

    assert main(["latency", "--quotes", str(day / "quotes.jsonl"),
                 "--notices", str(day / "notices.jsonl"), *SETTINGS,
                 "--out", str(tmp_path / "out")]) == 0
    rows = {(r["group"], r["key"]): r for r in _rows(tmp_path / "out" / "latency.csv")}
    aaa = rows[("symbol", "AAA")]
    assert (aaa["count"], aaa["median_ms"], aaa["p90_ms"], aaa["max_ms"]) == (
        "4", "25.0000", "400.0000", "400.0000")
    bbb = rows[("hour", "2026-11-13 11:00")]
    assert (bbb["count"], bbb["min_ms"], bbb["clock_skew"]) == ("1", "-5.0000", "1")
    assert rows[("hour", "2026-11-13 10:00")]["count"] == "4"
    (notices,) = _rows(tmp_path / "out" / "notices.csv")
    assert notices == {"hour": "2026-11-13 10:00", "notices": "2", "drop_sum": "2",
                       "drop_max": "2", "sent_sum": "7", "sent_max": "4",
                       "rtt_median": "42.5000", "rtt_max": "45"}
    summary = json.loads((tmp_path / "out" / "latency_summary.json").read_text())
    assert summary == {"source": "stream", "quotes": 5, "symbols": 2, "hours": 2,
                       "clock_skew_quotes": 1, "min_latency_ms": "-5.0000", "notices": 2,
                       "partial_last_lines": 1}


@pytest.mark.parametrize("name, line, cause", [
    ("quotes.jsonl", "not json\n", "invalid JSON"),
    ("quotes.jsonl", '{"symbol": "AAA"}\n', "not a captured record"),
    ("notices.jsonl", '{"type": "1001", "drop": -1, "rtt": 1, "sent": 0, "provider": "w",'
                      ' "receipt_time": "2026-11-13T15:00:00+00:00"}\n', "drop is not"),
    ("notices.jsonl", '{"type": "1001", "drop": 0, "rtt": 1, "sent": 0, "provider": "w",'
                      ' "receipt_time": "2026-11-13T15:00:00"}\n', "not an aware"),
])
def test_malformed_capture_lines_are_refused(tmp_path, capsys, name, line, cause):
    quotes = tmp_path / "quotes.jsonl"
    quotes.write_text("")
    (tmp_path / name).write_text(line)
    assert main(["latency", "--quotes", str(quotes), "--notices",
                 str(tmp_path / "notices.jsonl") if name == "notices.jsonl" else str(quotes),
                 *SETTINGS, "--out", str(tmp_path / "out")]) == 2
    err = capsys.readouterr().err
    assert f"{name}:1: " in err and cause in err
