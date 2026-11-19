from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from baseline import load_baseline

from tickr.calendar import NEW_YORK
from tickr.risk.timers import WindowKind
from tickr.risk.watched_stop import Watch, WatchState, book_refusal, watch, watch_limits
from tickr.types import Quote

D = Decimal
CFG = load_baseline()
LIM = watch_limits(WindowKind.POST, CFG)
NOW = datetime(2026, 11, 16, 17, 0, tzinfo=NEW_YORK)
STOP = D("9.80")


def q(bid: str, ask: str, age: float = 1) -> Quote:
    t = NOW - timedelta(seconds=age)
    return Quote("ABCD", D(bid), D(ask), D(100), D(100), t, NOW)


def run(quotes, step: float = 1, *, alive: bool = False, stop: Decimal = STOP):
    state, out = WatchState(started=0.0), []
    for i, quote in enumerate(quotes):
        result = watch(stop, quote, NOW, (i + 1) * step, state, LIM, feed_alive=alive)
        state = result.state
        out.append(result.verdict)
    return out, state


def test_limits_follow_the_window_and_the_regular_window_has_none() -> None:
    pre = watch_limits(WindowKind.PRE, CFG)
    assert pre.max_spread_ratio == CFG.session.premarket_max_spread_ratio
    assert LIM.confirm_checks == CFG.session.extended_stop_confirm_checks
    with pytest.raises(ValueError):
        watch_limits(WindowKind.REGULAR, CFG)


def test_a_breach_needs_consecutive_usable_checks() -> None:
    verdicts, _ = run([q("9.80", "9.82"), q("9.79", "9.81")])
    assert verdicts == [Watch.HOLD, Watch.BREACH]
    # A bid back above the stop resets the count.
    verdicts, state = run([q("9.80", "9.82"), q("9.85", "9.87"), q("9.80", "9.82")])
    assert verdicts == [Watch.HOLD] * 3 and state.breach_checks == 1


BROKEN = [q("0", "9.82"), q("9.80", "0"), q("9.80", "9.70"), q("9.00", "9.90"), None,
          q("9.80", "9.82", age=60), q("9.80", "9.82", age=-3600)]


@pytest.mark.parametrize("bad", BROKEN)
def test_a_broken_book_neither_counts_nor_clears_a_breach(bad) -> None:
    assert run([bad])[0] == [Watch.HOLD]
    verdicts, state = run([q("9.80", "9.82"), bad, q("9.80", "9.82")])
    assert verdicts == [Watch.HOLD, Watch.HOLD, Watch.BREACH] and state.breach_checks == 2


@pytest.mark.parametrize("bad", [q("9.00", "9.90"), q("9.50", "0")])
def test_a_book_flapping_under_the_stop_is_exited(bad) -> None:
    verdicts, _ = run([q("9.50", "9.52"), bad] * 300)
    assert verdicts[:3] == [Watch.HOLD, Watch.HOLD, Watch.BREACH]
    # Even if every usable book needed more checks than come, the run ends blind in time.
    strict = replace(LIM, confirm_checks=10_000)
    state, out = WatchState(started=0.0), []
    for i, quote in enumerate([q("9.50", "9.52"), bad] * 300):
        result = watch(STOP, quote, NOW, float(i + 1), state, strict, feed_alive=False)
        state, out = result.state, out + [result.verdict]
    first = out.index(Watch.BLIND)
    assert first + 1 <= LIM.blind_seconds + 1


def test_a_quote_stamped_in_the_future_is_not_usable() -> None:
    ahead = q("9.90", "9.92", age=-(float(CFG.risk.max_clock_skew_seconds) + 1))
    assert "ahead of our clock" in str(book_refusal(ahead, NOW, LIM, feed_alive=True))
    assert book_refusal(q("9.90", "9.92", age=-0.1), NOW, LIM, feed_alive=False) is None


def test_an_old_quote_is_usable_while_the_feed_is_proven_alive() -> None:
    assert book_refusal(q("9.90", "9.92", age=60), NOW, LIM, feed_alive=True) is None
    assert "not alive" in str(book_refusal(q("9.90", "9.92", age=60), NOW, LIM,
                                           feed_alive=False))
    verdicts, _ = run([q("9.70", "9.72", age=60)] * 2, alive=True)
    assert verdicts == [Watch.HOLD, Watch.BREACH]


def test_blind_after_no_usable_book_for_the_blind_time() -> None:
    step = float(LIM.max_gap_seconds) / 2
    checks = int(LIM.blind_seconds / D(str(step))) + 1
    verdicts, state = run([q("9.90", "9.92")] + [None] * checks, step=step)
    assert verdicts[-1] is Watch.BLIND and Watch.BLIND not in verdicts[:-1]
    assert state.last_usable_bid == D("9.90")
    # Never a usable book at all: blind from the watch's start.
    verdicts, state = run([None] * (checks + 1), step=step)
    assert verdicts[-1] is Watch.BLIND and state.last_usable_bid is None


def test_a_watch_gap_is_blind() -> None:
    state = WatchState(started=0.0, last_check=0.0, breach_checks=1)
    gap = float(LIM.max_gap_seconds) + 1
    result = watch(STOP, q("9.80", "9.82"), NOW, gap, state, LIM, feed_alive=True)
    assert result.verdict is Watch.BLIND and "watch gap" in result.cause
    assert result.state.last_check == gap and result.state.breach_checks == 1
    # The exit is priced from the book the gap check itself received, when it is usable.
    assert result.state.last_usable_bid == D("9.80")
