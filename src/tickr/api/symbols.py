"""GET /symbols and GET /movers: every symbol of a day through the pipeline, and its fast movers.

Read-only. Sources (docs/api.md, "GET /symbols", "GET /movers"):
- the day's captured `discovery.jsonl` (one line per discovery pass, `data.discovery.pass_record`):
  first seen, the discovery stages reached (screeners, universe, quote, bars), the latest pass's
  outcome with its cause, and the fast movers;
- the day's screener chips (`flow.ScreenerChips`): screeners, ranks and the change;
- the decision file (active strategy): the strategy and risk stages;
- the bot database (`History.entries`): the order and stop stages;
- the active strategy's strategy.toml `[spike]`: the fast-mover thresholds.

The capture is read incrementally like the screener chips: only lines appended since the last
read are parsed and folded, so a request costs the new passes, not the whole day.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tickr.api import contract as api
from tickr.api.causes import RowRefused, safe_cause
from tickr.api.reasons import TENTH_EXPONENT
from tickr.archive import GZ_SUFFIX, existing, open_text
from tickr.data.capture import DISCOVERY_FILE, CaptureError
from tickr.data.discovery import StopCause
from tickr.observers.errors import ObserverError
from tickr.research.records import Admission, DecisionRecord
from tickr.strategy.base import Check, Verdict

S = api.SymbolStage
ORDER = tuple(S)  # pipeline order
NO_PASS = "no discovery pass recorded for this day"
NO_RULES = "the active strategy defines no fast movers ([spike] in its strategy.toml)"
SIGNALS = ("volume_ratio", "move_atr", "dollar_volume_ratio")
# Where a refusal of each discovery stage leaves the symbol (data.discovery.Stage values).
_DROPPED_AT = {"screener": S.UNIVERSE, "snapshot": S.QUOTE, "bars": S.BARS}
NEVER_VERIFIED = "never_verified"


def discovery_path(capture: Path, day: date) -> Path | None:
    """The day's discovery capture, plain or archived; None when there is none."""
    return existing(capture / day.isoformat() / DISCOVERY_FILE)


def _later(a: S, b: S) -> S:
    return a if ORDER.index(a) >= ORDER.index(b) else b


def _decimal(value: Any, where: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise RowRefused(f"{where} is not a number: {value!r}") from exc


def _time(value: Any, where: str) -> datetime:
    if not isinstance(value, str):
        raise RowRefused(f"{where} is not a time")
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise RowRefused(f"{where} has no UTC offset")
    return at


# A platform cause's age in seconds as the bot writes it ("9.957056s", "<= 5.0s").
_SECONDS = re.compile(r"(-?[0-9]+(?:\.[0-9]+)?)s\b")
_SIGNS = (("<=", "\u2264"), (">=", "\u2265"))


def _seconds(match: re.Match[str]) -> str:
    """An age to a tenth of a second, as every shown age ("10.0 s", never "9.957056s")."""
    tenth = Decimal(match.group(1)).quantize(Decimal(1).scaleb(TENTH_EXPONENT), ROUND_HALF_UP)
    return f"{tenth} s"


def _readable(text: str) -> str:
    """A discovery pass's measured value or threshold as a person reads it: ages to a tenth of a
    second and the signs as signs (owner, 2026-11-18: "9.957056s (needs <= 5.0s)")."""
    text = _SECONDS.sub(_seconds, text)
    for written, sign in _SIGNS:
        text = text.replace(written, sign)
    return text


def _detail(measured: Any, threshold: Any) -> str:
    """"10.0 s (needs \u2264 5.0 s)": what a discovery pass measured against what it needs."""
    return f"{_readable(str(measured))} (needs {_readable(str(threshold))})"


# Why a pass stopped before deciding a symbol (`data.discovery.StopCause`), as the code a row's
# `cause` carries; the stage is the row's own.
_STOP_CAUSES = {StopCause.BUDGET.value: api.NotReachedCause.REQUEST_BUDGET,
                StopCause.DELAYED_DATA.value: api.NotReachedCause.DELAYED_DATA}


def _stop_cause(raw: Any) -> api.NotReachedCause:
    try:
        return _STOP_CAUSES[raw]
    except (KeyError, TypeError):
        raise RowRefused(f"unknown stop cause {raw!r}") from None


def _check(raw: Any) -> Check | None:
    if raw is None:
        return None
    measured, threshold = raw["measured"], raw["threshold"]
    return Check(raw["rule"], None if measured is None else _decimal(measured, "check"),
                 raw["operator"], None if threshold is None else _decimal(threshold, "check"),
                 bool(raw["passed"]), raw["note"])


@dataclass
class _Outcome:
    """A symbol's state in one discovery pass."""

    stage: S
    state: api.SymbolState
    at: datetime | None
    cause: str | None = None
    detail: str | None = None
    check: Check | None = None


@dataclass(frozen=True)
class _Mover:
    """A fast mover's latest bar, parsed when its pass is folded."""

    at: datetime
    close: Decimal
    bar_dollar_volume: Decimal
    volume_ratio: Decimal
    move_atr: Decimal
    dollar_volume_ratio: Decimal
    fired: tuple[str, ...]
    spike_up: bool

    @staticmethod
    def parse(m: dict[str, Any]) -> _Mover:
        for signal in SIGNALS:
            if not isinstance(m[signal]["fired"], bool):
                raise RowRefused(f"{signal} fired is not true or false")
        if not isinstance(m["spike_up"], bool):
            raise RowRefused("spike_up is not true or false")
        return _Mover(
            _time(m["bar_start"], "bar_start"), _decimal(m["close"], "close"),
            _decimal(m["bar_dollar_volume"], "bar_dollar_volume"),
            *(_decimal(m[s]["value"], s) for s in SIGNALS),
            tuple(s for s in SIGNALS if m[s]["fired"]), m["spike_up"])


@dataclass
class DiscoveryFolded:
    """What the passes folded so far say, per symbol."""

    first_seen: dict[str, datetime] = field(default_factory=dict)
    furthest: dict[str, S] = field(default_factory=dict)
    reached: dict[str, dict[S, datetime]] = field(default_factory=dict)  # first time per stage
    latest: dict[str, _Outcome] = field(default_factory=dict)
    movers: dict[str, _Mover] = field(default_factory=dict)
    checked: dict[str, datetime] = field(default_factory=dict)  # of the latest pass record
    measured: bool | None = None  # of the latest pass
    as_of: datetime | None = None

    def fold(self, raw: dict[str, Any]) -> None:
        started = _time(raw["started_at"], "started_at")
        finished = _time(raw["finished_at"], "finished_at")

        def reach(symbol: str, stage: S) -> None:
            self.first_seen.setdefault(symbol, started)
            self.furthest[symbol] = _later(self.furthest.get(symbol, S.SCREENERS), stage)
            times = self.reached.setdefault(symbol, {})
            times.setdefault(S.SCREENERS, started)
            for passed in ORDER[1:ORDER.index(stage) + 1]:  # judged in this pass
                times.setdefault(passed, finished)

        for instrument in raw["instruments"]:
            reach(instrument["symbol"], S.SCREENERS)
        for r in raw["rejections"]:
            stage = _DROPPED_AT[r["stage"]]
            reach(r["symbol"], stage)
            check = _check(r["check"])
            self.latest[r["symbol"]] = _Outcome(
                stage, api.SymbolState.DROPPED, finished, r["cause"],
                None if check is not None else _detail(r["measured"], r["threshold"]),
                check)
        for symbol in raw["candidates"]:
            reach(symbol, S.STRATEGY)
            self.latest[symbol] = _Outcome(S.STRATEGY, api.SymbolState.WAITING, finished)
        stopped = raw["stopped"]
        cause = None if stopped is None else _stop_cause(stopped["cause"])
        for symbol in raw["not_reached"]:
            stage = S.BARS if stopped is not None and stopped["stage"] == "bars" else S.QUOTE
            reach(symbol, stage)
            # The code only: the pass's own text (the broker route and its wait) stays in
            # discovery.jsonl and never reaches a screen (owner: no raw API text in the apps).
            self.latest[symbol] = _Outcome(stage, api.SymbolState.NOT_REACHED, finished, cause)
        for m in raw["movers"]:
            if not isinstance(m["symbol"], str):
                raise RowRefused("a mover's symbol is not text")
            self.movers[m["symbol"]] = _Mover.parse(m)
        # Records written before the check times were kept carry none: the old ones stay.
        if "checked_symbols" in raw:
            self.checked = {c["symbol"]: _time(c["checked_at"], "checked_at")
                            for c in raw["checked_symbols"]}
        if not isinstance(raw["movers_measured"], bool):
            raise RowRefused("movers_measured is not true or false")
        self.measured = raw["movers_measured"]
        self.as_of = finished

    def snapshot(self) -> DiscoveryFolded:
        """A copy whose maps a later fold never changes (their values are never mutated)."""
        return DiscoveryFolded(dict(self.first_seen), dict(self.furthest),
                               {k: dict(v) for k, v in self.reached.items()}, dict(self.latest),
                               dict(self.movers), dict(self.checked), self.measured,
                               self.as_of)


class DiscoveryDay:
    """The fold of one day's `discovery.jsonl`, read incrementally (see the module docstring).
    A file that is replaced, shrinks or is archived is read anew. Raises CaptureError."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset(None)

    def _reset(self, identity: tuple[object, ...] | None) -> None:
        self._identity = identity
        self._offset = 0
        self._lines = 0
        self._day = DiscoveryFolded()

    def read(self, path: Path | None, open_tail: bool) -> DiscoveryFolded:
        if path is None:
            return DiscoveryFolded()
        with self._lock:
            try:
                path = existing(path) or path
                stat = path.stat()
                identity = (str(path), stat.st_dev, stat.st_ino, open_tail)
                if identity != self._identity or stat.st_size < self._offset:
                    self._reset(identity)
                self._read(path, open_tail)
            except OSError as exc:
                raise CaptureError(f"capture file unreadable ({type(exc).__name__})") from exc
            except CaptureError:
                self._reset(None)
                raise
            return self._day.snapshot()  # requests run on threads: never hand out the live fold

    def _read(self, path: Path, open_tail: bool) -> None:
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
                raise CaptureError(f"capture file is not UTF-8 ({type(exc).__name__})") from exc
            self._offset += len(complete)
        for text in lines:
            self._lines += 1
            if not text.strip():
                continue
            try:
                self._day.fold(json.loads(text))
            except (ValueError, TypeError, KeyError) as exc:
                raise CaptureError(f"line {self._lines}: {safe_cause(exc)}") from exc


def _decision_state(record: DecisionRecord, places: int) -> _Outcome:
    d = record.decision
    if d.verdict is Verdict.IGNORE:
        return _Outcome(S.STRATEGY, api.SymbolState.DROPPED, record.time, Verdict.IGNORE.value,
                        check=d.decisive)
    if d.verdict is Verdict.ENTER:
        if record.admission is Admission.TAKEN:
            return _Outcome(S.ORDER, api.SymbolState.WAITING, record.time)
        return _Outcome(S.RISK, api.SymbolState.DROPPED, record.time, record.admission.value,
                        record.refusal_cause)
    return _Outcome(S.STRATEGY, api.SymbolState.WAITING, record.time)  # WATCH, HOLD, EXIT


def symbols_document(day: date, folded: DiscoveryFolded, chips: Sequence[api.FlowChipV1],
                     records: Sequence[DecisionRecord], stops: Sequence[api.FlowStopV1],
                     places: int) -> api.SymbolsV1:
    """`records`: the active strategy's records of `day`; `stops`: one per trade whose entry
    filled that day."""
    in_order = sorted(records, key=lambda r: r.time)
    latest_record = {r.symbol: r for r in in_order}
    first_record: dict[str, datetime] = {}
    first_taken: dict[str, datetime] = {}
    for r in in_order:
        first_record.setdefault(r.symbol, r.time)
        if r.decision.verdict is Verdict.ENTER and r.admission is Admission.TAKEN:
            first_taken.setdefault(r.symbol, r.time)
    taken = set(first_taken)
    stop_of = {s.symbol: s for s in stops}
    chip_of = {c.symbol: c for c in chips}
    symbols = sorted(set(chip_of) | set(folded.furthest) | set(latest_record) | set(stop_of))
    rows = []
    for symbol in symbols:
        furthest = folded.furthest.get(symbol, S.SCREENERS)
        record = latest_record.get(symbol)
        if record is not None:
            furthest = _later(furthest, S.STRATEGY)
        if symbol in taken:
            furthest = _later(furthest, S.RISK)
        now = folded.latest.get(symbol)
        if record is not None and (now is None or record.time >= now.at
                                   or now.state is api.SymbolState.WAITING):
            now = _decision_state(record, places)
        stop = stop_of.get(symbol)
        if stop is not None:
            furthest = _later(furthest, S.ORDER)
            # Each stage at its own journal time (the fill, the stop seen working), never the
            # time of a later decision on the symbol (owner test 2026-11-18: two trades of a
            # day both showed 12:22, the minute of a later HOLD).
            if stop.state is api.FlowStopState.VERIFIED:
                furthest = S.STOP
                now = _Outcome(S.STOP, api.SymbolState.PASSED, stop.stop_confirmed_at)
            elif stop.state is api.FlowStopState.PENDING:
                now = _Outcome(S.STOP, api.SymbolState.WAITING, stop.filled_at)
            else:
                now = _Outcome(S.STOP, api.SymbolState.DROPPED, stop.filled_at,
                               stop.state.value)
        if now is None:  # listed by a screener after the latest recorded pass
            now = _Outcome(S.UNIVERSE, api.SymbolState.NOT_REACHED, None, NO_PASS)
        furthest = _later(furthest, now.stage)  # where it stands now, it got to
        chip = chip_of.get(symbol)
        times = {**folded.reached.get(symbol, {})}
        if symbol in first_record:
            times.setdefault(S.STRATEGY, first_record[symbol])
        if symbol in first_taken:
            times.setdefault(S.RISK, first_taken[symbol])
        if stop is not None:
            if stop.filled_at is not None:
                times.setdefault(S.ORDER, stop.filled_at)
            if stop.stop_confirmed_at is not None:
                times.setdefault(S.STOP, stop.stop_confirmed_at)
        reached = tuple(api.SymbolReachV1(stage, times.get(stage))
                        for stage in ORDER[:ORDER.index(furthest) + 1])
        rows.append(api.SymbolRowV1(
            symbol=symbol, sources=chip.ranks if chip else (),
            change_pct=chip.change_pct if chip else None,
            first_seen=folded.first_seen.get(symbol), furthest=furthest, stage=now.stage,
            state=now.state, cause=now.cause,
            check=None if now.check is None else api.shown_check(now.check, places),
            detail=now.detail, verdict=None if record is None else record.decision.verdict,
            setup=None if record is None else record.decision.setup, at=now.at,
            price=chip.price if chip else None, reached=reached,
            decided_at=None if record is None else record.time,
            strategy=None if record is None
            else f"{record.decision.strategy}@{record.decision.version}",
            checks=() if record is None
            else tuple(api.shown_check(c, places) for c in record.decision.checks),
            checked_at=folded.checked.get(symbol)))
    counts = tuple(api.SymbolStageCountV1(
        stage, sum(1 for r in rows if ORDER.index(r.furthest) >= ORDER.index(stage)))
        for stage in ORDER)
    return api.SymbolsV1(day, folded.as_of, None if folded.as_of else NO_PASS, counts,
                         tuple(rows))


def movers_document(day: date, folded: DiscoveryFolded, rules: api.FastMoverRulesV1 | None,
                    records: Sequence[DecisionRecord]) -> api.MoversV1:
    """Every fast mover of the day, newest bar first, then by symbol."""
    latest = {r.symbol: r for r in sorted(records, key=lambda r: r.time)}
    movers = []
    for symbol, m in folded.movers.items():
        record = latest.get(symbol)
        movers.append(api.FastMoverV1(
            symbol=symbol, at=m.at, close=m.close, bar_dollar_volume=m.bar_dollar_volume,
            volume_ratio=m.volume_ratio, move_atr=m.move_atr,
            dollar_volume_ratio=m.dollar_volume_ratio, fired=m.fired, spike_up=m.spike_up,
            watching=record is not None and record.decision.verdict is Verdict.WATCH))
    movers.sort(key=lambda m: (-m.at.timestamp(), m.symbol))
    return api.MoversV1(day, rules, None if rules else NO_RULES, tuple(movers))


def mover_rules(strategy_toml: dict[str, Any]) -> api.FastMoverRulesV1 | None:
    """The `[spike]` thresholds of a parsed strategy.toml, or None when it has none.
    ObserverError when the section is there but unreadable."""
    spike = strategy_toml.get("spike")
    if spike is None:
        return None
    try:
        return api.FastMoverRulesV1(
            int(spike["lookback_bars"]), _decimal(str(spike["min_volume_ratio"]), "spike"),
            _decimal(str(spike["min_move_atr"]), "spike"),
            _decimal(str(spike["min_dollar_volume_ratio"]), "spike"),
            _decimal(str(spike["min_bar_dollar_volume_usd"]), "spike"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ObserverError("[spike] of the active strategy unreadable "
                            f"({type(exc).__name__})") from exc
