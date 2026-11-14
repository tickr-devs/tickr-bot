"""Rule-by-rule oracle tests for price-action-v1 on generated bars."""

from __future__ import annotations

import copy
import random
from dataclasses import replace
from datetime import date

import pytest
from strategy_bars import (
    PRIOR_DAYS,
    SYMBOL,
    TODAY,
    bar,
    d,
    flat_session,
    prior_sessions,
    quote,
    rising_session,
    strategy_values,
)

from aperture.strategy import indicators as ind
from aperture.strategy import price_action_v1 as pa
from aperture.strategy.base import PositionView, SymbolContext, Verdict


def make(**overrides):
    """Strategy from the repo toml. Rule tests use a warm-up multiple of 1 so short generated
    sessions are enough; the warm-up itself is tested with the repo value."""
    overrides = {"indicators__warmup_period_multiple": 1, **overrides}
    values = copy.deepcopy(strategy_values())
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        values[section][key] = value
    return pa.create(values)


def ctx(bars, q=None, prior=None, position=None):
    return SymbolContext(SYMBOL, bars, prior_sessions() if prior is None else prior, q, position)


# Breakout baseline: 30 rising bars, close 20.1 .. 23.0, TR 0.14 each, ATR 0.14.
# Trigger = high of bar 28 = 22.92; invalidation = low of bar 24 = 22.38.
BREAKOUT_BARS = rising_session(TODAY, 30)
TRIGGER = d("22.92")
INVALIDATION = d("22.38")
ATR = d("0.14")
GOOD_QUOTE = quote("22.99", "23.00")


def rules(decision):
    return [c.rule for c in decision.checks]


def assert_refused(decision, verdict, rule):
    assert decision.verdict == verdict
    assert decision.decisive is not None and decision.decisive.rule == rule
    assert decision.checks[-1].rule == rule  # checks stop at the single cause


# ---------------------------------------------------------------- create()


def test_create_from_repo_toml():
    s = pa.create(strategy_values())
    assert (s.name, s.version) == ("price-action-v1", "1")
    assert s.settings.minimum_relative_volume == d("2.0")
    # 10 periods of EMA20 = 200 bars, ATR14 = 141 bars.
    assert s.settings.required_bars == 200


def test_warmup_history_may_come_from_prior_sessions():
    s = pa.create(strategy_values())
    today = rising_session(TODAY, 30)
    assert_refused(s.evaluate(ctx(today, GOOD_QUOTE)), Verdict.IGNORE, "history_bars")
    # 170 rising bars yesterday ending where today starts (20.0), same 0.14 true range.
    yesterday = rising_session(PRIOR_DAYS[-1], 170, start=3.0)
    decision = s.evaluate(ctx(yesterday + today, GOOD_QUOTE))
    assert decision.verdict == Verdict.ENTER
    assert decision.entry_high == TRIGGER + ATR


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda v: v["entry"].update(extra=1), "unknown keys ['extra']"),
        (lambda v: v["entry"].pop("slippage_reserve_atr"), "missing keys ['slippage_reserve_atr']"),
        (lambda v: v.pop("liquidity"), "missing ['liquidity']"),
        (lambda v: v.update(exits={}), "unknown ['exits']"),
        (lambda v: v["indicators"].update(atr_period=14.0), "atr_period must be a positive int"),
        (lambda v: v["indicators"].update(atr_period=True), "atr_period must be a positive int"),
        (lambda v: v["indicators"].update(atr_period=0), "atr_period must be a positive int"),
        (lambda v: v["entry"].update(max_round_trip_atr_ratio="0.25"), "must be a number"),
        (lambda v: v["entry"].update(slippage_reserve_atr=-0.1), "must not be negative"),
        (
            lambda v: v["indicators"].update(refuse_when_relative_volume_unavailable=1),
            "must be true or false",
        ),
        (lambda v: v["strategy"].update(name="other"), "name must be"),
        (lambda v: v["indicators"].update(fast_ema_period=20), "fast_ema_period must be below"),
        (lambda v: v.update(entry=[1]), "[entry] must be a table"),
    ],
)
def test_create_refuses_bad_values(mutate, message):
    values = copy.deepcopy(strategy_values())
    mutate(values)
    with pytest.raises(ValueError) as err:
        pa.create(values)
    assert message in str(err.value)


# ---------------------------------------------------------------- breakout baseline


def test_breakout_enter():
    s = make()
    decision = s.evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))
    assert decision.verdict == Verdict.ENTER
    assert decision.decisive is None
    assert (decision.strategy, decision.version, decision.symbol) == (
        "price-action-v1",
        "1",
        SYMBOL,
    )
    assert decision.setup == "breakout"
    assert decision.trigger == TRIGGER == decision.entry_low
    assert decision.invalidation == INVALIDATION == decision.stop
    assert decision.entry_high == TRIGGER + ATR
    assert rules(decision) == [
        "bars_consistent",
        "history_bars",
        "relative_volume",
        "dollar_volume_usd",
        "close_above_vwap",
        "fast_ema_above_slow_ema",
        "atr",
        "breakout_invalidation_below_trigger",
        "breakout_close_above_trigger",
        "quote_valid",
        "spread_ratio",
        "bid_above_invalidation",
        "ask_above_trigger",
        "ask_within_price_window",
        "stop_distance",
        "round_trip_atr_ratio",
        "round_trip_risk_ratio",
        "sizing_risk_per_share",
        "quantity",
    ]
    by_rule = {c.rule: c for c in decision.checks}
    assert by_rule["relative_volume"].measured == d(10_000) / d(3_000)
    assert by_rule["stop_distance"].measured == d("23.00") - INVALIDATION
    assert by_rule["stop_distance"].threshold == d("0.005") * d("23.00")
    assert by_rule["round_trip_atr_ratio"].measured == d("0.02") / ATR
    # worst fill to stop, plus the spread, plus 0.05 ATR on each leg
    assert by_rule["sizing_risk_per_share"].measured == (
        TRIGGER + ATR - INVALIDATION + d("0.01") + d("0.1") * ATR
    )


def quantity(decision):
    measured = {c.rule: c for c in decision.checks}["quantity"].measured
    if decision.verdict == Verdict.ENTER:
        assert decision.quantity == measured
    else:
        assert decision.quantity is None
    return measured


def test_quantity_by_planned_loss_and_notional_cap():
    # sizing risk 0.704: 50 / 0.704 = 71.02 -> 71; 2500 / 23.06 = 108.4 -> 108
    assert quantity(make().evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))) == d(71)
    # planned loss 100 -> 142 by loss; the notional cap binds at 108
    assert quantity(
        make(sizing__planned_loss_usd=100).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))
    ) == (d(108))
    # exactly 1 share passes; below 1 refuses
    one = make(sizing__planned_loss_usd=0.704).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))
    assert one.verdict == Verdict.ENTER and quantity(one) == d(1)
    decision = make(sizing__planned_loss_usd=0.7).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))
    assert_refused(decision, Verdict.IGNORE, "quantity")
    assert decision.quantity is None
    decision = make(sizing__max_position_notional_usd=23).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))
    assert_refused(decision, Verdict.IGNORE, "quantity")


def test_watch_without_quote():
    decision = make().evaluate(ctx(BREAKOUT_BARS))
    assert decision.verdict == Verdict.WATCH
    assert (decision.trigger, decision.invalidation) == (TRIGGER, INVALIDATION)
    assert decision.entry_high is None and decision.stop is None


def test_open_position_holds():
    position = PositionView(d(23), d(10), d(22))
    decision = make().evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE, position=position))
    assert decision.verdict == Verdict.HOLD
    assert rules(decision) == ["position_open"]


# ---------------------------------------------------------------- platform-data rules


def test_bars_out_of_order_refused():
    bars = list(BREAKOUT_BARS)
    bars[3], bars[4] = bars[4], bars[3]
    assert_refused(make().evaluate(ctx(bars, GOOD_QUOTE)), Verdict.IGNORE, "bars_consistent")


def test_foreign_symbol_bar_refused():
    bars = list(BREAKOUT_BARS)
    bars[0] = replace(bars[0], symbol="OTHER")
    assert_refused(make().evaluate(ctx(bars, GOOD_QUOTE)), Verdict.IGNORE, "bars_consistent")


def test_history_boundary():
    # Required = max(20, 9, 14, 6, 5) = 20.
    s = make()
    bars = rising_session(TODAY, 20, volume=20_000)
    assert_refused(s.evaluate(ctx(bars[:19])), Verdict.IGNORE, "history_bars")
    assert s.evaluate(ctx(bars)).verdict == Verdict.WATCH


def test_relative_volume_boundary():
    s = make()
    # current 10000/min; prior 5000/min -> exactly 2.0 passes; 5001 fails
    assert s.evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE, prior_sessions(volume=5_000))).verdict == (
        Verdict.ENTER
    )
    decision = s.evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE, prior_sessions(volume=5_001)))
    assert_refused(decision, Verdict.IGNORE, "relative_volume")


def test_relative_volume_unavailable_refused_or_waived_by_config():
    short = prior_sessions(count=4)
    decision = make().evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE, short))
    assert_refused(decision, Verdict.IGNORE, "relative_volume_available")
    waived = make(indicators__refuse_when_relative_volume_unavailable=False)
    decision = waived.evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE, short))
    assert decision.verdict == Verdict.ENTER
    assert "relative_volume_available" in rules(decision)


def test_dollar_volume_boundary():
    measured = ind.dollar_volume(BREAKOUT_BARS)
    assert (
        make(liquidity__min_dollar_volume_usd=int(measured))
        .evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))
        .verdict
        == Verdict.ENTER
    )
    decision = make(liquidity__min_dollar_volume_usd=int(measured) + 1).evaluate(
        ctx(BREAKOUT_BARS, GOOD_QUOTE)
    )
    assert_refused(decision, Verdict.IGNORE, "dollar_volume_usd")


def test_close_below_vwap_refused():
    bars = BREAKOUT_BARS[:-1] + [bar(TODAY, 29, 22.9, 22.9, 20.0, 20.0, 10_000)]
    assert_refused(make().evaluate(ctx(bars, GOOD_QUOTE)), Verdict.IGNORE, "close_above_vwap")


def test_fast_ema_below_slow_refused():
    # Yesterday's 20 bars at 30 are history for the EMAs but not part of today's VWAP.
    yesterday = flat_session(PRIOR_DAYS[-1], 20, price=30, volume=30_000)
    bars = yesterday + rising_session(TODAY, 10, volume=30_000)
    decision = make().evaluate(ctx(bars, GOOD_QUOTE))
    assert_refused(decision, Verdict.IGNORE, "fast_ema_above_slow_ema")


# ---------------------------------------------------------------- setups


def test_breakout_arms_before_the_break():
    # Last bar closes 0.07 below the trigger (0.5 ATR): WATCH, not yet broken.
    bars = BREAKOUT_BARS[:-1] + [bar(TODAY, 29, 22.9, 22.9, 22.84, 22.85, 10_000)]
    decision = make().evaluate(ctx(bars))
    assert decision.verdict == Verdict.WATCH
    assert decision.setup == "breakout"
    assert (decision.trigger, decision.invalidation) == (TRIGGER, INVALIDATION)
    assert rules(decision)[-1] == "breakout_pre_breakout_distance_atr"
    # The same bars refuse when arming before the break is not allowed.
    strict = make(continuation__maximum_pre_breakout_distance_atr=0)
    decision = strict.evaluate(ctx(bars))
    assert decision.verdict == Verdict.IGNORE
    assert decision.checks[-1].rule == "breakout_pre_breakout_distance_atr"


def test_no_setup_records_both_setups():
    # Last bar closes below the trigger; arming distance 0; the continuation base is too wide.
    last = bar(TODAY, 29, 21.45, 21.47, 21.43, 21.44, 10_000)
    bars = rising_session(TODAY, 29, step=0.05) + [last]
    decision = make(continuation__maximum_pre_breakout_distance_atr=0).evaluate(
        ctx(bars, GOOD_QUOTE)
    )
    assert decision.verdict == Verdict.IGNORE
    assert decision.decisive.rule == "base_range_atr"
    assert rules(decision)[-4:] == [
        "impulse_atr",
        "base_range_atr",
        "breakout_invalidation_below_trigger",
        "breakout_pre_breakout_distance_atr",
    ]
    assert decision.trigger is None


def continuation_bars():
    """25 trend bars (close 22.5), a 0.6 impulse over 2 bars, a tight 3-bar base."""
    return rising_session(TODAY, 25) + [
        bar(TODAY, 25, 22.5, 22.82, 22.48, 22.8, 10_000),
        bar(TODAY, 26, 22.8, 23.12, 22.78, 23.1, 10_000),
        bar(TODAY, 27, 23.1, 23.15, 23.0, 23.05, 10_000),
        bar(TODAY, 28, 23.05, 23.12, 23.0, 23.08, 10_000),
        bar(TODAY, 29, 23.08, 23.14, 23.02, 23.1, 10_000),
    ]


def test_continuation_enter():
    bars = continuation_bars()
    atr = ind.atr_series(bars, 14)[-1]
    decision = make().evaluate(ctx(bars, quote("23.16", "23.17")))
    assert decision.verdict == Verdict.ENTER
    assert decision.setup == "continuation"
    assert (decision.trigger, decision.stop) == (d("23.15"), d("23.0"))
    assert decision.entry_high == d("23.15") + atr
    assert rules(decision)[7:11] == [
        "impulse_atr",
        "base_range_atr",
        "continuation_invalidation_below_trigger",
        "pre_breakout_distance_atr",
    ]
    assert "breakout_close_above_trigger" not in rules(decision)
    by_rule = {c.rule: c for c in decision.checks}
    assert by_rule["impulse_atr"].measured == d("0.6") / atr
    assert by_rule["base_range_atr"].measured == d("0.15") / atr
    assert by_rule["pre_breakout_distance_atr"].measured == d("0.05") / atr


@pytest.mark.parametrize(
    "rule, key, factor",
    [
        ("impulse_atr", "continuation__minimum_impulse_atr", 2),
        ("base_range_atr", "continuation__maximum_base_range_atr", 0.5),
        ("pre_breakout_distance_atr", "continuation__maximum_pre_breakout_distance_atr", 0.5),
    ],
)
def test_continuation_rule_failures(rule, key, factor):
    bars = continuation_bars()
    measured = {c.rule: c for c in make().evaluate(ctx(bars)).checks}[rule].measured
    decision = make(**{key: float(measured) * factor}).evaluate(ctx(bars))
    if rule == "pre_breakout_distance_atr":
        # The same key also limits breakout arming (0.05 below its trigger 23.15): no setup,
        # and the continuation failure is the recorded cause.
        assert decision.verdict == Verdict.IGNORE
        assert decision.decisive.rule == rule
    else:
        # Continuation fails; the breakout setup arms before its break instead, and only the
        # chosen setup's checks are recorded.
        assert decision.verdict == Verdict.WATCH
        assert decision.setup == "breakout"


# ---------------------------------------------------------------- quote rules


@pytest.mark.parametrize(
    "q, verdict, rule",
    [
        (quote("22.99", "23.00", symbol="OTHER"), Verdict.IGNORE, "quote_valid"),
        (quote("23.01", "23.00"), Verdict.IGNORE, "quote_valid"),
        (quote("0", "23.00"), Verdict.IGNORE, "quote_valid"),
        (quote("22.50", "23.00"), Verdict.IGNORE, "spread_ratio"),
        (quote("22.38", "22.39"), Verdict.IGNORE, "bid_above_invalidation"),
        (quote("22.91", "22.92"), Verdict.WATCH, "ask_above_trigger"),
        (quote("23.06", "23.07"), Verdict.IGNORE, "ask_within_price_window"),
    ],
)
def test_quote_rules(q, verdict, rule):
    decision = make().evaluate(ctx(BREAKOUT_BARS, q))
    assert_refused(decision, verdict, rule)
    assert (decision.trigger, decision.invalidation) == (TRIGGER, INVALIDATION)
    assert decision.stop is None and decision.entry_high is None


def test_price_window_top_is_inclusive():
    assert make().evaluate(ctx(BREAKOUT_BARS, quote("23.05", "23.06"))).verdict == Verdict.ENTER


def test_spread_ratio_boundary():
    # spread 0.3 / mid 20 = 0.015 exactly; use a wide price window so only spread matters.
    s = make(
        entry__entry_extension_max_atr=100,
        entry__max_round_trip_atr_ratio=100,
        entry__max_round_trip_risk_ratio=100,
        entry__minimum_stop_spreads=0,
    )
    bars = BREAKOUT_BARS
    ok = s.evaluate(ctx(bars, quote("22.85", "23.15")))
    assert {c.rule: c for c in ok.checks}["spread_ratio"].measured == d("0.3") / d("23.0")
    assert ok.verdict == Verdict.ENTER
    wide = s.evaluate(ctx(bars, quote("22.80", "23.15")))
    assert_refused(wide, Verdict.IGNORE, "spread_ratio")


def test_minimum_stop_distance_percent_and_spreads():
    # Distance 0.62 at ask 23.00. 0.62 / 23 = 2.6957 percent.
    assert_refused(
        make(entry__minimum_stop_percent=2.7).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE)),
        Verdict.IGNORE,
        "stop_distance",
    )
    assert (
        make(entry__minimum_stop_percent=2.69).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE)).verdict
        == Verdict.ENTER
    )
    # Spread 0.01: 62 spreads pass exactly, 63 fail.
    assert (
        make(entry__minimum_stop_spreads=62).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE)).verdict
        == Verdict.ENTER
    )
    assert_refused(
        make(entry__minimum_stop_spreads=63).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE)),
        Verdict.IGNORE,
        "stop_distance",
    )


def test_cost_rule_against_atr():
    # round trip 0.02 / ATR 0.14 = 0.142857...
    assert_refused(
        make(entry__max_round_trip_atr_ratio=0.14).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE)),
        Verdict.IGNORE,
        "round_trip_atr_ratio",
    )
    assert (
        make(entry__max_round_trip_atr_ratio=0.143).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE)).verdict
        == Verdict.ENTER
    )


def test_cost_rule_against_risk():
    # round trip 0.02 / (top of window 23.06 - stop 22.38 = 0.68) = 0.029411...
    assert_refused(
        make(entry__max_round_trip_risk_ratio=0.029).evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE)),
        Verdict.IGNORE,
        "round_trip_risk_ratio",
    )
    assert (
        make(entry__max_round_trip_risk_ratio=0.033)
        .evaluate(ctx(BREAKOUT_BARS, GOOD_QUOTE))
        .verdict
        == Verdict.ENTER
    )


# ---------------------------------------------------------------- no look-ahead


def _corrupt_after(bars, t, rng):
    tail = [
        replace(
            b,
            open=d(rng.uniform(1, 99)),
            high=d(120),
            low=d("0.5"),
            close=d(rng.uniform(1, 99)),
            volume=d(rng.randint(0, 10**8)),
        )
        for b in bars[t + 1 :]
    ]
    return list(bars[: t + 1]) + tail


@pytest.mark.parametrize("scenario", ["breakout", "continuation", "random"])
def test_decision_at_t_ignores_bars_after_t(scenario):
    """The platform hands the strategy bars up to t. Corrupting bars after t in the source
    series never changes the decision at t, and evaluation never mutates its inputs."""
    rng = random.Random(7)
    if scenario == "breakout":
        series = rising_session(TODAY, 60)
    elif scenario == "continuation":
        series = continuation_bars() + rising_session(date(2026, 11, 13), 60)[30:]
    else:
        series = []
        price = 20.0
        for i in range(60):
            o = round(price, 2)
            c = round(o + rng.uniform(-0.1, 0.2), 2)
            series.append(bar(TODAY, i, o, max(o, c) + 0.02, min(o, c) - 0.02, c, 20_000))
            price = c
    s = make()
    prior = prior_sessions()
    for t in range(len(series)):
        clean = s.evaluate(ctx(series[: t + 1], GOOD_QUOTE, prior))
        corrupted = _corrupt_after(series, t, rng)
        snapshot = list(corrupted)
        again = s.evaluate(ctx(corrupted[: t + 1], GOOD_QUOTE, prior))
        assert again == clean
        assert corrupted == snapshot
