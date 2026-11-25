"""Backlog T2/T3 (owner 2026-11-15): the planned loss and the notional cap live only in
config/bot.toml [risk] and reach every strategy with its input (`SymbolContext.sizing`).

Before the change each strategy file carried its own copy (`[sizing] planned_loss_usd = 50`,
`max_position_notional_usd = 1000`). The quantities pinned here were measured on that code
(origin/main before T2/T3) with the same bars, quotes and budgets: with today's [risk] values
the shipped strategies size every trade exactly as before.
"""

from __future__ import annotations

import copy
import tomllib

import pytest
from strategy_bars import ROOT, SIZING, SYMBOL, TODAY, d, prior_sessions, quote, rising_session

from tickr.strategy import price_action_v1, price_action_v3
from tickr.strategy.base import SymbolContext, Verdict

# The values every shipped strategy file carried before T2/T3.
BEFORE_PLANNED_LOSS_USD = d(50)
BEFORE_NOTIONAL_CAP_USD = d(1000)


def test_the_platform_values_are_the_ones_the_strategies_carried():
    assert (SIZING.planned_loss_usd, SIZING.notional_cap_usd) == (
        BEFORE_PLANNED_LOSS_USD, BEFORE_NOTIONAL_CAP_USD)


def _strategy(folder, module):
    values = copy.deepcopy(tomllib.loads(
        (ROOT / "strategies" / folder / "strategy.toml").read_text()))
    values["indicators"]["warmup_period_multiple"] = 1  # short generated sessions suffice
    return module.create(values)


@pytest.mark.parametrize(("folder", "module"), [
    ("price-action-v1", price_action_v1),
    ("price-action-v3", price_action_v3),  # the active strategy
])
@pytest.mark.parametrize(("budget", "shares"), [
    (None, 43),    # the notional cap binds: floor(1000 / 23.06)
    (d(20), 28),   # the daily room binds: floor(20 / 0.704)
])
def test_a_trade_is_sized_as_before(folder, module, budget, shares):
    strategy = _strategy(folder, module)
    context = SymbolContext(SYMBOL, rising_session(TODAY, 30), prior_sessions(),
                            quote("22.99", "23.00"), None, budget, sizing=SIZING)
    decision = strategy.evaluate(context)
    assert decision.verdict is Verdict.ENTER and decision.quantity == d(shares)
    checks = {c.rule: c.measured for c in decision.checks}
    assert checks["planned_loss_limit_usd"] == BEFORE_PLANNED_LOSS_USD
    assert checks["notional_cap_usd"] == BEFORE_NOTIONAL_CAP_USD
