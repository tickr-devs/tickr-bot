"""GET /trades/whatif: what another strategy version would have done on one trade's symbol and
day. SIMULATED, read-only, offline (docs/api.md, "What another version would have done").

Inputs, all already on disk, nothing fetched and nothing written:
- the day's captured M1 bars of the symbol (`<capture_dir>/<environment>/<day>/bars.jsonl`);
- the prior sessions the bot cached for the symbol that day
  (`<history_dir>/<environment>/<day>/<symbol>-<[market_data] history_sessions>.jsonl`);
- the strategy folder under `strategies/` (its module from `[observers] strategy_modules`),
  `[api] replay_settings_file` and the session calendar with bot.toml `[session]`.

The version is replayed over those bars by the replay engine (`tickr.replay`, its fill and
exit rules, docs/replay.md), the prior sessions only as the strategy's history: only signals and
trades of `day` are answered. R is per share; USD is for the quantity the version itself chose,
gross (slippage in, commissions and fees out). Refused during the regular session of a trading
day when `[api] trade_whatif_outside_session_only = true`, to keep the CPU for the bot.

Every missing input is a `cause` with no numbers, never a partial answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from tickr.api import contract as api
from tickr.api.observer_results import NoInputs, SimulationPaths, day_bars, replay_inputs
from tickr.calendar import CalendarCause, CalendarRefused, SessionCalendar, ny_date_of
from tickr.config import Config
from tickr.data.capture import CapturedBarsDays
from tickr.observers.errors import ObserverError
from tickr.observers.loader import check_history, load_strategy, parse_modules
from tickr.replay.bars import load_bars
from tickr.replay.engine import SignalOutcome, replay
from tickr.replay.errors import ReplayError

ZERO = Decimal(0)
# Calendar answers that mean "no session now", so a what-if may run.
NO_SESSION = (CalendarCause.WEEKEND, CalendarCause.HOLIDAY)


class TradeWhatIfRefused(Exception):
    """Not computed now (in session); the message is the single cause."""


@dataclass(frozen=True)
class TradeWhatIfSettings:
    root: Path  # repo root: strategies/ lives here
    paths: SimulationPaths
    history: Path  # <data_dir>/<history_dir>/<environment>
    history_sessions: int  # [market_data] history_sessions: the cache file's session count
    modules: tuple[str, ...]  # [observers] strategy_modules
    outside_session_only: bool  # [api] trade_whatif_outside_session_only

    @classmethod
    def from_config(cls, cfg: Config) -> TradeWhatIfSettings:
        data = cfg.root / cfg.run.data_dir
        return cls(cfg.root, SimulationPaths.from_config(cfg),
                   data / cfg.market_data.history_dir / cfg.environment_name,
                   cfg.market_data.history_sessions, tuple(cfg.observers.strategy_modules),
                   cfg.api.trade_whatif_outside_session_only)


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places))


def _relative(settings: TradeWhatIfSettings, text: str) -> str:
    """`text` with the repo root taken out of its paths: no home folder leaves the Mac."""
    for root in {str(settings.root), str(settings.root.resolve())}:
        text = text.replace(root + "/", "")
    return text


def refuse_in_session(settings: TradeWhatIfSettings, now: datetime) -> None:
    if not settings.outside_session_only:
        return
    try:
        today = SessionCalendar.load(settings.paths.calendar).day_at(now)
    except OSError as exc:
        raise TradeWhatIfRefused(f"calendar unreadable: {exc}") from exc
    except CalendarRefused as exc:
        if exc.cause in NO_SESSION:
            return
        raise TradeWhatIfRefused(f"calendar: {exc}") from exc
    if today.in_regular_session(now):
        raise TradeWhatIfRefused("refused during the regular session "
                                 "([api] trade_whatif_outside_session_only)")


def what_if(settings: TradeWhatIfSettings, folder: str, symbol: str, day: date,
            now: datetime, bars: CapturedBarsDays) -> api.TradeWhatIfV1:
    """The version in strategies/<folder> replayed on `symbol`'s bars of `day`. Raises
    TradeWhatIfRefused in session; every other failure is the document's cause."""
    refuse_in_session(settings, now)

    def failed(cause: str, strategy: str | None = None,
               version: str | None = None) -> api.TradeWhatIfV1:
        return api.TradeWhatIfV1(day, symbol, folder, strategy, version, None, (), None, None,
                                 _relative(settings, cause))

    try:
        modules = parse_modules(settings.modules)
        loaded = load_strategy(settings.root, folder, modules)
        check_history(loaded, settings.history_sessions)
    except ObserverError as exc:
        return failed(str(exc))
    name, version = loaded.strategy.name, loaded.strategy.version
    try:
        today_bars = day_bars(bars, settings.paths, day, ny_date_of(now)).get(symbol, ())
        replay_settings, clock_for = replay_inputs(settings.paths)
    except NoInputs as exc:
        return failed(str(exc), name, version)
    if replay_settings.history_sessions != settings.history_sessions:
        return failed(f"replay settings history_sessions {replay_settings.history_sessions} "
                      f"differ from [market_data] history_sessions {settings.history_sessions}: "
                      "the version would not see the history the bot cached", name, version)
    today_bars = tuple(b for b in today_bars if ny_date_of(b.start) == day)
    if not today_bars:
        return failed(f"no captured bars for {symbol} on {day.isoformat()}", name, version)
    cache = settings.history / day.isoformat() / f"{symbol}-{settings.history_sessions}.jsonl"
    if not cache.exists():
        return failed(f"no cached prior sessions for {symbol} on {day.isoformat()}",
                      name, version)
    try:
        prior = load_bars([cache]).get(symbol, ())
    except (OSError, ReplayError, UnicodeDecodeError) as exc:
        return failed(f"cached prior sessions refused: {exc}", name, version)
    if not prior:
        return failed(f"cached prior sessions hold no bar of {symbol}", name, version)
    if prior and prior[-1].start >= today_bars[0].start:
        return failed("cached prior sessions overlap the day's bars", name, version)
    try:
        result = replay(lambda: load_strategy(settings.root, folder, modules).strategy,
                        {symbol: prior + today_bars}, replay_settings, clock_for)
    except (ReplayError, ObserverError) as exc:
        return failed(f"simulation refused: {exc}", name, version)
    except Exception as exc:  # noqa: BLE001 - a version's own bug is a cause, never a 500
        return failed(f"simulation failed: {type(exc).__name__}: {exc}", name, version)
    places = replay_settings.output_decimal_places
    trades = tuple(t for t in result.trades if t.session_date == day)
    signals = tuple(s for s in result.signals if s.session_date == day)
    counts = {outcome: 0 for outcome in SignalOutcome}
    for signal in signals:
        counts[signal.outcome] += 1
    rows = tuple(
        api.TradeWhatIfTradeV1(
            t.signal_bar_start, t.entry_time, _quantize(t.entry_price, places),
            _quantize(t.stop, places), t.exit_time, _quantize(t.exit_price, places),
            t.exit_reason, _quantize(t.r_multiple, places), t.quantity,
            None if t.pnl_usd is None else _quantize(t.pnl_usd, places))
        for t in trades)
    total_r = _quantize(sum((t.r_multiple for t in trades), ZERO), places)
    usd = [t.pnl_usd for t in trades]
    total_usd = None if any(u is None for u in usd) else _quantize(
        sum((u for u in usd if u is not None), ZERO), places)
    return api.TradeWhatIfV1(day, symbol, folder, name, version,
                             api.TradeWhatIfSignalsV1(
                                 counts[SignalOutcome.FILLED], counts[SignalOutcome.MISSED],
                                 counts[SignalOutcome.REJECTED], counts[SignalOutcome.IGNORED]),
                             rows, total_r, total_usd, None)
