"""Stop tightening by replace (backlog T1): where the broker stop of one open long position
should be now. Pure: no I/O, no clock, no broker, no journal.

Nothing in the trading loop calls this yet: stop replacement waits for the Webull replace
check (docs/webull-facts.md F5, `tickr probe-replace-stop`), and `[trailing_stop] enabled`
stays false until the loop integration is merged. The caller checks `enabled`.

The rule, in this order (`[trailing_stop]`, config/bot.toml):
1. Every input must be present, a finite Decimal and consistent, else REFUSED with one cause.
2. trail = best price since entry - `atr_multiple` x ATR, rounded down to its tick.
3. Break-even: once the best price since entry reached fill + `breakeven_after_r` x R
   (R = fill - initial stop), the stop is at least the fill price, rounded up to its tick.
4. The candidate is the higher of the two.
5. A stop only moves toward the price: a candidate at or below the current stop is KEEP
   (NOT_TIGHTER), never sent.
6. A sell stop at or above the bid would trigger at once: a candidate at or above the bid
   is KEEP (NOT_BELOW_BID), never clipped to a lower price.
7. A candidate less than `min_move_usd` above the current stop is KEEP (STEP_TOO_SMALL), so
   tiny moves do not spend broker requests (0 = no minimum step, the shipped value until the
   owner sets a measured one).
8. Otherwise MOVE to the candidate, with the part of the rule that set it as the cause.

Ticks are the order path's (`risk.ticks.tick`: 0.01 at or above 1 USD, 0.0001 below).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from tickr.config import Config, TrailingStopConfig
from tickr.risk.ticks import tick
from tickr.types import Side


class TrailAction(StrEnum):
    KEEP = "keep"
    MOVE = "move"
    REFUSED = "refused"


class TrailCause(StrEnum):
    """The one recorded cause of a decision."""

    # MOVE: which part of the rule set the new stop.
    TRAIL = "TRAIL"
    BREAKEVEN = "BREAKEVEN"
    # KEEP
    NOT_TIGHTER = "NOT_TIGHTER"
    NOT_BELOW_BID = "NOT_BELOW_BID"
    STEP_TOO_SMALL = "STEP_TOO_SMALL"
    # REFUSED
    INPUT_MISSING = "INPUT_MISSING"
    INPUT_NOT_DECIMAL = "INPUT_NOT_DECIMAL"
    INPUT_NOT_FINITE = "INPUT_NOT_FINITE"
    SIDE_NOT_LONG = "SIDE_NOT_LONG"
    PRICE_NOT_POSITIVE = "PRICE_NOT_POSITIVE"
    ATR_NOT_POSITIVE = "ATR_NOT_POSITIVE"
    INITIAL_STOP_NOT_BELOW_FILL = "INITIAL_STOP_NOT_BELOW_FILL"
    STOP_BELOW_INITIAL = "STOP_BELOW_INITIAL"
    PARAMS_INVALID = "PARAMS_INVALID"


@dataclass(frozen=True)
class TrailInputs:
    """One open position as the caller knows it. `side` is the entry side (BUY = long).
    `current_stop` is the stop price the broker shows; `best_price` the highest price since
    entry; `atr` the ATR of completed bars, USD per share; `bid` the current bid."""

    side: Side | None
    fill_price: Decimal | None
    initial_stop: Decimal | None
    current_stop: Decimal | None
    best_price: Decimal | None
    atr: Decimal | None
    bid: Decimal | None


@dataclass(frozen=True)
class TrailDecision:
    """`stop_price` is the new stop on MOVE, else None. `candidate`, `trail_price` and
    `breakeven_price` (None while the break-even step is not reached) explain a MOVE or KEEP;
    `field` names the input a refusal is about."""

    action: TrailAction
    cause: TrailCause
    stop_price: Decimal | None = None
    candidate: Decimal | None = None
    trail_price: Decimal | None = None
    breakeven_price: Decimal | None = None
    field: str | None = None


_PRICES = ("fill_price", "initial_stop", "current_stop", "best_price", "atr", "bid")


def _refused(cause: TrailCause, name: str | None = None) -> TrailDecision:
    return TrailDecision(TrailAction.REFUSED, cause, field=name)


def _floor_tick(price: Decimal) -> Decimal:
    step = tick(price)
    return price.quantize(step, rounding=ROUND_FLOOR)


def _ceil_tick(price: Decimal) -> Decimal:
    step = tick(price)
    return price.quantize(step, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class _Checked:
    fill: Decimal
    initial: Decimal
    current: Decimal
    best: Decimal
    atr: Decimal
    bid: Decimal


def _check(inputs: TrailInputs) -> _Checked | TrailDecision:
    """The inputs as checked Decimals, or the refusal of the first one that fails."""
    values: dict[str, Decimal] = {}
    for name in _PRICES:
        value = getattr(inputs, name)
        if value is None:
            return _refused(TrailCause.INPUT_MISSING, name)
        if not isinstance(value, Decimal):
            return _refused(TrailCause.INPUT_NOT_DECIMAL, name)
        if not value.is_finite():
            return _refused(TrailCause.INPUT_NOT_FINITE, name)
        values[name] = value
    if inputs.side is None:
        return _refused(TrailCause.INPUT_MISSING, "side")
    if inputs.side is not Side.BUY:
        return _refused(TrailCause.SIDE_NOT_LONG, "side")
    checked = _Checked(values["fill_price"], values["initial_stop"], values["current_stop"],
                       values["best_price"], values["atr"], values["bid"])
    for name, value in (("fill_price", checked.fill), ("initial_stop", checked.initial),
                        ("current_stop", checked.current), ("best_price", checked.best),
                        ("bid", checked.bid)):
        if value <= 0:
            return _refused(TrailCause.PRICE_NOT_POSITIVE, name)
    if checked.atr <= 0:
        return _refused(TrailCause.ATR_NOT_POSITIVE, "atr")
    if checked.initial >= checked.fill:
        return _refused(TrailCause.INITIAL_STOP_NOT_BELOW_FILL, "initial_stop")
    if checked.current < checked.initial:
        # The broker shows a looser stop than the trade opened with: a conflict for
        # reconciliation, not something to trail from.
        return _refused(TrailCause.STOP_BELOW_INITIAL, "current_stop")
    return checked


def _params_valid(params: TrailingStopConfig) -> bool:
    """`atr_multiple` and `breakeven_after_r` above 0, `min_move_usd` at least 0."""
    numbers = (params.atr_multiple, params.breakeven_after_r, params.min_move_usd)
    if not all(isinstance(v, Decimal) and v.is_finite() for v in numbers):
        return False
    return params.atr_multiple > 0 and params.breakeven_after_r > 0 and params.min_move_usd >= 0


def next_stop(inputs: TrailInputs, config: Config) -> TrailDecision:
    """Keep the stop, or move it to a tighter price, for one open long position, by the
    `[trailing_stop]` values of `config`."""
    return stop_by_rule(inputs, config.trailing_stop)


def stop_by_rule(inputs: TrailInputs, params: TrailingStopConfig) -> TrailDecision:
    """`next_stop` with the rule's values given directly."""
    if not _params_valid(params):
        return _refused(TrailCause.PARAMS_INVALID)
    checked = _check(inputs)
    if isinstance(checked, TrailDecision):
        return checked
    fill, current, best, bid = checked.fill, checked.current, checked.best, checked.bid

    trail = _floor_tick(best - params.atr_multiple * checked.atr)
    one_r = fill - checked.initial
    breakeven = (_ceil_tick(fill) if best >= fill + params.breakeven_after_r * one_r
                 else None)
    if breakeven is not None and breakeven > trail:
        candidate, cause = breakeven, TrailCause.BREAKEVEN
    else:
        candidate, cause = trail, TrailCause.TRAIL

    def keep(why: TrailCause) -> TrailDecision:
        return TrailDecision(TrailAction.KEEP, why, candidate=candidate, trail_price=trail,
                             breakeven_price=breakeven)

    if candidate <= current:
        return keep(TrailCause.NOT_TIGHTER)
    if candidate >= bid:
        return keep(TrailCause.NOT_BELOW_BID)
    if candidate - current < params.min_move_usd:
        return keep(TrailCause.STEP_TOO_SMALL)
    return TrailDecision(TrailAction.MOVE, cause, stop_price=candidate, candidate=candidate,
                         trail_price=trail, breakeven_price=breakeven)
