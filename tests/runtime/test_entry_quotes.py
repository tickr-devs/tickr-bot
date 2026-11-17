"""A working entry whose stream quote is too old is judged by a REST snapshot instead, read at
most every `[risk] entry_snapshot_interval_seconds`, every such symbol in one request (owner
2026-11-17: a thin stock's stream can stay silent while its book moves). The snapshot passes
the same age and window checks."""

from __future__ import annotations

from datetime import timedelta

from tickr.broker.base import RateLimited
from tickr.data.base import SymbolsRefused
from tickr.runtime.entries import ENTRY_QUOTE_SNAPSHOT, EntryQuotes, guard_entries

from .test_entries import CALENDAR, CFG, NOW, Desk, opened, quote

STALE = float(CFG.risk.max_quote_age_seconds) + 2
INTERVAL = float(CFG.risk.entry_snapshot_interval_seconds)


class Snapshots:
    """A snapshot route: answers with `answer` (quotes, or an exception), counts the reads."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.reads: list[list[str]] = []

    def __call__(self, symbols: list[str]):
        self.reads.append(symbols)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def guarded(desk: Desk, stream, snapshots: Snapshots | None, symbols=frozenset({"ABC"})):
    quotes = EntryQuotes(desk.engine, CFG, lambda symbol: stream, snapshots, lambda: 0.0)
    quotes.refresh(set(symbols), NOW)
    guard_entries(desk.engine, CFG, CALENDAR, lambda symbol: quotes(symbol, NOW), NOW)
    return quotes


def cancels(desk: Desk) -> list[str]:
    return [r.body["cause"] for r in desk.store.journal("entry-cancel")]


def test_a_fresh_stream_quote_needs_no_snapshot(tmp_path):
    desk = Desk(tmp_path)
    opened(desk)
    snapshots = Snapshots([quote()])
    guarded(desk, quote(), snapshots)
    assert snapshots.reads == [] and cancels(desk) == []


def test_a_fresh_snapshot_keeps_the_entry_while_the_stream_is_silent(tmp_path):
    desk = Desk(tmp_path)
    opened(desk)
    snapshots = Snapshots([quote(ask="10.06", age_seconds=1)])
    guarded(desk, quote(age_seconds=STALE), snapshots)
    assert snapshots.reads == [["ABC"]] and cancels(desk) == []
    (row,) = desk.store.journal(ENTRY_QUOTE_SNAPSHOT)
    assert row.key == "ABC" and row.body["stream_age"] == STALE
    assert row.body["read_seconds"] == 0
    assert (row.body["snapshot_age"], row.body["ask"]) == (1.0, "10.06")


def test_a_stuck_snapshot_still_cancels_as_stale(tmp_path):
    """OMH 2026-11-17: the snapshot's own time stood still for minutes."""
    desk = Desk(tmp_path)
    opened(desk)
    guarded(desk, quote(age_seconds=STALE), Snapshots([quote(age_seconds=STALE * 10)]))
    (cause,) = cancels(desk)
    assert cause.startswith(f"quote stale ({STALE} s)")  # the newer of the two


def test_a_snapshot_outside_the_window_cancels(tmp_path):
    desk = Desk(tmp_path)
    opened(desk)
    guarded(desk, quote(age_seconds=STALE), Snapshots([quote(ask="10.11", age_seconds=1)]))
    assert cancels(desk) == ["ask 10.11 above the limit 10.10"]


def test_a_refused_snapshot_read_leaves_the_stream_quote_and_is_journaled(tmp_path):
    desk = Desk(tmp_path)
    opened(desk)
    guarded(desk, quote(age_seconds=STALE), Snapshots(RateLimited("snapshot")))
    assert cancels(desk)[0].startswith("quote stale")
    (row,) = desk.store.journal(ENTRY_QUOTE_SNAPSHOT)
    assert row.body["error"] == "RateLimited" and "snapshot_age" not in row.body


def test_a_refused_symbol_leaves_the_stream_quote_and_never_ends_the_guard(tmp_path):
    """Fable, #622: the snapshot route answers 417 INVALID_SYMBOL (`SymbolsRefused`, a market
    data error, not a broker error) for a symbol its own screener listed (DJT, 2026-11-16)."""
    desk = Desk(tmp_path)
    opened(desk)
    refused = SymbolsRefused("SNAPSHOT", 417, "INVALID_SYMBOL")
    guarded(desk, quote(age_seconds=STALE), Snapshots(refused))
    assert cancels(desk)[0].startswith("quote stale")
    (row,) = desk.store.journal(ENTRY_QUOTE_SNAPSHOT)
    assert row.body["error"] == "SymbolsRefused" and "snapshot_age" not in row.body


def test_every_due_symbol_in_one_request(tmp_path):
    desk = Desk(tmp_path)
    snapshots = Snapshots([])
    guarded(desk, quote(age_seconds=STALE), snapshots, {"ABC", "XYZ"})
    assert snapshots.reads == [["ABC", "XYZ"]]
    assert sorted(r.key for r in desk.store.journal(ENTRY_QUOTE_SNAPSHOT)) == ["ABC", "XYZ"]


def test_one_read_per_interval_and_the_last_snapshot_is_reused_in_between(tmp_path):
    desk = Desk(tmp_path)
    clock = [0.0]
    snap = quote(age_seconds=1)
    snapshots = Snapshots([snap])
    quotes = EntryQuotes(desk.engine, CFG, lambda symbol: quote(age_seconds=STALE), snapshots,
                         lambda: clock[0])
    quotes.refresh({"ABC"}, NOW)
    assert quotes("ABC", NOW) == snap
    clock[0] += INTERVAL / 2
    quotes.refresh({"ABC"}, NOW)
    assert quotes("ABC", NOW) == snap
    assert len(snapshots.reads) == 1
    clock[0] += INTERVAL
    quotes.refresh({"ABC"}, NOW)
    assert len(snapshots.reads) == 2


def test_a_newer_stream_quote_wins_over_a_held_snapshot(tmp_path):
    desk = Desk(tmp_path)
    stream = [quote(age_seconds=STALE)]
    quotes = EntryQuotes(desk.engine, CFG, lambda symbol: stream[0],
                         Snapshots([quote(age_seconds=STALE - 1)]), lambda: 0.0)
    quotes.refresh({"ABC"}, NOW)
    assert quotes("ABC", NOW).provider_time == NOW - timedelta(seconds=STALE - 1)
    stream[0] = quote(age_seconds=STALE - 1.5)
    assert quotes("ABC", NOW) == stream[0]


def test_a_symbol_without_a_working_entry_is_dropped(tmp_path):
    desk = Desk(tmp_path)
    snapshots = Snapshots([quote(age_seconds=1)])
    quotes = EntryQuotes(desk.engine, CFG, lambda symbol: None, snapshots, lambda: 0.0)
    quotes.refresh({"ABC"}, NOW)
    assert quotes("ABC", NOW) is not None
    quotes.refresh(set(), NOW)
    assert quotes("ABC", NOW) is None


def test_symbols_due_at_different_times_still_share_one_read_per_interval(tmp_path):
    """Reviewer, #622: a per-symbol timer let entries read in turn, more often than the
    interval; the interval is for every symbol together."""
    desk = Desk(tmp_path)
    clock = [0.0]
    snapshots = Snapshots([])
    quotes = EntryQuotes(desk.engine, CFG, lambda symbol: quote(age_seconds=STALE), snapshots,
                         lambda: clock[0])
    quotes.refresh({"ABC"}, NOW)
    clock[0] += INTERVAL / 2
    quotes.refresh({"ABC", "XYZ"}, NOW)  # XYZ is new, but the interval has not passed
    assert snapshots.reads == [["ABC"]]
    clock[0] += INTERVAL / 2
    quotes.refresh({"ABC", "XYZ"}, NOW)
    assert snapshots.reads == [["ABC"], ["ABC", "XYZ"]]


def test_without_a_market_nothing_is_read(tmp_path):
    desk = Desk(tmp_path)
    opened(desk)
    guarded(desk, None, None)
    assert cancels(desk) == ["no quote"] and desk.store.journal(ENTRY_QUOTE_SNAPSHOT) == []
