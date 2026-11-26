"""What of an exception may reach an API response, a document's cause or a log line.

The text of a system exception (OSError, sqlite3, json, Unicode, a parser) carries the absolute
local path, an OS message or a piece of the data, none of which may leave this Mac. A refusal of
our own is written by us as a cause, so its text is kept, but only for the classes in
REFUSALS, whose raise sites tests/test_refusal_texts.py proves path-free. Every other class,
ours or not, is its type name, e.g. `PermissionError` or `SettingsError` (fail closed).
tests/test_no_exception_text.py holds the rule for what a handler may write.
"""

from __future__ import annotations

from pathlib import PurePath

from tickr.research.errors import RecordFileError

# The refusals whose text may be shown: "module.Class" of the exact type (a subclass is not
# listed until it is audited). Each one's raise sites hold no path, file or caught-exception text
# (tests/test_refusal_texts.py); a class not here shows as its type name.
REFUSALS = frozenset({
    "tickr.runtime.alert_routing.RoutingRefused",
    "tickr.api.advisor.Unreadable",
    "tickr.api.advisor.DecisionRefused",
    "tickr.api.cleanday._Unreadable",
    "tickr.api.history.HistoryUnavailable",
    "tickr.api.history.MalformedJournal",
    "tickr.api.observer_results.NoInputs",
    "tickr.api.lab_replay.LabReplayRefused",
    "tickr.api.trade_whatif.TradeWhatIfRefused",
    "tickr.api.server.BadQuery",
    "tickr.api.settingsview.SettingsUnavailable",
    "tickr.api.settingsview.BadDraft",
    "tickr.api.contract.ContractError",
    "tickr.api.causes.RowRefused",
    "tickr.data.capture.CaptureError",
    "tickr.observers.errors.ObserverError",
    "tickr.calendar.CalendarRefused",
    "tickr.data.base.DelayedData",
    "tickr.data.base.SymbolsRefused",
    "tickr.broker.base.RateLimited",
    "tickr.broker.budget.BudgetExhausted",
})


def safe_cause(exc: BaseException) -> str:
    """The exception's own cause text when it is a tickr refusal, else its type name."""
    kind = type(exc)
    if isinstance(exc, RecordFileError):  # its text holds the file's absolute path and the data
        return f"{PurePath(exc.path).name}:{exc.line}: the record is malformed"
    if f"{kind.__module__}.{kind.__qualname__}" in REFUSALS:
        return str(exc) or kind.__name__
    return kind.__name__


class RowRefused(ValueError):
    """A captured row we parsed and found wrong; its text names the field and is ours to show."""
