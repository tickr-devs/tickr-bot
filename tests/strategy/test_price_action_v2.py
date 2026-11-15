"""price-action-v2: trigger confirmation and the no-progress exit on top of price-action-v1."""

from __future__ import annotations

import copy
import tomllib
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from strategy_bars import (
    NY,
    PRIOR_DAYS,
    SYMBOL,
    TODAY,
    d,
    flat_session,
    minute,
    prior_sessions,
    quote,
    rising_session,
)

from tickr import config
from tickr.observers.loader import load_strategy, parse_modules
from tickr.replay.__main__ import session_clock
from tickr.replay.engine import ExitReason, replay
from tickr.replay.settings import load_settings
from tickr.research.compare import DiffKind, decide, diff
from tickr.strategy import price_action_v1 as v1
from tickr.strategy import price_action_v2 as v2
from tickr.strategy.base import PositionView, SymbolContext, Verdict
from tickr.types import Bar

ROOT = Path(__file__).resolve().parents[2]


def values(folder: str) -> dict:
    return tomllib.loads((ROOT / "strategies" / folder / "strategy.toml").read_text())


def make_v1():
    vals = copy.deepcopy(values("price-action-v1"))
    vals["indicators"]["warmup_period_multiple"] = 1
    return v1.create(vals)


def make(**overrides):
    """v2 from the repo toml with a warm-up multiple of 1, so short sessions are enough."""
    vals = copy.deepcopy(values("price-action-v2"))
    vals["indicators"]["warmup_period_multiple"] = 1
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        vals[section][key] = value
    return v2.create(vals)


def ctx(bars, q=None, position=None):
    return SymbolContext(SYMBOL, bars, prior_sessions(), q, position)


BARS = rising_session(TODAY, 40)


def through(bars):
    """An executable quote just above the bar's trigger (v1 must say ENTER on it)."""
    trigger = make().evaluate(ctx(bars)).trigger
    q = quote(trigger, trigger + d("0.01"))
    assert make_v1().evaluate(ctx(bars, q)).verdict is Verdict.ENTER
    return q


def not_through(bars):
    trigger = make().evaluate(ctx(bars)).trigger
    return quote(trigger - d("0.01"), trigger)


def last_check(decision):
    return decision.checks[-1]


# ---------------------------------------------------------------- create()


def test_create_from_repo_toml_values_are_v1_plus_rules():
    s = v2.create(values("price-action-v2"))
    assert (s.name, s.version) == ("price-action-v2", "2")
    assert s.rules == v2.Rules(2, d("0.5"), 15)
    base = v1.create(values("price-action-v1")).settings
    assert replace(s.settings, name=base.name, version=base.version) == base


@pytest.mark.parametrize("mutate, message", [
    (lambda v: v.pop("confirmation"), "[confirmation] must be a table"),
    (lambda v: v["no_progress"].update(extra=1), "unknown keys ['extra']"),
    (lambda v: v["no_progress"].pop("window_minutes"), "missing keys ['window_minutes']"),
    (lambda v: v["confirmation"].update(confirmation_quotes=0), "must be a positive integer"),
    (lambda v: v["no_progress"].update(minimum_progress_r=0), "must be positive"),
    (lambda v: v["strategy"].update(name="price-action-v1"), "name must be 'price-action-v2'"),
])
def test_create_refuses_bad_values(mutate, message):
    vals = copy.deepcopy(values("price-action-v2"))
    mutate(vals)
    with pytest.raises(ValueError) as err:
        v2.create(vals)
    assert message in str(err.value)


def test_selectable_as_observer_with_the_repo_modules():
    modules = parse_modules(config.load(ROOT / "config" / "bot.toml").observers.strategy_modules)
    loaded = load_strategy(ROOT, "price-action-v2", modules)
    assert loaded.identity == "price-action-v2 v2"


def test_v1_refuses_the_v2_file():
    with pytest.raises(ValueError, match="unknown"):
        v1.create(values("price-action-v2"))


# ---------------------------------------------------------------- trigger confirmation


def test_enter_needs_two_consecutive_quotes_through_the_trigger():
    s = make()
    first = s.evaluate(ctx(BARS[:39], through(BARS[:39])))
    assert first.verdict is Verdict.WATCH
    assert first.decisive is last_check(first)
    assert (first.decisive.rule, first.decisive.measured) == ("trigger_confirmation", 1)
    assert first.trigger is not None and first.entry_high is first.stop is first.quantity is None
    second = s.evaluate(ctx(BARS, through(BARS)))
    assert second.verdict is Verdict.ENTER
    assert (last_check(second).rule, last_check(second).measured) == ("trigger_confirmation", 2)
    assert second.decisive is None
    # same levels and checks as v1 on the same input, plus the confirmation
    expected = make_v1().evaluate(ctx(BARS, through(BARS)))
    assert replace(second, strategy="", version="", checks=second.checks[:-1]) == replace(
        expected, strategy="", version="")


def test_a_quote_not_through_resets_the_count():
    s = make()
    s.evaluate(ctx(BARS[:38], through(BARS[:38])))
    reset = s.evaluate(ctx(BARS[:39], not_through(BARS[:39])))
    assert reset.verdict is Verdict.WATCH and reset.decisive.rule == "ask_above_trigger"
    again = s.evaluate(ctx(BARS, through(BARS)))
    assert again.verdict is Verdict.WATCH
    assert (again.decisive.rule, again.decisive.measured) == ("trigger_confirmation", 1)


def test_a_missing_quote_resets_the_count():
    s = make()
    s.evaluate(ctx(BARS[:38], through(BARS[:38])))
    assert s.evaluate(ctx(BARS[:39])).verdict is Verdict.WATCH
    assert s.evaluate(ctx(BARS, through(BARS))).verdict is Verdict.WATCH


def test_count_needs_the_previous_bar():
    s = make()
    s.evaluate(ctx(BARS[:38], through(BARS[:38])))  # bar 38 skipped
    assert s.evaluate(ctx(BARS, through(BARS))).verdict is Verdict.WATCH


def test_count_does_not_carry_across_sessions():
    s = make()
    yesterday = rising_session(PRIOR_DAYS[-1], 40)
    s.evaluate(ctx(yesterday, through(yesterday)))
    s.evaluate(ctx(yesterday + rising_session(TODAY, 1, start=24.0)))
    assert s._streaks[SYMBOL][1] == 0  # nothing carried into the first bar of the session


def test_the_same_bar_again_is_not_counted_twice():
    s = make()
    s.evaluate(ctx(BARS[:39], through(BARS[:39])))
    assert s.evaluate(ctx(BARS[:39], through(BARS[:39]))).verdict is Verdict.WATCH


def test_symbols_are_counted_separately():
    s = make()
    s.evaluate(ctx(BARS[:39], through(BARS[:39])))
    other = [replace(b, symbol="WXYZ") for b in BARS]
    s.evaluate(SymbolContext("WXYZ", other, (), None))
    assert s.evaluate(ctx(BARS, through(BARS))).verdict is Verdict.ENTER


# ---------------------------------------------------------------- no-progress exit

ENTRY, STOP = d("23.00"), d("22.50")  # R = 0.50; X = 0.5 R -> best high must reach 23.25
POSITION = PositionView(ENTRY, d(10), STOP)
FLAT = flat_session(TODAY, 40, price=23.0)


def held(s, bars):
    return s.evaluate(ctx(bars, position=POSITION))


def test_no_progress_exits_at_n_minutes():
    s = make()
    for end in range(10, 24):  # first seen on bar 9: minutes held 1 .. 14
        decision = held(s, FLAT[:end])
        assert decision.verdict is Verdict.HOLD
        assert decision.decisive.rule == "minutes_held"
    exit_ = held(s, FLAT[:24])  # bar 23 completes minute 15
    assert exit_.verdict is Verdict.EXIT and exit_.decisive is None
    assert [c.rule for c in exit_.checks] == ["bars_consistent", "risk_per_share", "minutes_held",
                                           "progress_r"]
    assert last_check(exit_).measured == 0


@pytest.mark.parametrize("best_high, verdict", [
    ("23.25", Verdict.HOLD),   # exactly 0.5 R: progress made
    ("23.24", Verdict.EXIT),   # 0.48 R: below X
])
def test_progress_boundary(best_high, verdict):
    s = make()
    bars = list(FLAT)
    bars[15] = replace(bars[15], high=d(best_high))
    held(s, bars[:10])
    decision = held(s, bars[:24])
    assert decision.verdict is verdict
    assert last_check(decision).rule == "progress_r"


def test_progress_before_entry_does_not_count():
    s = make()
    bars = list(FLAT)
    bars[5] = replace(bars[5], high=d("30"))
    held(s, bars[:10])
    assert held(s, bars[:24]).verdict is Verdict.EXIT


def test_the_entry_bar_high_before_the_fill_does_not_count():
    s = make()
    bars = list(FLAT)
    bars[9] = replace(bars[9], high=d("23.40"))  # first bar seen: 0.8 R, but before the fill
    held(s, bars[:10])
    decision = held(s, bars[:24])
    assert decision.verdict is Verdict.EXIT
    assert last_check(decision).rule == "progress_r" and last_check(decision).measured == 0


def test_a_new_position_restarts_the_clock():
    s = make()
    held(s, FLAT[:10])
    s.evaluate(ctx(FLAT[:20]))  # flat again
    assert held(s, FLAT[:24]).verdict is Verdict.HOLD
    other = PositionView(d("23.10"), d(10), STOP)
    s.evaluate(ctx(FLAT[:25], position=POSITION))
    assert s.evaluate(ctx(FLAT[:30], position=other)).decisive.rule == "minutes_held"


def test_foreign_bars_hold_with_a_cause():
    bars = [replace(b, symbol="WXYZ") for b in FLAT]
    decision = make().evaluate(ctx(bars, position=POSITION))
    assert decision.verdict is Verdict.HOLD and decision.decisive.rule == "bars_consistent"


def test_no_risk_holds_with_a_cause():
    decision = make().evaluate(ctx(FLAT, position=PositionView(ENTRY, d(10), ENTRY)))
    assert decision.verdict is Verdict.HOLD and decision.decisive.rule == "risk_per_share"


# ---------------------------------------------------------------- replay and decide/diff


def _day() -> list[Bar]:
    """17 rising minutes on high volume, then flat: an entry that makes no progress."""
    bars, price, step, wick = [], d(20), d("0.30"), d("0.05")
    for i in range(17):
        bars.append(Bar(SYMBOL, minute(TODAY, i), price, price + step + wick, price - wick,
                        price + step, d(30000)))
        price += step
    bars += [Bar(SYMBOL, minute(TODAY, i), price, price + wick, price - wick, price, d(30000))
             for i in range(17, 60)]
    return bars


def _flat_day(day: date) -> list[Bar]:
    return [Bar(SYMBOL, minute(day, i), d(20), d(20), d(20), d(20), d(3000)) for i in range(390)]


def test_replay_and_diff_against_v1():
    bars = {SYMBOL: [b for day in PRIOR_DAYS for b in _flat_day(day)] + _day()}
    settings = load_settings(ROOT / "config" / "replay.toml")
    clock = session_clock(ROOT / "config" / "bot.toml")
    old_values, new_values = values("price-action-v1"), values("price-action-v2")

    old = replay(lambda: v1.create(old_values), bars, settings, clock)
    new = replay(lambda: v2.create(new_values), bars, settings, clock)
    (old_trade,), (new_trade,) = old.trades, new.trades
    # confirmation: v2 enters one bar after v1
    assert new_trade.signal_bar_start == old_trade.signal_bar_start + v2.M1
    # no progress: v2 leaves 15 minutes after its entry bar, v1 holds to the end of data
    assert old_trade.exit_reason is ExitReason.END_OF_DATA
    assert new_trade.exit_reason is ExitReason.STRATEGY_EXIT
    assert new_trade.exit_time == new_trade.entry_time + 15 * v2.M1

    baseline = decide(lambda: v1.create(old_values), bars, settings, clock)
    candidate = decide(lambda: v2.create(new_values), bars, settings, clock)
    result = diff(baseline, candidate, NY)
    changed = {(r.bar_start, r.baseline.decision.verdict if r.baseline else None,
                r.candidate.decision.verdict) for r in result.rows
               if r.kind is DiffKind.VERDICT_CHANGED and r.candidate is not None}
    signal = old_trade.signal_bar_start
    assert (signal, Verdict.ENTER, Verdict.WATCH) in changed
    assert (new_trade.exit_time - v2.M1, Verdict.HOLD, Verdict.EXIT) in changed
