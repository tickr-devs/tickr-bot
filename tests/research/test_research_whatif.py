from __future__ import annotations

from decimal import Decimal

import pytest
from research_helpers import CLOCK, flat, record, replay_settings, series

from aperture.replay.engine import ExitReason
from aperture.research.errors import ResearchError
from aperture.research.records import Admission
from aperture.research.whatif import WhatIfOutcome, summarize, what_if
from aperture.strategy.base import Role, Verdict

# Replay test settings: slippage = max(0.1 % of price, 0.01), entry_wait_bars = 2.
DECISION = ("10.00", "10.05", "9.95", "10.05")


def enter(clock="10:00", quantity="10", **kw):
    return record(clock, low="10.00", high="10.10", stop="9.80", quantity=quantity, **kw)


def run(records, bars, quantity=None):
    return what_if(records, {"ABC": bars}, replay_settings(), CLOCK, quantity)


def test_refused_enter_filled_then_stopped():
    bars = series("10:00", [DECISION, ("10.05", "10.08", "10.00", "10.06"),
                            ("10.00", "10.00", "9.70", "9.75")])
    (row,) = run([enter()], bars)
    assert row.outcome is WhatIfOutcome.FILLED
    entry = Decimal("10.10") + Decimal("0.0101")
    exit_ = Decimal("9.80") - Decimal("0.01")
    assert (row.trade.entry_price, row.trade.exit_price) == (entry, exit_)
    assert row.trade.exit_reason is ExitReason.STOP
    assert row.r_multiple == (exit_ - entry) / (entry - Decimal("9.80"))
    assert row.result_usd == (exit_ - entry) * 10
    assert row.risk_usd == (entry - Decimal("9.80")) * 10


def test_stop_first_when_the_fill_bar_also_touches_the_stop():
    bars = series("10:00", [DECISION, ("10.05", "10.50", "9.70", "10.40"), flat("10.50")])
    (row,) = run([enter()], bars)
    assert row.trade.exit_reason is ExitReason.STOP
    assert row.trade.exit_time == row.trade.entry_time
    assert row.r_multiple < 0


def test_flatten_closes_at_the_open_of_the_flatten_bar():
    rows = [DECISION, ("10.05", "10.08", "10.00", "10.06")] + [flat("10.50")] * 14
    bars = series("15:40", rows)  # 15:41 fills; 15:55 is the flatten bar
    (row,) = run([enter("15:40")], bars, quantity=Decimal(3))
    assert row.trade.exit_reason is ExitReason.FLATTEN
    assert row.trade.exit_price == Decimal("10.50") - Decimal("0.0105")
    assert row.quantity == 3
    assert row.result_usd == (row.trade.exit_price - row.trade.entry_price) * 3


def test_never_chased_when_price_runs_away():
    bars = series("10:00", [DECISION, flat("10.50"), flat("10.60"), flat("10.70")])
    (row,) = run([enter()], bars)
    assert (row.outcome, row.cause) == (WhatIfOutcome.MISSED, "price outside window")
    assert row.result_usd is None and row.r_multiple is None


def test_enter_completing_at_last_entry_is_rejected():
    bars = series("15:44", [DECISION, flat("10.05")])
    (row,) = run([enter("15:44")], bars)
    assert (row.outcome, row.cause) == (WhatIfOutcome.REJECTED, "after last entry time")


def test_only_enters_not_taken_are_simulated():
    bars = series("10:00", [DECISION, flat("10.05")])
    records = [
        enter(admission=Admission.TAKEN),
        enter(role=Role.OBSERVER, admission=Admission.NOT_EVALUATED),
        record("10:00", Verdict.WATCH),
        enter(),
    ]
    rows = run(records, bars)
    assert [r.record.admission for r in rows] == [Admission.NOT_EVALUATED, Admission.REFUSED]


def test_quantity_must_be_known():
    bars = series("10:00", [DECISION, flat("10.05")])
    (row,) = run([enter(quantity=None)], bars)
    assert row.outcome is WhatIfOutcome.NOT_SIMULATED
    assert row.cause == "no quantity: none recorded and none given"
    (row,) = run([enter(quantity="2.5")], bars)
    assert row.outcome is WhatIfOutcome.NOT_SIMULATED
    with pytest.raises(ResearchError, match="whole number"):
        run([enter()], bars, quantity=Decimal(0))


def test_missing_bars_are_causes():
    bars = series("10:00", [DECISION, flat("10.05")])
    rows = run([enter("11:00"), enter(symbol="XYZ")], bars)
    assert [r.cause for r in rows] == ["decision bar not in bars", "no bars for the symbol"]


def test_each_decision_is_simulated_alone_and_summarized():
    bars = series("10:00", [DECISION, DECISION, ("10.05", "10.08", "10.00", "10.06"),
                            ("10.00", "10.00", "9.70", "9.75")])
    rows = run([enter("10:00"), enter("10:01")], bars)
    assert [r.outcome for r in rows] == [WhatIfOutcome.FILLED, WhatIfOutcome.FILLED]
    summary = summarize(rows)
    assert (summary.trades, summary.losses, summary.wins) == (2, 2, 0)
    assert summary.total_usd == sum(r.result_usd for r in rows)
    assert summary.by_outcome["filled"] == 2
    assert summary.exits_by_reason == {"stop": 2}
