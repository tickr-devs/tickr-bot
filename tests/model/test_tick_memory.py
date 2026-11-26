"""The peak-memory check of one stock group against the converter's default (codex-108 point
2), on the synthetic day of test_ticks.py: each conversion in its own process, the tables equal
exactly, the numbers recorded (printed; no limit is asserted, none is measured yet). Larger days:
`python tests/model/tick_memory.py SYMBOLS TRADES QUOTES`."""

from __future__ import annotations

import json

import model_ticks as synthetic
import tick_memory

from tickr.model.features import ticks

SYMBOLS, TRADES, QUOTES = 8, 1500, 2500  # the day of test_ticks.py


def test_one_group_and_the_default_in_their_own_processes(tmp_path, capsys):
    trades, quotes = synthetic.day_events(tick_memory.DAY, SYMBOLS, TRADES, QUOTES)
    t, q = tmp_path / "trades.csv.gz", tmp_path / "quotes.csv.gz"
    ticks.write_flat_file(trades, t, synthetic.TRADE_FILE_COLUMNS)
    ticks.write_flat_file(quotes, q, synthetic.QUOTE_FILE_COLUMNS)
    records, equal = tick_memory.compare(tmp_path, t, q)
    assert equal
    assert [r["groups"] for r in records] == [ticks.default_groups(), ticks.ONE_GROUP]
    assert records[0]["rows"] == records[1]["rows"] > 0
    assert all(r["peak_rss_mib"] >= r["peak_rss_mib_before"] > 0 for r in records)
    with capsys.disabled():
        for record in records:
            print(json.dumps(record))
