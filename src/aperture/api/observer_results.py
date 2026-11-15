"""Simulated day result of each observer for GET /observers. SIMULATED, read-only.

The simulation is the research observers report's (`aperture.research.observer_report`, the
same one `python -m aperture.research observers` runs): every ENTER of an identity replayed
alone through the replay engine like `whatif`, an ENTER inside an earlier simulated trade of the
same symbol skipped, gross USD (slippage in, commissions out) for the recorded quantity. Inputs:
that day's decision records, its captured bars, `[api] replay_settings_file` and the session
calendar with bot.toml `[session]`. Nothing here reads the broker or writes a file.

A result that cannot be computed is None with one cause, never 0: no capture file, a refused
capture file, unreadable settings or calendar, a refused simulation, an ENTER that could not be
simulated, a simulated trade without a recorded whole quantity. An identity with no ENTER
simulates no trade: its result is 0.

Each answer is kept in memory with the day, whether it was today, and the size and modification
time of its decision file, capture file, replay settings file and calendar files, so a client
polling the same day re-simulates only after one of them changed. An answer decided by a
failure that can pass without a file changing (an OSError, a calendar or simulation refusal) is
not kept. Today's result is provisional: a simulated trade still open ends at the last
captured bar's close.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from aperture.api.contract import ObserverResult
from aperture.archive import existing
from aperture.calendar import CalendarRefused, SessionCalendar
from aperture.config import Config, SessionConfig
from aperture.data.capture import BARS_FILE, CaptureError, read_captured_bars
from aperture.replay.clock import ClockFor, calendar_clock
from aperture.replay.errors import ReplayError
from aperture.replay.settings import ReplaySettings, load_settings
from aperture.research import observer_report
from aperture.research.errors import ResearchError
from aperture.research.records import DecisionRecord
from aperture.research.whatif import WhatIfOutcome
from aperture.strategy.base import Verdict
from aperture.types import Bar

ZERO = Decimal(0)
Identity = tuple[str, str]  # (strategy name, version)
_Stamp = tuple[str, int, int] | None


class _NoInputs(Exception):
    """A simulation input is missing or refused; the message is the single cause. `transient`:
    the failure may pass without any file changing (an OSError, a calendar refusal), so its
    answer is not remembered."""

    def __init__(self, cause: str, transient: bool = False) -> None:
        super().__init__(cause)
        self.transient = transient


@dataclass(frozen=True)
class SimulationPaths:
    capture: Path  # <capture_dir>/<environment>
    replay_settings: Path  # config/replay.toml
    calendar: Path  # folder of the session calendar files
    session: SessionConfig

    @classmethod
    def from_config(cls, config: Config) -> SimulationPaths:
        data = config.root / config.run.data_dir
        return cls(data / config.market_data.capture_dir / config.environment_name,
                   config.root / config.api.replay_settings_file,
                   (config.root / config.session.calendar_file).parent, config.session)


def _stamp(path: Path) -> _Stamp:
    found = existing(path)
    if found is None:
        return None
    stat = found.stat()
    return (str(found), stat.st_mtime_ns, stat.st_size)


def _folder_stamp(folder: Path) -> tuple[_Stamp, ...] | None:
    """Stamps of every file in `folder` (the calendar), or None when it cannot be listed."""
    try:
        return tuple(_stamp(p) for p in sorted(folder.iterdir()) if p.is_file())
    except OSError:
        return None


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places))


def _result(row: observer_report.IdentityReport, places: int) -> ObserverResult:
    unsimulated = row.simulated.get(WhatIfOutcome.NOT_SIMULATED.value, 0)
    if unsimulated:
        return None, (f"{unsimulated} ENTER(s) could not be simulated: the decision bar is not "
                      "in the captured bars")
    if row.usd_trades != row.trades:
        return None, (f"{row.trades - row.usd_trades} simulated trade(s) have no recorded whole "
                      "quantity")
    return _quantize(row.total_usd, places), None


class ObserverResults:
    """Thread safe; remembers the last day it simulated."""

    def __init__(self, paths: SimulationPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()
        self._key: tuple[object, ...] | None = None
        self._value: dict[Identity, ObserverResult] = {}

    def results(self, day: date, today: date, records: Sequence[DecisionRecord],
                decision_file: Path) -> dict[Identity, ObserverResult]:
        """The result of every identity in `records` (observer records of `day`, read from
        `decision_file`). Remembered until any input file changes; an answer with a transient
        failure is never remembered."""
        key = (day, day == today, _stamp(decision_file), _stamp(self._bars_file(day)),
               _stamp(self.paths.replay_settings), _folder_stamp(self.paths.calendar))
        with self._lock:
            if key == self._key:
                return self._value
        value, transient = self._compute(day, today, records)
        if not transient:
            with self._lock:
                self._key, self._value = key, value
        return value

    def _bars_file(self, day: date) -> Path:
        return self.paths.capture / day.isoformat() / BARS_FILE

    def _compute(self, day: date, today: date, records: Sequence[DecisionRecord],
                 ) -> tuple[dict[Identity, ObserverResult], bool]:
        """(result per identity, True when a transient failure decided it)."""
        groups: dict[Identity, list[DecisionRecord]] = defaultdict(list)
        for record in records:
            groups[(record.decision.strategy, record.decision.version)].append(record)
        out: dict[Identity, ObserverResult] = {}
        trading = []
        for identity, group in groups.items():
            if any(r.decision.verdict is Verdict.ENTER for r in group):
                trading.append(identity)
            else:
                out[identity] = (ZERO, None)
        if not trading:
            return out, False
        try:
            bars = self._bars(day, today)
            settings, clock_for = self._settings()
            report = observer_report.report(
                [r for i in trading for r in groups[i]], bars, settings, clock_for)
        except _NoInputs as exc:
            return out | {i: (None, str(exc)) for i in trading}, exc.transient
        except (ReplayError, ResearchError) as exc:
            # A calendar refusal (date outside the calendar) may be fixed by a new file.
            return out | {i: (None, f"simulation refused: {exc}") for i in trading}, True
        for row in report.identities:
            out[(row.strategy, row.version)] = _result(row, settings.output_decimal_places)
        return out, False

    def _settings(self) -> tuple[ReplaySettings, ClockFor]:
        try:
            settings = load_settings(self.paths.replay_settings)
        except ReplayError as exc:
            raise _NoInputs(f"replay settings unreadable: {exc}") from exc
        except OSError as exc:
            raise _NoInputs(f"replay settings unreadable: {exc}", transient=True) from exc
        try:
            calendar = SessionCalendar.load(self.paths.calendar)
        except (CalendarRefused, OSError) as exc:
            raise _NoInputs(f"session calendar unreadable: {exc}", transient=True) from exc
        return settings, calendar_clock(calendar, self.paths.session)

    def _bars(self, day: date, today: date) -> dict[str, tuple[Bar, ...]]:
        path = self._bars_file(day)
        if existing(path) is None:
            raise _NoInputs(f"no captured bars for {day.isoformat()}")
        try:
            # Today's file may be half way through a line the bot is writing.
            read = read_captured_bars(path, open_tail=day == today)
        except CaptureError as exc:
            raise _NoInputs(f"capture file refused: {exc}") from exc
        except OSError as exc:
            raise _NoInputs(f"capture file refused: {exc}", transient=True) from exc
        by_symbol: dict[str, list[Bar]] = defaultdict(list)
        for (symbol, _), bar in sorted(read.bars.items()):
            by_symbol[symbol].append(bar)
        return {symbol: tuple(bars) for symbol, bars in by_symbol.items()}
