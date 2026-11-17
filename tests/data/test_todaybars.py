"""TodayBars: one bars read shared by discovery and evaluation."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal

from tickr.calendar import NEW_YORK
from tickr.data.todaybars import TodayBars, reaches_open
from tickr.types import Bar

OPEN = datetime(2026, 11, 16, 9, 30, tzinfo=NEW_YORK)
M1 = timedelta(minutes=1)


def bar(symbol: str, minute: int) -> Bar:
    one = Decimal(1)
    return Bar(symbol, OPEN + minute * M1, one, one, one, one, one)


def test_a_read_serves_only_minutes_over_when_it_was_sent():
    kept = TodayBars()
    sent = OPEN + 5 * M1 + timedelta(seconds=1)
    kept.put(OPEN, ["AAA", "BBB"], [bar("AAA", 4), bar("AAA", 3), bar("CCC", 4)], sent)
    assert kept.complete(OPEN, "AAA", OPEN + 5 * M1) == (bar("AAA", 3), bar("AAA", 4))
    assert kept.complete(OPEN, "BBB", OPEN + 5 * M1) == ()  # asked, no bars: an empty read
    assert kept.complete(OPEN, "AAA", OPEN + 6 * M1) is None  # sent before 09:35 was over
    assert kept.complete(OPEN, "CCC", OPEN + 5 * M1) is None  # not asked: not kept


def test_an_older_read_never_replaces_a_newer_one():
    kept = TodayBars()
    late = OPEN + 5 * M1 + timedelta(seconds=30)
    kept.put(OPEN, ["AAA"], [bar("AAA", 4)], late)
    kept.put(OPEN, ["AAA"], [], late - timedelta(seconds=10))
    assert kept.complete(OPEN, "AAA", OPEN + 5 * M1) == (bar("AAA", 4),)


def test_reads_from_before_the_current_minute_are_pruned_on_put():
    kept = TodayBars()
    for minute in range(1, 300):  # a long session of reads of many symbols
        sent = OPEN + minute * M1 + timedelta(seconds=1)
        kept.put(OPEN, [f"S{minute}", f"T{minute}"], [], sent)
    assert len(kept) == 2  # only the latest minute's reads remain
    assert kept.complete(OPEN, "S299", OPEN + 299 * M1) == ()


def test_a_new_session_drops_everything():
    kept = TodayBars()
    kept.put(OPEN, ["AAA"], [bar("AAA", 0)], OPEN + M1)
    tomorrow = OPEN + timedelta(days=1)
    assert kept.complete(tomorrow, "AAA", OPEN + M1) is None
    assert kept.complete(OPEN, "AAA", OPEN + M1) is None  # gone for good
    assert len(kept) == 0


def test_puts_from_two_threads_keep_every_symbol():
    kept = TodayBars()
    sent = OPEN + M1

    def put(prefix: str) -> None:
        for n in range(200):
            kept.put(OPEN, [f"{prefix}{n}"], [], sent)

    threads = [threading.Thread(target=put, args=(p,)) for p in "AB"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(kept) == 400


def test_a_read_proves_the_session_from_the_open_only_by_reaching_it():
    """Minutes without trades have no bar, so a missing 09:30 bar proves nothing: the read
    must reach back to the open or earlier, or hold fewer bars than asked (no older one)."""
    late_start = [bar("A", m) for m in (3, 4, 5)]  # first trade 09:33
    assert not reaches_open(late_start, OPEN, 3)  # full read: older bars may exist
    assert reaches_open(late_start, OPEN, 4)  # fewer than asked: nothing older exists
    assert reaches_open([bar("A", -1), *late_start], OPEN, 4)  # reaches before the open
    assert reaches_open([bar("A", 0), bar("A", 1)], OPEN, 2)  # starts at the open
