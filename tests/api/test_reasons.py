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
    ("order-conflict:c1", "broker shows 20 filled of 2 ABC BUY LIMIT, sent 2 ABC BUY LIMIT: "
     "not recorded; shares may be held without a stop: check it in the Webull app",
     "broker_fill_contradicts",
     {"filled": "20", "quantity": "2", "sent": "2", "symbol": "ABC"}),
    ("order-conflict:c1", "changed at the broker: 1 at stop 6.00, sent 2 at stop 9.80; not "
     "counted until it agrees", "stop_changed_at_broker",
     {"quantity": "1", "price": "6.00", "sent": "2", "sent_price": "9.80"}),
    ("order-conflict:c1", "changed at the broker: 3 at limit 10.00, sent 2 at limit 10.00; not "
     "counted until it agrees", "limit_changed_at_broker",
     {"quantity": "3", "price": "10.00", "sent": "2", "sent_price": "10.00"}),
    ("order-conflict:c1", "changed at the broker: 1 shares, sent 2; not counted until it agrees",
     "quantity_changed_at_broker", {"quantity": "1", "sent": "2"}),
    ("order-conflict:c1", "last seen working but not in the broker's open orders: proves "
     "nothing until its Detail is read", "order_not_listed", {}),
    ("unprotected:t1", "verified stop covers 40 of 100", "stop_covers_part",
     {"verified": "40", "expected": "100"}),
    ("over-protected:t1", "stops cover 120, Detail holds 100", "stops_over_position",
     {"covered": "120", "held": "100"}),
    ("unprotected-timeout:t1", "no verified stop for 31 s; exiting", "unprotected_exiting",
     {"seconds": "31"}),
    ("unprotected-timeout:t1", "no verified stop for 31 s and a stop's Detail cannot be read: "
     "that stop is kept, nothing is sold; close the position in the Webull app",
     "unprotected_stop_unreadable", {"seconds": "31"}),
    ("unprotected-timeout:t1", "no verified stop for 31 s and the closing order was refused "
     "before: close the position in the Webull app", "unprotected_close_by_hand",
     {"seconds": "31"}),
    ("cancel-unknown:c1", "cancel unknown; waiting for Detail", "cancel_outcome_unknown",
     {"outcome": "unknown"}),
    ("cancel-unknown:c1", "crashed during cancel", "cancel_crashed", {}),
    ("detail-unreadable:c1", "Detail unreadable for 31 s: order state unknown; check it in "
     "the Webull app", "detail_unreadable", {"seconds": "31"}),
    ("detail-unreadable:c1", "stop cancel sent and its Detail unreadable for 30 s: the stop "
     "may be gone and nothing is sold; check the position in the Webull app",
     "stop_cancel_unconfirmed", {"seconds": "30"}),
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
    ("disk-low", "less than 2048 MB free on the data disk: no new entry until there is room",
     "disk_low", {"mb": "2048"}),
    ("disk-low", "free space on the data disk unreadable (PermissionError): no new entry until "
     "it reads", "disk_unknown", {"error": "PermissionError"}),
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
    # As risk/admission.py writes it since extended hours: the window last, the age to the µs.
    ("QUOTE_STALE", "quote 183.415621 s old > 5 s (regular)", {"age": "183.4", "limit": "5"}),
    ("BROKER_READ_STALE", "last broker read 31.06 s ago > 30 s", {"age": "31.1", "limit": "30"}),
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
    ("rejected: before regular open", (("replay_before_open", {}),)),
    ("rejected: after last entry time", (("replay_after_last_entry", {}),)),
    ("missed: last entry time", (("replay_after_last_entry", {}),)),
    ("rejected: half day: trading disabled", (("replay_half_day", {}),)),
    ("missed: price outside window", (("replay_price_outside", {}),)),
    ("missed: no further bars", (("replay_no_further_bars", {}),)),
    ("ignored: position open", (("replay_position_open", {}),)),
    ("ignored: entry pending", (("replay_entry_pending", {}),)),
    ("rejected: scale-out (partial exits) is not modelled",
     (("simulation_outcome", {"outcome": "rejected",
                              "detail": "scale-out (partial exits) is not modelled"}),)),
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


# ---------------------------------------------------------------- refusals the apps show

API = Path(reasons.__file__).resolve().parent
REFUSALS = {"LabReplayRefused", "TradeWhatIfRefused", "StartRefused"}
# One example of every refusal the Lab's replay, another version on a trade and the Start button
# can answer, as the bot writes it (each `raise` below must have one: see the next test).
REFUSAL_SAMPLES = (
    "a replay is already running",
    "the API was started without its config file path",
    "no fetched backtest data: pool.csv is missing in [api] lab_replay_data_dir",
    "2026-11-13 is not a day of the fetched data",
    "compare names the same version twice",
    "[observers] strategy_modules entry is not name=module: 'x'",
    "[observers] strategy_modules lists 'x' twice",
    "no strategy folder 'x' under strategies/",
    "my-idea: strategy.toml has no [strategy] name",
    "my-idea: unreadable",
    "no module for strategy 'x' in [observers] strategy_modules",
    "[observers] strategy_modules is malformed",
    "calendar: 2026-11-16 is not in the calendar",
    "calendar unreadable: [Errno 2] No such file",
    "refused during the regular session until 2026-11-16T21:00:00.000Z "
    "([api] lab_replay_outside_session_only)",
    "refused during the regular session until 2026-11-16T21:00:00.000Z "
    "([api] trade_whatif_outside_session_only)",
    "no start command is configured on this machine ([api] start_bot_command is empty): start "
    "the bot where it runs",
    "the bot is running: nothing to start",
    "the bot's lock file could not be read (PermissionError): nothing was started",
    "a bot process (pid 42) still holds the lock: wait for it to end, then start again",
    "the operator stop could not be cleared (PermissionError): nothing was started",
    "the start command did not return within 10 s",
    "the start command failed (FileNotFoundError)",
    "the start command exited 1 (the operator stop was cleared, so the next scheduled start runs)",
)


def _raised_texts() -> list[tuple[str, str]]:
    """(file, leading literal text) of each refusal raised in the API with a written cause."""
    import ast
    found = []
    for path in sorted(API.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            func = node.exc.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in REFUSALS or not node.exc.args:
                continue
            arg = node.exc.args[-1]
            parts = arg.values if isinstance(arg, ast.JoinedStr) else [arg]
            if parts and isinstance(parts[0], ast.Constant) and isinstance(parts[0].value, str):
                found.append((path.name, parts[0].value))
    return found


def test_every_written_refusal_has_a_sample():
    raised = _raised_texts()
    assert raised
    missing = [(f, text) for f, text in raised
               if not any(sample.startswith(text) or f": {text}" in sample or text in sample
                          for sample in REFUSAL_SAMPLES)]
    assert missing == []


@pytest.mark.parametrize("text", REFUSAL_SAMPLES)
def test_every_refusal_the_apps_show_has_a_code(text):
    """So the apps say it in the person's language (apps/scripts/kit_reason_strings.py gives
    every code an English and a Spanish sentence: tests/test_api_codes.py)."""
    assert [r.code for r in reasons.prose_reasons(text)] != [reasons.UNRECOGNIZED]


# ---------------------------------------------------------------- causes the apps show as text

SRC = API.parent
# How to pass the chart's probe C1 (probes.chart_client_gate).
CHART_CHECK_HOW = ("run `python -m tickr.probes chart-client SYMBOL` next to the running bot, then "
                   "start the API again")
# One example of every cause the API can put in these fields, as the bot writes it.
FIELD_SAMPLES = {
    # GET /chart's error-v1 cause: refused, off, starting or failed (api/chart.py, its query in
    # api/server.py, its source in api/__main__.py and probes.chart_client_gate)
    "chart error cause": (
        "chart market data is off ([chart] webull_enabled = false)",
        "chart market data is off: chart probe C1 is not recorded for sandbox; " + CHART_CHECK_HOW,
        "chart market data is off: chart probe C1 last ended 'failed' for sandbox "
        "(2026-11-16T15:00:00+00:00); " + CHART_CHECK_HOW,
        "chart market data is off: chart probe C1: 20261116-chart-client-1 cannot be read "
        "(PermissionError); remove that run's folder, then " + CHART_CHECK_HOW,
        "chart market data is off: chart probe C1: 20261116-chart-client-1/summary.json cannot "
        "be read (KeyError); remove that run's folder, then " + CHART_CHECK_HOW,
        "chart market data is off: the chart probe C1 gate failed (OSError)",
        "chart market data is starting: its Webull client is still being built",
        "chart market data unavailable: client build failed (RuntimeError)",
        "chart market data unavailable: ConfigError: WEBULL_APP_KEY is not set",
        "chart read budget: market-data-bars: HTTP 429",
        "market data is delayed: data delayed by 15 min for ABCD",
        "market data read failed: SymbolsRefused: HTTP 417 INVALID_SYMBOL",
        "market data read failed: BrokerError: timeout",
        "calendar: OUT_OF_COVERAGE: 2031-01-02 is not covered by any file",
        "symbol 'abc' is not an upper-case US ticker",
        "interval must be one of: 1m, 5m, 30m, 1h, 1d, 1w, 1mo",
        "range must be one of: 1d, 5d, 1m, 3m, 1y",
        "interval 1m is not served for range 1y: 1m/5m/30m/1h take 1d or 5d, 1d/1w/1mo take 1m, "
        "3m or 1y",
        "ABCD bar at 2026-11-16T01:00:00.000Z is outside the calendar's sessions of 2026-11-16",
        "ABCD bar at 2026-11-16T14:30:00.000Z: the provider says PRE, the calendar says RTH",
        "ABCD bars are not in strictly increasing time order",
        "fewer than 5 sessions in the 14 days before 2026-11-16 ([chart] session_lookback_days)",
        "one request of 1000 M1 bars did not reach the start of 2026-11-16 ([chart] "
        "bars_per_request)",
        "ABCD daily bars: two bars or a step back at 2026-11-13",
        "a year of daily bars needs up to 400 bars, more than [chart] bars_per_request",
        "malformed query string: bad query field: 'x'",
        "unknown query parameter 'x'",
        "query parameter 'symbol' given twice",
        "query parameter 'symbol' is empty",
        "missing query parameters: interval, range",
    ),
    "movers rules_cause": (
        "the active strategy defines no fast movers ([spike] in its strategy.toml)",),
    "jobs session_cause": (
        "WEEKEND: 2026-11-21 is a weekend",
        "HOLIDAY: 2026-11-26 is a market closure (https://www.nyse.com/markets/hours-calendars)",
        "OUT_OF_COVERAGE: 2031-01-02 is not covered by any file",
        "CALENDAR_INVALID: no calendar files in calendar",
        "NAIVE_TIME: naive datetime datetime.datetime(2026, 11, 16, 10, 0)",
        "INVALID_OFFSET: last entry 15:50:00 is not before flatten 15:45:00",
        "no session 2026-11-27: half day, half_days_enabled = false",
    ),
    "jobs backup cause": (
        "the bot has not created its database yet",
        "database unreadable (DatabaseError)",
        "no backup recorded yet",
    ),
    "jobs nightly cause": (
        "the nightly fetch has not run yet",
        "nightly.json unreadable (JSONDecodeError)",
        "refused during the regular session",
        "1200 MB free, below [backtest] nightly_min_free_disk_mb 5000",
        "no current/pool.csv yet: pass --seed with a folder made by fetch-all",
        "/data/backtest/current/pool.csv is empty",
        "12 sessions missing after 2026-10-30, more than [backtest] nightly_max_new_sessions 5: "
        "run fetch-all instead",
        "no session within 10 days after 2026-12-24",
        "stopped at 2026-11-17T09:00:00-05:00, before the next session ([backtest] "
        "nightly_stop_minutes_before_open); current is unchanged",
    ),
    "risk unavailable_cause": (
        "the bot has not written a status file",
        "status file is invalid (status-v1: missing field 'run_id')",
        "status file is dated in the future",
    ),
    "flow agreement_cause and fences_cause": (
        "only the latest reconciliation is kept; it is not recorded per day",
        "only the latest status is kept; it is not recorded per day",
        "the bot has not written an account file",
        "account file is invalid (account-v1: missing field 'read_at')",
        "the latest broker read was on 2026-11-13",
        "the bot has not written a status file",
        "status file is invalid (status-v1: missing field 'run_id')",
        "the latest status was written on 2026-11-13",
    ),
    "flow change_cause": (
        "the captured row has no price",
        "the captured row has no pre_close",
        "the captured price is not a number: 'n/a'",
        "the captured pre_close is not a number: 'NaN'",
        "the captured pre_close is not positive: 0",
    ),
    "flow excluded_cause": (
        "CONFLICTING_ROWS: listed under an instrument with conflicting rows",
        "CONFLICTING_ROWS: 1/ABCD/NAS/USD; 2/ABCD/NYSE/USD; symbol ABCD under several "
        "instrument ids",
        "EXCHANGE_NOT_ALLOWED: OTC",
        "CURRENCY_NOT_ALLOWED: CAD",
        "SECURITY_TYPE_UNKNOWN: missing",
        "SECURITY_TYPE_NOT_ALLOWED: ETF",
    ),
    "flow binding_cause": (
        "not proven by the record: the planned loss, the notional cap or the daily room",),
    "chart cause": ("no 5m bars from webull for ABCD in range 1d",),
    "advisor runs cause": (
        "no advisor run yet: the proposals folder does not exist",
        "proposals folder unreadable (PermissionError)",
    ),
    "advisor unreadable run cause": (
        "the folder name is not <UTC time>-<strategy name>",
        "folder unreadable (PermissionError)",
        "no run.json (a run written before the apps read runs, or an interrupted one)",
        "run.json: KeyError: 'summary'",
        "judge-a.json: JSONDecodeError",
        "decision.json is not tickr-advisor-decision-v1",
    ),
    "advisor run cause": (
        "answer is not JSON: Expecting value",
        "answer does not match the proposal schema",
        "changes is not a list",
        "5 changes proposed, at most 3 allowed",
        "malformed change: {'key': 'x'}",
        "[entry] min_volume changed twice",
        "[strategy] version: [strategy] name and version are not the model's to change",
        "[entry] nope: no such key in the base strategy.toml",
        "[entry] min_volume: not a TOML value: 'abc'",
        "[entry] min_volume: new value 'x' is a string, current is integer",
        "[entry] min_volume: new value equals the current value 100",
        "no changes, no idea and no no_change_reason",
        "new_strategy_idea does not match the schema",
        "new_strategy_idea lacks hypothesis, entry_rule",
        "new_strategy_idea name 'Idea' is not like 'name-v1'",
        "/repo/strategies/x/strategy.toml: version 'v2' is not a whole number",
        "no strategy folder named price-action under /repo/strategies",
        "[entry] min_volume changed twice; [entry] nope: no such key in the base strategy.toml",
        "my-idea-v2: no module for strategy 'my-idea' in strategy_modules",
    ),
    "command record cause": (
        "read after stop-bot 7c1e: the bot is stopping",
        "expired: requested 400 s ago (limit 300 s); steps done: entry-stop-on, "
        "cancel-own-orders; not run: flatten",
        "requested 5 s in the future; steps done: entry-stop-on; not run: cancel-own-orders, "
        "flatten",
        "expired: requested 400 s ago (limit 300 s); steps done: entry-stop-on, "
        "cancel-own-orders, flatten; not run: none",
        "unreadable command file: command-v1: missing field 'kind'",
        "file name does not match command_id",
        "a close is in progress; send stop-bot again after it completes",
        "2 trade(s) open or entering; flatten first (or send emergency), then stop-bot",
        "no open position in ABCD",
        "closing order refused before for t1, t2: look in the Webull app, then clear the fence",
        "a close of t1 was due outside the regular session and was not sent (fence "
        "market-closed); the stop stays: close it in the Webull app, or clear the fence in the "
        "regular session",
        "no closing order is sent now: before the regular open 09:30 ET; the stops stay. Close "
        "the position in the Webull app",
        "nothing open",
        "entry-stop-on: done; cancel-own-orders: 2 entry cancel(s) requested; flatten: nothing "
        "open",
        "flatten: a close of t1 was due outside the regular session and was not sent (fence "
        "market-closed); the stop stays: close it in the Webull app, or clear the fence in the "
        "regular session",
        "flatten: no closing order is sent now: at or after 15:58:00 ET, 120 s before the "
        "regular close 16:00 ET of 2026-11-16; the stops stay. Close the position in the Webull "
        "app",
    ),
}
SAMPLES = tuple(s for samples in FIELD_SAMPLES.values() for s in samples)


@pytest.mark.parametrize("text", SAMPLES)
def test_every_cause_these_fields_show_has_a_code(text):
    """So the apps say it in the person's language (an English and a Spanish sentence per code:
    tests/test_api_codes.py), never in the bot's words."""
    codes = [r.code for r in reasons.prose_reasons(text)]
    assert codes and reasons.UNRECOGNIZED not in codes


def test_an_emergency_step_carries_its_own_code():
    found = reasons.prose_reasons("entry-stop-on: done; flatten: no open position in ABCD")
    assert found == (
        reasons.Reason("command_step", {"step": "entry-stop-on", "step_code": "step_done"}),
        reasons.Reason("command_step", {"step": "flatten", "step_code": "no_open_position",
                                        "symbol": "ABCD"}))
    assert reasons.prose_reasons("flatten: anything else") == (reasons.Reason("unrecognized"),)


def test_a_quoted_refusal_in_no_known_shape_reads_as_the_fields_fallback():
    """A nightly fetch or an advisor run can quote any refusal of a deeper layer: the whole
    text becomes the fallback's `detail`, so the apps still put it in a sentence."""
    deep = "fetch: the history source answered 503"
    assert reasons.prose_reasons_or(deep, reasons.NIGHTLY_REFUSED) == (
        reasons.Reason(reasons.NIGHTLY_REFUSED, {"detail": deep}),)
    assert reasons.prose_reasons_or("", reasons.ADVISOR_REFUSED) == ()
    assert reasons.prose_reasons_or("nothing open", reasons.ADVISOR_REFUSED) == (
        reasons.Reason("nothing_open"),)
    run = contract.to_jsonable(contract.NightlyFetchJobV1(None, None, None, deep))
    assert run["cause_codes"] == [{"code": reasons.NIGHTLY_REFUSED, "params": {"detail": deep}}]


# Where each field's causes are written: (file under src/tickr, function or constant, the calls,
# statements or assignment the texts are in; empty = the whole function).
CAUSE_WRITERS = (
    ("api/symbols.py", "NO_RULES", ()),
    ("api/jobs.py", "NO_DATABASE", ()), ("api/jobs.py", "NO_BACKUP", ()),
    ("api/jobs.py", "NIGHTLY_NEVER_RAN", ()),
    ("api/jobs.py", "_session", ()), ("api/jobs.py", "_nightly", ("NightlyFetchJobV1",)),
    ("api/jobs.py", "_from_database", ("BackupJobV1",)),
    ("backtest/nightly.py", "run_nightly", ("BacktestError",)),
    ("backtest/nightly.py", "_night", ("BacktestError",)),
    ("backtest/nightly.py", "next_open", ("BacktestError",)),
    ("backtest/nightly.py", "check", ("BacktestError",)),
    ("api/server.py", "status_response", ()),
    ("api/flow.py", "PAST_DAY", ()), ("api/flow.py", "CONFLICTING", ()),
    ("api/flow.py", "change_pct", ()), ("api/flow.py", "reconcile", ()),
    ("api/contract.py", "BINDING_NOT_RECORDED", ()),
    ("api/chart.py", "OFF_CAUSE", ()),
    ("api/chart.py", "answer", ("=cause", "ChartRefused")),
    ("api/chart.py", "parse_query", ("ChartRefused",)),
    ("api/chart.py", "place", ("ChartRefused",)),
    ("api/chart.py", "aggregate_intraday", ("ChartRefused",)),
    ("api/chart.py", "_sessions", ("ChartRefused",)),
    ("api/chart.py", "_minutes", ("ChartRefused",)),
    ("api/chart.py", "_daily", ("ChartRefused",)),
    ("api/chart.py", "_daily_rows", ("ChartRefused",)),
    ("api/server.py", "_query", ("BadQuery",)),
    ("api/__main__.py", "CHART_STARTING", ()),
    ("api/__main__.py", "attach_chart_source", ("=cause", "=refused")),
    ("probes.py", "chart_client_gate", ("return", "=how", "=remove")),
    ("api/advisor.py", "NO_FOLDER", ()), ("api/advisor.py", "advisor_document", ("AdvisorRunsV1",)),
    ("api/advisor.py", "read_run", ("Unreadable",)), ("api/advisor.py", "_json", ("Unreadable",)),
    ("advisor/proposal.py", "parse_reply", ("AdvisorError", "append")),
    ("advisor/proposal.py", "_idea", ("append",)),
    ("advisor/proposal.py", "next_version", ("AdvisorError",)),
    ("runtime/service.py", "CLOSE_IN_PROGRESS", ()),
    ("runtime/service.py", "process_commands", ("_record",)),
    ("runtime/service.py", "_close_interrupted", ("_record",)),
    ("runtime/service.py", "_flatten_rows", ("return",)),
    ("runtime/service.py", "_stop_bot", ("_record",)),
    ("runtime/service.py", "_emergency", ("_record", "step")),
    ("api/contract.py", "read_commands", ("InboxItem",)),
)
_LETTER = re.compile(r"[A-Za-z]")


# Texts in those calls that are not a cause: the exit reason an emergency's flatten records,
# and a chart refusal's error-v1 code and the attribute its retry time is read from.
NOT_CAUSES = {"operator-emergency", "bad_request", "unavailable", "rate_limited",
              "retry_after_seconds"}


def _pieces(node) -> list[str]:
    """The literal text pieces under `node` (an f-string's constant parts, a `str.format`
    template's parts), keys of a lookup (`body["cause"]`) left out."""
    import ast
    keys = {id(n.slice) for n in ast.walk(node) if isinstance(n, ast.Subscript)}
    return [piece for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in keys
            for piece in n.value.split("{}")
            if _LETTER.search(piece) and piece not in NOT_CAUSES]


def _written(path: Path, name: str, where: tuple[str, ...]) -> list[str]:
    import ast
    tree = ast.parse(path.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return _pieces(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body[1:] if ast.get_docstring(node) else node.body
            if not where:
                return [p for statement in body for p in _pieces(statement)]
            found = []
            for inner in (n for statement in body for n in ast.walk(statement)):
                if isinstance(inner, ast.Call):
                    func = inner.func
                    called = func.attr if isinstance(func, ast.Attribute) else getattr(
                        func, "id", "")
                    if called in where:
                        found += [p for arg in inner.args for p in _pieces(arg)]
                elif isinstance(inner, ast.Return) and "return" in where and inner.value:
                    found += _pieces(inner.value)
                elif isinstance(inner, ast.Assign) and any(
                        isinstance(t, ast.Name) and f"={t.id}" in where for t in inner.targets):
                    found += _pieces(inner.value)
            return found
    raise AssertionError(f"{path.name}: no {name}")


def test_every_written_cause_of_these_fields_has_a_sample():
    """A new cause written in one of these fields needs a sample above (and so a code)."""
    missing = []
    for file, name, where in CAUSE_WRITERS:
        pieces = _written(SRC / file, name, where)
        assert pieces, (file, name)
        missing += [(file, name, piece) for piece in pieces
                    if not any(piece.strip() in sample for sample in SAMPLES)]
    assert missing == []


def test_a_chart_refusal_carries_its_code_and_values():
    """GET /chart's 503 says why in the reader's language: the apps never show the bot's words
    ("[chart] webull_enabled = false") for a chart that is off, starting or refused."""
    from tickr.api.__main__ import CHART_STARTING
    from tickr.api.chart import OFF_CAUSE

    off = contract.to_jsonable(contract.ErrorV1("unavailable", OFF_CAUSE))
    assert off["cause_codes"] == [{"code": "chart_off", "params": {}}]
    assert reasons.prose_reasons(CHART_STARTING) == (reasons.Reason("chart_starting"),)
    assert reasons.prose_reasons(
        "ABCD bar at 2026-11-16T14:30:00.000Z: the provider says PRE, the calendar says RTH") == (
        reasons.Reason("chart_bar_session_differs", {"symbol": "ABCD", "time": "09:30",
                                                     "provider": "PRE", "calendar": "RTH"}),)
    assert reasons.prose_reasons("market data is delayed: data delayed by 15 min for ABCD") == (
        reasons.Reason("chart_data_delayed", {"minutes": "15", "symbols": "ABCD"}),)
    assert reasons.prose_reasons("market data read failed: SymbolsRefused: HTTP 417") == (
        reasons.Reason("chart_symbol_unknown", {"detail": "HTTP 417"}),)

