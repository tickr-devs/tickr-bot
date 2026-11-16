"""price-action-v3: v1 plus the spike setup on fast movers, and the detector behind it."""

from __future__ import annotations

import copy
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from strategy_bars import SYMBOL, TODAY, bar, d, prior_sessions, quote, rising_session

from tickr import config
from tickr.data.movers import MoverSettings, measure
from tickr.observers.loader import load_strategy, parse_modules
from tickr.strategy import price_action_v1 as v1
from tickr.strategy import price_action_v3 as v3
from tickr.strategy.base import SymbolContext, Verdict

ROOT = Path(__file__).resolve().parents[2]


def values(folder: str) -> dict:
    return tomllib.loads((ROOT / "strategies" / folder / "strategy.toml").read_text())


def make(**overrides):
    """v3 from the repo toml with a warm-up multiple of 1, so short sessions are enough."""
    vals = copy.deepcopy(values("price-action-v3"))
    vals["indicators"]["warmup_period_multiple"] = 1
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        vals[section][key] = value
    return v3.create(vals)


def make_v1():
    vals = copy.deepcopy(values("price-action-v1"))
    vals["indicators"]["warmup_period_multiple"] = 1
    return v1.create(vals)


def ctx(bars, q=None):
    return SymbolContext(SYMBOL, bars, prior_sessions(), q, None)


SETTINGS = MoverSettings(10, d(5), d(2), d(5), d(250_000))


def choppy(count, volume=3_000):
    """Sideways minutes: 20.00 / 20.02 closes, a 0.10 range, `volume` shares each."""
    return [bar(TODAY, i, 20.0, 20.05, 19.95, 20.0 + 0.02 * (i % 2), volume) for i in range(count)]


def spiked(count=30, close=20.5, volume=40_000, low=20.0):
    """`count` choppy minutes, then one bar that jumps on heavy volume."""
    bars = choppy(count)
    previous = bars[-1].close
    return [*bars, bar(TODAY, count, previous, close + 0.1, low, close, volume)]


# ---------------------------------------------------------------- detector


def test_detector_measures_the_latest_bar_against_its_lookback():
    mover = measure(SYMBOL, spiked(), SETTINGS)
    assert mover is not None and mover.spike_up
    assert mover.volume_ratio.value == d(40_000) / d(3_000) and mover.volume_ratio.fired
    assert mover.move_atr.fired and mover.move_atr.value > d(2)
    assert mover.dollar_volume_ratio.fired
    assert (mover.close, mover.low, mover.bar_dollar_volume) == (d("20.5"), d("20.0"),
                                                                 d("20.5") * 40_000)


def test_detector_needs_lookback_plus_two_bars():
    bars = spiked()
    assert measure(SYMBOL, bars[-SETTINGS.lookback_bars - 1:], SETTINGS) is None
    assert measure(SYMBOL, bars[-SETTINGS.lookback_bars - 2:], SETTINGS) is not None


def test_a_quiet_bar_a_tiny_bar_and_a_rangeless_lookback_are_not_movers():
    assert measure(SYMBOL, choppy(30), SETTINGS) is None
    floor = MoverSettings(10, d(1), d(2), d(1), d(1_000_000))  # 820k USD: below this floor
    assert measure(SYMBOL, spiked(), floor) is None
    flat = [bar(TODAY, i, 20, 20, 20, 20, 3_000) for i in range(20)]
    flat.append(bar(TODAY, 20, 20, 21, 20, 21, 40_000))
    assert measure(SYMBOL, flat, SETTINGS) is None  # no range to measure the move in


def test_a_down_move_is_a_mover_but_not_an_upward_spike():
    mover = measure(SYMBOL, spiked(close=19.5, low=19.4), SETTINGS)
    assert mover is not None and mover.move_atr.value < 0 and mover.move_atr.fired
    assert not mover.spike_up


def test_volume_alone_is_a_mover_but_not_a_spike():
    mover = measure(SYMBOL, spiked(close=20.02, low=19.98, volume=40_000), SETTINGS)
    assert mover is not None and mover.volume_ratio.fired and not mover.move_atr.fired
    assert not mover.spike_up


# ---------------------------------------------------------------- strategy


def test_v1_ignores_the_spike_and_v3_arms_it():
    bars = spiked()
    assert make_v1().evaluate(ctx(bars)).verdict is Verdict.IGNORE
    decision = make().evaluate(ctx(bars))
    assert decision.verdict is Verdict.WATCH and decision.setup == v3.SPIKE
    assert (decision.trigger, decision.invalidation) == (d("20.5"), d("20.0"))
    assert [c.rule for c in decision.checks] == [
        "bars_consistent", "history_bars", "spike_volume_ratio", "spike_move_atr",
        "spike_invalidation_below_trigger", "spike_age_bars", "atr"]


def test_spike_enters_inside_the_window_with_v1_quote_rules():
    decision = make().evaluate(ctx(spiked(), quote("20.51", "20.52")))
    assert decision.verdict is Verdict.ENTER and decision.setup == v3.SPIKE
    assert decision.entry_low == d("20.5") and decision.stop == d("20.0")
    assert decision.quantity is not None and decision.quantity >= 1


def test_spike_below_its_trigger_waits_and_far_above_is_never_chased():
    below = make().evaluate(ctx(spiked(), quote("20.48", "20.49")))
    assert below.verdict is Verdict.WATCH and below.checks[-1].rule == "ask_above_trigger"
    far = make().evaluate(ctx(spiked(), quote("23.00", "23.01")))
    assert far.verdict is Verdict.IGNORE and far.checks[-1].rule == "ask_within_price_window"


def test_not_a_spike_keeps_v1_decision():
    bars = choppy(31)
    assert make().evaluate(ctx(bars)) == _renamed(make_v1().evaluate(ctx(bars)))


def test_an_armed_v1_setup_wins_over_a_spike():
    bars = rising_session(TODAY, 40)
    decision = make().evaluate(ctx(bars))
    assert decision.verdict is Verdict.WATCH and decision.setup in (v1.BREAKOUT, v1.CONTINUATION)


def _renamed(decision):
    version = values("price-action-v3")["strategy"]["version"]
    return replace(decision, strategy=v3.NAME, version=version)


def test_session_liquidity_admits_a_thin_symbol_only_while_it_spikes():
    strategy = make()
    assert [c.passed for c in strategy.session_liquidity(spiked())] == [True]
    assert strategy.session_liquidity(spiked())[0].rule == v3.SPIKE_RULE
    quiet = strategy.session_liquidity(choppy(31))
    assert [(c.rule, c.passed) for c in quiet] == [(v3.SPIKE_RULE, False)]
    liquid = make(liquidity__min_dollar_volume_usd=1)
    assert [c.rule for c in liquid.session_liquidity(choppy(31))] == ["dollar_volume_usd"]


def test_fast_mover_uses_the_strategy_values():
    assert make().fast_mover(SYMBOL, spiked()) == measure(SYMBOL, spiked(), make().movers)
    assert make(spike__min_volume_ratio=50, spike__min_move_atr=50,
                spike__min_dollar_volume_ratio=50).fast_mover(SYMBOL, spiked()) is None


@pytest.mark.parametrize("key", ["lookback_bars", "min_volume_ratio", "min_move_atr",
                                 "min_dollar_volume_ratio", "min_bar_dollar_volume_usd",
                                 "signal_valid_bars"])
def test_every_spike_key_is_required(key):
    vals = values("price-action-v3")
    del vals["spike"][key]
    with pytest.raises(ValueError, match=key):
        v3.create(vals)


def test_v3_keeps_every_v1_value():
    a, b = values("price-action-v1"), values("price-action-v3")
    for section in a:
        if section != "strategy":
            assert a[section] == b[section], section


def test_v3_is_registered_and_loads():
    cfg = config.load(ROOT / "config" / "bot.toml")
    loaded = load_strategy(ROOT, "price-action-v3", parse_modules(cfg.observers.strategy_modules))
    assert loaded.strategy.name == v3.NAME


def after(bars, *closes_and_lows):
    """`bars` plus quiet minutes, each (close, low), on normal volume."""
    out = list(bars)
    for close, low in closes_and_lows:
        i = len(out)
        out.append(bar(TODAY, i, out[-1].close, max(close, float(out[-1].close)) + 0.02, low, close,
                       3_000))
    return out


def test_a_spike_stays_armed_for_its_window_while_its_low_holds():
    one = after(spiked(), (20.55, 20.40))
    decision = make().evaluate(ctx(one))
    assert decision.verdict is Verdict.WATCH and decision.setup == v3.SPIKE
    assert decision.trigger == d("20.5") and decision.invalidation == d("20.0")
    checks = {c.rule: c for c in decision.checks}
    assert checks["spike_age_bars"].measured == 1 and checks["spike_low_held"].passed
    entered = make().evaluate(ctx(after(spiked(), (20.55, 20.40), (20.56, 20.45)),
                                  quote("20.55", "20.56")))
    assert entered.verdict is Verdict.ENTER and entered.setup == v3.SPIKE


def test_a_spike_past_its_window_or_below_its_low_is_gone():
    late = after(spiked(), (20.55, 20.40), (20.56, 20.45), (20.57, 20.50))
    assert make().evaluate(ctx(late)).setup != v3.SPIKE
    broken = after(spiked(), (19.98, 19.90))
    assert make().evaluate(ctx(broken)).setup != v3.SPIKE
    assert make().spike(SYMBOL, broken) is None


def test_discovery_admits_a_thin_symbol_through_the_window():
    strategy = make()
    held = after(spiked(), (20.55, 20.40))
    assert [c.passed for c in strategy.session_liquidity(held)] == [True]
    late = after(held, (20.56, 20.45), (20.57, 20.50))
    assert [c.passed for c in strategy.session_liquidity(late)] == [False]

