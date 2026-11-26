"""Stop tightening by replace (backlog T1): exact cases of `risk.trailing.next_stop`."""

from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal

import pytest
from baseline import load_baseline

from tickr.config import TrailingStopConfig
from tickr.risk.trailing import TrailAction, TrailCause, TrailInputs, next_stop, stop_by_rule
from tickr.types import Side

D = Decimal
KEEP, MOVE, REFUSED = TrailAction.KEEP, TrailAction.MOVE, TrailAction.REFUSED

# The backlog's provisional values; min move 5 cents. Enabled is the caller's switch.
PARAMS = TrailingStopConfig(enabled=False, atr_multiple=D("1.5"), breakeven_after_r=D("1.0"),
                            min_move_usd=D("0.05"))
CONFIG = replace(load_baseline(), trailing_stop=PARAMS)

# Fill 10.00, initial stop 9.50: R = 0.50, break-even after a best price of 10.50.
BASE = TrailInputs(side=Side.BUY, fill_price=D("10.00"), initial_stop=D("9.50"),
                   current_stop=D("9.50"), best_price=D("10.20"), atr=D("0.20"),
                   bid=D("10.15"))


def _at(**changes: object) -> TrailInputs:
    return replace(BASE, **changes)


def test_trail_moves_to_best_minus_atr_multiple():
    decision = next_stop(BASE, CONFIG)
    assert decision.action is MOVE
    assert decision.cause is TrailCause.TRAIL
    assert decision.stop_price == D("9.90")  # 10.20 - 1.5 x 0.20
    assert decision.breakeven_price is None


def test_trail_above_the_fill_wins_over_the_breakeven_step():
    decision = next_stop(_at(best_price=D("10.50"), bid=D("10.45")), CONFIG)
    assert (decision.action, decision.cause) == (MOVE, TrailCause.TRAIL)
    assert decision.stop_price == D("10.20")
    assert decision.breakeven_price == D("10.00")


def test_breakeven_step_lifts_the_stop_to_the_fill_once_one_r_is_reached():
    decision = next_stop(_at(best_price=D("10.50"), atr=D("0.50"), bid=D("10.40")), CONFIG)
    assert (decision.action, decision.cause) == (MOVE, TrailCause.BREAKEVEN)
    assert decision.stop_price == D("10.00")
    assert decision.trail_price == D("9.75")


def test_breakeven_step_waits_for_the_full_r():
    decision = next_stop(_at(best_price=D("10.49"), atr=D("0.50"), bid=D("10.40")), CONFIG)
    assert decision.breakeven_price is None
    assert (decision.action, decision.cause) == (MOVE, TrailCause.TRAIL)
    assert decision.stop_price == D("9.74")  # 10.49 - 0.75, not the fill


def test_breakeven_of_a_sub_cent_fill_rounds_up_to_stay_at_least_the_fill():
    decision = next_stop(_at(fill_price=D("10.004"), best_price=D("10.51"), atr=D("0.50"),
                             bid=D("10.40")), CONFIG)
    assert (decision.action, decision.cause) == (MOVE, TrailCause.BREAKEVEN)
    assert decision.stop_price == D("10.01")


def test_a_looser_candidate_is_kept_never_sent():
    """The platform rule: a stop only moves toward the price."""
    decision = next_stop(_at(current_stop=D("9.95")), CONFIG)  # trail 9.90
    assert (decision.action, decision.cause) == (KEEP, TrailCause.NOT_TIGHTER)
    assert decision.stop_price is None
    assert decision.candidate == D("9.90")


def test_a_candidate_equal_to_the_current_stop_is_kept():
    decision = next_stop(_at(current_stop=D("9.90")), CONFIG)
    assert (decision.action, decision.cause) == (KEEP, TrailCause.NOT_TIGHTER)


def test_a_candidate_at_or_above_the_bid_is_kept_not_clipped():
    # Break-even reached, then the price fell back under the fill.
    decision = next_stop(_at(best_price=D("10.50"), atr=D("0.50"), bid=D("9.98")), CONFIG)
    assert (decision.action, decision.cause) == (KEEP, TrailCause.NOT_BELOW_BID)
    assert decision.candidate == D("10.00")
    decision = next_stop(_at(bid=D("9.90")), CONFIG)
    assert (decision.action, decision.cause) == (KEEP, TrailCause.NOT_BELOW_BID)


def test_one_tick_below_the_bid_is_allowed():
    decision = next_stop(_at(bid=D("9.91")), CONFIG)
    assert (decision.action, decision.stop_price) == (MOVE, D("9.90"))


def test_a_move_below_min_move_is_kept():
    decision = next_stop(_at(current_stop=D("9.86")), CONFIG)  # 9.90 - 9.86 = 0.04
    assert (decision.action, decision.cause) == (KEEP, TrailCause.STEP_TOO_SMALL)
    decision = next_stop(_at(current_stop=D("9.85")), CONFIG)  # exactly 0.05
    assert (decision.action, decision.stop_price) == (MOVE, D("9.90"))


def test_min_move_zero_sends_a_one_cent_tightening():
    """0 = no minimum step, the shipped value."""
    no_minimum = replace(CONFIG, trailing_stop=replace(PARAMS, min_move_usd=D(0)))
    decision = next_stop(_at(current_stop=D("9.89")), no_minimum)
    assert (decision.action, decision.cause, decision.stop_price) == (
        MOVE, TrailCause.TRAIL, D("9.90"))
    decision = next_stop(_at(current_stop=D("9.90")), no_minimum)
    assert (decision.action, decision.cause) == (KEEP, TrailCause.NOT_TIGHTER)


def test_trail_rounds_down_to_whole_cents():
    decision = next_stop(_at(best_price=D("10.237")), CONFIG)  # 9.937
    assert decision.stop_price == D("9.93")


def test_trail_rounds_down_to_four_places_under_one_dollar():
    inputs = TrailInputs(side=Side.BUY, fill_price=D("0.80"), initial_stop=D("0.70"),
                         current_stop=D("0.70"), best_price=D("0.85"), atr=D("0.0333"),
                         bid=D("0.84"))
    decision = next_stop(inputs, CONFIG)  # 0.85 - 0.04995 = 0.80005
    assert (decision.action, decision.cause) == (MOVE, TrailCause.TRAIL)
    assert decision.stop_price == D("0.8000")
    assert decision.stop_price.as_tuple().exponent == -4


@pytest.mark.parametrize("name", ["fill_price", "initial_stop", "current_stop", "best_price",
                                  "atr", "bid", "side"])
def test_a_missing_input_is_refused(name: str):
    decision = next_stop(_at(**{name: None}), CONFIG)
    assert (decision.action, decision.cause, decision.field) == (
        REFUSED, TrailCause.INPUT_MISSING, name)
    assert decision.stop_price is None


@pytest.mark.parametrize("value", [D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity")])
@pytest.mark.parametrize("name", ["fill_price", "best_price", "atr", "bid"])
def test_a_non_finite_input_is_refused(name: str, value: Decimal):
    decision = next_stop(_at(**{name: value}), CONFIG)
    assert (decision.action, decision.cause, decision.field) == (
        REFUSED, TrailCause.INPUT_NOT_FINITE, name)


def test_a_float_input_is_refused():
    decision = next_stop(_at(bid=10.15), CONFIG)
    assert (decision.action, decision.cause, decision.field) == (
        REFUSED, TrailCause.INPUT_NOT_DECIMAL, "bid")


@pytest.mark.parametrize(("changes", "cause", "field"), [
    ({"side": Side.SELL}, TrailCause.SIDE_NOT_LONG, "side"),
    ({"bid": D(0)}, TrailCause.PRICE_NOT_POSITIVE, "bid"),
    ({"best_price": D("-1")}, TrailCause.PRICE_NOT_POSITIVE, "best_price"),
    ({"atr": D(0)}, TrailCause.ATR_NOT_POSITIVE, "atr"),
    ({"initial_stop": D("10.00")}, TrailCause.INITIAL_STOP_NOT_BELOW_FILL, "initial_stop"),
    ({"current_stop": D("9.49")}, TrailCause.STOP_BELOW_INITIAL, "current_stop"),
])
def test_inconsistent_inputs_are_refused(changes: dict, cause: TrailCause, field: str):
    decision = next_stop(_at(**changes), CONFIG)
    assert (decision.action, decision.cause, decision.field) == (REFUSED, cause, field)


@pytest.mark.parametrize("changes", [{"atr_multiple": D(0)}, {"breakeven_after_r": D("-1")},
                                     {"min_move_usd": D("-0.01")}, {"min_move_usd": D("NaN")},
                                     {"min_move_usd": 0.05}])
def test_invalid_params_are_refused(changes: dict):
    decision = stop_by_rule(BASE, replace(PARAMS, **changes))
    assert (decision.action, decision.cause) == (REFUSED, TrailCause.PARAMS_INVALID)


def test_the_stop_follows_a_price_path_and_never_moves_down():
    """bid path with a rise, a pullback and a new high; the caller keeps the best price and
    applies each MOVE as the new current stop."""
    path = ["10.10", "10.30", "10.55", "10.40", "10.20", "10.35", "10.70", "10.45", "10.90",
            "10.60", "10.95"]
    stop, best = D("9.50"), D("10.00")
    stops = [stop]
    for price in map(D, path):
        best = max(best, price)
        decision = next_stop(_at(current_stop=stop, best_price=best, bid=price), CONFIG)
        if decision.action is MOVE:
            assert decision.stop_price is not None and decision.stop_price > stop
            stop = decision.stop_price
        stops.append(stop)
    assert stops == [D(s) for s in ["9.50", "9.80", "10.00", "10.25", "10.25", "10.25", "10.25",
                                    "10.40", "10.40", "10.60", "10.60", "10.65"]]


@pytest.mark.parametrize("min_move", [D(0), D("0.05")])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_ratchet_never_moves_down_on_random_paths(seed: int, min_move: Decimal):
    params = replace(PARAMS, min_move_usd=min_move)
    rng = random.Random(seed)
    for _ in range(200):
        fill = D(rng.randint(50, 5000)) / 100
        initial = fill - D(rng.randint(1, 50)) / 100
        stop, best, price = initial, fill, fill
        for _ in range(60):
            price = max(D("0.01"), price + D(rng.randint(-20, 20)) / 100)
            best = max(best, price)
            atr = D(rng.randint(1, 60)) / 100
            decision = stop_by_rule(TrailInputs(Side.BUY, fill, initial, stop, best, atr, price),
                                    params)
            assert decision.action is not REFUSED, (seed, decision)
            if decision.action is MOVE:
                new = decision.stop_price
                assert new is not None, (seed, decision)
                assert new > stop and new - stop >= min_move, (seed, stop, decision)
                assert new < price, (seed, price, decision)
                assert new == new.quantize(D("0.01") if new >= 1 else D("0.0001"))
                stop = new
