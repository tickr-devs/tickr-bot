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
import threading
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr.api import contract as api
from tickr.archive import GZ_SUFFIX, existing, open_text
from tickr.calendar import ny_date_of
from tickr.data.base import ScreenerKind, ScreenerRow
from tickr.data.capture import SCREENER_FILE, CaptureError
from tickr.data.discovery import DiscoverySettings, merge, universe_cause
from tickr.research.records import Admission, DecisionRecord
from tickr.strategy.base import Verdict

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


class _Passes:
    """Splits captured screener lines into discovery passes as they arrive. The rows of one
    screener read share a receipt time, and a pass reads the screeners in `ScreenerKind`
    order, so a pass starts at a read whose kind does not come after the previous read's."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines = 0  # lines consumed, for error messages
        self._previous: tuple[ScreenerKind, datetime] | None = None

    def add(self, text: str, passes: list[list[ScreenerRow]]) -> bool:
        """Parse one line into `passes`; True when it opened a new pass. Raises CaptureError."""
        self.lines += 1
        if not text.strip():
            return False
        try:
            at, row = _row(json.loads(text))
        except (ValueError, TypeError) as exc:  # JSONDecodeError is a ValueError
            raise CaptureError(f"{self.path}:{self.lines}: {exc}") from exc
        read = (row.kind, at)
        previous = self._previous
        opened = previous is None or (read != previous
                                      and _KIND_ORDER[row.kind] <= _KIND_ORDER[previous[0]])
        if opened:
            passes.append([])
        passes[-1].append(row)
        self._previous = read
        return opened


def read_passes(path: Path, *, open_tail: bool) -> list[list[ScreenerRow]]:
    """The discovery passes of a captured screener.jsonl, oldest first (see `_Passes`). With
    `open_tail` a last line without its newline is not read yet. Raises CaptureError."""
    with open_text(path) as handle:  # plain or archived (.gz)
        lines = handle.readlines()
    if open_tail and lines and not lines[-1].endswith("\n"):
        lines.pop()
    passes: list[list[ScreenerRow]] = []
    splitter = _Passes(path)
    for text in lines:
        splitter.add(text, passes)
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


class _ChipState:
    """What `chips` needs from the passes folded so far: per symbol its screeners, its latest
    row, the universe outcome of its latest pass, and where it was last listed (pass number,
    first position in that pass) for the most-recently-listed-first order."""

    def __init__(self) -> None:
        self.sources: dict[str, set[ScreenerKind]] = {}
        self.latest: dict[str, ScreenerRow] = {}
        self.outcome: dict[str, str | None] = {}
        self.listed: dict[str, tuple[int, int]] = {}
        self.ranks: dict[str, dict[ScreenerKind, int]] = {}
        self.passes = 0

    def copy(self) -> _ChipState:
        state = _ChipState()
        state.sources = {k: set(v) for k, v in self.sources.items()}
        state.latest, state.outcome = dict(self.latest), dict(self.outcome)
        state.listed, state.passes = dict(self.listed), self.passes
        state.ranks = {k: dict(v) for k, v in self.ranks.items()}
        return state

    def fold(self, rows: Sequence[ScreenerRow], settings: DiscoverySettings) -> None:
        instruments, rejections = merge(rows)
        found: dict[str, str | None] = {r.symbol: f"{r.cause}: {r.measured}"
                                        for r in rejections}
        for instrument in instruments:
            refusal = universe_cause(instrument, settings)
            found[instrument.symbol] = (None if refusal is None
                                        else f"{refusal.cause}: {refusal.measured}")
        number = self.passes
        seen: set[str] = set()
        for position, row in enumerate(rows):
            self.sources.setdefault(row.symbol, set()).add(row.kind)
            self.ranks.setdefault(row.symbol, {})[row.kind] = row.rank
            self.latest[row.symbol] = row
            self.outcome[row.symbol] = found.get(row.symbol, CONFLICTING)
            if row.symbol not in seen:
                seen.add(row.symbol)
                self.listed[row.symbol] = (number, position)
        self.passes += 1

    def chips(self, places: int) -> tuple[api.FlowChipV1, ...]:
        order = sorted(self.listed, key=lambda s: (-self.listed[s][0], self.listed[s][1]))
        result = []
        for symbol in order:
            change, cause = change_pct(self.latest[symbol], places)
            sources = tuple(k for k in ScreenerKind if k in self.sources[symbol])
            result.append(api.FlowChipV1(
                symbol=symbol, sources=sources,
                change_pct=change, change_cause=cause, excluded_cause=self.outcome[symbol],
                ranks=tuple(api.SymbolSourceV1(k, self.ranks[symbol][k]) for k in sources)))
        return tuple(result)


def chips(passes: Sequence[Sequence[ScreenerRow]], settings: DiscoverySettings,
          places: int) -> tuple[api.FlowChipV1, ...]:
    """One chip per symbol listed that day, most recently listed first (within a pass in read
    order: screener, then rank). The universe outcome and the change come from the latest
    pass and row that listed the symbol; `sources` from every pass."""
    state = _ChipState()
    for rows in passes:
        state.fold(rows, settings)
    return state.chips(places)


class ScreenerChips:
    """`chips` of one day's captured screeners, read incrementally. The bot appends a pass
    about once a minute while the apps read GET /flow every few seconds; parsing and folding
    the whole day's capture on every change grew with the day (2026-11-16: 18 MB by 10:15 ET,
    seconds per read, GET /flow timing out under load). Now only the bytes appended since the
    last read are parsed and only finished passes are folded; the open last pass is folded on
    a copy. A file that is replaced, shrinks or is archived (its .gz twin) is read anew from
    the start. Raises CaptureError like `read_passes`."""

    def __init__(self, settings: DiscoverySettings, places: int) -> None:
        self.settings = settings
        self.places = places
        self._lock = threading.Lock()
        self._reset(None)

    def _reset(self, identity: tuple[object, ...] | None) -> None:
        self._identity = identity
        self._offset = 0  # bytes of the file consumed (complete lines only)
        self._state = _ChipState()  # finished passes
        self._open: list[list[ScreenerRow]] = []  # the last pass, still being written
        self._splitter: _Passes | None = None
        self._key: tuple[object, ...] | None = None
        self._chips: tuple[api.FlowChipV1, ...] = ()

    def chips(self, path: Path | None, open_tail: bool) -> tuple[api.FlowChipV1, ...]:
        if path is None:
            return chips([], self.settings, self.places)
        with self._lock:
            try:
                path = existing(path) or path  # archived meanwhile: its .gz twin
                stat = path.stat()
                identity = (str(path), stat.st_dev, stat.st_ino, open_tail)
                if identity != self._identity or stat.st_size < self._offset:
                    self._reset(identity)
                key = (stat.st_mtime_ns, stat.st_size)
                if key != self._key:
                    self._read(path, open_tail)
                    state = self._state.copy() if self._open else self._state
                    for rows in self._open:
                        state.fold(rows, self.settings)
                    self._chips = state.chips(self.places)
                    self._key = key
            except OSError as exc:
                raise CaptureError(str(exc)) from exc
            except CaptureError:
                self._reset(None)  # read the file anew next time
                raise
            return self._chips

    def _read(self, path: Path, open_tail: bool) -> None:
        if self._splitter is None:
            self._splitter = _Passes(path)
        if path.name.endswith(GZ_SUFFIX):  # archived: read once, never appended to
            if self._offset:
                return
            with open_text(path) as handle:
                lines = handle.readlines()
            self._offset = path.stat().st_size
        else:
            with open(path, "rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
            complete = data if not open_tail else data[:data.rfind(b"\n") + 1]
            try:
                lines = complete.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError as exc:
                raise CaptureError(f"{path}: {exc}") from exc
            self._offset += len(complete)
        for text in lines:
            if self._splitter.add(text, self._open) and len(self._open) > 1:
                finished = self._open.pop(0)
                self._state.fold(finished, self.settings)


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
