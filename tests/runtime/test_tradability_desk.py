"""The entry desk's tradability read (backlog T8) on the fake broker: read only when the check
is on and every earlier check admitted the entry; a refusal sends nothing; exits are never
checked. Every Tradability here is SYNTHETIC, not a recorded Webull answer."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest
from funded import FUNDS

from tickr.broker.fake import FakeBroker
from tickr.runtime.entries import EntryDesk
from tickr.strategy.base import Verdict
from tickr.types import Tradability

from .test_entries import CFG, GATE_OPEN, Desk, decision, quote

pytestmark = pytest.mark.skipif(not GATE_OPEN, reason="the configured trading gate is closed")

ON = replace(CFG, risk=replace(CFG.risk, tradability_check_enabled=True))


def desk(tmp_path, broker: FakeBroker | None = None, cfg=ON) -> Desk:
    made = Desk(tmp_path, broker or FakeBroker(funds=FUNDS))
    made.desk = EntryDesk(made.engine, cfg, calendar=made.desk.calendar,
                          last_broker_read_at=lambda: made.read_at, log=made.desk.log)
    return made


def answer(**changes) -> Tradability:
    return replace(Tradability("ABC", "OC", "ALL", None), **changes)


def test_off_reads_nothing(tmp_path):
    d = desk(tmp_path, cfg=CFG)
    assert d.decide(decision()).admission == "taken"
    assert d.broker.tradability_reads == 0


def test_on_and_tradable_opens_the_trade_after_one_read(tmp_path):
    d = desk(tmp_path)
    d.broker.tradable["ABC"] = answer()
    assert d.decide(decision()).admission == "taken"
    assert d.broker.tradability_reads == 1


@pytest.mark.parametrize("given, cause", [
    (answer(trade_policy="CLOSE_ONLY"), "INSTRUMENT_CLOSE_ONLY"),
    (answer(trade_policy="DENIED"), "INSTRUMENT_DENIED"),
    (answer(trade_policy="HALTED"), "INSTRUMENT_HALTED"),
    (None, "TRADABILITY_UNKNOWN"),  # the fake raises BrokerError: the read failed
])
def test_on_refuses_sends_nothing_and_skips_the_balance(tmp_path, given, cause):
    d = desk(tmp_path)
    if given is not None:
        d.broker.tradable["ABC"] = given
    record = d.decide(decision())
    assert record.admission == "refused" and record.refusal_cause.startswith(cause)
    assert d.broker.sent == [] and d.broker.balance_reads == 0


def test_no_budget_for_the_reads_refuses_before_reading(tmp_path):
    d = desk(tmp_path)
    d.broker.tradable["ABC"] = answer()
    d.broker.tradability_budget = False
    record = d.decide(decision())
    assert record.refusal_cause.startswith("TRADABILITY_BUDGET_UNAVAILABLE")
    assert d.broker.tradability_reads == 0 and d.broker.sent == []


def test_an_earlier_refusal_reads_no_tradability(tmp_path):
    d = desk(tmp_path)
    record = d.decide(decision(), q=quote(ask="10.50"))  # ask above the window
    assert record.refusal_cause.startswith("PRICE_OUTSIDE_WINDOW")
    assert d.broker.tradability_reads == 0


def test_exits_are_never_blocked(tmp_path):
    d = desk(tmp_path)
    d.broker.tradable["ABC"] = answer()
    d.decide(decision())
    (trade,) = d.engine.trades()
    d.broker.fill(d.engine.orders(trade.trade_id)[0].client_order_id, D(10), D("10.05"))
    for _ in range(3):
        d.engine.step()
    d.broker.tradable["ABC"] = answer(trade_policy="CLOSE_ONLY")
    reads = d.broker.tradability_reads
    record = d.decide(decision(Verdict.EXIT))
    assert record.admission == "not_applicable"
    assert d.engine.trade(trade.trade_id).exit_requested == "strategy-exit"
    assert d.broker.tradability_reads == reads
