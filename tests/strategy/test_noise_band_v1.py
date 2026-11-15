"""noise-band-v1: first close beyond the usual move for the time of day, as an observer."""

from __future__ import annotations

import copy
import gc
import time
import tomllib
from pathlib import Path

import pytest
from strategy_bars import PRIOR_DAYS, SYMBOL, TODAY, bar, d, quote

from tickr import config
from tickr.observers.loader import check_history, load_strategy, parse_modules
from tickr.strategy import noise_band_v1 as nb
from tickr.strategy.base import PositionView, SymbolContext, Verdict

ROOT = Path(__file__).resolve().parents[2]
VALUES = tomllib.loads((ROOT / "strategies" / "noise-band-v1" / "strategy.toml").read_text())


def make(**overrides):
    vals = copy.deepcopy(VALUES)
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        vals[section][key] = value
    return nb.create(vals)


def wobbly_prior(day, minutes=390):
    """Opens at 20.00 and closes 1% above the open at every minute: band = 0.01."""
    return [bar(day, i, 20, 20.2, 19.9, 20.2, 3_000) for i in range(minutes)]


def prior(minutes=390):
    return [wobbly_prior(day, minutes) for day in PRIOR_DAYS]


def today(count, jump_at=None, open_=20.0):
    """Flat at the open; from `jump_at` on, closes 2% above the open (beyond the 1% band)."""
    bars = []
    for i in range(count):
        c = open_ * 1.02 if jump_at is not None and i >= jump_at else open_
        low = open_ - 0.05 if c == open_ else round(c - 0.05, 2)  # never back under the band
        bars.append(bar(TODAY, i, open_, round(c + 0.05, 2), low, round(c, 2), 5_000))
    return bars


def ctx(bars, q=None, position=None, prior_sessions=None, budget=None):
    return SymbolContext(SYMBOL, bars, prior() if prior_sessions is None else prior_sessions, q,
                         position, budget)


def rules(decision):
    return [(c.rule, c.passed) for c in decision.checks]


def test_close_beyond_the_band_is_watch_with_the_band_level():
    decision = make().evaluate(ctx(today(41, jump_at=40)))
    assert decision.verdict is Verdict.WATCH
    assert decision.setup == nb.SETUP and decision.trigger == d("20.4")
    # VWAP of 40 flat bars at typical price (20.05 + 19.95 + 20) / 3 = 20 and one at 20.4 is
    # below the band level 20 x 1.01 = 20.2, so the band level is the stop level
    assert decision.invalidation == d("20.20")


def test_vwap_below_the_close_and_above_the_band_raises_the_stop_level():
    bars = [bar(TODAY, i, 20.3, 20.35, 20.25, 20.3, 5_000) for i in range(30)]
    bars.append(bar(TODAY, 30, 20.3, 20.45, 20.3, 20.4, 5_000))
    bars[0] = bar(TODAY, 0, 20.0, 20.35, 19.95, 20.3, 5_000)
    decision = make().evaluate(ctx(bars))
    assert decision.verdict is Verdict.WATCH
    assert d("20.2") < decision.invalidation < decision.trigger


def test_vwap_above_the_close_is_not_a_stop_level():
    bars = [bar(TODAY, i, 20.0, 20.05, 19.95, 20.0, 5_000) for i in range(30)]
    bars[5] = bar(TODAY, 5, 20.0, 23.0, 22.0, 22.5, 200_000)  # early spike lifts VWAP
    bars[0] = bar(TODAY, 0, 20.0, 20.05, 19.95, 20.0, 5_000)
    bars.append(bar(TODAY, 30, 20.3, 20.45, 20.3, 20.4, 5_000))
    decision = make().evaluate(ctx(bars))
    assert decision.verdict is Verdict.WATCH
    assert decision.invalidation == d("20.20")


def test_after_the_latest_minute_is_no_signal():
    assert rules(make().evaluate(ctx(today(332, jump_at=331))))[-1] == ("signal", False)


def test_enter_has_the_admission_shape():
    decision = make().evaluate(ctx(today(41, jump_at=40), quote("20.39", "20.41")))
    assert decision.verdict is Verdict.ENTER
    assert d(0) < decision.stop < decision.entry_low <= decision.entry_high
    assert decision.stop <= decision.entry_low * d("0.995")


def test_inside_the_band_is_no_signal():
    assert rules(make().evaluate(ctx(today(41))))[-1] == ("signal", False)


def test_before_the_earliest_minute_is_no_signal():
    assert rules(make().evaluate(ctx(today(20, jump_at=19))))[-1] == ("signal", False)


def test_the_first_signal_stays_valid_for_signal_valid_minutes_then_expires():
    still = make().evaluate(ctx(today(45, jump_at=40)))  # signal bar 40, last bar 44
    assert still.verdict is Verdict.WATCH and still.trigger == d("20.4")
    expired = make().evaluate(ctx(today(46, jump_at=40)))
    assert expired.verdict is Verdict.IGNORE
    assert rules(expired)[-1] == ("signal_age_minutes", False)


def test_no_look_ahead_over_every_prefix():
    bars = today(50, jump_at=42)
    verdicts = [make().evaluate(ctx(bars[:k])).verdict for k in range(1, len(bars) + 1)]
    # WATCH from the signal bar 42 for signal_valid_minutes = 5 bars, never before
    assert [k for k, v in enumerate(verdicts) if v is Verdict.WATCH] == [42, 43, 44, 45, 46]
    altered = bars[:43] + [bar(TODAY, i, 30, 31, 29, 30, 9_000) for i in range(43, 50)]
    assert make().evaluate(ctx(altered[:43])) == make().evaluate(ctx(bars[:43]))


def test_a_prior_session_missing_the_minute_is_refused():
    sessions = prior()
    sessions[1] = sessions[1][:40] + sessions[1][41:]
    decision = make().evaluate(ctx(today(41, jump_at=40), prior_sessions=sessions))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("prior_band_complete", False)


def test_a_prior_half_day_silences_the_afternoon_only():
    sessions = prior()
    sessions[0] = wobbly_prior(PRIOR_DAYS[0], minutes=210)  # 13:00 early close
    afternoon = today(241, jump_at=240)
    assert rules(make().evaluate(ctx(afternoon, prior_sessions=sessions)))[-1] == (
        "prior_band_complete", False)
    assert make().evaluate(ctx(today(41, jump_at=40), prior_sessions=sessions)).verdict is (
        Verdict.WATCH)


def test_today_without_the_0930_bar_is_refused():
    decision = make().evaluate(ctx(today(41, jump_at=40)[1:]))
    assert rules(decision)[-1] == ("session_from_open", False)


def test_missing_prior_session_and_cheap_stock_are_refused():
    short = make().evaluate(ctx(today(41, jump_at=40), prior_sessions=prior()[1:]))
    assert rules(short)[-1] == ("prior_sessions", False)
    cheap = make(liquidity__min_previous_close_usd=25).evaluate(ctx(today(41, jump_at=40)))
    assert rules(cheap)[-1] == ("previous_close_usd", False)


def test_open_position_is_held():
    position = PositionView(d("20.4"), d("10"), d("20.1"))
    assert make().evaluate(ctx(today(41, jump_at=40), position=position)).verdict is Verdict.HOLD


def test_a_full_day_evaluates_well_inside_the_observer_time_limit():
    # The claim is about the strategy's own cost, so this measures this thread's CPU time
    # with the garbage collector paused: under a loaded full-suite run, wall time also counted
    # other processes and a collection of the whole suite's heap, which made it flaky.
    bars = today(380)
    strategy = make()
    context = ctx(bars)
    cfg = config.load(ROOT / "config" / "bot.toml")
    collecting = gc.isenabled()
    gc.disable()
    try:
        started = time.thread_time()
        strategy.evaluate(context)
        spent = time.thread_time() - started
    finally:
        if collecting:
            gc.enable()
    assert spent < float(cfg.observers.max_evaluation_seconds) / 10


@pytest.mark.parametrize("section,key,value", [
    ("signal", "earliest_minutes_after_open", 340),
    ("signal", "latest_minutes_after_open", 390),
    ("signal", "band_multiple", 0),
    ("signal", "extra", 1),
])
def test_bad_values_are_refused(section, key, value):
    vals = copy.deepcopy(VALUES)
    vals[section][key] = value
    with pytest.raises(ValueError):
        nb.create(vals)


def test_loads_as_an_observer_from_the_repo_config():
    cfg = config.load(ROOT / "config" / "bot.toml")
    modules = parse_modules(cfg.observers.strategy_modules)
    loaded = load_strategy(ROOT, "noise-band-v1", modules)
    assert (loaded.strategy.name, loaded.strategy.version) == ("noise-band-v1", "1")
    check_history(loaded, cfg.market_data.history_sessions)


def test_a_signal_whose_band_level_was_broken_is_not_entered():
    bars = today(42, jump_at=40)
    bars[41] = bar(TODAY, 41, 20.4, 20.45, 20.1, 20.4, 5_000)
    decision = make().evaluate(ctx(bars, quote("20.39", "20.41")))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("signal_not_invalidated", False)
