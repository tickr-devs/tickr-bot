"""The bot service behind `aperture run`: keep what the engine owns protected, act on operator
commands, flatten at the session's flatten time, write status.json, drain on SIGTERM.

With an evaluator the active strategy runs once per completed minute and its ENTER decisions
go through the entry desk; without one (tests, tools) the service never opens a trade and
status says so as the entry-blocking reason.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from aperture.api import contract as api
from aperture.calendar import NEW_YORK, CalendarRefused, SessionCalendar
from aperture.config import Config, trading_gate
from aperture.engine.core import Engine, Fence, Leg, OrderRow, SendState, TradeRow
from aperture.engine.loop import drain
from aperture.engine.manual import flatten_deadline
from aperture.runtime.alerts import AlertKind
from aperture.runtime.dayend import DayEnd

if TYPE_CHECKING:
    from aperture.observers.runner import ObserverSet
    from aperture.runtime.evaluation import Evaluator
    from aperture.runtime.market import MarketRuntime
    from aperture.runtime.watch import Watch

STRATEGIES_DIR = "strategies"
STRATEGY_FILE = "strategy.toml"
COMMAND = "command"
COMMAND_FILE_REFUSED = "command-file-refused"
COMMAND_EFFECT = "command-effect"
OBSERVER_FAILURE = "observer-failure"
OBSERVERS_STOPPED = "observers-stopped"
OBSERVERS_NOT_LOADED = "observers-not-loaded"
EVALUATION_FAILED = "evaluation-failed"
STRATEGY_NOT_LOADED = "strategy-not-loaded"
NOT_WIRED = api.EntryBlock("entries_not_wired",
                           "This build does not open trades on its own yet.")
ZERO = Decimal(0)


@dataclass(frozen=True)
class ServicePaths:
    status: Path
    inbox: Path
    processed: Path

    @classmethod
    def from_config(cls, config: Config) -> ServicePaths:
        data = config.root / config.run.data_dir
        commands = data / config.api.commands_dir
        return cls(data / config.api.status_file, commands / api.INBOX,
                   commands / api.PROCESSED)


def strategy_id(config: Config) -> api.StrategyId:
    """Name and version of the active strategy, from its strategy.toml."""
    path = config.root / STRATEGIES_DIR / config.strategy.active / STRATEGY_FILE
    with path.open("rb") as handle:
        section = tomllib.load(handle)["strategy"]
    return api.StrategyId(str(section["name"]), str(section["version"]))


def load_calendar(config: Config) -> SessionCalendar | None:
    try:
        return SessionCalendar.load((config.root / config.session.calendar_file).parent)
    except CalendarRefused:
        return None


def _iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _average(orders: list[OrderRow]) -> Decimal | None:
    filled = [o for o in orders if o.filled_quantity > 0 and o.average_fill_price is not None]
    quantity = sum((o.filled_quantity for o in filled), ZERO)
    if quantity == 0:
        return None
    return sum((o.filled_quantity * o.average_fill_price for o in filled), ZERO) / quantity


def trades_today(engine: Engine, now: datetime) -> tuple[api.Trade, ...]:
    """Trades closed today (New York date) that had fills, with gross result."""
    today = now.astimezone(NEW_YORK).date()
    result = []
    for row in engine.store.query(
            "SELECT trade_id, symbol, opened_at, closed_at, close_cause FROM trades "
            "WHERE closed_at IS NOT NULL ORDER BY closed_at"):
        closed = _iso(row["closed_at"])
        if closed.astimezone(NEW_YORK).date() != today:
            continue
        orders = engine.orders(row["trade_id"])
        entries = [o for o in orders if o.leg is Leg.ENTRY]
        sells = [o for o in orders if o.leg is not Leg.ENTRY]
        entry_price, exit_price = _average(entries), _average(sells)
        quantity = sum((o.filled_quantity for o in sells), ZERO)
        if entry_price is None or exit_price is None or quantity == 0:
            continue  # never filled: not a trade
        result.append(api.Trade(row["symbol"], quantity, entry_price, exit_price,
                                _iso(row["opened_at"]), closed, row["close_cause"],
                                (exit_price - entry_price) * quantity))
    return tuple(result)


class Service:
    def __init__(self, engine: Engine, *, now: Callable[[], datetime],
                 calendar: SessionCalendar | None, strategy: api.StrategyId,
                 paths: ServicePaths, market: MarketRuntime | None = None,
                 evaluator: Evaluator | None = None, entries_off: str | None = None,
                 observers: ObserverSet | None = None,
                 watch: Watch | None = None) -> None:
        self.engine = engine
        self.market = market
        self.evaluator = evaluator
        self.entries_off = entries_off  # why there is no evaluator (status says so)
        self.observers = observers
        self.watch = watch
        self.config = engine.config
        self.now = now
        self.calendar = calendar
        self.strategy = strategy
        self.paths = paths
        self.state = api.BotState.STARTING
        self.last_broker_read_at: datetime | None = None
        self._flatten_day: date | None = None
        self._flatten_at: datetime | None = None
        self._flatten_passed = False  # the flatten time of _flatten_day has been reached
        self.day_end = DayEnd(engine, calendar, watch)

    # --- commands --------------------------------------------------------------------------

    def _seen(self, command_id: str) -> bool:
        return bool(self.engine.store.journal(COMMAND, command_id))

    def process_commands(self) -> None:
        """Journal, act, retire (docs/api.md). Commands run in request order, so of two
        entry-stop commands the later one wins. A command already journaled (crash before
        retire) is not acted on again, except a cancel-own-orders whose effect was not yet
        recorded: cancelling entries is idempotent, so it is finished instead of lost."""
        store = self.engine.store
        items = api.read_commands(self.paths.inbox, self.now(),
                                  self.config.api.command_max_age_seconds)
        for item in items:
            command = item.command
            if command is None:
                with store.transaction():
                    store.append(COMMAND_FILE_REFUSED,
                                 {"file": item.path.name, "cause": item.refusal},
                                 run_id=self.engine.run_id)
            elif not self._seen(command.command_id):
                self._act(command)
            elif (command.kind is api.CommandKind.CANCEL_OWN_ORDERS
                  and not self.engine.store.journal(COMMAND_EFFECT, command.command_id)):
                self._cancel_entries(command)
            api.retire_command(item, self.paths.processed)

    def _record(self, command: api.CommandV1, outcome: api.CommandOutcome,
                cause: str | None) -> None:
        self.engine.store.append(COMMAND, {
            "kind": command.kind, "requested_at": api.format_time(command.requested_at),
            "source": command.source, "outcome": outcome, "cause": cause},
            key=command.command_id, run_id=self.engine.run_id)

    def _act(self, command: api.CommandV1) -> None:
        engine, store = self.engine, self.engine.store
        kind = command.kind
        if kind in (api.CommandKind.ENTRY_STOP_ON, api.CommandKind.ENTRY_STOP_OFF):
            with store.transaction():
                self._record(command, api.CommandOutcome.DONE, None)
                engine.entry_stop_row(kind is api.CommandKind.ENTRY_STOP_ON,
                                      f"operator command {command.command_id}")
            return
        trades = engine.trades()
        if kind is api.CommandKind.CANCEL_OWN_ORDERS:
            with store.transaction():
                self._record(command, api.CommandOutcome.DONE, None)
            self._cancel_entries(command)
            return
        # FLATTEN: through the normal exit path; refused while a close waits for the operator.
        refused = [t.trade_id for t in trades
                   if f"{Fence.EXIT_REFUSED}:{t.trade_id}" in engine.fences()]
        with store.transaction():  # the record and the exit requests are one write
            if refused:
                self._record(command, api.CommandOutcome.REFUSED,
                             f"closing order refused before for {', '.join(refused)}: "
                             "look in the Webull app, then clear the fence")
            else:
                self._record(command, api.CommandOutcome.DONE,
                             None if trades else "nothing open")
                for trade in trades:
                    engine.exit_request_row(trade.trade_id, "operator-flatten")

    def _cancel_entries(self, command: api.CommandV1) -> None:
        asked = self.engine.cancel_entries()
        with self.engine.store.transaction():
            self.engine.store.append(COMMAND_EFFECT, {"entries_cancel_requested": asked},
                                     key=command.command_id, run_id=self.engine.run_id)

    # --- session timer ---------------------------------------------------------------------

    def flatten_due(self) -> None:
        """At the session's flatten time (or at once with no usable calendar or session)
        ask every open trade to exit through the normal path; from the flatten time plus
        grace, check by broker reads that the day ended flat (runtime/dayend.py)."""
        now = self.now()
        day = now.astimezone(NEW_YORK).date()
        if self._flatten_day != day:
            self._flatten_day = day
            self._flatten_passed = False
            self._flatten_at = (now if self.calendar is None
                                else flatten_deadline(self.config, self.calendar, now))
        if self._flatten_at is None or now < self._flatten_at:
            return
        if not self._flatten_passed:
            self._flatten_passed = True
            self.engine.broker_view = None  # only reads from now on can show the day's end
        for trade in self.engine.trades():
            if not trade.exit_requested:
                self.engine.request_exit(trade.trade_id, "session-flatten")
        self.day_end.check(self._flatten_at, now)

    # --- working entries -------------------------------------------------------------------

    def _guard_entries(self) -> None:
        """A working entry is cancelled once its quote leaves the price window or goes stale,
        and at the last-entry time (spec 9). Needs the market's quotes, so only with one."""
        from aperture.runtime.entries import guard_entries  # entries imports this module

        if self.market is None:
            return
        guard_entries(self.engine, self.config, self.calendar, self.market.inbox.latest,
                      self.now())

    # --- strategy --------------------------------------------------------------------------

    def _evaluate(self) -> None:
        """The active strategy on the latest completed minute; its WATCH set feeds the L1
        subscriptions (spec 6)."""
        if self.evaluator is None or self.market is None:
            return
        found = self.market.discovery
        candidates = [] if found is None else [c.instrument.symbol for c in found.candidates]
        try:
            self.evaluator.tick(candidates)
        except Exception as exc:  # noqa: BLE001 - evaluation never ends the trading loop
            with self.engine.store.transaction():
                self.engine.store.append(EVALUATION_FAILED, {"error": type(exc).__name__,
                                                             "detail": str(exc)},
                                         run_id=self.engine.run_id)
        self.market.watch = set(self.evaluator.watch)
        self._feed_observers()

    def _feed_observers(self) -> None:
        """The contexts the active strategy just saw go to the observers, after all broker
        work of this pass. Failures are journaled every pass. A full queue (a hung observer)
        or any error here switches the observers off for the run, with one alert; trading
        goes on."""
        observers, evaluator = self.observers, self.evaluator
        if observers is None or evaluator is None:
            return
        try:
            overflow = self._hand_over(observers, evaluator)
        except Exception as exc:  # noqa: BLE001 - observers never end the trading loop
            self._stop_observers(observers, {"error": type(exc).__name__, "detail": str(exc)},
                                 f"observer hook failed ({type(exc).__name__}): observers "
                                 "switched off for this run")
            return
        if overflow:
            self._stop_observers(observers, {"error": "queue-full"},
                                 "observer queue full: observers switched off for this run")

    def _hand_over(self, observers: ObserverSet, evaluator: Evaluator) -> bool:
        """Queue this pass's contexts and journal worker failures; True on queue overflow."""
        overflow = False
        at = evaluator.evaluated_at
        if at is not None:
            for context in evaluator.contexts:
                if observers.on_evaluation(context, at):
                    overflow = True
                    break
        failures = observers.take_failures()
        if failures:
            store = self.engine.store
            with store.transaction():
                for f in failures:
                    store.append(OBSERVER_FAILURE, {"folder": f.folder, "identity": f.identity,
                                                    "at": f.at.isoformat(), "cause": f.cause},
                                 run_id=self.engine.run_id)
        return overflow

    def _stop_observers(self, observers: ObserverSet, body: dict[str, object],
                        text: str) -> None:
        self.observers = None
        try:
            finished = observers.close(float(self.config.observers.max_evaluation_seconds))
        except Exception:  # noqa: BLE001 - the set is dropped either way
            finished = False
        with self.engine.store.transaction():
            self.engine.store.append(OBSERVERS_STOPPED, {**body, "finished": finished},
                                     run_id=self.engine.run_id)
        if self.watch is not None:
            self.watch.alert(AlertKind.OBSERVERS_STOPPED, text)

    # --- status ----------------------------------------------------------------------------

    def _entry_block(self, gate_open: bool, gate_cause: str,
                     fences: list[tuple[str, str, str]]) -> api.EntryBlock | None:
        if self.state is not api.BotState.RUNNING:
            return api.EntryBlock(f"bot_{self.state}", f"The bot is {self.state}.")
        if not gate_open:
            return api.EntryBlock("trading_gate_closed", f"Trading gate closed: {gate_cause}.")
        names = [name for name, _, _ in fences]
        if str(Fence.ENTRY_STOP) in names:
            return api.EntryBlock("entry_stop", "Entries stopped by the operator.")
        if fences:
            name, cause, _ = fences[0]
            return api.EntryBlock(name.split(":", 1)[0].replace("-", "_"), f"{name}: {cause}")
        if self.entries_off is not None:
            return api.EntryBlock("strategy_not_loaded",
                                  f"Active strategy not loaded: {self.entries_off}")
        return NOT_WIRED if self.evaluator is None else None

    def _stop(self, trade: TradeRow, stops: list[OrderRow]) -> api.Stop:
        live = [s for s in stops if s.live]
        verified = [s for s in live if s.verified]
        if verified:
            return api.Stop(api.StopState.VERIFIED, trade.stop_price,
                            verified[0].client_order_id)
        unknown = [s for s in live if s.send_state is SendState.UNKNOWN]
        if unknown:
            return api.Stop(api.StopState.UNKNOWN, trade.stop_price,
                            unknown[0].client_order_id)
        if live:
            return api.Stop(api.StopState.PENDING, trade.stop_price, live[0].client_order_id)
        return api.Stop(api.StopState.MISSING, None, None)

    def _opened_at(self, trade_id: str) -> datetime:
        row = self.engine.store.query("SELECT opened_at, closed_at FROM trades "
                                      "WHERE trade_id = ?", (trade_id,))[0]
        return _iso(row["opened_at"])

    def _position(self) -> api.Position | None:
        for trade in self.engine.trades():
            orders = self.engine.orders(trade.trade_id)
            quantity = self.engine.expected_quantity(orders)
            entry_price = _average([o for o in orders if o.leg is Leg.ENTRY])
            if quantity <= 0 or entry_price is None:
                continue
            stops = [o for o in orders if o.leg is Leg.STOP]
            return api.Position(trade.symbol, quantity, entry_price,
                                self._opened_at(trade.trade_id), self._stop(trade, stops))
        return None

    def _trades_today(self) -> tuple[api.Trade, ...]:
        return trades_today(self.engine, self.now())

    def _recent_commands(self) -> tuple[api.CommandRecord, ...]:
        rows = self.engine.store.journal(COMMAND)[-self.config.api.status_recent_commands:]
        return tuple(api.CommandRecord(
            r.key or "", api.CommandKind(r.body["kind"]), _iso(r.body["requested_at"]),
            _iso(r.at_utc), api.CommandOutcome(r.body["outcome"]), r.body["cause"])
            for r in reversed(rows))

    def status(self) -> api.StatusV1:
        gate = trading_gate(self.config)
        fences = self.engine.fence_rows()
        trades = self._trades_today()
        return api.StatusV1(
            written_at=self.now(), bot_state=self.state, run_id=self.engine.run_id,
            environment=self.config.environment_name,
            trading_gate=api.Gate(gate.open, gate.cause), strategy=self.strategy,
            entry_stop=any(name == Fence.ENTRY_STOP for name, _, _ in fences),
            entry_block=self._entry_block(gate.open, gate.cause, fences),
            fences=tuple(api.Fence(name, _iso(set_at), cause) for name, cause, set_at in fences),
            position=self._position(), trades_today=trades,
            result_today_gross_usd=sum((t.result_gross_usd for t in trades), ZERO),
            freshness=(self.market.freshness(self.last_broker_read_at)
                       if self.market is not None
                       else api.Freshness(self.last_broker_read_at, None, api.StreamHealth.OFF)),
            recent_commands=self._recent_commands())

    def write_status(self) -> None:
        api.write_status(self.paths.status, self.status())

    # --- loop ------------------------------------------------------------------------------

    def _observe(self, reconcile: bool) -> None:
        ok = self.engine.reconcile() if reconcile else True
        if not reconcile:
            self.engine.step()
        if ok and not any(n == Fence.BROKER_READ for n in self.engine.fences()):
            self.last_broker_read_at = self.now()

    def run(self, *, should_stop: Callable[[], bool],
            sleep: Callable[[float], None] = time.sleep,
            monotonic: Callable[[], float] = time.monotonic) -> bool:
        """Recover, loop until `should_stop()`, then drain (spec 11). Returns False when the
        drain hit shutdown_deadline_seconds: the caller exits non-zero and the supervisor
        alerts."""
        orders = self.config.orders
        self.write_status()
        self.engine.recover()
        if not any(n == Fence.BROKER_READ for n in self.engine.fences()):
            self.last_broker_read_at = self.now()
        self.state = api.BotState.RUNNING
        last_reconcile = monotonic()
        while not should_stop():
            self.process_commands()
            self.flatten_due()
            self.write_status()
            if self.watch is not None:
                self.watch.check()
                self.watch.backup_daily()
            sleep(float(orders.detail_poll_seconds))
            due = monotonic() - last_reconcile >= float(orders.reconcile_interval_seconds)
            self._observe(reconcile=due)
            if self.market is not None:
                self.market.tick()  # after the engine's broker work of this pass
                self._guard_entries()
            self._evaluate()
            if due:
                last_reconcile = monotonic()
        self.state = api.BotState.STOPPING
        self.write_status()
        clean = drain(self.engine, sleep=sleep, monotonic=monotonic)
        self.state = api.BotState.STOPPED
        self.write_status()
        if not clean and self.watch is not None:
            pending = self.engine.store.journal("shutdown-deadline")[-1].body["pending"]
            self.watch.alert(AlertKind.BOT_DID_NOT_STOP,
                             "shutdown deadline passed with work pending: " + "; ".join(pending))
        return clean
