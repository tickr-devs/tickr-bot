"""`python -m tickr.research collar` (backlog T10): the distribution of entry-price distance to
the last completed bar's close and to the previous quote's ask, from capture files. Every bar
and quote here is SYNTHETIC (made-up prices written through the real capture writer)."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from tickr.data.capture import CaptureWriter
from tickr.research.__main__ import main
from tickr.research.collar import distribution
from tickr.research.records import Admission, DecisionRecord, write_records
from tickr.strategy.base import Decision, Role, Verdict
from tickr.types import Bar, Quote

T0 = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # 10:00 New York
DAY = "2026-11-13"


def quote(symbol: str, seconds: int, ask: str) -> Quote:
    at = T0 + timedelta(seconds=seconds)
    return Quote(symbol, D(ask) - D("0.01"), D(ask), D(1), D(1), at, at)


def bar(symbol: str, minute: int, close: str) -> Bar:
    return Bar(symbol, T0 + timedelta(minutes=minute), D(close), D(close), D(close), D(close),
               D(100))


def test_nearest_rank_percentiles():
    dist = distribution([D(v) for v in range(1, 1001)])
    assert (dist.count, dist.percentiles, dist.max) == (1000, (500, 900, 990, 999), 1000)
    assert distribution([D(7)]).percentiles == (7, 7, 7, 7)


def test_collar_from_capture_files_end_to_end(tmp_path):
    w = CaptureWriter(tmp_path / "cap", "webull")
    # Bars: 09:59 (ends 10:00) closes 10.00; 10:00 (ends 10:01) closes 11.00, first written
    # as 10.50 then revised: the last version counts.
    w.bars([bar("AAA", -1, "10.00"), bar("AAA", 0, "10.50")], "webull", T0)
    w.bars([bar("AAA", 0, "11.00")], "webull", T0 + timedelta(minutes=1, seconds=5))
    w.on_quote(quote("AAA", 10, "10.10"))  # bar 10.00: 0.01; no previous quote
    w.on_quote(quote("AAA", 20, "10.20"))  # bar 0.02; previous 10.10: 0.0099
    w.on_quote(quote("AAA", 15, "99.00"))  # older than the newest: the inbox ignores it
    w.on_quote(quote("AAA", 70, "11.22"))  # bar 11.00: 0.02; previous 10.20: 0.1
    w.on_quote(quote("BBB", 10, "5.00"))  # no bar of BBB: missing reference
    w.quotes([quote("AAA", 30, "50.00")], "webull", "snapshot")  # not a stream quote
    w.close()
    day = tmp_path / "cap" / DAY
    with open(day / "quotes.jsonl", "a") as handle:
        handle.write('{"symbol": "AAA", "half written')  # the bot is appending right now
    decisions = tmp_path / "decisions.jsonl"
    enter = Decision("s", "1", "AAA", Verdict.ENTER, (), entry_low=D("11.00"),
                     entry_high=D("11.556"), stop=D("10.50"), quantity=D(1))
    write_records(decisions, [DecisionRecord(T0 + timedelta(seconds=75), T0, Role.ACTIVE, enter,
                                             Admission.TAKEN)])

    assert main(["collar", "--quotes", str(day / "quotes.jsonl"),
                 "--bars", str(day / "bars.jsonl"), "--decisions", str(decisions),
                 "--out", str(tmp_path / "out")]) == 0
    with open(tmp_path / "out" / "collar.csv", newline="") as handle:
        rows = {(r["entry"], r["reference"], r["symbol"]): r for r in csv.DictReader(handle)}
    bar_all = rows[("quote", "bar", "all")]
    assert (bar_all["count"], bar_all["p50"], bar_all["max"]) == ("3", "0.0200", "0.0200")
    assert rows[("quote", "bar", "AAA")]["count"] == "3"
    prev = rows[("quote", "quote", "AAA")]
    assert (prev["count"], prev["p50"], prev["p99_9"]) == ("2", "0.0099", "0.1000")
    # The decision's limit 11.55 at 10:01:15: bar 11.00 -> 0.05; the quote before the newest
    # (11.22 at 10:01:10) is 10.20 -> 0.1324.
    assert rows[("decision", "bar", "AAA")]["max"] == "0.0500"
    assert rows[("decision", "quote", "AAA")]["max"] == "0.1324"
    summary = json.loads((tmp_path / "out" / "collar_summary.json").read_text())
    assert summary["missing_reference"] == {"quote/bar": 1, "quote/quote": 2}
    assert summary["partial_last_lines"] == 1
    assert summary["overall"]["quote/quote"]["count"] == 2


def test_a_malformed_bar_file_is_refused(tmp_path, capsys):
    quotes = tmp_path / "quotes.jsonl"
    quotes.write_text("")
    bars = tmp_path / "bars.jsonl"
    bars.write_text("{not json}\n")
    assert main(["collar", "--quotes", str(quotes), "--bars", str(bars),
                 "--out", str(tmp_path / "out")]) == 2
    assert "bars.jsonl:1" in capsys.readouterr().err
