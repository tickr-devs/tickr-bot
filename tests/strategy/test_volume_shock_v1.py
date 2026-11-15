"""volume-shock-v1: first early volume shock in a stock up 1-3 percent, as an observer."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest
from strategy_bars import SYMBOL, TODAY, bar, d, prior_sessions, quote

from tickr import config
from tickr.observers.loader import check_history, load_strategy, parse_modules
from tickr.strategy import volume_shock_v1 as vs
from tickr.strategy.base import PositionView, SymbolContext, Verdict

ROOT = Path(__file__).resolve().parents[2]
VALUES = tomllib.loads((ROOT / "strategies" / "volume-shock-v1" / "strategy.toml").read_text())
PREVIOUS = 20  # prior_sessions() are flat at 20.00 with 3000 shares a minute


def make(**overrides):
    vals = copy.deepcopy(VALUES)
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        vals[section][key] = value
    return vs.create(vals)


def quiet(count, price=20.2, volume=3_000):
    return [bar(TODAY, i, price, price, price, price, volume) for i in range(count)]


def shock(count=10, at=None, close=20.4, volume=80_000):
    """Quiet bars at +1%, one bar at index `at` (default the last) closing at `close` at its
    high with `volume` shares (4 x 3000 + 80000 >= 5 x 5 x 3000), then quiet bars at `close`."""
    at = count - 1 if at is None else at
    bars = quiet(at)
    low = round(close - 0.1, 2)
    bars.append(bar(TODAY, at, low, close, low, close, volume))
    bars += [bar(TODAY, i, close, close, close, close, 3_000) for i in range(at + 1, count)]
    return bars


def ctx(bars, q=None, position=None, prior=None):
    return SymbolContext(SYMBOL, bars, prior_sessions() if prior is None else prior, q, position)


def rules(decision):
    return [(c.rule, c.passed) for c in decision.checks]


def test_shock_without_quote_is_watch_with_levels():
    decision = make().evaluate(ctx(shock()))
    assert decision.verdict is Verdict.WATCH
    assert decision.setup == vs.SETUP
    assert decision.trigger == d("20.4") and decision.invalidation == d("20.3")
    assert all(c.passed for c in decision.checks)


def admissible_shape(decision):
    """The platform's admission shape: 0 < stop < entry_low <= entry_high."""
    return d(0) < decision.stop < decision.entry_low <= decision.entry_high


def test_shock_with_quote_enters_below_the_window_floor():
    decision = make().evaluate(ctx(shock(), quote("20.40", "20.42")))
    assert decision.verdict is Verdict.ENTER
    assert admissible_shape(decision)
    # the signal-bar low 20.3 is above entry_low 20.298 x 0.995, so the minimum distance wins
    assert decision.entry_low == d("20.4") * d("0.995")
    assert decision.stop == decision.entry_low * d("0.995")
    assert decision.entry_low <= d("20.42") <= decision.entry_high
    # notional cap 2500 / entry_high 20.502 = 121 is below the loss sizing
    assert decision.quantity == 121


def test_a_low_far_below_is_the_stop():
    bars = shock()
    bars[-1] = bar(TODAY, len(bars) - 1, 20.0, 20.4, 19.9, 20.4, 80_000)
    decision = make().evaluate(ctx(bars, quote("20.40", "20.42")))
    assert decision.verdict is Verdict.ENTER
    assert decision.stop == d("19.9") and admissible_shape(decision)


def test_loss_budget_sizes_down():
    bars = shock()
    q = quote("20.40", "20.42")
    full = make().evaluate(ctx(bars, q))
    small = make().evaluate(SymbolContext(SYMBOL, bars, prior_sessions(), q, None, d("5")))
    assert small.verdict is Verdict.ENTER and small.quantity < full.quantity
    assert ("daily_loss_budget_usd", True) in rules(small)


def test_a_signal_older_than_signal_valid_minutes_minutes_expires():
    decision = make().evaluate(ctx(shock(count=11, at=5)))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("signal_age_minutes", False)


@pytest.mark.parametrize("close", [20.1, 20.7])
def test_change_outside_the_band_is_no_signal(close):
    decision = make().evaluate(ctx(shock(close=close)))
    assert rules(decision)[-1] == ("signal", False)


def test_normal_volume_is_no_signal():
    decision = make().evaluate(ctx(shock(volume=40_000)))
    assert rules(decision)[-1] == ("signal", False)


def test_a_close_low_in_the_bar_is_no_signal():
    bars = shock()
    bars[-1] = bar(TODAY, len(bars) - 1, 20.4, 20.6, 20.3, 20.35, 80_000)
    assert rules(make().evaluate(ctx(bars)))[-1] == ("signal", False)


def test_missing_prior_session_is_refused():
    decision = make().evaluate(ctx(shock(), prior=prior_sessions(count=4)))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("prior_sessions", False)


def test_cheap_stock_is_refused():
    decision = make(liquidity__min_previous_close_usd=25).evaluate(ctx(shock()))
    assert rules(decision)[-1] == ("previous_close_usd", False)


def test_ask_outside_the_window_is_ignored():
    decision = make().evaluate(ctx(shock(), quote("20.60", "20.61")))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("ask_within_price_window", False)


def test_open_position_is_held():
    position = PositionView(d("20.4"), d("10"), d("20.3"))
    assert make().evaluate(ctx(shock(), position=position)).verdict is Verdict.HOLD


def test_no_look_ahead_the_signal_fires_only_on_its_bar_and_later_bars_change_nothing():
    bars = shock(count=14, at=8)
    verdicts = [make().evaluate(ctx(bars[:k])).verdict for k in range(1, len(bars) + 1)]
    assert [k for k, v in enumerate(verdicts) if v is Verdict.WATCH] == [8, 9, 10, 11, 12]
    altered = bars[:9] + [bar(TODAY, i, 30, 31, 29, 30, 500_000) for i in range(9, 14)]
    assert make().evaluate(ctx(altered[:9])) == make().evaluate(ctx(bars[:9]))
    assert make().evaluate(ctx(altered[:12])).trigger == make().evaluate(ctx(bars[:9])).trigger


def test_a_minute_missing_in_a_prior_session_is_refused_not_zero():
    prior = prior_sessions()
    prior[2] = prior[2][:7] + prior[2][8:]  # minute 7 missing on one prior day
    decision = make().evaluate(ctx(shock(count=10, at=9), prior=prior))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("prior_window_complete", False)


@pytest.mark.parametrize("section,key,value", [
    ("signal", "change_min_percent", 3.0),
    ("signal", "close_location_min", 1.5),
    ("signal", "volume_window_bars", 0),
    ("signal", "extra", 1),
])
def test_bad_values_are_refused(section, key, value):
    vals = copy.deepcopy(VALUES)
    vals[section][key] = value
    with pytest.raises(ValueError):
        vs.create(vals)


def test_loads_as_an_observer_from_the_repo_config():
    cfg = config.load(ROOT / "config" / "bot.toml")
    modules = parse_modules(cfg.observers.strategy_modules)
    loaded = load_strategy(ROOT, "volume-shock-v1", modules)
    assert (loaded.strategy.name, loaded.strategy.version) == ("volume-shock-v1", "1")
    check_history(loaded, cfg.market_data.history_sessions)


def test_the_stop_keeps_the_minimum_distance_below_the_bid():
    # ask inside the window, bid below its floor: the bid rule sets the stop
    decision = make().evaluate(ctx(shock(), quote("20.25", "20.30")))
    assert decision.verdict is Verdict.ENTER
    assert decision.stop == d("20.25") * d("0.995") < decision.entry_low * d("0.995")


@pytest.mark.parametrize("key,value",
                         [("entry_window_percent", 100), ("minimum_stop_percent", 150)])
def test_percent_bounds_are_refused(key, value):
    vals = copy.deepcopy(VALUES)
    vals["entry"][key] = value
    with pytest.raises(ValueError):
        vs.create(vals)


def test_today_not_starting_at_the_open_is_refused():
    decision = make().evaluate(ctx(shock()[1:]))
    assert rules(decision)[-1] == ("session_from_open", False)


def test_a_signal_whose_low_was_broken_is_not_entered():
    bars = shock(count=11, at=9)
    bars[10] = bar(TODAY, 10, 20.4, 20.4, 20.2, 20.31, 3_000)
    decision = make().evaluate(ctx(bars, quote("20.30", "20.31")))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("signal_not_invalidated", False)


def test_signal_age_counts_minutes_across_a_gap():
    bars = shock(count=10, at=9) + [bar(TODAY, 30, 20.4, 20.4, 20.4, 20.4, 3_000)]
    decision = make().evaluate(ctx(bars))
    assert decision.verdict is Verdict.IGNORE
    assert rules(decision)[-1] == ("signal_age_minutes", False)


def test_a_later_signal_after_the_first_expired_is_ignored():
    bars = shock(count=20, at=5)
    bars[19] = bar(TODAY, 19, 20.3, 20.4, 20.3, 20.4, 80_000)  # a second genuine shock
    decision = make().evaluate(ctx(bars))
    assert rules(decision)[-1] == ("signal_age_minutes", False)


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_non_finite_values_are_refused(value):
    vals = copy.deepcopy(VALUES)
    vals["sizing"]["planned_loss_usd"] = float(value)
    with pytest.raises(ValueError):
        vs.create(vals)


def test_a_later_low_equal_to_the_signal_low_invalidates():
    bars = shock(count=11, at=9)
    bars[10] = bar(TODAY, 10, 20.4, 20.4, 20.3, 20.35, 3_000)
    assert rules(make().evaluate(ctx(bars)))[-1] == ("signal_not_invalidated", False)
