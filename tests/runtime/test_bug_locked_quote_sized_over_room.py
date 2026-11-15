"""BUG reproduction (fails until fixed): on a locked quote (bid == ask) the strategy's size
does not fit the platform's own check, so the entry is refused for money.

price-action-v1 sizes on `entry_high - stop + spread + 2 x slippage reserve` with the RAW
stop; the platform checks `quantity x (limit - stop)` on the tick-ROUNDED prices, and
`round_to_tick` rounds the stop DOWN (up to one tick more risk per share). The spread term
normally covers that tick; with bid == ask (allowed: the strategy accepts 0 < bid <= ask) and
a small ATR it does not. Result: a full-size entry is refused with PLANNED_LOSS_OVER_LIMIT,
and near the daily ceiling the sized-down entry is refused with DAILY_CEILING although the
room is above `min_daily_room_usd`, against the owner decision of 2026-11-14 ("near the
ceiling a trade is sized down to the room left, never refused only because the full planned
loss no longer fits") and against risk/sizing.py ("a strategy may size on a larger risk ...
and so be stricter, never looser").

Where: src/aperture/strategy/price_action_v1.py (`sizing_risk`, `stop`, `entry_high` in
`_with_quote`) versus src/aperture/runtime/entries.py `EntryDesk._inputs` (`round_to_tick`
of entry_high and stop). Safe direction (no money at risk), but trades are lost.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from aperture.risk.admission import admit
from aperture.risk.ticks import round_to_tick
from aperture.strategy.base import SymbolContext, Verdict
from aperture.types import Quote

from .test_daily_room_properties import (
    CEILING,
    LIMITS,
    RISK,
    STRATEGY,
    SYMBOL,
    _admissible,
    _bars,
    ny,
)

NOW = ny(date(2026, 11, 16), 11, 0)
# The strategy tests' breakout baseline at a fifth of its prices: trigger 4.584, top of the
# window 4.612, stop 4.476 (sent as 4.47), ATR 0.028.
TODAY, PRIOR = _bars(D("0.2"))
LOCKED = Quote(SYMBOL, D("4.60"), D("4.60"), D(100), D(100), NOW, NOW)


@pytest.mark.xfail(strict=True, reason=(
    "bug: locked quote, strategy sizes on the raw stop; the tick-rounded stop exceeds the limit"))
@pytest.mark.parametrize("room", [None, D(20)])
def test_a_locked_quote_entry_is_sized_to_what_admission_accepts(room):
    decision = STRATEGY.evaluate(SymbolContext(SYMBOL, TODAY, PRIOR, LOCKED, None, room))
    assert decision.verdict is Verdict.ENTER
    limit, stop = round_to_tick(decision.entry_high), round_to_tick(decision.stop)
    assert (limit, stop) == (D("4.61"), D("4.47"))
    left = RISK.planned_loss_usd if room is None else room
    result = admit(replace(_admissible(NOW, decision, LOCKED), limit_price=limit,
                           stop_price=stop, realized_loss_today_usd=CEILING - (room or CEILING)),
                   LIMITS)
    # Today: 360 shares x 0.14 = 50.40 > 50 (PLANNED_LOSS_OVER_LIMIT), and with 20 USD of
    # room 144 shares x 0.14 = 20.16 > 20 (DAILY_CEILING).
    assert result.ok, (decision.quantity, result.cause, result.detail)
    assert result.planned_loss_usd <= left
