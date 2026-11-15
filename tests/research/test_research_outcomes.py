from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal

from research_helpers import NY, close_at_16, flat, record, research_settings, series

from tickr.calendar import CalendarCause, CalendarRefused
from tickr.research.outcomes import outcomes, summarize
from tickr.research.records import Admission
from tickr.strategy.base import Verdict

# 10:00 .. 10:06; 10:03 has no trades. ATR(2) at 10:01 = (0.3 + 0.3) / 2 = 0.3.
ROWS = [
    ("10.0", "10.2", "9.9", "10.1"),
    ("10.1", "10.3", "10.0", "10.2"),
    ("10.2", "10.6", "10.1", "10.5"),
    flat("10.5"),
    ("10.5", "10.5", "9.8", "9.9"),
    ("9.9", "10.0", "9.9", "10.0"),
    flat("10.0"),
]
BARS = series("10:00", ROWS, skip=(3,)) + series("09:30", [flat("11")], day=date(2026, 11, 11))


def run(records, bars=BARS, close_for=close_at_16):
    return outcomes(records, {"ABC": bars}, research_settings(), close_for)


def by_horizon(rows):
    return {r.horizon_minutes: r for r in rows}


def test_oracle_moves_in_percent_and_atr():
    rows = by_horizon(run([record("10:01")]))
    one = rows[1]
    assert (one.reference, one.atr, one.price) == (Decimal("10.2"), Decimal("0.3"),
                                                   Decimal("10.5"))
    assert one.in_atr(one.move) == 1
    assert one.percent(one.move) == Decimal("0.3") / Decimal("10.2") * 100
    assert (one.max_up, one.max_down) == (Decimal("0.4"), Decimal("-0.1"))
    assert one.cause == ""
    # 10:03 had no trades: the price at the 2-minute horizon is still the 10:02 close.
    assert (rows[2].price, rows[2].max_up, rows[2].max_down) == (
        Decimal("10.5"), Decimal("0.4"), Decimal("-0.1"))
    five = rows[5]
    assert five.move == Decimal("-0.2")
    assert (five.max_up, five.max_down) == (Decimal("0.4"), Decimal("-0.4"))
    assert five.in_atr(five.max_down) == Decimal("-0.4") / Decimal("0.3")


def test_horizon_past_the_session_has_no_numbers():
    rows = by_horizon(run([record("10:05", Verdict.WATCH)]))
    assert rows[1].move == 0 and rows[1].cause == ""
    for horizon in (2, 5):  # the next day's bar does not count
        assert rows[horizon].move is None
        assert rows[horizon].cause == "bars of the session end before the horizon"
        assert rows[horizon].reference == Decimal("10.0")


def test_atr_unavailable_keeps_percent():
    one = by_horizon(run([record("10:00", Verdict.WATCH)]))[1]
    assert one.atr is None and one.in_atr(one.move) is None
    assert one.percent(one.move) == Decimal("0.1") / Decimal("10.1") * 100
    assert one.cause == "atr unavailable (fewer than 2 bars)"


def test_missing_decision_bar_and_symbol_are_causes():
    rows = run([record("10:03"), record("10:01", symbol="XYZ")])
    assert {r.cause for r in rows} == {"decision bar not in bars", "no bars for the symbol"}
    assert all(r.move is None for r in rows)


def test_only_watch_and_enter_are_measured():
    rows = run([record("10:01", v) for v in Verdict])
    assert {r.record.decision.verdict for r in rows} == {Verdict.WATCH, Verdict.ENTER}
    assert len(rows) == 2 * 3


def test_no_look_ahead_in_atr_and_reference():
    before = by_horizon(run([record("10:01")]))[1]
    changed = BARS[:2] + tuple(replace(b, high=b.high * 2, close=b.close * 2) for b in BARS[2:])
    after = by_horizon(run([record("10:01")], changed))[1]
    assert (after.reference, after.atr) == (before.reference, before.atr)


def test_summary_groups_enter_by_admission():
    records = [
        record("10:01"),
        record("10:01", admission=Admission.TAKEN, cause=None),
        record("10:02", Verdict.WATCH),
    ]
    stats = {(s.group, s.horizon_minutes): s for s in summarize(run(records))}
    assert set(stats) == {(g, h) for g in ("ENTER:taken", "ENTER:refused", "WATCH")
                          for h in (1, 2, 5)}
    refused = stats[("ENTER:refused", 1)]
    assert (refused.rows, refused.measured, refused.up, refused.down) == (1, 1, 1, 0)
    assert refused.mean_move_atr == 1
    assert (stats[("WATCH", 2)].measured, stats[("WATCH", 2)].down) == (1, 1)
    assert stats[("WATCH", 5)].measured == 0  # past the end of the session's bars


def test_horizon_is_capped_at_the_regular_close_even_with_post_market_bars():
    bars = series("15:57", [flat("10.0"), flat("10.1"), flat("10.2"), flat("10.3"),
                            flat("10.4"), flat("10.5"), flat("10.6"), flat("10.7")])
    rows = by_horizon(run([record("15:57", Verdict.WATCH)], bars))
    assert rows[1].price == Decimal("10.1")
    assert rows[2].price == Decimal("10.2")  # ends 16:00 exactly: last regular bar 15:59
    assert rows[5].move is None and rows[5].cause == "horizon ends after the regular close"


def test_half_day_close_comes_from_the_calendar():
    def early(day):
        return datetime.combine(day, time(10, 3), NY)

    rows = by_horizon(run([record("10:01")], close_for=early))
    assert rows[1].move is not None
    assert rows[2].cause == "horizon ends after the regular close"


def test_day_refused_by_the_calendar_is_a_cause():
    def refuse(day):
        raise CalendarRefused(CalendarCause.HOLIDAY, "closed")

    rows = run([record("10:01")], close_for=refuse)
    assert all(r.move is None and r.cause.startswith("calendar refuses 2026-11-10") for r in rows)
