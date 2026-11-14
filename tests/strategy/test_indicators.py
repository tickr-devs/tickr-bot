"""Oracle tests for the pure indicators: hand-computed values and independent references."""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from strategy_bars import bar, d, flat_session, rising_session

from aperture.strategy import indicators as ind


def test_ema_hand_computed():
    values = [d(x) for x in (1, 2, 3, 4, 5)]
    # period 3: seed = first value; alpha = 0.5
    assert ind.ema_series(values, 3) == [d(1), d("1.5"), d("2.25"), d("3.125"), d("4.0625")]


def test_atr_hand_computed():
    day = date(2026, 11, 13)
    bars = [
        bar(day, 0, 10, 11, 9, 10, 1),  # TR 2 (no previous close)
        bar(day, 1, 10, 10.5, 9.5, 10, 1),  # TR 1
        bar(day, 2, 10, 13, 12, 12.5, 1),  # TR max(1, 3, 2) = 3 (gap up)
        bar(day, 3, 12, 12.5, 8.5, 9, 1),  # TR max(4, 0, 4) = 4
    ]
    assert ind.true_range_series(bars) == [d(2), d(1), d(3), d(4)]
    # period 2: seed (2+1)/2 = 1.5; (1.5*1+3)/2 = 2.25; (2.25+4)/2 = 3.125
    assert ind.atr_series(bars, 2) == [None, d(1.5), d(2.25), d(3.125)]


def test_vwap_and_dollar_volume_hand_computed():
    day = date(2026, 11, 13)
    bars = [bar(day, 0, 10, 12, 9, 9, 100), bar(day, 1, 9, 15, 12, 15, 300)]
    # typical prices 10 and 14 -> (1000 + 4200) / 400 = 13
    assert ind.vwap_series(bars) == [d(10), d(13)]
    assert ind.dollar_volume(bars) == d(9 * 100 + 15 * 300)


def test_vwap_is_none_until_volume_appears():
    day = date(2026, 11, 13)
    bars = [bar(day, 0, 10, 10, 10, 10, 0), bar(day, 1, 10, 10, 10, 10, 5)]
    assert ind.vwap_series(bars) == [None, d(10)]


def _reference_ema(values, period):
    """Independent float-free reference using Fractions."""
    xs = [Fraction(v) for v in values]
    prev = xs[0]
    out = [prev]
    for x in xs[1:]:
        prev = prev + (x - prev) * Fraction(2, period + 1)
        out.append(prev)
    return out


def _reference_atr(bars, period):
    trs = []
    for i, b in enumerate(bars):
        h, low = Fraction(b.high), Fraction(b.low)
        if i == 0:
            trs.append(h - low)
        else:
            c = Fraction(bars[i - 1].close)
            trs.append(max(h - low, abs(h - c), abs(low - c)))
    out = [None] * (period - 1)
    prev = sum(trs[:period]) / period
    out.append(prev)
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


def _random_bars(seed, count=120):
    rng = random.Random(seed)
    day = date(2026, 11, 13)
    price = 20.0
    bars = []
    for i in range(count):
        o = round(price, 2)
        c = round(max(1.0, o + rng.uniform(-0.3, 0.3)), 2)
        h = round(max(o, c) + rng.uniform(0, 0.1), 2)
        low = round(min(o, c) - rng.uniform(0, 0.1), 2)
        bars.append(bar(day, i, o, h, low, c, rng.randint(0, 50_000)))
        price = c
    return bars


def _close(a, b):
    if a is None or b is None:
        return a is b
    return abs(Fraction(a) - b) < Fraction(1, 10**18)


@pytest.mark.parametrize("seed", range(5))
def test_ema_and_atr_match_reference(seed):
    bars = _random_bars(seed)
    closes = [b.close for b in bars]
    for period in (9, 20):
        ours = ind.ema_series(closes, period)
        assert all(_close(a, b) for a, b in zip(ours, _reference_ema(closes, period), strict=True))
    ours = ind.atr_series(bars, 14)
    assert all(_close(a, b) for a, b in zip(ours, _reference_atr(bars, 14), strict=True))


@pytest.mark.parametrize("seed", range(5))
def test_series_never_look_ahead(seed):
    """Corrupting bars after i never changes any series value at i."""
    bars = _random_bars(seed, 60)
    clean = {
        "ema": ind.ema_series([b.close for b in bars], 9),
        "atr": ind.atr_series(bars, 14),
        "vwap": ind.vwap_series(bars),
    }
    rng = random.Random(seed + 100)
    for i in range(len(bars) - 1):
        corrupted = bars[: i + 1] + [
            bar(date(2026, 11, 13), j, 99, 120, 1, rng.uniform(1, 99), 10**7)
            for j in range(i + 1, len(bars))
        ]
        assert ind.ema_series([b.close for b in corrupted], 9)[i] == clean["ema"][i]
        assert ind.atr_series(corrupted, 14)[i] == clean["atr"][i]
        assert ind.vwap_series(corrupted)[i] == clean["vwap"][i]


def test_current_session_takes_trailing_ny_date():
    yesterday = rising_session(date(2026, 11, 12), 10)
    today = rising_session(date(2026, 11, 13), 4)
    assert list(ind.current_session(yesterday + today)) == today
    assert list(ind.current_session([])) == []


def test_relative_volume_aligns_by_ny_minute_across_dst():
    # 2026-10-30 is daylight time (UTC-4), 2026-11-13 standard time (UTC-5).
    prior = [flat_session(date(2026, 10, 30), 390, volume=100)]
    today = flat_session(date(2026, 11, 13), 10, volume=300)
    # 10 minutes each: 3000 / 1000
    assert ind.relative_volume(today, prior, 1) == d(3)


def test_relative_volume_averages_the_most_recent_sessions():
    prior = [
        flat_session(date(2026, 11, 10), 390, volume=1_000),  # not used
        flat_session(date(2026, 11, 11), 390, volume=100),
        flat_session(date(2026, 11, 12), 390, volume=300),
    ]
    today = flat_session(date(2026, 11, 13), 5, volume=400)
    # current 2000 / average(500, 1500) = 2
    assert ind.relative_volume(today, prior, 2) == d(2)


def test_relative_volume_unavailable_cases():
    today = flat_session(date(2026, 11, 13), 5)
    assert ind.relative_volume(today, [flat_session(date(2026, 11, 12), 390)], 2) is None
    zero = [flat_session(date(2026, 11, 12), 390, volume=0)]
    assert ind.relative_volume(today, zero, 1) is None
    assert ind.relative_volume([], [flat_session(date(2026, 11, 12), 390)], 1) is None


def test_relative_volume_ignores_prior_minutes_after_now():
    prior = [flat_session(date(2026, 11, 12), 390, volume=100)]
    today = flat_session(date(2026, 11, 13), 1, volume=100)
    assert ind.relative_volume(today, prior, 1) == Decimal(1)
