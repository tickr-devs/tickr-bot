"""Stable codes and parameters for the bot's plain-words causes, so the apps can say them in
the reader's language (docs/api.md, "Reason codes"). The English text stays the fallback.

Every function here reads a text the bot itself wrote (the trading gate, an engine fence, a
recorded admission refusal) against the exact shapes that code writes. A text that matches no
shape gets the code UNRECOGNIZED and no parameters, and clients show the text: a new shape is
added here on purpose, never guessed. Parameter values are plain text: numbers as written,
times as HH:MM New York (a close cutoff as HH:MM:SS), days as YYYY-MM-DD."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from tickr.calendar import NEW_YORK

UNRECOGNIZED = "unrecognized"

_NUM = r"-?[0-9][0-9.E+-]*"
_DAY = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"


@dataclass(frozen=True)
class Reason:
    code: str
    params: dict[str, str] = field(default_factory=dict)


def _first(shapes: tuple[tuple[str, re.Pattern[str]], ...], text: str) -> Reason:
    for code, shape in shapes:
        match = shape.fullmatch(text)
        if match is not None:
            return Reason(code, {k: v for k, v in match.groupdict().items() if v is not None})
    return Reason(UNRECOGNIZED)


def _shapes(*pairs: tuple[str, str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((code, re.compile(pattern)) for code, pattern in pairs)


# ---------------------------------------------------------------- trading gate (config.py)

_GATE = _shapes(
    ("trading_enabled", r"trading enabled by configuration"),
    ("authorization_valid", r"owner authorization valid"),
    ("trading_disabled", r"trading is disabled for environment '(?P<environment>[^']*)'"),
    ("authorization_file_not_set",
     r"owner authorization required but no authorization_file is set"),
    ("authorization_file_missing", r"owner authorization file not found: (?P<file>.+)"),
    ("authorization_not_toml", r"owner authorization file is not valid TOML"),
    ("authorization_keys_wrong", r"owner authorization file must have exactly: (?P<keys>.+)"),
    ("authorization_other_environment", r"owner authorization is for another environment"),
    ("authorization_digest_mismatch",
     r"owner authorization does not match the current config digest"),
)


def gate_reason(cause: str) -> Reason:
    """The code of a `config.trading_gate` cause."""
    return _first(_GATE, cause)


# ---------------------------------------------------------------- fences (engine/core.py)

_CLOSE_REFUSAL = (r"(?:no regular session now \((?P<session>.+)\)"
                  r"|before the regular open (?P<open>[0-9]{2}:[0-9]{2}) ET"
                  r"|at or after (?P<cutoff>[0-9]{2}:[0-9]{2}:[0-9]{2}) ET, (?P<cutoff_seconds>"
                  + _NUM + r") s before the regular close (?P<close>[0-9]{2}:[0-9]{2}) ET of "
                  r"(?P<day>" + _DAY + r"))")

# Keyed by the fence kind (the part of the fence name before ":").
_FENCE = {
    "startup-reconcile": _shapes(("startup_reconcile_pending", r"startup reconciliation pending")),
    "broker-read-failed": _shapes(
        ("positions_read_failed", r"positions read failed: (?P<error>\w+)"),
        ("open_orders_read_failed", r"open orders read failed: (?P<error>\w+)")),
    "detail-budget": _shapes(("detail_budget", r"Detail reads refused by the budget")),
    "unknown-order": _shapes(("send_outcome_unknown", r"send outcome unknown"),
                             ("crashed_during_send", r"crashed during send")),
    "detail-unreadable": _shapes(
        ("detail_unreadable", r"Detail unreadable for (?P<seconds>[0-9]+) s: order state "
         r"unknown; check it in the Webull app"),
        ("stop_cancel_unconfirmed", r"stop cancel sent and its Detail unreadable for "
         r"(?P<seconds>[0-9]+) s: the stop may be gone and nothing is sold; check the position "
         r"in the Webull app")),
    "order-missing": _shapes(("order_missing", r"acknowledged order not found by Detail")),
    "status-unknown": _shapes(("status_unmapped", r"unmapped status '(?P<status>[^']*)'")),
    "unprotected": _shapes(
        ("stop_covers_part", r"verified stop covers (?P<verified>" + _NUM + r") of (?P<expected>"
         + _NUM + r")")),
    "over-protected": _shapes(
        ("stops_over_position", r"stops cover (?P<covered>" + _NUM + r"), Detail holds (?P<held>"
         + _NUM + r")")),
    "unprotected-timeout": _shapes(
        ("unprotected_close_by_hand", r"no verified stop for (?P<seconds>[0-9]+) s and the "
         r"closing order was refused before: close the position in the Webull app"),
        ("unprotected_exiting", r"no verified stop for (?P<seconds>[0-9]+) s; exiting"),
        ("unprotected_stop_unreadable", r"no verified stop for (?P<seconds>[0-9]+) s and a "
         r"stop's Detail cannot be read: that stop is kept, nothing is sold; close the position "
         r"in the Webull app")),
    "stop-rejected": _shapes(
        ("stop_rejected_exiting", r"protective stop refused after retries; exiting")),
    "exit-refused": _shapes(
        ("exit_refused_no_stop", r"closing order refused and the stop was refused after every "
         r"retry: NO broker stop; close the position in the Webull app"),
        ("exit_refused_stop_again",
         r"closing order refused; the stop is placed again, operator must look")),
    "cancel-unknown": _shapes(
        ("cancel_outcome_unknown", r"cancel (?P<outcome>\w+); waiting for Detail"),
        ("cancel_crashed", r"crashed during cancel")),
    "cancel-failed": _shapes(("cancel_failed", r"order still working after every cancel")),
    "cancel-not-sent": _shapes(
        ("cancel_not_sent", r"no cancel could be sent for (?P<seconds>[0-9]+) s "
         r"\(request budget\)")),
    "resurrected": _shapes(
        ("given_up_order_filled", r"order given up on was found with a fill"),
        ("working_not_in_records", r"working at the broker but not live in records")),
    "foreign-position": _shapes(("foreign_position", r"position not opened by this bot")),
    "foreign-order": _shapes(("foreign_order", r"open order not in our records")),
    "quantity-mismatch": _shapes(
        ("quantity_mismatch", r"broker holds (?P<held>" + _NUM + r"), Detail says (?P<expected>"
         + _NUM + r")")),
    "shutting-down": _shapes(("shutting_down", r"shutdown in progress")),
    "entry-stop": _shapes(("operator_command", r"operator command \S+"),
                          ("operator_emergency", r"operator emergency \S+")),
    "replace-unresolved": _shapes(
        ("replace_unresolved", r"stop replace to (?P<stop_price>" + _NUM + r") not confirmed; "
         r"operator must look")),
    "stop-held": _shapes(
        ("held_after_replace", r"stop and close held after an unresolved replace: open orders "
         r"not yet shown clean twice; check the Webull app"),
        ("held_untracked_sell", r"stop and close held: untracked SELL (?P<orders>.+) rests in "
         r"(?P<symbol>\S+) after an unresolved replace; resolve it in the Webull app")),
    "exit-not-filled": _shapes(
        ("exit_not_filled", r"closing order (?P<order>\S+) for (?P<quantity>" + _NUM + r") "
         r"(?P<symbol>\S+) not filled after (?P<seconds>[0-9]+) s \(filled (?P<filled>\S+), "
         r"state (?P<state>[^)]+)\); nothing more is sent: look in the Webull app "
         r"\(halt\? no buyers\?\)")),
    "watchdog-started": _shapes(
        ("watchdog_started", r"the watchdog started the bot at (?P<time>[0-9]{2}:[0-9]{2}) "
         r"for a resting entry: no new entry until the operator clears this \(tickr "
         r"clear-fence watchdog-started\)"),
        ("watchdog_record_unreadable", r"the watchdog's early restart record is unreadable "
         r"\((?P<error>\w+)\): no new entry until the operator clears this \(tickr "
         r"clear-fence watchdog-started\)")),
    "market-closed": _shapes(
        ("close_not_sent", r"(?P<quantity>" + _NUM + r") of (?P<expected>" + _NUM + r") "
         r"(?P<symbol>\S+) not sent: " + _CLOSE_REFUSAL + r"; the stop stays\. Close the "
         r"position in the Webull app, or clear this fence in the next regular session and the "
         r"bot sends the close")),
}


def fence_reason(name: str, cause: str) -> Reason:
    """The code of an engine fence's cause; `name` is the fence name (`kind` or `kind:key`)."""
    shapes = _FENCE.get(name.partition(":")[0])
    return Reason(UNRECOGNIZED) if shapes is None else _first(shapes, cause)


# ---------------------------------------------------------------- admission refusals

# Keyed by the recorded cause code (risk/causes.py); the detail as risk/admission.py,
# risk/sizing.py and risk/daily.py write it.
_REFUSAL = {
    "CLOCK_INVALID": _shapes(("", r"last broker read is (?P<seconds>" + _NUM
                              + r") s in the future")),
    "BROKER_READ_STALE": _shapes(
        ("", r"last broker read (?P<age>" + _NUM + r") s ago > (?P<limit>" + _NUM + r") s")),
    "CALENDAR_UNKNOWN": _shapes(("", r"(?P<calendar>[A-Z_]+): .*")),
    "CALENDAR_DAY_MISMATCH": _shapes(
        ("", r"calendar day (?P<calendar_day>" + _DAY + r") is not today (?P<today>" + _DAY
         + r") in New York")),
    "HALF_DAY": _shapes(("", r"(?P<day>" + _DAY + r") is a half day; half days disabled")),
    "OUTSIDE_REGULAR_SESSION": _shapes(
        ("", r"\S+ is outside (?P<open>[0-9]{2}:[0-9]{2}):[0-9]{2}-(?P<close>[0-9]{2}:[0-9]{2})"
         r":[0-9]{2} New York")),
    "ENTRY_STOP_SET": _shapes(("", r"operator entry-stop is set")),
    "OWN_POSITION_OPEN": _shapes(("", r"(?P<count>[0-9]+) own position\(s\) open")),
    "OWN_ORDER_WORKING": _shapes(("", r"(?P<count>[0-9]+) own order\(s\) working")),
    "POSITION_LIMIT_REACHED": _shapes(
        ("", r"(?P<open>[0-9]+) open position\(s\), limit (?P<limit>[0-9]+)")),
    "WORKING_ENTRY_LIMIT_REACHED": _shapes(
        ("", r"(?P<working>[0-9]+) entries working, limit (?P<limit>[0-9]+)")),
    "SYMBOL_COOLDOWN": _shapes(("", r"(?P<symbol>\S+) closed at a loss at \S+; no entry before "
                                    r"\S+")),
    "DECISION_NOT_ENTER": _shapes(("", r"verdict is (?P<verdict>\S+)")),
    "DECISION_NOT_ACTIVE": _shapes(
        ("", r"decision from (?P<strategy>\S+) (?P<version>\S+), active is (?P<active>\S+)")),
    "DECISION_INCOMPLETE": _shapes(("", r"ENTER without (?P<fields>.+)")),
    "DECISION_INVALID": _shapes(
        ("", r"need 0 < stop (?P<stop>" + _NUM + r") < entry_low (?P<entry_low>" + _NUM
         + r") <= entry_high (?P<entry_high>" + _NUM + r")")),
    "ORDER_PRICES_INVALID": _shapes(
        ("", r"limit (?P<limit>" + _NUM + r") outside window (?P<entry_low>" + _NUM
         + r")\.\.(?P<entry_high>" + _NUM + r")"),
        ("", r"need 0 < stop price (?P<stop>" + _NUM + r") < limit (?P<limit>" + _NUM + r")")),
    "QUOTE_SYMBOL_MISMATCH": _shapes(
        ("", r"quote (?P<quote_symbol>\S+) for decision (?P<symbol>\S+)")),
    "QUOTE_INVALID": _shapes(
        ("", r"(?:non-finite )?bid (?P<bid>\S+) / ask (?P<ask>\S+)(?: not executable)?")),
    "QUOTE_TIME_INVALID": _shapes(
        ("", r"provider time (?P<seconds>" + _NUM + r") s ahead of our clock > (?P<limit>" + _NUM
         + r") s")),
    "QUOTE_STALE": _shapes(
        ("", r"quote (?P<age>" + _NUM + r") s old > (?P<limit>" + _NUM + r") s")),
    "PRICE_OUTSIDE_WINDOW": _shapes(
        ("", r"ask (?P<ask>" + _NUM + r") outside (?P<entry_low>" + _NUM + r")\.\.limit "
         r"(?P<limit>" + _NUM + r")")),
    "QUANTITY_NOT_WHOLE": _shapes(
        ("", r"quantity (?P<quantity>" + _NUM + r") is not a whole number of shares")),
    "QUANTITY_BELOW_ONE": _shapes(
        ("", r"quantity (?P<quantity>" + _NUM + r") < 1 \(platform maximum (?P<maximum>" + _NUM
         + r")\)")),
    "NOTIONAL_OVER_CAP": _shapes(
        ("", r"notional (?P<notional>" + _NUM + r") > cap (?P<cap>" + _NUM + r")")),
    "PLANNED_LOSS_OVER_LIMIT": _shapes(
        ("", r"planned loss (?P<planned_loss>" + _NUM + r") > limit (?P<limit>" + _NUM + r")")),
    "DAILY_MONEY_UNKNOWN": _shapes(("", r"unknown: (?P<missing>.+)")),
    "DAILY_CEILING": _shapes(
        ("", r"room (?P<room>" + _NUM + r") = ceiling (?P<ceiling>" + _NUM + r") - realized "
         r"(?P<realized>" + _NUM + r") - reserved (?P<reserved>" + _NUM + r") is below "
         r"min_daily_room_usd (?P<min_room>" + _NUM + r")"),
        ("", r"realized (?P<realized>" + _NUM + r") \+ reserved (?P<reserved>" + _NUM
         + r") \+ new (?P<new>" + _NUM + r") = (?P<total>" + _NUM + r") vs ceiling "
         r"(?P<ceiling>" + _NUM + r")")),
}


def _ny_time(value: datetime) -> str:
    return f"{value.astimezone(NEW_YORK):%H:%M}"


def refusal_params(code: str, detail: str, since: datetime | None,
                   until: datetime | None) -> dict[str, str]:
    """The values in a recorded refusal detail, for the code's translated sentence. Empty when
    the code has none or the detail is not in a shape this module knows. GATE_CLOSED carries
    the gate's own code (`gate`) and its parameters."""
    if code == "GATE_CLOSED":
        gate = gate_reason(detail)
        return {} if gate.code == UNRECOGNIZED else {"gate": gate.code, **gate.params}
    shapes = _REFUSAL.get(code)
    params = {} if shapes is None else _first(shapes, detail).params
    if code == "AFTER_LAST_ENTRY" and since is not None:
        params = {"time": _ny_time(since)}
    if code == "SYMBOL_COOLDOWN" and params and until is not None:
        params = {**params, "until": _ny_time(until)}
    return params


# ---------------------------------------------------------------- the API's own prose

_WIRE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
_ERROR = r"[A-Z][A-Za-z]*(?:Error|Exception|Refused|Unavailable|Journal)"
# The Start button's note that it cleared the operator stop (api/botstart.py).
_STOP_CLEARED = r" \(the operator stop was cleared, so the next scheduled start runs\)"
# How to pass the chart's probe C1, as probes.chart_client_gate tells it.
_CHART_CHECK_HOW = (r"run `python -m tickr\.probes chart-client SYMBOL` next to the running bot, "
                    r"then start the API again")
# A strategy.toml key an advisor answer names: `[section] key` (advisor/proposal.py).
_KEY = r"\[(?P<section>[^\]]*)\] (?P<key>\S+)"

# Causes the API writes itself (api/history, after, observer_results, cleanday, performance,
# contract closeness). Wire times in them are given as HH:MM New York.
_PROSE = _shapes(
    # entry send (history.entry_send) and paths that do not read it
    ("entry_orders_count", r"trade \S+ has (?P<count>[0-9]+) entry orders, expected 1"),
    ("no_intent_row", r"no order-intent row"),
    ("never_attempted", r"never attempted: no order-attempted row"),
    ("no_fill_row", r"no order-observed row shows the entry filled"),
    ("journal_unreadable", r"journal unreadable: (?P<detail>.+)"),
    ("not_read", r"not read on this path"),
    ("not_computed", r"not computed on this path"),
    # prices after and overshoot (api/after.py)
    ("session_ended", r"the session ended at (?P<time>" + _WIRE + ")"),
    ("no_bars_for_symbol", r"no bar recorded for (?P<symbol>\S+) that day"),
    ("bars_end_before", r"the bars end at (?P<time>" + _WIRE + "), before (?P<until>" + _WIRE
     + ")"),
    ("bars_end", r"the bars end at (?P<time>" + _WIRE + ")"),
    ("no_bar_for_minute", r"no bar recorded for the minute (?P<time>" + _WIRE + ")"),
    ("no_reference_price", r"the decision bar was not captured: no reference price"),
    ("calendar_refuses", r"calendar refuses (?P<day>" + _DAY + r"): (?P<detail>.+)"),
    ("no_minute_before_close",
     r"no minute after the exit before the close (?P<time>" + _WIRE + ")"),
    ("no_bar_after_exit", r"no bar recorded after the exit minute up to (?P<until>" + _WIRE + ")"),
    ("planned_loss_not_positive",
     r"planned loss (?P<planned_loss>" + _NUM + r") is not positive: no R"),
    # what-if and observer simulations (api/after.py, api/observer_results.py)
    ("simulation_refused", r"simulation refused: (?P<detail>.+)"),
    ("skipped_overlap", r"skipped_overlap: an earlier simulated trade of (?P<symbol>\S+) was "
                        r"still open"),
    ("skipped_same_signal", r"skipped_same_signal: the same signal \(setup (?P<setup>\S+), "
                            r"trigger (?P<trigger>\S+)\) already made a simulated trade of "
                            r"(?P<symbol>\S+) that day"),
    ("simulation_outcome", r"(?P<outcome>filled|missed|rejected|ignored|not_simulated): "
                           r"(?P<detail>.*)"),
    ("no_decisions", r"no decisions recorded that day"),
    ("no_captured_bars", r"no captured bars for (?P<day>" + _DAY + ")"),
    ("capture_refused", r"capture file refused: (?P<detail>.+)"),
    ("replay_settings_unreadable", r"replay settings unreadable: (?P<detail>.+)"),
    ("calendar_unreadable", r"session calendar unreadable: (?P<detail>.+)"),
    ("enters_not_simulated", r"(?P<count>[0-9]+) ENTER\(s\) could not be simulated: the "
                             r"decision bar is not in the captured bars"),
    ("trades_without_quantity",
     r"(?P<count>[0-9]+) simulated trade\(s\) have no recorded whole quantity"),
    ("result_in_r_only", r"no recorded whole quantity: result in R only"),
    # clean-day verdict (api/cleanday.py)
    ("day_not_over", r"the day is not over"),
    ("reconcile_not_recorded",
     r"not recorded: no reconcile-checked row that date \(the bot records them since "
     r"\[orders\] reconcile_proof_seconds was added\)"),
    ("no_day_end", r"no day-end row that date \(the bot was not running after the flatten "
                   r"time\)"),
    ("day_end_unknown", r"the day-end check had no broker reads \(day_clean null\)"),
    ("no_calendar", r"the session calendar cannot be read"),
    ("calendar_error", r"calendar: (?P<detail>.+)"),
    ("reconcile_gap", r"no reconcile-checked row from (?P<since>" + _WIRE + ") to (?P<until>"
                      + _WIRE + r") \((?P<seconds>[0-9]+) s, more than \[orders\] "
                      r"reconcile_max_gap_seconds (?P<max_gap>" + _NUM + r")\)"),
    ("reconcile_mismatch", r"reconcile: (?P<detail>.+)"),
    ("fence_still_set", r"(?P<fence>[a-z-]+(?::\S+)?): still set at the end of the day"),
    ("day_end_not_clean", r"day end: broker_flat (?P<broker_flat>\S+), agree (?P<agree>\S+), "
                          r"open_trades (?P<open_trades>\S+)"),
    ("unprotected_alert", r"alert: (?P<detail>.+)"),
    ("safety_exit", r"exit (?P<exit>\S+) for trade (?P<trade>\S+)"),
    ("emergency_command", r"emergency command \((?P<outcome>[^)]*)\)"),
    # closeness of the decisive check (api/contract.closeness)
    ("all_checks_passed", r"every recorded check passed"),
    ("quote_age_not_recorded", r"not recorded: decision records hold no quote time"),
    ("yes_no_check", r"(?P<rule>\S+): a yes/no check has no measured value"),
    ("needs_positive_threshold", r"(?P<rule>\S+): needs threshold > 0 and measured >= 0"),
    ("needs_positive_measured", r"(?P<rule>\S+): needs measured > 0 and threshold >= 0"),
    ("no_direction", r"(?P<rule>\S+): operator '(?P<operator>[^']*)' has no direction"),
    ("check_contradicts_record",
     r"(?P<rule>\S+): recorded as failed but measured meets the threshold"),
    ("no_invalidation",
     r"(?P<rule>\S+): no invalidation recorded to measure the trigger from"),
    ("invalidation_wrong_side",
     r"(?P<rule>\S+): the invalidation is not on the far side of the trigger"),
    ("past_invalidation", r"(?P<rule>\S+): measured is past the invalidation"),
    # performance (api/performance.py)
    ("planned_loss_not_positive_r", r"planned loss is not positive: no 1 R to divide by"),
    ("no_trades", r"no counted trades in the range"),
    ("no_wins", r"no winning trades in the range"),
    ("no_losses", r"no losing trades in the range"),
    # the Lab's replay and another version on one trade (api/lab_replay.py, api/trade_whatif.py)
    ("outside_session_only", r"refused during the regular session until (?P<until>" + _WIRE
     + r") \(\[api\] (?:lab_replay|trade_whatif)_outside_session_only\)"),
    ("replay_running", r"a replay is already running"),
    ("api_without_config", r"the API was started without its config file path"),
    ("no_backtest_data",
     r"no fetched backtest data: pool\.csv is missing in \[api\] lab_replay_data_dir"),
    ("not_a_data_day", r"(?P<day>" + _DAY + r") is not a day of the fetched data"),
    ("compare_same_version", r"compare names the same version twice"),
    ("no_strategy_folder", r"no strategy folder '(?P<folder>[^']*)' under strategies/"),
    ("strategy_unreadable", r"(?P<folder>\S+): unreadable"),
    ("strategy_file_unreadable", r"(?P<folder>\S+): cannot read \S+: (?P<detail>.+)"),
    ("strategy_without_name", r"(?P<folder>\S+): strategy\.toml has no \[strategy\] name"),
    ("strategy_without_version",
     r"(?P<folder>\S+): strategy\.toml has no \[strategy\] version string"),
    ("modules_malformed", r"\[observers\] strategy_modules is malformed"),
    ("module_entry_malformed",
     r"\[observers\] strategy_modules entry is not name=module: (?P<detail>.+)"),
    ("module_listed_twice", r"\[observers\] strategy_modules lists '(?P<strategy>[^']*)' twice"),
    ("no_module", r"no module for strategy '(?P<strategy>[^']*)' in \[observers\] "
                  r"strategy_modules"),
    ("backtest_refused", r"backtest refused: (?P<detail>.+)"),
    ("run_without_report", r"the run ended without a report"),
    ("version_never_started", r"the API stopped before this version started"),
    ("run_file_unreadable", r"(?P<file>current|report)\.json unreadable \((?P<error>\w+)\)"),
    ("calendar_file_unreadable", r"calendar unreadable: (?P<detail>.+)"),
    ("no_bars_for_symbol_on", r"no captured bars for (?P<symbol>\S+) on (?P<day>" + _DAY + ")"),
    ("no_cached_history", r"no cached prior sessions for (?P<symbol>\S+) on (?P<day>" + _DAY
     + ")"),
    ("cached_history_refused", r"cached prior sessions refused: (?P<detail>.+)"),
    ("cached_history_empty", r"cached prior sessions hold no bar of (?P<symbol>\S+)"),
    ("cached_history_overlaps", r"cached prior sessions overlap the day's bars"),
    ("history_sessions_differ",
     r"replay settings history_sessions (?P<replay>[0-9]+) differ from \[market_data\] "
     r"history_sessions (?P<bot>[0-9]+): the version would not see the history the bot cached"),
    ("simulation_failed", r"simulation failed: (?P<error>\w+): (?P<detail>.*)"),
    ("strategy_without_module",
     r"(?P<folder>\S+): no module for strategy '(?P<strategy>[^']*)' in strategy_modules"),
    ("module_not_importable", r"(?P<folder>\S+): module (?P<module>\S+) cannot be imported: "
                              r"(?P<detail>.+)"),
    ("module_without_create", r"(?P<folder>\S+): module (?P<module>\S+) has no create\(values\)"),
    ("values_refused", r"(?P<folder>\S+): values refused by (?P<module>\S+): (?P<detail>.+)"),
    ("identity_differs", r"(?P<folder>\S+): strategy reports .+, file says .+"),
    ("no_history_need", r"(?P<folder>\S+): .+ declares no valid prior_sessions_needed \(got .*\)"),
    ("history_too_short", r"(?P<folder>\S+): .+ needs (?P<need>[0-9]+) prior sessions, "
                          r"\[market_data\] history_sessions = (?P<bot>[0-9]+)"),
    # the API's guard and pairing refusals (api/server.py)
    ("host_not_allowed", r"Host (?P<host>.+) is not allowed"),
    ("origin_not_allowed", r"Origin (?P<origin>.+) is not allowed"),
    ("no_phone_paired", r"no phone is paired; run `python -m tickr\.api pair`"),
    ("token_required", r"a bearer token is required"),
    ("token_not_paired", r"the bearer token is not the paired one"),
    ("no_pairing_waiting", r"no pairing is waiting; run `python -m tickr\.api pair` on the Mac"),
    ("pairing_expired",
     r"the pairing code has expired; run `python -m tickr\.api pair` on the Mac again"),
    ("pairing_code_wrong", r"the pairing code is not the waiting one"),
    # reads the bot has not written yet, and the Start button (api/server.py, api/botstart.py)
    ("no_account_file", r"the bot has not written an account file"),
    ("no_quotes_file", r"the bot has not written a quotes file"),
    ("no_start_command", r"no start command is configured on this machine \(\[api\] "
                         r"start_bot_command is empty\): start the bot where it runs"),
    ("bot_not_stopped", r"the bot is (?P<state>\w+): nothing to start"),
    ("lock_unreadable",
     r"the bot's lock file could not be read \((?P<error>\w+)\): nothing was started"),
    ("lock_held", r"a bot process \(pid (?P<pid>[0-9]+)\) still holds the lock: wait for it to "
                  r"end, then start again"),
    ("stop_not_cleared",
     r"the operator stop could not be cleared \((?P<error>\w+)\): nothing was started"),
    ("start_timeout_cleared", r"the start command did not return within (?P<seconds>" + _NUM
     + r") s" + _STOP_CLEARED),
    ("start_timeout", r"the start command did not return within (?P<seconds>" + _NUM + r") s"),
    ("start_failed_cleared", r"the start command failed \((?P<error>\w+)\)" + _STOP_CLEARED),
    ("start_failed", r"the start command failed \((?P<error>\w+)\)"),
    ("start_exited_cleared", r"the start command exited (?P<code>-?[0-9]+)" + _STOP_CLEARED),
    ("start_exited", r"the start command exited (?P<code>-?[0-9]+)"),
    # fast movers (api/symbols.py)
    ("no_fast_mover_rules",
     r"the active strategy defines no fast movers \(\[spike\] in its strategy\.toml\)"),
    # the session calendar's own refusals (calendar.CalendarRefused) and the day's session
    # (api/jobs.py)
    ("calendar_weekend", r"WEEKEND: (?P<day>" + _DAY + r") is a weekend"),
    ("calendar_holiday", r"HOLIDAY: (?P<day>" + _DAY + r") is a market closure \(.*\)"),
    ("calendar_not_covered", r"OUT_OF_COVERAGE: (?P<day>" + _DAY + r") is not covered by any "
                             r"file"),
    ("calendar_invalid", r"CALENDAR_INVALID: (?P<detail>.+)"),
    ("calendar_naive_time", r"NAIVE_TIME: (?P<detail>.+)"),
    ("calendar_invalid_offset", r"INVALID_OFFSET: (?P<detail>.+)"),
    ("half_day_off", r"no session (?P<day>" + _DAY + r"): half day, half_days_enabled = false"),
    ("no_next_session", r"no regular session within (?P<days>[0-9]+) days after (?P<day>"
                        + _DAY + ")"),
    # the jobs strip: backup and nightly fetch (api/jobs.py)
    ("no_database", r"the bot has not created its database yet"),
    ("database_unreadable", r"database unreadable \((?P<error>\w+)\)"),
    ("no_backup", r"no backup recorded yet"),
    ("nightly_never_ran", r"the nightly fetch has not run yet"),
    ("nightly_file_unreadable", r"nightly\.json unreadable \((?P<error>\w+)\)"),
    # the nightly fetch's own refusals (backtest/nightly.py); any other is `nightly_refused`
    ("nightly_in_session", r"refused during the regular session"),
    ("nightly_low_disk", r"(?P<free_mb>[0-9]+) MB free, below \[backtest\] "
                         r"nightly_min_free_disk_mb (?P<min_mb>[0-9]+)"),
    ("nightly_no_pool", r"no current/pool\.csv yet: pass --seed with a folder made by fetch-all"),
    ("nightly_pool_empty", r".+pool\.csv is empty"),
    ("nightly_too_many_missing",
     r"(?P<count>[0-9]+) sessions missing after (?P<day>" + _DAY + r"), more than \[backtest\] "
     r"nightly_max_new_sessions (?P<max>[0-9]+): run fetch-all instead"),
    ("nightly_no_session_ahead", r"no session within (?P<days>[0-9]+) days after (?P<day>"
     + _DAY + ")"),
    ("nightly_stopped_before_open",
     r"stopped at \S+, before the next session \(\[backtest\] "
     r"nightly_stop_minutes_before_open\); current is unchanged"),
    # the bot's status file and account file as the API reads them (api/server.py, api/flow.py)
    ("no_status_file", r"the bot has not written a status file"),
    ("status_file_invalid", r"status file is invalid \((?P<detail>.+)\)"),
    ("status_from_future", r"status file is dated in the future"),
    ("account_file_invalid", r"account file is invalid \((?P<detail>.+)\)"),
    ("quotes_file_invalid", r"quotes file is invalid \((?P<detail>.+)\)"),
    ("past_day_reconciliation",
     r"only the latest reconciliation is kept; it is not recorded per day"),
    ("past_day_status", r"only the latest status is kept; it is not recorded per day"),
    ("broker_read_other_day", r"the latest broker read was on (?P<day>" + _DAY + ")"),
    ("status_other_day", r"the latest status was written on (?P<day>" + _DAY + ")"),
    # a screener chip's change and universe outcome (api/flow.py, data/discovery.py Cause)
    ("row_without_price", r"the captured row has no price"),
    ("row_without_pre_close", r"the captured row has no pre_close"),
    ("price_not_number", r"the captured price is not a number: (?P<value>.+)"),
    ("pre_close_not_number", r"the captured pre_close is not a number: (?P<value>.+)"),
    ("pre_close_not_positive", r"the captured pre_close is not positive: (?P<value>" + _NUM
     + ")"),
    ("universe_conflicting_rows", r"CONFLICTING_ROWS: (?P<measured>.+)"),
    ("universe_exchange", r"EXCHANGE_NOT_ALLOWED: (?P<measured>.+)"),
    ("universe_currency", r"CURRENCY_NOT_ALLOWED: (?P<measured>.+)"),
    ("universe_type_unknown", r"SECURITY_TYPE_UNKNOWN: (?P<measured>.+)"),
    ("universe_type", r"SECURITY_TYPE_NOT_ALLOWED: (?P<measured>.+)"),
    # the latest size (api/contract.latest_size)
    ("binding_not_proven", r"not proven by the record: the planned loss, the notional cap or "
                           r"the daily room"),
    # a chart without candles (api/chart.py)
    ("no_chart_bars", r"no (?P<interval>\S+) bars from (?P<source>\S+) for (?P<symbol>\S+) in "
                      r"range (?P<range>\S+)"),
    # GET /chart refused (api/chart.py ChartRefused; its query, api/server.py _query); the
    # chart's source switched off, still being built or failed to build (api/__main__.py,
    # probes.chart_client_gate)
    ("chart_off", r"chart market data is off \(\[chart\] webull_enabled = false\)"),
    ("chart_check_not_run", r"chart market data is off: chart probe C1 is not recorded for "
                            r"(?P<environment>\S+); " + _CHART_CHECK_HOW),
    ("chart_check_not_passed",
     r"chart market data is off: chart probe C1 last ended '(?P<verdict>[^']*)' for "
     r"(?P<environment>\S+) \([^)]*\); " + _CHART_CHECK_HOW),
    ("chart_check_unreadable",
     r"chart market data is off: chart probe C1: (?P<folder>\S+) cannot be read "
     r"\((?P<error>\w+)\); remove that run's folder, then " + _CHART_CHECK_HOW),
    ("chart_check_error",
     r"chart market data is off: the chart probe C1 gate failed \((?P<error>\w+)\)"),
    ("chart_starting",
     r"chart market data is starting: its Webull client is still being built"),
    ("chart_client_failed",
     r"chart market data unavailable: client build failed \((?P<error>\w+)\)"),
    ("chart_client_failed", r"chart market data unavailable: (?P<error>\w+): (?P<detail>.*)"),
    ("chart_read_budget", r"chart read budget: (?P<detail>.*)"),
    ("chart_data_delayed", r"market data is delayed: data delayed by (?P<minutes>" + _NUM
     + r") min for (?P<symbols>.+)"),
    ("chart_data_delayed", r"market data is delayed: (?P<detail>.*)"),
    ("chart_symbol_unknown", r"market data read failed: SymbolsRefused: (?P<detail>.*)"),
    ("chart_read_failed", r"market data read failed: (?P<error>\w+): (?P<detail>.*)"),
    ("chart_symbol_invalid", r"symbol '(?P<symbol>[^']*)' is not an upper-case US ticker"),
    ("chart_interval_invalid", r"interval must be one of: (?P<allowed>.+)"),
    ("chart_range_invalid", r"range must be one of: (?P<allowed>.+)"),
    ("chart_pair_not_served", r"interval (?P<interval>\S+) is not served for range "
                              r"(?P<range>\S+): .+"),
    ("chart_bar_outside_sessions", r"(?P<symbol>\S+) bar at (?P<time>" + _WIRE + r") is outside "
                                   r"the calendar's sessions of (?P<day>" + _DAY + ")"),
    ("chart_bar_session_differs", r"(?P<symbol>\S+) bar at (?P<time>" + _WIRE + r"): the provider "
                                  r"says (?P<provider>\S+), the calendar says (?P<calendar>\S+)"),
    ("chart_bars_out_of_order", r"(?P<symbol>\S+) bars are not in strictly increasing time order"),
    ("chart_too_few_sessions", r"fewer than (?P<count>[0-9]+) sessions in the (?P<days>[0-9]+) "
                               r"days before (?P<day>" + _DAY + r") \(\[chart\] "
                               r"session_lookback_days\)"),
    ("chart_request_too_short", r"one request of (?P<count>[0-9]+) M1 bars did not reach the "
                                r"start of (?P<day>" + _DAY + r") \(\[chart\] bars_per_request\)"),
    ("chart_daily_out_of_order", r"(?P<symbol>\S+) daily bars: two bars or a step back at "
                                 r"(?P<day>" + _DAY + ")"),
    ("chart_year_too_many_bars", r"a year of daily bars needs up to (?P<count>[0-9]+) bars, more "
                                 r"than \[chart\] bars_per_request"),
    ("query_malformed", r"malformed query string: (?P<detail>.*)"),
    ("query_unknown_parameter", r"unknown query parameter '(?P<name>[^']*)'"),
    ("query_parameter_twice", r"query parameter '(?P<name>[^']*)' given twice"),
    ("query_parameter_empty", r"query parameter '(?P<name>[^']*)' is empty"),
    ("query_parameters_missing", r"missing query parameters: (?P<names>.+)"),
    # the AI advisor's folder and an answer it refused (api/advisor.py, advisor/proposal.py);
    # any other refusal of a run is `advisor_refused`
    ("no_advisor_runs", r"no advisor run yet: the proposals folder does not exist"),
    ("proposals_unreadable", r"proposals folder unreadable \((?P<error>\w+)\)"),
    ("run_folder_misnamed", r"the folder name is not <UTC time>-<strategy name>"),
    ("run_folder_unreadable", r"folder unreadable \((?P<error>\w+)\)"),
    ("run_file_missing", r"no (?P<file>[\w.-]+\.json) \(a run written before the apps read runs, "
                         r"or an interrupted one\)"),
    ("run_file_invalid", r"(?P<file>[\w.-]+\.json): (?P<error>\w+)(?:: .*)?"),
    ("run_file_wrong_schema", r"(?P<file>[\w.-]+\.json) is not (?P<schema>\S+)"),
    ("answer_not_json", r"answer is not JSON: (?P<detail>.+)"),
    ("answer_off_schema", r"answer does not match the proposal schema"),
    ("changes_not_list", r"changes is not a list"),
    ("too_many_changes", r"(?P<count>[0-9]+) changes proposed, at most (?P<max>[0-9]+) allowed"),
    ("change_malformed", r"malformed change: (?P<detail>.+)"),
    ("change_twice", _KEY + r" changed twice"),
    ("change_fixed_section",
     _KEY + r": \[strategy\] name and version are not the model's to change"),
    ("change_unknown_key", _KEY + r": no such key in the base strategy\.toml"),
    ("change_not_toml", _KEY + r": not a TOML value: (?P<value>.+)"),
    ("change_other_kind",
     _KEY + r": new value (?P<value>.+) is a (?P<kind>\w+), current is (?P<current_kind>\w+)"),
    ("change_same_value", _KEY + r": new value equals the current value (?P<value>.+)"),
    ("answer_empty", r"no changes, no idea and no no_change_reason"),
    ("idea_off_schema", r"new_strategy_idea does not match the schema"),
    ("idea_incomplete", r"new_strategy_idea lacks (?P<fields>.+)"),
    ("idea_name_wrong", r"new_strategy_idea name (?P<name>.+) is not like 'name-v1'"),
    ("version_not_whole", r"(?P<file>.+): version (?P<version>.+) is not a whole number"),
    ("no_strategy_named", r"no strategy folder named (?P<strategy>\S+) under (?P<folder>.+)"),
    # an operator command's outcome (runtime/service.py) and a command file the bot refused
    # (api/contract.read_inbox); an emergency's steps are `command_step`
    ("command_after_stop", r"read after stop-bot (?P<command>\S+): the bot is stopping"),
    ("close_in_progress", r"a close is in progress; send stop-bot again after it completes"),
    ("trades_still_open", r"(?P<count>[0-9]+) trade\(s\) open or entering; flatten first \(or "
                          r"send emergency\), then stop-bot"),
    ("no_open_position", r"no open position in (?P<symbol>\S+)"),
    ("close_refused_before", r"closing order refused before for (?P<trades>.+): look in the "
                             r"Webull app, then clear the fence"),
    ("close_held_outside_session",
     r"a close of (?P<trades>.+) was due outside the regular session and was not sent \(fence "
     r"market-closed\); the stop stays: close it in the Webull app, or clear the fence in the "
     r"regular session"),
    ("close_not_sent_now", r"no closing order is sent now: " + _CLOSE_REFUSAL + r"; the stops "
                           r"stay\. Close the position in the Webull app"),
    ("nothing_open", r"nothing open"),
    ("entry_cancels_requested", r"(?P<count>[0-9]+) entry cancel\(s\) requested"),
    ("step_done", r"done"),
    ("steps_done", r"steps done: (?P<steps>.*)"),
    ("every_step_ran", r"not run: none"),
    ("steps_not_run", r"not run: (?P<steps>.+)"),
    ("command_file_unreadable", r"unreadable command file: (?P<detail>.+)"),
    ("command_file_misnamed", r"file name does not match command_id"),
    ("command_from_future", r"requested (?P<seconds>" + _NUM + r") s in the future"),
    ("command_expired", r"expired: requested (?P<seconds>" + _NUM + r") s ago \(limit "
                        r"(?P<limit>" + _NUM + r") s\)"),
    # a display part that failed unexpectedly: `TypeName: message`
    ("display_failed", r"(?P<error>" + _ERROR + r"): (?P<detail>.*)"),
)
_WIRE_PARAMS = ("time", "until", "since")
# One step of an emergency command and what it did (runtime/service.py EMERGENCY_STEPS).
_COMMAND_STEP = re.compile(r"(?P<step>entry-stop-on|cancel-own-orders|flatten): (?P<cause>.+)")
COMMAND_STEP = "command_step"
_FENCE_CAUSE = re.compile(r"(?P<fence>[a-z-]+(?::\S+)?): (?P<cause>.+)")
_SEPARATOR = "; "


def _ny_wire(text: str) -> str:
    """A wire time as HH:MM New York; a text the pattern matched but no calendar has (month
    13, hour 25) stays as written."""
    try:
        return _ny_time(datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z"))
    except (ValueError, OverflowError):
        return text


def _prose_one(text: str) -> Reason:
    found = _first(_PROSE, text)
    if found.code == UNRECOGNIZED:
        # An emergency's step with its own outcome (`<step>: <cause>`).
        step = _COMMAND_STEP.fullmatch(text)
        if step is not None:
            inner = _prose_one(step["cause"])
            if inner.code not in (UNRECOGNIZED, COMMAND_STEP):
                return Reason(COMMAND_STEP, {"step": step["step"], "step_code": inner.code,
                                             **inner.params})
        # A clean-day fault quoting a fence's own cause (`<fence name>: <cause>`).
        match = _FENCE_CAUSE.fullmatch(text)
        if match is not None:
            fence = fence_reason(match["fence"], match["cause"])
            if fence.code != UNRECOGNIZED:
                return Reason("fence", {"fence": match["fence"], "fence_code": fence.code,
                                        **fence.params})
        return found
    params = {k: _ny_wire(v) if k in _WIRE_PARAMS and re.fullmatch(_WIRE, v) else v
              for k, v in found.params.items()}
    return Reason(found.code, params)


def prose_reasons(text: str | None) -> tuple[Reason, ...]:
    """The codes of a cause the API wrote: empty for null, one reason when the whole text reads
    as one, else one per `; `-joined part (an entry send's missing rows, a clean day's faults),
    a part joined with the next when only the two read as one cause. A part in no known shape
    is UNRECOGNIZED: clients then show the text. Never raises on a string."""
    if text is None:
        return ()
    whole = _prose_one(text)
    if whole.code != UNRECOGNIZED or _SEPARATOR not in text:
        return (whole,)
    parts = text.split(_SEPARATOR)
    found: list[Reason] = []
    start = 0
    while start < len(parts):
        # One part, or a part with the next: an engine fence cause quoted in a clean-day fault
        # may itself hold one `; ` ("...; exiting"). Linear in the number of parts.
        end, reason = start + 1, _prose_one(parts[start])
        if reason.code == UNRECOGNIZED and start + 1 < len(parts):
            pair = _prose_one(_SEPARATOR.join(parts[start:start + 2]))
            if pair.code != UNRECOGNIZED:
                end, reason = start + 2, pair
        found.append(reason)
        start = end
    return tuple(found)


# Causes whose writer can quote any refusal of a deeper layer (a nightly fetch's BacktestError,
# an advisor run's AdvisorError): a text in no known shape reads as one of these, the whole text
# its `detail`, so the apps always have a sentence to put it in.
NIGHTLY_REFUSED = "nightly_refused"
ADVISOR_REFUSED = "advisor_refused"


def prose_reasons_or(text: str | None, otherwise: str) -> tuple[Reason, ...]:
    """`prose_reasons`, or one `otherwise` reason with the whole text as `detail` when any part
    is in no known shape; none for an empty text (an advisor run that was not refused)."""
    if not text:
        return ()
    found = prose_reasons(text)
    if all(r.code != UNRECOGNIZED for r in found):
        return found
    return (Reason(otherwise, {"detail": text}),)
