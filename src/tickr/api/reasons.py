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
        ("unprotected_exiting", r"no verified stop for (?P<seconds>[0-9]+) s; exiting")),
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
    "OWN_POSITION_OPEN": _shapes(("", r"(?P<count>[0-9]+) own position\(s\) open")),
    "OWN_ORDER_WORKING": _shapes(("", r"(?P<count>[0-9]+) own order\(s\) working")),
    "POSITION_LIMIT_REACHED": _shapes(
        ("", r"(?P<open>[0-9]+) open position\(s\), limit (?P<limit>[0-9]+)")),
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
    # performance (api/performance.py)
    ("planned_loss_not_positive_r", r"planned loss is not positive: no 1 R to divide by"),
    ("no_trades", r"no counted trades in the range"),
    ("no_wins", r"no winning trades in the range"),
    ("no_losses", r"no losing trades in the range"),
    # a display part that failed unexpectedly: `TypeName: message`
    ("display_failed", r"(?P<error>" + _ERROR + r"): (?P<detail>.*)"),
)
_WIRE_PARAMS = ("time", "until", "since")
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
