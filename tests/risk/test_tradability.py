"""Tradability check (backlog T8): off by default; when on, CLOSE_ONLY, DENIED, HALTED and
listed profile statuses refuse with their own cause, anything unknown refuses as unknown.

Every Tradability here is SYNTHETIC (made up for the test), not a recorded Webull answer: how a
halt shows is still unproven (docs/webull-facts.md, `tickr probe-tradability`)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tickr.api.contract import RefusalCode, refusal_of
from tickr.risk.admission import (
    CHECKS,
    TradabilityRules,
    admit,
    refusal_before_broker_reads,
)
from tickr.risk.causes import AdmissionCause as C
from tickr.types import Tradability

from .conftest import CONFIG, inputs, limits

RULES = TradabilityRules(frozenset({"OC"}), frozenset({"XX"}))


def on():
    return replace(limits(), tradability=RULES)


def synthetic(**changes) -> Tradability:
    base = Tradability("ABCD", profile_status="OC", trade_policy="ALL", forbid_reason=None)
    return replace(base, **changes)


def test_shipped_config_has_the_check_off_and_admits_without_a_read():
    assert CONFIG.risk.tradability_check_enabled is False
    assert limits().tradability is None
    assert admit(inputs(), limits()).ok  # no tradability read at all


def test_on_with_a_tradable_answer_admits():
    assert admit(inputs(tradability=synthetic()), on()).ok


@pytest.mark.parametrize("answer, cause", [
    (synthetic(trade_policy="CLOSE_ONLY", forbid_reason="why"), C.INSTRUMENT_CLOSE_ONLY),
    (synthetic(trade_policy="DENIED"), C.INSTRUMENT_DENIED),
    (synthetic(trade_policy="HALTED"), C.INSTRUMENT_HALTED),
    (synthetic(profile_status="HALTED"), C.INSTRUMENT_HALTED),
    (synthetic(profile_status="XX"), C.INSTRUMENT_NOT_TRADABLE),
    (None, C.TRADABILITY_UNKNOWN),
    (synthetic(read_error="BrokerReadFailed: x"), C.TRADABILITY_UNKNOWN),
    (synthetic(symbol="OTHER"), C.TRADABILITY_UNKNOWN),
    (synthetic(trade_policy="SOMETHING_NEW"), C.TRADABILITY_UNKNOWN),
    (synthetic(trade_policy=None), C.TRADABILITY_UNKNOWN),
    (synthetic(forbid_reason="unexplained"), C.TRADABILITY_UNKNOWN),
    (synthetic(profile_status="NT"), C.TRADABILITY_UNKNOWN),
    (synthetic(profile_status=None), C.TRADABILITY_UNKNOWN),
])
def test_on_refuses_with_one_typed_cause(answer, cause):
    result = admit(inputs(tradability=answer), on())
    assert not result.ok and result.cause is cause


@pytest.mark.parametrize("answer, cause", [
    (synthetic(trade_policy="CLOSE_ONLY", forbid_reason="why"), C.INSTRUMENT_CLOSE_ONLY),
    (synthetic(trade_policy="DENIED"), C.INSTRUMENT_DENIED),
    (synthetic(trade_policy="HALTED"), C.INSTRUMENT_HALTED),
    (synthetic(profile_status="XX"), C.INSTRUMENT_NOT_TRADABLE),
    (None, C.TRADABILITY_UNKNOWN),
])
def test_the_api_reads_every_tradability_refusal(answer, cause):
    result = admit(inputs(tradability=answer), on())
    refusal = refusal_of(f"{result.cause}: {result.detail}")
    assert refusal.code is RefusalCode(cause) and refusal.message


def test_check_7_refuses_when_the_tradability_reads_do_not_fit():
    starved = inputs(tradability_budget_available=False, tradability=synthetic())
    result = admit(starved, on())
    assert result.cause is C.TRADABILITY_BUDGET_UNAVAILABLE
    assert refusal_before_broker_reads(starved, on()) == result  # before any read is made
    assert admit(starved, limits()).ok  # the check off: the reads are not needed


def test_close_only_detail_carries_the_forbid_reason():
    result = admit(inputs(tradability=synthetic(trade_policy="CLOSE_ONLY", forbid_reason="r")),
                   on())
    assert result.detail == "ABCD is close only (r)"


def test_it_runs_after_the_budget_and_before_the_buying_power():
    names = [c.__name__ for c in CHECKS]
    assert names[-3:] == ["_check_budget", "_check_tradability", "_check_buying_power"]
    # An earlier refusal wins, and the desk reads nothing for it.
    refused = inputs(entry_budget_available=False, tradability=None)
    assert admit(refused, on()).cause is C.ENTRY_BUDGET_UNAVAILABLE
    assert refusal_before_broker_reads(inputs(tradability=None), on()) is None
