"""Rehearsal: a resting entry never outlives its price window (spec 9).

AAA runs up all morning; the quote feed lags the exchange by a few seconds, so each limit at the
top of the window arrives after the price has already moved past it. Every such entry must be
cancelled within a pass or two, never left to fill later; here the price then collapses
through every earlier limit, which is exactly when a forgotten entry would have filled. An
entry that does fill on arrival is protected and stopped out as usual.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D

from rehearsal.harness import DAY, Rehearsal, history
from rehearsal.world import build_day

HOT = D(30000)


def test_missed_entries_are_cancelled_not_left_resting(tmp_path, monkeypatch):
    r = Rehearsal(tmp_path, monkeypatch)
    r.world.add_symbol("AAA", build_day("AAA", DAY, D(20), D(3000), [
        ("09:30", "10:00", D(29), HOT),   # steady run-up, faster than the feed
        ("10:30", "10:35", D(22), HOT),   # collapse back through every earlier limit
    ]), history("AAA", D(20)))
    r.world.exchange_lead = timedelta(seconds=3)
    assert r.run_until("11:00") == 0

    trades = r.trades()
    missed = [t for t in trades if t["close_cause"] == "entry-not-filled"]
    filled = [t for t in trades if t["close_cause"] != "entry-not-filled"]
    assert missed, "the lagging feed must make at least one entry miss"
    # An entry fills on arrival or not at all: never minutes later, in the collapse.
    entry_fills = [at for at, side, kind, _, _ in r.fills("AAA") if kind == "LIMIT"]
    assert [at.isoformat() for at in entry_fills] == [t["opened_at"] for t in filled]
    cancels = {c.key: c for c in r.journal("entry-cancel")}
    assert len(cancels) == len(missed)
    poll = float(r.config.orders.detail_poll_seconds)
    for trade, cancel in zip(missed, sorted(cancels.values(), key=lambda c: c.seq),
                             strict=True):
        assert "above the limit" in cancel.body["cause"]
        assert seconds(trade["opened_at"], cancel.at_utc) <= poll
        assert seconds(trade["opened_at"], trade["closed_at"]) <= 2 * poll
    # A cancelled entry never blocks the next signal.
    assert not any("OWN_ORDER_WORKING" in str(d) for d in r.decisions())
    assert r.status().position is None


def seconds(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()
