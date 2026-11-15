"""Reason codes (api/reasons.py): every shape the bot writes reads into its code and values;
anything else is `unrecognized` with no values, never a guess."""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from tickr.api import contract, reasons
from tickr.engine.core import Fence
from tickr.risk.daily import check_daily
from tickr.risk.sizing import check_size

CORE = Path(reasons.__file__).resolve().parent.parent / "engine" / "core.py"


def test_every_fence_the_engine_sets_has_shapes():
    """A fence kind the engine sets but this module does not know would always be
    `unrecognized`; adding one means adding its shapes here."""
    source = CORE.read_text("utf-8")
    used = set(re.findall(r"_(?:set_fence|fence_row)\(\s*Fence\.([A-Z_]+)", source))
    assert used and {str(Fence[name]) for name in used} <= set(reasons._FENCE)


def test_every_fixed_fence_text_the_engine_writes_is_recognized():
    """Each fence the engine sets with a plain literal cause reads into a code (f-string
    causes are covered by the shape rows below)."""
    calls = re.findall(r'_(?:set_fence|fence_row)\(\s*Fence\.([A-Z_]+),[^,]+,\s*"([^"{}]*)"\)',
                       CORE.read_text("utf-8"))
    assert len(calls) >= 10
    unread = [(kind, text) for kind, text in calls
              if reasons.fence_reason(str(Fence[kind]), text).code == reasons.UNRECOGNIZED]
    assert unread == []


@pytest.mark.parametrize(("name", "text", "code", "params"), [
    ("startup-reconcile", "startup reconciliation pending", "startup_reconcile_pending", {}),
    ("broker-read-failed", "positions read failed: TimeoutError", "positions_read_failed",
     {"error": "TimeoutError"}),
    ("broker-read-failed:open-orders", "open orders read failed: BrokerError",
     "open_orders_read_failed", {"error": "BrokerError"}),
    ("unknown-order:c1", "crashed during send", "crashed_during_send", {}),
    ("status-unknown:c1", "unmapped status 'WEIRD'", "status_unmapped", {"status": "WEIRD"}),
    ("unprotected:t1", "verified stop covers 40 of 100", "stop_covers_part",
     {"verified": "40", "expected": "100"}),
    ("over-protected:t1", "stops cover 120, Detail holds 100", "stops_over_position",
     {"covered": "120", "held": "100"}),
    ("unprotected-timeout:t1", "no verified stop for 31 s; exiting", "unprotected_exiting",
     {"seconds": "31"}),
    ("unprotected-timeout:t1", "no verified stop for 31 s and the closing order was refused "
     "before: close the position in the Webull app", "unprotected_close_by_hand",
     {"seconds": "31"}),
    ("cancel-unknown:c1", "cancel unknown; waiting for Detail", "cancel_outcome_unknown",
     {"outcome": "unknown"}),
    ("cancel-unknown:c1", "crashed during cancel", "cancel_crashed", {}),
    ("cancel-not-sent:c1", "no cancel could be sent for 62 s (request budget)",
     "cancel_not_sent", {"seconds": "62"}),
    ("foreign-position:EFGH", "position not opened by this bot", "foreign_position", {}),
    ("quantity-mismatch:t1", "broker holds 50, Detail says 100", "quantity_mismatch",
     {"held": "50", "expected": "100"}),
    ("entry-stop", "operator command 0f1e", "operator_command", {}),
    ("replace-unresolved:c1", "stop replace to 11.85 not confirmed; operator must look",
     "replace_unresolved", {"stop_price": "11.85"}),
    ("stop-held:t1", "stop and close held: untracked SELL x1, x2 rests in ABCD after an "
     "unresolved replace; resolve it in the Webull app", "held_untracked_sell",
     {"orders": "x1, x2", "symbol": "ABCD"}),
    ("exit-not-filled:t1", "closing order c9 for 100 ABCD not filled after 120 s (filled 0, "
     "state accepted); nothing more is sent: look in the Webull app (halt? no buyers?)",
     "exit_not_filled", {"order": "c9", "quantity": "100", "symbol": "ABCD", "seconds": "120",
                         "filled": "0", "state": "accepted"}),
    ("market-closed:t1", "100 of 100 ABCD not sent: before the regular open 09:30 ET; the stop "
     "stays. Close the position in the Webull app, or clear this fence in the next regular "
     "session and the bot sends the close", "close_not_sent",
     {"quantity": "100", "expected": "100", "symbol": "ABCD", "open": "09:30"}),
    ("market-closed:t1", "100 of 100 ABCD not sent: at or after 15:59:30 ET, 30 s before the "
     "regular close 16:00 ET of 2026-11-16; the stop stays. Close the position in the Webull "
     "app, or clear this fence in the next regular session and the bot sends the close",
     "close_not_sent", {"quantity": "100", "expected": "100", "symbol": "ABCD",
                        "cutoff": "15:59:30", "cutoff_seconds": "30", "close": "16:00",
                        "day": "2026-11-16"}),
])
def test_fence_shapes(name, text, code, params):
    assert reasons.fence_reason(name, text) == reasons.Reason(code, params)


@pytest.mark.parametrize(("name", "text"), [
    ("foreign-position:EFGH", "position not opened by the bot"),  # not the exact words
    ("no-such-fence", "position not opened by this bot"),
    ("unprotected:t1", "verified stop covers forty of 100"),
])
def test_unknown_fence_text_is_unrecognized(name, text):
    assert reasons.fence_reason(name, text) == reasons.Reason(reasons.UNRECOGNIZED, {})


def test_gate_text_of_another_shape_is_unrecognized():
    assert reasons.gate_reason("trading enabled") == reasons.Reason(reasons.UNRECOGNIZED, {})


def _refusal(code: str, detail: str) -> dict[str, str]:
    refusal = contract.refusal_of(f"{code}: {detail}")
    assert refusal is not None and refusal.code == code
    return refusal.params


def test_size_and_daily_refusals_carry_their_values():
    size = check_size(Decimal(500), Decimal(12), Decimal(11), Decimal(100), Decimal(10000))
    assert _refusal(str(size.cause), size.detail) == {"planned_loss": "500", "limit": "100"}
    room = check_daily(Decimal(295), Decimal(0), Decimal(10), Decimal(300), Decimal(10))
    assert _refusal(str(room.cause), room.detail) == {
        "room": "5", "ceiling": "300", "realized": "295", "reserved": "0", "min_room": "10"}
    over = check_daily(Decimal(250), Decimal(0), Decimal(60), Decimal(300), Decimal(10))
    assert _refusal(str(over.cause), over.detail) == {
        "realized": "250", "reserved": "0", "new": "60", "total": "310", "ceiling": "300"}


@pytest.mark.parametrize(("code", "detail", "params"), [
    ("POSITION_LIMIT_REACHED", "1 open position(s), limit 1", {"open": "1", "limit": "1"}),
    ("BROKER_READ_STALE", "last broker read 40.5 s ago > 30 s", {"age": "40.5", "limit": "30"}),
    ("OUTSIDE_REGULAR_SESSION", "2026-11-16T09:29:00-05:00 is outside 09:30:00-16:00:00 New York",
     {"open": "09:30", "close": "16:00"}),
    ("AFTER_LAST_ENTRY", "last entry was 2026-11-16T15:45:00-05:00", {"time": "15:45"}),
    ("QUOTE_STALE", "quote 6.2 s old > 5 s", {"age": "6.2", "limit": "5"}),
    ("PRICE_OUTSIDE_WINDOW", "ask 12.40 outside 12.00..limit 12.10",
     {"ask": "12.40", "entry_low": "12.00", "limit": "12.10"}),
    ("GATE_CLOSED", "trading is disabled for environment 'paper'",
     {"gate": "trading_disabled", "environment": "paper"}),
    ("ENTRY_BUDGET_UNAVAILABLE", "no request budget for the entry order", {}),
    ("POSITION_LIMIT_REACHED", "too many", {}),  # code kept, values unknown: none given
])
def test_refusal_params(code, detail, params):
    assert _refusal(code, detail) == params


# ---------------------------------------------------------------- the API's own prose

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _codes(node: object) -> list[tuple[str, str]]:
    """(source field, code) of every `<field>_codes` entry in a decoded document."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("_codes") and isinstance(value, list):
                found += [(key, reason["code"]) for reason in value]
            found += _codes(value)
    elif isinstance(node, list):
        for item in node:
            found += _codes(item)
    return found


def test_every_cause_in_the_fixtures_has_a_code():
    import json
    seen = [(path.name, key, code) for path in sorted(FIXTURES.glob("*.json"))
            for key, code in _codes(json.loads(path.read_text("utf-8")))]
    assert seen and [s for s in seen if s[2] == reasons.UNRECOGNIZED] == []


@pytest.mark.parametrize(("text", "expected"), [
    (None, ()),
    ("no captured bars for 2026-11-16", (("no_captured_bars", {"day": "2026-11-16"}),)),
    ("the bars end at 2026-11-16T15:49:00.000Z", (("bars_end", {"time": "10:49"}),)),
    ("the bars end at 2026-11-16T15:49:00.000Z, before 2026-11-16T16:48:00.000Z",
     (("bars_end_before", {"time": "10:49", "until": "11:48"}),)),
    ("no order-intent row; never attempted: no order-attempted row",
     (("no_intent_row", {}), ("never_attempted", {}))),
    ("journal unreadable: KeyError: 'a'; 'b'",
     (("journal_unreadable", {"detail": "KeyError: 'a'; 'b'"}),)),
    ("unprotected-timeout:t1: no verified stop for 30 s; exiting; alert: t1",
     (("fence", {"fence": "unprotected-timeout:t1", "fence_code": "unprotected_exiting",
                 "seconds": "30"}), ("unprotected_alert", {"detail": "t1"}))),
    ("no reconcile-checked row from 2026-11-16T14:30:00.000Z to 2026-11-16T15:00:00.000Z "
     "(1800 s, more than [orders] reconcile_max_gap_seconds 900)",
     (("reconcile_gap", {"since": "09:30", "until": "10:00", "seconds": "1800",
                         "max_gap": "900"}),)),
    ("relative_volume: needs threshold > 0 and measured >= 0",
     (("needs_positive_threshold", {"rule": "relative_volume"}),)),
    ("skipped_overlap: an earlier simulated trade of ABCD was still open",
     (("skipped_overlap", {"symbol": "ABCD"}),)),
    ("something new; no order-intent row", (("unrecognized", {}), ("no_intent_row", {}))),
])
def test_prose_reasons(text, expected):
    assert tuple((r.code, r.params) for r in reasons.prose_reasons(text)) == expected


def test_codes_are_computed_when_encoding_and_never_read_from_a_document():
    trade = contract.WhatIfV1(None, None, None, "simulation refused: no bars")
    assert trade.cause_codes == ()  # building a record parses nothing (the daily-loss path)
    raw = contract.to_jsonable(trade)
    assert raw["cause_codes"] == [{"code": "simulation_refused", "params": {"detail": "no bars"}}]
    forged = {**raw, "cause_codes": [{"code": "forged", "params": {}}]}
    assert contract.to_jsonable(contract.from_jsonable(contract.WhatIfV1, forged)) == raw
    del forged["cause_codes"]  # a document from before the codes
    assert contract.to_jsonable(contract.from_jsonable(contract.WhatIfV1, forged)) == raw


def test_the_daily_loss_records_never_parse_a_cause(monkeypatch):
    def boom(_text):
        raise AssertionError("parsed on a build path")
    monkeypatch.setattr(contract, "prose_reasons", boom)
    contract.WhatIfV1(None, None, None, "simulation refused: x")  # builds without parsing
    # and an encoder that meets a failing table shows the cause unrecognized, never raises
    assert contract.to_jsonable(contract.WhatIfV1(None, None, None, "x"))["cause_codes"] == [
        {"code": reasons.UNRECOGNIZED, "params": {}}]


def test_planned_loss_texts_of_after_and_performance_read():
    from tickr.api.performance import NOT_POSITIVE_PLANNED_LOSS
    assert reasons.prose_reasons(NOT_POSITIVE_PLANNED_LOSS)[0].code == (
        "planned_loss_not_positive_r")
    assert reasons.prose_reasons("planned loss 0 is not positive: no R")[0].params == {
        "planned_loss": "0"}


@pytest.mark.parametrize(("text", "codes"), [
    ("the bars end at 2026-13-45T25:61:00.000Z",
     [("bars_end", {"time": "2026-13-45T25:61:00.000Z"})]),
    ("the session ended at 0001-01-01T00:00:00.000Z",  # before New York's offset exists
     [("session_ended", {"time": "0001-01-01T00:00:00.000Z"})]),
    ("x; the bars end at 2026-02-30T10:00:00.000Z",
     [("unrecognized", {}), ("bars_end", {"time": "2026-02-30T10:00:00.000Z"})]),
    ("no reconcile-checked row from 2026-02-30T14:30:00.000Z to 2026-11-16T15:00:00.000Z "
     "(1800 s, more than [orders] reconcile_max_gap_seconds 900)",
     [("reconcile_gap", {"since": "2026-02-30T14:30:00.000Z", "until": "10:00",
                         "seconds": "1800", "max_gap": "900"})]),
])
def test_an_impossible_wire_time_stays_as_written_and_never_raises(text, codes):
    assert [(r.code, r.params) for r in reasons.prose_reasons(text)] == codes


def test_a_status_with_an_odd_cause_writes_and_reads_back():
    from .samples import status as sample_status
    status = sample_status()
    trade = status.trades_today[0]
    odd = replace(trade, entry_cause="the session ended at 2026-13-45T25:61:61.000Z")
    text = contract.dumps(replace(status, trades_today=(odd,)))
    back = contract.loads(contract.StatusV1, text)
    assert back.trades_today[0].entry_cause == odd.entry_cause
    assert contract.dumps(back) == text


def test_many_unknown_parts_stay_linear():
    import time
    text = "; ".join(f"odd part {i}" for i in range(5000))
    started = time.monotonic()
    assert len(reasons.prose_reasons(text)) == 5000
    assert time.monotonic() - started < 2  # pair-wise only, never every run of parts
