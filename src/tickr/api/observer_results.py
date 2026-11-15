"""Simulated day result of each observer for GET /observers. SIMULATED, read-only.

The simulation is the research observers report's (`tickr.research.observer_report`, the
same one `python -m tickr.research observers` runs): every ENTER of an identity replayed
alone through the replay engine like `whatif`, an ENTER inside an earlier simulated trade of the
same symbol skipped, gross USD (slippage in, commissions out) for the recorded quantity. Inputs:
that day's decision records, its captured bars, `[api] replay_settings_file` and the session
calendar with bot.toml `[session]`. Nothing here reads the broker or writes a file.

Each observer also gets its simulated trades: every ENTER as the simulation replayed it (entry,
exit, result in USD and R, or the cause it has none), oldest first, at most `max_trades`.

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

from tickr.api.contract import ObserverResult, ObserverTradeV1
from tickr.archive import existing
from tickr.calendar import CalendarRefused, SessionCalendar
from tickr.config import Config, SessionConfig
from tickr.data.capture import BARS_FILE, CaptureError, read_captured_bars
from tickr.replay.clock import ClockFor, calendar_clock
from tickr.replay.errors import ReplayError
from tickr.replay.settings import ReplaySettings, load_settings
from tickr.research import observer_report
from tickr.research.errors import ResearchError
from tickr.research.records import DecisionRecord
from tickr.research.whatif import WhatIfOutcome
from tickr.strategy.base import Verdict
from tickr.types import Bar

ZERO = Decimal(0)
Identity = tuple[str, str]  # (strategy name, version)
_Stamp = tuple[str, int, int] | None


class NoInputs(Exception):
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


def stamp(path: Path) -> _Stamp:
    found = existing(path)
    if found is None:
        return None
    stat = found.stat()
    return (str(found), stat.st_mtime_ns, stat.st_size)


def folder_stamp(folder: Path) -> tuple[_Stamp, ...] | None:
    """Stamps of every file in `folder` (the calendar), or None when it cannot be listed."""
    try:
        return tuple(stamp(p) for p in sorted(folder.iterdir()) if p.is_file())
    except OSError:
        return None


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places))


def _gross(row: observer_report.IdentityReport, places: int) -> tuple[Decimal | None, str | None]:
    unsimulated = row.simulated.get(WhatIfOutcome.NOT_SIMULATED.value, 0)
    if unsimulated:
        return None, (f"{unsimulated} ENTER(s) could not be simulated: the decision bar is not "
                      "in the captured bars")
    if row.usd_trades != row.trades:
        return None, (f"{row.trades - row.usd_trades} simulated trade(s) have no recorded whole "
                      "quantity")
    return _quantize(row.total_usd, places), None


def _trade(enter: observer_report.SimulatedEnter, places: int) -> ObserverTradeV1:
    record, trade = enter.record, enter.trade
    if trade is None:
        return ObserverTradeV1(record.symbol, record.bar_start, None, None, None, None, None,
                               None, None, None, f"{enter.outcome}: {enter.cause}")
    usd = None if enter.result_usd is None else _quantize(enter.result_usd, places)
    quantity = None if usd is None else record.decision.quantity
    return ObserverTradeV1(
        record.symbol, record.bar_start, trade.entry_time, _quantize(trade.entry_price, places),
        trade.exit_time, _quantize(trade.exit_price, places), quantity, trade.exit_reason, usd,
        _quantize(trade.r_multiple, places),
        None if usd is not None else "no recorded whole quantity: result in R only")


def _result(row: observer_report.IdentityReport, enters: Sequence[observer_report.SimulatedEnter],
            places: int, max_trades: int) -> ObserverResult:
    gross, cause = _gross(row, places)
    ordered = sorted(enters, key=lambda e: (e.record.bar_start, e.record.symbol))
    try:
        trades = tuple(_trade(e, places) for e in ordered[:max_trades])
    except Exception as exc:  # noqa: BLE001 - display only: a cause, never a broken document
        return ObserverResult(gross, cause, None, f"{type(exc).__name__}: {exc}")
    return ObserverResult(gross, cause, trades, None, len(ordered) > max_trades)


class ObserverResults:
    """Thread safe; remembers the last day it simulated."""

    def __init__(self, paths: SimulationPaths, max_trades: int) -> None:
        self.paths = paths
        self.max_trades = max_trades  # [api] observer_trades_max_rows
        self._lock = threading.Lock()
        self._key: tuple[object, ...] | None = None
        self._value: dict[Identity, ObserverResult] = {}

    def results(self, day: date, today: date, records: Sequence[DecisionRecord],
                decision_file: Path) -> dict[Identity, ObserverResult]:
        """The result of every identity in `records` (observer records of `day`, read from
        `decision_file`). Remembered until any input file changes; an answer with a transient
        failure is never remembered."""
        key = inputs_key(self.paths, day, today, decision_file)
        with self._lock:
            if key == self._key:
                return self._value
        value, transient = self._compute(day, today, records)
        if not transient:
            with self._lock:
                self._key, self._value = key, value
        return value

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
                out[identity] = ObserverResult(ZERO, None)
        if not trading:
            return out, False
        try:
            bars = day_bars(self.paths, day, today)
            settings, clock_for = replay_inputs(self.paths)
            report = observer_report.report(
                [r for i in trading for r in groups[i]], bars, settings, clock_for)
        except NoInputs as exc:
            return out | {i: _failed(str(exc)) for i in trading}, exc.transient
        except (ReplayError, ResearchError) as exc:
            # A calendar refusal (date outside the calendar) may be fixed by a new file.
            return out | {i: _failed(f"simulation refused: {exc}") for i in trading}, True
        for row in report.identities:
            enters = report.enters[(row.strategy, row.version, row.role)]
            out[(row.strategy, row.version)] = _result(
                row, enters, settings.output_decimal_places, self.max_trades)
        return out, False


def _failed(cause: str) -> ObserverResult:
    return ObserverResult(None, cause, None, cause)


def bars_file(paths: SimulationPaths, day: date) -> Path:
    return paths.capture / day.isoformat() / BARS_FILE


def inputs_key(paths: SimulationPaths, day: date, today: date,
               decision_file: Path) -> tuple[object, ...]:
    """What a simulated answer of `day` depends on: it differs once any input file changed."""
    return (day, day == today, stamp(decision_file), stamp(bars_file(paths, day)),
            stamp(paths.replay_settings), folder_stamp(paths.calendar))


def load_calendar(paths: SimulationPaths) -> SessionCalendar:
    try:
        return SessionCalendar.load(paths.calendar)
    except (CalendarRefused, OSError) as exc:
        raise NoInputs(f"session calendar unreadable: {exc}", transient=True) from exc


def replay_inputs(paths: SimulationPaths) -> tuple[ReplaySettings, ClockFor]:
    try:
        settings = load_settings(paths.replay_settings)
    except ReplayError as exc:
        raise NoInputs(f"replay settings unreadable: {exc}") from exc
    except OSError as exc:
        raise NoInputs(f"replay settings unreadable: {exc}", transient=True) from exc
    return settings, calendar_clock(load_calendar(paths), paths.session)


def day_bars(paths: SimulationPaths, day: date, today: date) -> dict[str, tuple[Bar, ...]]:
    path = bars_file(paths, day)
    if existing(path) is None:
        raise NoInputs(f"no captured bars for {day.isoformat()}")
    try:
        # Today's file may be half way through a line the bot is writing.
        read = read_captured_bars(path, open_tail=day == today)
    except CaptureError as exc:
        raise NoInputs(f"capture file refused: {exc}") from exc
    except OSError as exc:
        raise NoInputs(f"capture file refused: {exc}", transient=True) from exc
    by_symbol: dict[str, list[Bar]] = defaultdict(list)
    for (symbol, _), bar in sorted(read.bars.items()):
        by_symbol[symbol].append(bar)
    return {symbol: tuple(bars) for symbol, bars in by_symbol.items()}
