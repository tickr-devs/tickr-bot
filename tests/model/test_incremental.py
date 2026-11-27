"""The incremental table (`features.incremental.DayTable`) against the training's way: for every
tested minute, the rows of `LiveContext.table_rows` (the live context's feature-only day2 on the
whole market's day so far) and of the incremental path are the same frame, bit for bit
(check_exact), on a randomized market larger than the list depth (see model_market; one
market, seed 11: a second one was dropped for the suite's time)."""

from __future__ import annotations

import datetime

import model_inputs as inputs
import model_market as synthetic
import model_shared
import pandas as pd
import pytest

from tickr.model import features
from tickr.model.errors import ModelRefused
from tickr.model.features import full, incremental

TICKERS = 650  # more than the lists' depth (500): the lists churn
HISTORY_DAYS = 6  # the table code needs at least 5 earlier days (MINH) for the hist group
DAYS = [d.date().isoformat() for d in pd.bdate_range(end="2026-11-18", periods=HISTORY_DAYS + 1)]
# Closed minutes (from 04:00): pre-market, the regular open and its first lists, mid-day, a
# minute right before a session's last row minute, after a flatten (no rows), post-market.
MINUTES = (45, 335, 336, 480, 690, 708, 709, 721, 800, 953)
CUTOFF_LAG = datetime.timedelta(seconds=5)  # the receipt cutoff after the minute's mark


def _context(today):
    return full.live_context(inputs.market_day(today))


def _cutoff(today, m):
    return _context(today).mark(m) + CUTOFF_LAG


def _cut(bars, today, m):
    """The day's bars through `m`, each received one second after its minute ends."""
    return inputs.received_bars(synthetic.cut(bars, today, m))


def _whole(cut, today, m, state, types):
    """The whole-market call: the context's table rows of `m`."""
    return _context(today).table_rows(cut, m, state, types, _cutoff(today, m))


# One randomized market: a second one (seed 12) was dropped for the suite's time, each costs
# about 15 s to build and 30 s to check
SEED = 11


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return model_shared.once(tmp_path_factory, f"incremental-{SEED}", lambda root: _world(SEED))


def _world(seed):
    days = synthetic.market(DAYS, TICKERS, seed)
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    want = {m: _whole(_cut(bars, today, m), today, m, state, None) for m in MINUTES}
    return today, bars, state, want


def _same(got, want):
    if want is None or not len(want):
        assert got is None or not len(got)
        return 0
    pd.testing.assert_frame_equal(got.reset_index(drop=True), want.reset_index(drop=True),
                                  check_exact=True)
    return len(want)


@pytest.mark.parametrize(("chunk", "workers"), [(None, 1), (97, 2)])
def test_every_minute_equals_the_whole_market_prefix(world, chunk, workers):
    """Part 2 in one chunk or in several (in worker processes too): the same rows."""
    today, bars, state, want = world
    table = incremental.DayTable(_context(today), state, None, workers)
    rows = 0
    try:
        for m in MINUTES:
            rows += _same(table.rows(_cut(bars, today, m), m, _cutoff(today, m), chunk),
                          want[m])
    finally:
        table.close()
    assert rows > 0 and table.rebuilds == 0


def test_minutes_one_by_one_and_a_types_filter(world):
    today, bars, state, _ = world
    types = set(bars.ticker.unique()[::3]) | {"SPY"}
    table = incremental.DayTable(_context(today), state, types)
    for m in range(331, 337):
        cut = _cut(bars, today, m)
        _same(table.rows(cut, m, _cutoff(today, m)), _whole(cut, today, m, state, types))


def test_a_changed_or_late_consumed_bar_starts_the_market_part_again(world):
    """A corrected bar of a consumed minute, then a bar that arrives after its minute was
    consumed: the rows are those of the bars handed in, as with the whole-day call."""
    today, bars, state, _ = world
    table = incremental.DayTable(_context(today), state, None)
    table.rows(_cut(bars, today, 340), 340, _cutoff(today, 340))
    cut = _cut(bars, today, 341).copy()
    early = cut.index[cut.window_start == cut.window_start.min()][0]
    cut.loc[early, "close"] *= 1.01
    _same(table.rows(cut, 341, _cutoff(today, 341)), _whole(cut, today, 341, state, None))
    assert table.rebuilds == 1
    late = _cut(bars, today, 343)
    first = late.index[late.window_start == late.window_start.max()][0]
    # the corrected close is back: started again
    table.rows(late.drop(index=first), 343, _cutoff(today, 343))
    assert table.rebuilds == 2
    cut = _cut(bars, today, 344)
    _same(table.rows(cut, 344, _cutoff(today, 344)), _whole(cut, today, 344, state, None))
    assert table.rebuilds == 3


def test_minutes_in_order_and_no_bar_after_the_minute(world):
    today, bars, state, _ = world
    table = incremental.DayTable(_context(today), state, None)
    with pytest.raises(ModelRefused) as e:
        table.rows(_cut(bars, today, 341), 340, _cutoff(today, 341))
    assert e.value.cause == "bar_after_minute"
    table.rows(_cut(bars, today, 340), 340, _cutoff(today, 340))
    with pytest.raises(ModelRefused) as e:
        table.rows(_cut(bars, today, 340), 340, _cutoff(today, 340))
    assert e.value.cause == "minute_order"


def test_the_minute_s_bounds_and_receipts_hold_as_in_the_context(world):
    """The live context's bounds refuse before anything is consumed; a bar received after the
    cutoff is left out, as `LiveContext.table_rows` leaves it out."""
    today, bars, state, _ = world
    ctx = _context(today)
    table = incremental.DayTable(ctx, state, None)
    cut = _cut(bars, today, 340)
    for when, cause in ((ctx.mark(340) - CUTOFF_LAG, "cutoff_before_mark"),
                        (_cutoff(today, 340).replace(tzinfo=None), "cutoff_not_aware")):
        with pytest.raises(ModelRefused) as e:
            table.rows(cut, 340, when)
        assert e.value.cause == cause
    with pytest.raises(ModelRefused) as e:
        table.rows(cut.drop(columns=[full.RECEIPT_COLUMN]), 340, _cutoff(today, 340))
    assert e.value.cause == "receipt_missing"
    assert table.closed == -1 and table.last == -1
    spy = cut.index[(cut.ticker == "SPY") & (cut.window_start == cut.window_start.max())][0]
    late = cut.copy()
    late.loc[spy, full.RECEIPT_COLUMN] += 10 * 60 * 10**9  # received ten minutes later
    got = table.rows(late, 340, _cutoff(today, 340))
    _same(got, _whole(late, today, 340, state, None))
    _same(_whole(late.drop(index=spy), today, 340, state, None), got)
