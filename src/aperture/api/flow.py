"""GET /flow: the day's pipeline from screeners to broker stops, from records only.

Read-only. Sources (docs/api.md, "GET /flow"):
- screener chips, the screeners and universe stages: the day's captured `screener.jsonl`; the
  universe filter is applied again to each captured pass with the discovery code itself
  (`merge`, `universe_cause`) and the configured allowed lists, because the bot records only
  the pass totals, not each symbol's refusal;
- strategy and risk stages: the decision file (active strategy);
- order and stop stages: the bot database (trades, orders, journal), via `History.entries`;
- reconcile: the bot's account and status files, which hold only the latest state.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aperture.api import contract as api
from aperture.archive import existing, open_text
from aperture.calendar import ny_date_of
from aperture.data.base import ScreenerKind, ScreenerRow
from aperture.data.capture import SCREENER_FILE, CaptureError
from aperture.data.discovery import DiscoverySettings, merge, universe_cause
from aperture.research.records import Admission, DecisionRecord
from aperture.strategy.base import Verdict

_RECORD_KEYS = frozenset({"kind", "rank", "instrument_id", "symbol", "exchange_code", "currency",
                          "security_type", "fields", "provider", "receipt_time"})
_KIND_ORDER = {kind: index for index, kind in enumerate(ScreenerKind)}
PERCENT = Decimal(100)
PRICE, PRE_CLOSE = "price", "pre_close"  # Webull screener row keys (docs/webull-facts.md)
CONFLICTING = "CONFLICTING_ROWS: listed under an instrument with conflicting rows"
PAST_DAY = "only the latest {} is kept; it is not recorded per day"


def screener_path(capture: Path, day: date) -> Path | None:
    """The day's screener capture, plain or archived; None when there is none."""
    return existing(capture / day.isoformat() / SCREENER_FILE)


def _row(raw: Any) -> tuple[datetime, ScreenerRow]:
    if not isinstance(raw, dict) or set(raw) != _RECORD_KEYS:
        raise ValueError("not a captured screener record")
    texts = ("instrument_id", "symbol", "exchange_code", "currency")
    fields = raw["fields"]
    if (not all(isinstance(raw[k], str) for k in texts)
            or not isinstance(raw["security_type"], str | None)
            or not isinstance(raw["rank"], int) or isinstance(raw["rank"], bool)
            or not isinstance(fields, dict)
            or not all(isinstance(v, str) for v in fields.values())):
        raise ValueError("a screener record field has the wrong type")
    at = datetime.fromisoformat(raw["receipt_time"])
    if at.tzinfo is None:
        raise ValueError("receipt_time has no UTC offset")
    return at, ScreenerRow(ScreenerKind(raw["kind"]), raw["rank"], raw["instrument_id"],
                           raw["symbol"], raw["exchange_code"], raw["currency"],
                           raw["security_type"], fields)


def read_passes(path: Path, *, open_tail: bool) -> list[list[ScreenerRow]]:
    """The discovery passes of a captured screener.jsonl, oldest first. The rows of one
    screener read share a receipt time, and a pass reads the screeners in `ScreenerKind` order,
    so a pass starts at a read whose kind does not come after the previous read's. With
    `open_tail` a last line without its newline is not read yet. Raises CaptureError."""
    with open_text(path) as handle:  # plain or archived (.gz)
        lines = handle.readlines()
    if open_tail and lines and not lines[-1].endswith("\n"):
        lines.pop()
    passes: list[list[ScreenerRow]] = []
    previous: tuple[ScreenerKind, datetime] | None = None
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        try:
            at, row = _row(json.loads(text))
        except (ValueError, TypeError) as exc:  # JSONDecodeError is a ValueError
            raise CaptureError(f"{path}:{number}: {exc}") from exc
        read = (row.kind, at)
        if previous is None or (read != previous
                                and _KIND_ORDER[row.kind] <= _KIND_ORDER[previous[0]]):
            passes.append([])
        passes[-1].append(row)
        previous = read
    return passes


def change_pct(row: ScreenerRow, places: int) -> tuple[Decimal | None, str | None]:
    """(price - pre_close) / pre_close x 100 of the provider row, rounded half-even to
    `places`; (None, cause) when the row cannot give it."""
    values = []
    for key in (PRICE, PRE_CLOSE):
        text = row.fields.get(key)
        if text is None:
            return None, f"the captured row has no {key}"
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None, f"the captured {key} is not a number: {text!r}"
        if not value.is_finite():
            return None, f"the captured {key} is not a number: {text!r}"
        values.append(value)
    price, pre_close = values
    if pre_close <= 0:
        return None, f"the captured pre_close is not positive: {pre_close}"
    return ((price - pre_close) / pre_close * PERCENT).quantize(Decimal(1).scaleb(-places)), None


def chips(passes: Sequence[Sequence[ScreenerRow]], settings: DiscoverySettings,
          places: int) -> tuple[api.FlowChipV1, ...]:
    """One chip per symbol listed that day, most recently listed first (within a pass in read
    order: screener, then rank). The universe outcome and the change come from the latest
    pass and row that listed the symbol; `sources` from every pass."""
    sources: dict[str, set[ScreenerKind]] = {}
    latest: dict[str, ScreenerRow] = {}
    outcome: dict[str, str | None] = {}
    for rows in passes:
        instruments, rejections = merge(rows)
        found: dict[str, str | None] = {r.symbol: f"{r.cause}: {r.measured}"
                                        for r in rejections}
        for instrument in instruments:
            refusal = universe_cause(instrument, settings)
            found[instrument.symbol] = (None if refusal is None
                                        else f"{refusal.cause}: {refusal.measured}")
        for row in rows:
            sources.setdefault(row.symbol, set()).add(row.kind)
            latest[row.symbol] = row
            outcome[row.symbol] = found.get(row.symbol, CONFLICTING)
    order = dict.fromkeys(row.symbol for rows in reversed(passes) for row in rows)
    result = []
    for symbol in order:
        change, cause = change_pct(latest[symbol], places)
        result.append(api.FlowChipV1(
            symbol=symbol, sources=tuple(k for k in ScreenerKind if k in sources[symbol]),
            change_pct=change, change_cause=cause, excluded_cause=outcome[symbol]))
    return tuple(result)


def reconcile(day: date, today: date, status_path: Path,
              account_path: Path) -> api.FlowReconcileV1:
    """Agreement from the account file and fences from the status file, each only when that
    file was written on `day`; otherwise null with the cause."""
    agreement = read_at = mismatch = fences = None
    agreement_cause = fences_cause = None
    if day != today:
        agreement_cause = PAST_DAY.format("reconciliation")
        fences_cause = PAST_DAY.format("status")
    else:
        try:
            account = api.read_account(account_path)
        except FileNotFoundError:
            agreement_cause = "the bot has not written an account file"
        except (api.ContractError, OSError) as exc:
            agreement_cause = f"account file is invalid ({exc})"
        else:
            if ny_date_of(account.read_at) != day:
                agreement_cause = f"the latest broker read was on {ny_date_of(account.read_at)}"
            else:
                agreement = (api.Agreement.MATCH if account.matches_records
                             else api.Agreement.MISMATCH)
                read_at, mismatch = account.read_at, account.mismatch
        try:
            status = api.read_status(status_path)
        except FileNotFoundError:
            fences_cause = "the bot has not written a status file"
        except (api.ContractError, OSError) as exc:
            fences_cause = f"status file is invalid ({exc})"
        else:
            if ny_date_of(status.written_at) != day:
                fences_cause = f"the latest status was written on {ny_date_of(status.written_at)}"
            else:
                fences = status.fences
    return api.FlowReconcileV1(agreement, agreement_cause, read_at, mismatch, fences,
                               fences_cause)


def flow_document(environment: str, day: date, chip_list: tuple[api.FlowChipV1, ...],
                  records: tuple[DecisionRecord, ...], stops: tuple[api.FlowStopV1, ...],
                  reconciled: api.FlowReconcileV1) -> api.FlowV1:
    """`records`: the active strategy's records of `day`; `stops`: one per trade whose entry
    filled that day (`History.entries`)."""
    verified = [s.symbol for s in stops if s.state is api.FlowStopState.VERIFIED]
    stages = (
        (api.FlowStage.SCREENERS, [c.symbol for c in chip_list]),
        (api.FlowStage.UNIVERSE, [c.symbol for c in chip_list if c.excluded_cause is None]),
        (api.FlowStage.STRATEGY, [r.symbol for r in records]),
        (api.FlowStage.RISK, [r.symbol for r in records if r.decision.verdict is Verdict.ENTER
                              and r.admission is Admission.TAKEN]),
        (api.FlowStage.ORDER, [s.symbol for s in stops]),
        (api.FlowStage.STOP, verified),
    )
    return api.FlowV1(
        environment=environment, day=day, chips=chip_list,
        stages=tuple(api.FlowStageV1(stage, len(set(symbols)), tuple(sorted(set(symbols))))
                     for stage, symbols in stages),
        stops=stops, reconcile=reconciled)
