"""The bot service behind `tickr run`: keep what the engine owns protected, act on operator
commands, flatten at the session's flatten time, write status.json, drain on SIGTERM.

With an evaluator the active strategy runs once per completed minute and its ENTER decisions
go through the entry desk; without one (tests, tools) the service never opens a trade and
status says so as the entry-blocking reason.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from tickr.api import contract as api
from tickr.api import reasons
from tickr.api.history import (
    NO_ENTRY,
    average_fill_price,
    closed_trades,
    entry_send,
    trade_metrics,
    trade_strategy,
)
from tickr.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
)
from tickr.config import Config, trading_gate
from tickr.engine.core import Engine, Fence, Leg, OrderRow, SendState, TradeRow
from tickr.engine.loop import drain
from tickr.engine.manual import flatten_deadline
from tickr.risk.admission import Admission, AdmissionLimits, session_refusal
from tickr.risk.causes import AdmissionCause
from tickr.runtime import stopmarker
from tickr.runtime.account import account_document, record_reconcile
from tickr.runtime.alerts import AlertKind
from tickr.runtime.dayend import DayEnd
from tickr.runtime.protection import LatencyLog, busy

if TYPE_CHECKING:
    from tickr.observers.runner import ObserverSet
    from tickr.runtime.evaluation import Evaluator
    from tickr.runtime.market import MarketRuntime
    from tickr.runtime.watch import Watch

STRATEGIES_DIR = "strategies"
STRATEGY_FILE = "strategy.toml"
COMMAND = "command"
COMMAND_FILE_REFUSED = "command-file-refused"
COMMAND_EFFECT = "command-effect"
COMMAND_STEP = "command-step"  # one row per finished step of an emergency command
EMERGENCY_STEPS = ("entry-stop-on", "cancel-own-orders", "flatten")
CLOSE_IN_PROGRESS = "a close is in progress; send stop-bot again after it completes"
OBSERVER_FAILURE = "observer-failure"
OBSERVERS_STOPPED = "observers-stopped"
OBSERVERS_NOT_LOADED = "observers-not-loaded"
EVALUATION_FAILED = "evaluation-failed"
EVALUATION_SKIPPED = "evaluation-skipped"  # one row per protect-first episode
EVALUATION_SLOW = "evaluation-slow"
LATENCY_FAILED = "protection-latency-failed"
STRATEGY_NOT_LOADED = "strategy-not-loaded"
ACCOUNT_WRITE_FAILED = "account-write-failed"  # one row per stretch of failed writes
STOP_TIMING_FAILED = "stop-timing-failed"  # one row per trade whose stop timing is unreadable
ENTRY_SEND_FAILED = "entry-send-failed"  # one row per trade whose entry send is unreadable
NOT_WIRED = api.EntryBlock("entries_not_wired",
                           "This build does not open trades on its own yet.")
ZERO = Decimal(0)


@dataclass(frozen=True)
class ServicePaths:
    status: Path
    inbox: Path
    processed: Path
    account: Path | None = None  # None: no account file is written (tools, some tests)

    @classmethod
    def from_config(cls, config: Config) -> ServicePaths:
        data = config.root / config.run.data_dir
        commands = data / config.api.commands_dir
        return cls(data / config.api.status_file, commands / api.INBOX,
                   commands / api.PROCESSED, data / config.api.account_file)


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


def _status_trade(t: api.DayTrade) -> api.Trade:
    return api.Trade(t.symbol, t.quantity, t.entry_price, t.exit_price, t.opened_at,
                     t.closed_at, t.exit_reason, t.result_gross_usd, t.planned_loss_usd,
                     t.stop_confirmed_after_seconds, t.entry, t.entry_cause, t.strategy)


def trades_today(engine: Engine, now: datetime) -> tuple[api.Trade, ...]:
    """Trades closed today (New York date) that had fills, with gross result. The daily-loss
    check reads this: it never reads the journal, so the display metrics are null here."""
    today = now.astimezone(NEW_YORK).date()
    return tuple(_status_trade(t) for t in closed_trades(engine.store, closed_on=today))


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
        self.latency: LatencyLog | None = LatencyLog(engine)
        self._skipping: str | None = None  # cause of the current protect-first episode
        self.stopped_by: str | None = None  # command_id of an obeyed stop-bot command
        self._account_failing = False  # the last account file write failed (journaled once)
        # The records disagreed with the broker on the last successful reconciliation
        # (record_reconcile: a disagreement is recorded once two passes in a row show it).
        self._disagreed = False
        # Stop confirmation time of each closed trade shown in status (final once closed).
        self._stop_timing: dict[str, Decimal | None] = {}
        self._strategy: dict[str, api.StrategyId | None] = {}  # per closed trade, display
        # Entry send of each trade shown, once final (trade closed, or its journal unreadable).
        self._entry: dict[str, tuple[api.EntrySend, str | None]] = {}

    # --- commands --------------------------------------------------------------------------

    def _seen(self, command_id: str) -> bool:
        return bool(self.engine.store.journal(COMMAND, command_id))

    def process_commands(self) -> None:
        """Journal, act, retire (docs/api.md). Commands run in request order, so of two
        entry-stop commands the later one wins. A command already journaled (crash before
        retire) is not acted on again, except a cancel-own-orders whose effect was not yet
        recorded: cancelling entries is idempotent, so it is finished instead of lost.
        Commands read after an obeyed stop-bot are refused, never acted on: the bot is
        stopping and nothing it asked for would be finished."""
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
                    self._close_interrupted(item)
            elif self._seen(command.command_id):
                if (command.kind is api.CommandKind.CANCEL_OWN_ORDERS
                        and store.journal(COMMAND, command.command_id)[-1].body["outcome"]
                        == api.CommandOutcome.DONE
                        and not store.journal(COMMAND_EFFECT, command.command_id)):
                    self._cancel_entries(command)
            elif self.stopped_by is not None:
                with store.transaction():
                    self._record(command, api.CommandOutcome.REFUSED,
                                 f"read after stop-bot {self.stopped_by}: the bot is stopping")
            else:
                self._act(command)
            api.retire_command(item, self.paths.processed)

    def _close_interrupted(self, item: api.InboxItem) -> None:
        """Inside the caller's transaction: an emergency that a crash interrupted and that
        expired before the restart is never finished late; its record says refused, with the
        steps done and the steps not run."""
        cid = item.path.stem
        done = [r.body["step"] for r in self.engine.store.journal(COMMAND_STEP, cid)]
        if not done or self._seen(cid):
            return
        try:
            command: api.CommandV1 = api.loads(api.CommandV1, item.path.read_bytes())
        except (api.ContractError, OSError):
            return  # not a command this bot can name; the file refusal row says why
        if command.command_id != cid:
            return
        missing = [name for name in EMERGENCY_STEPS if name not in done]
        self._record(command, api.CommandOutcome.REFUSED,
                     f"{item.refusal}; steps done: {', '.join(done)}; "
                     f"not run: {', '.join(missing) or 'none'}")

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
        if kind is api.CommandKind.CANCEL_OWN_ORDERS:
            with store.transaction():
                self._record(command, api.CommandOutcome.DONE, None)
            self._cancel_entries(command)
            return
        if kind is api.CommandKind.STOP_BOT:
            self._stop_bot(command)
            return
        if kind is api.CommandKind.EMERGENCY:
            self._emergency(command)
            return
        with store.transaction():  # the record and the exit requests are one write
            outcome, cause = self._flatten_rows(command.symbol, "operator-flatten")
            self._record(command, outcome, cause)

    def _flatten_rows(self, symbol: str | None,
                      reason: str) -> tuple[api.CommandOutcome, str | None]:
        """FLATTEN inside the caller's transaction: exit requests through the normal exit path
        for every open trade, or only those of `symbol`. Refused when `symbol` has no open
        trade, while a close of a chosen trade waits for the operator, and outside the
        regular session (no closing MARKET order is sent then; the stops stay)."""
        engine = self.engine
        trades = [t for t in engine.trades() if symbol is None or t.symbol == symbol]
        if symbol is not None and not trades:
            return api.CommandOutcome.REFUSED, f"no open position in {symbol}"
        fences = engine.fences()
        refused = [t.trade_id for t in trades if f"{Fence.EXIT_REFUSED}:{t.trade_id}" in fences]
        if refused:
            return api.CommandOutcome.REFUSED, (
                f"closing order refused before for {', '.join(refused)}: look in the Webull "
                "app, then clear the fence")
        held = [t.trade_id for t in trades if f"{Fence.MARKET_CLOSED}:{t.trade_id}" in fences]
        if held:
            return api.CommandOutcome.REFUSED, (
                f"a close of {', '.join(held)} was due outside the regular session and was not "
                "sent (fence market-closed); the stop stays: close it in the Webull app, or "
                "clear the fence in the regular session")
        closed = engine.close_refusal() if trades else None
        if closed is not None:
            return api.CommandOutcome.REFUSED, (
                f"no closing order is sent now: {closed}; the stops stay. Close the position "
                "in the Webull app")
        for trade in trades:
            engine.exit_request_row(trade.trade_id, reason)
        return api.CommandOutcome.DONE, None if trades else "nothing open"

    def _stop_bot(self, command: api.CommandV1) -> None:
        """Graceful stop, obeyed only when flat: the marker first (durable, so no later start
        can miss it), then the record; the loop then ends and drains as on SIGTERM. Refused
        while a close is requested or working (an operator flatten, an emergency, the session
        flatten): stopping then would abandon an exit request that a later start would carry
        out without anyone asking. Refused while any trade is not closed, a working entry with
        no fill included: a broker stop is a DAY order and a stopped bot does not flatten at
        the flatten time, so a position (or an entry filling during the drain) could go
        overnight unprotected."""
        engine = self.engine
        trades = engine.trades()
        for trade in trades:
            orders = engine.orders(trade.trade_id)
            if trade.exit_requested or any(o.leg is Leg.EXIT and o.live for o in orders):
                with engine.store.transaction():
                    self._record(command, api.CommandOutcome.REFUSED, CLOSE_IN_PROGRESS)
                return
        if trades:
            with engine.store.transaction():
                self._record(command, api.CommandOutcome.REFUSED, (
                    f"{len(trades)} trade(s) open or entering; flatten first (or send "
                    "emergency), then stop-bot"))
            return
        stopmarker.write(self.config, command.command_id, self.now())
        with engine.store.transaction():
            self._record(command, api.CommandOutcome.DONE, None)
        self.stopped_by = command.command_id

    def _emergency(self, command: api.CommandV1) -> None:
        """Entry-stop on, then cancel own orders, then flatten all: the existing handlers, each
        step journaled and run whatever an earlier step did. cancel-own-orders never touches a
        broker stop: the flatten's exit path cancels a stop only right before the close it
        sends, and keeps protection when that close cannot be sent. Steps already journaled
        (a crash before the command record) are not repeated; the record comes last."""
        engine, store, cid = self.engine, self.engine.store, command.command_id
        done = {r.body["step"]: r.body for r in store.journal(COMMAND_STEP, cid)}

        def step(name: str, outcome: api.CommandOutcome, cause: str | None) -> None:
            body = {"step": name, "outcome": outcome, "cause": cause}
            store.append(COMMAND_STEP, body, key=cid, run_id=engine.run_id)
            done[name] = body

        if "entry-stop-on" not in done:
            with store.transaction():
                engine.entry_stop_row(True, f"operator emergency {cid}")
                step("entry-stop-on", api.CommandOutcome.DONE, None)
        if "cancel-own-orders" not in done:
            asked = engine.cancel_entries()
            with store.transaction():
                step("cancel-own-orders", api.CommandOutcome.DONE,
                     f"{asked} entry cancel(s) requested")
        if "flatten" not in done:
            with store.transaction():
                step("flatten", *self._flatten_rows(None, "operator-emergency"))
        refused = [f"{name}: {body['cause']}" for name, body in done.items()
                   if body["outcome"] == api.CommandOutcome.REFUSED]
        with store.transaction():
            if refused:
                self._record(command, api.CommandOutcome.REFUSED, "; ".join(refused))
            else:
                self._record(command, api.CommandOutcome.DONE, "; ".join(
                    f"{name}: {body['cause'] or 'done'}" for name, body in done.items()))

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
        from tickr.runtime.entries import guard_entries  # entries imports this module

        if self.market is None:
            return
        guard_entries(self.engine, self.config, self.calendar, self.market.inbox.latest,
                      self.now())

    # --- strategy --------------------------------------------------------------------------

    def _record_latency(self) -> None:
        """Fill-to-stop timing rows. A measurement never ends the trading loop: on an error it
        is journaled once and switched off for the run."""
        if self.latency is None:
            return
        try:
            self.latency.record()
        except Exception as exc:  # noqa: BLE001 - see docstring
            self.latency = None
            with self.engine.store.transaction():
                self.engine.store.append(LATENCY_FAILED, {"error": type(exc).__name__,
                                                          "detail": str(exc)},
                                         run_id=self.engine.run_id)

    def _protect_first(self) -> bool:
        """True when strategy work may run this pass: no entry working, every fill covered by
        a verified stop, no exit in progress (runtime/protection.py). The first pass of each
        episode that holds it back is journaled once with its cause."""
        cause = busy(self.engine)
        if cause is not None and self._skipping is None and self.evaluator is not None:
            with self.engine.store.transaction():
                self.engine.store.append(EVALUATION_SKIPPED, {"cause": cause},
                                         run_id=self.engine.run_id)
        self._skipping = cause
        return cause is None

    def _strategy_pass(self, monotonic: Callable[[], float]) -> None:
        """Market upkeep and the entry guard every pass; discovery and the strategy only when
        protection allows, timed against `[runtime] evaluation_budget_seconds`."""
        allowed = self._protect_first()
        started = monotonic()
        if self.market is not None:
            self.market.tick(discover=allowed)  # after the engine's broker work of this pass
            self._guard_entries()  # every pass: it protects a working entry
        if not allowed:
            return
        self._evaluate()
        spent = monotonic() - started
        budget = float(self.config.runtime.evaluation_budget_seconds)
        if spent > budget:
            with self.engine.store.transaction():
                self.engine.store.append(EVALUATION_SLOW, {"seconds": spent, "budget": budget},
                                         run_id=self.engine.run_id)

    def _evaluate(self) -> None:
        """The active strategy on the latest completed minute; its WATCH set feeds the L1
        subscriptions (spec 6), and the observers' WATCH symbols a lower tier of them."""
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
        observers = self.observers
        self.market.observer_watch = (set() if observers is None
                                      else set(observers.watching(
                                          self.now(), timedelta(seconds=float(
                                              self.config.observers.watching_seconds)))))

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
            gate = reasons.gate_reason(gate_cause)
            return api.EntryBlock("trading_gate_closed", f"Trading gate closed: {gate_cause}.",
                                  params={"gate": gate.code, **gate.params})
        names = [name for name, _, _ in fences]
        if str(Fence.ENTRY_STOP) in names:
            return api.EntryBlock("entry_stop", "Entries stopped by the operator.")
        if fences:
            name, cause, _ = fences[0]
            fence = reasons.fence_reason(name, cause)
            return api.EntryBlock(name.split(":", 1)[0].replace("-", "_"), f"{name}: {cause}",
                                  params={"fence": fence.code, **fence.params})
        money = self._daily_money_block()
        if money is not None:
            return money
        if self.entries_off is not None:
            return api.EntryBlock("strategy_not_loaded",
                                  f"Active strategy not loaded: {self.entries_off}")
        if self.evaluator is None:
            return NOT_WIRED
        return self._ordinary_block()

    def _ordinary_block(self) -> api.EntryBlock | None:
        """Why admission refuses every entry right now although nothing is wrong: no session,
        outside the entry window, or a trade of our own is already open. Reported with
        `attention = False` so the apps can say "not now" without a warning. The answers are
        admission's own (its session check and its counts of our positions and orders), so
        the status cannot say "allowed" while admission refuses. A calendar that cannot
        answer is a real block."""
        from tickr.runtime import entries  # entries imports this module

        now = self.now()
        day = entries.session_at(self.calendar, now)
        refused = session_refusal(day, now, AdmissionLimits.from_config(self.config))
        if refused is not None:
            return self._session_block(day, refused)
        if any(entries.own_exposure(self.engine)):
            return api.EntryBlock(api.OWN_TRADE_OPEN, "The bot already has a trade open.",
                                  attention=False)
        return None

    def _session_block(self, day: MarketDay | CalendarRefused,
                       refused: Admission) -> api.EntryBlock:
        """Admission's session refusal in the status document's words."""
        if isinstance(day, CalendarRefused):
            if day.cause in (CalendarCause.WEEKEND, CalendarCause.HOLIDAY):
                return api.EntryBlock("no_session_today", "No regular session today.",
                                      attention=False, params={"day": day.cause.lower()})
            if self.calendar is None:
                return api.EntryBlock("calendar_unknown", "No trading calendar is loaded.",
                                      params={"calendar": "not_loaded"})
            return api.EntryBlock("calendar_unknown", f"Trading calendar: {day.cause}.",
                                  params={"calendar": day.cause.lower()})
        if refused.cause is AdmissionCause.HALF_DAY:
            return api.EntryBlock("no_session_today",
                                  "Half day: the bot does not trade half days.",
                                  attention=False, params={"day": "half_day"})
        if refused.cause is AdmissionCause.OUTSIDE_REGULAR_SESSION:
            opens = f"{day.regular_open.astimezone(NEW_YORK):%H:%M}"
            closes = f"{day.regular_close.astimezone(NEW_YORK):%H:%M}"
            return api.EntryBlock(
                "outside_regular_session",
                f"Outside the regular session ({opens}-{closes} New York).", attention=False,
                params={"open": opens, "close": closes})
        if refused.cause is AdmissionCause.AFTER_LAST_ENTRY:
            session = self.config.session
            times = day.session_times(session.last_entry_minutes_before_close,
                                      session.flatten_minutes_before_close)
            last = f"{times.last_entry.astimezone(NEW_YORK):%H:%M}"
            return api.EntryBlock("after_last_entry",
                                  f"Past the last entry time ({last} New York).",
                                  attention=False, params={"time": last})
        return api.EntryBlock("calendar_unknown", f"Trading calendar: {refused.detail}.",
                              params={"calendar": str(refused.cause).lower()})

    def _daily_money_block(self) -> api.EntryBlock | None:
        """Entries the daily ceiling refuses anyway: the room left is below
        `[risk] min_daily_room_usd` (spec 7, check 6), or today's money is unknown. Above it a
        trade is sized down to the room. Admission still decides each entry."""
        from tickr.runtime.entries import daily_loss_room  # entries imports this module

        room = daily_loss_room(self.engine, self.config, self.now())
        if room is None:
            return api.EntryBlock("daily_money_unknown",
                                  "A trade closed today has fills without a known price.")
        risk = self.config.risk
        if room < risk.min_daily_room_usd:
            return api.EntryBlock("daily_ceiling",
                                  f"Daily loss ceiling {risk.max_daily_loss_usd} USD: less than "
                                  f"{risk.min_daily_room_usd} USD of room left today.",
                                  params={"limit_usd": str(risk.max_daily_loss_usd),
                                          "min_room_usd": str(risk.min_daily_room_usd)})
        return None

    @staticmethod
    def _gate(open_: bool, cause: str) -> api.Gate:
        reason = reasons.gate_reason(cause)
        return api.Gate(open_, cause, reason.code, reason.params)

    @staticmethod
    def _fence(name: str, cause: str, set_at: str) -> api.Fence:
        reason = reasons.fence_reason(name, cause)
        return api.Fence(name, _iso(set_at), cause, reason.code, reason.params)

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

    def _positions(self) -> tuple[api.Position, ...]:
        found = []
        for trade in self.engine.trades():
            orders = self.engine.orders(trade.trade_id)
            quantity = self.engine.expected_quantity(orders)
            entry_price = average_fill_price([o for o in orders if o.leg is Leg.ENTRY])
            if quantity <= 0 or entry_price is None:
                continue
            stops = [o for o in orders if o.leg is Leg.STOP]
            entry, cause = self._entry_send(trade.trade_id, final=False)
            found.append(api.Position(trade.symbol, quantity, entry_price,
                                      self._opened_at(trade.trade_id), self._stop(trade, stops),
                                      entry, cause))
        return tuple(found)

    def _daily_money(self) -> tuple[Decimal | None, Decimal, Decimal | None]:
        """(net realized loss today, open reservations, room) as admission computes them;
        loss and room are None when today's money is unknown."""
        from tickr.runtime import entries  # entries imports this module

        now = self.now()
        reserved = sum((t.planned_loss_usd for t in self.engine.trades()), ZERO)
        return (entries.realized_loss_today(self.engine, now), reserved,
                entries.daily_loss_room(self.engine, self.config, now))

    def _trades_today(self) -> tuple[api.Trade, ...]:
        """For status only: today's trades with their stop confirmation time."""
        today = self.now().astimezone(NEW_YORK).date()
        return tuple(_status_trade(self._shown(t))
                     for t in closed_trades(self.engine.store, closed_on=today))

    def _shown(self, trade: api.DayTrade) -> api.DayTrade:
        """A display metric never ends the loop: an unreadable journal shows null, journaled
        once per trade."""
        if trade.trade_id not in self._stop_timing:
            try:
                timing = trade_metrics(self.engine.store, trade, None).stop_confirmed_after_seconds
            except Exception as exc:  # noqa: BLE001 - see docstring
                timing = None
                if not self.engine.store.journal(STOP_TIMING_FAILED, trade.trade_id):
                    with self.engine.store.transaction():
                        self.engine.store.append(STOP_TIMING_FAILED, {
                            "error": type(exc).__name__, "detail": str(exc)},
                            key=trade.trade_id, run_id=self.engine.run_id)
            self._stop_timing[trade.trade_id] = timing
        entry, cause = self._entry_send(trade.trade_id, final=True)
        if trade.trade_id not in self._strategy:
            try:
                self._strategy[trade.trade_id] = trade_strategy(self.engine.store, trade.trade_id)
            except Exception:  # noqa: BLE001 - display only, as above; null, never a stop
                self._strategy[trade.trade_id] = None
        return replace(trade, stop_confirmed_after_seconds=self._stop_timing[trade.trade_id],
                       entry=entry, entry_cause=cause, strategy=self._strategy[trade.trade_id])

    def _entry_send(self, trade_id: str, *, final: bool) -> tuple[api.EntrySend, str | None]:
        """As `_shown`: display only, never ends the loop; an unreadable journal shows null
        with the cause, journaled once per trade. Cached once final: the trade is closed
        (`final`), or a row is unreadable (journal rows never change)."""
        if trade_id in self._entry:
            return self._entry[trade_id]
        try:
            found = entry_send(self.engine.store, trade_id)
        except Exception as exc:  # noqa: BLE001 - see docstring
            found, final = (NO_ENTRY, f"journal unreadable: {type(exc).__name__}: {exc}"), True
            if not self.engine.store.journal(ENTRY_SEND_FAILED, trade_id):
                with self.engine.store.transaction():
                    self.engine.store.append(ENTRY_SEND_FAILED, {
                        "error": type(exc).__name__, "detail": str(exc)},
                        key=trade_id, run_id=self.engine.run_id)
        if final:
            self._entry[trade_id] = found
        return found

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
        positions = self._positions()
        loss, reserved, room = self._daily_money()
        return api.StatusV1(
            written_at=self.now(), bot_state=self.state, run_id=self.engine.run_id,
            environment=self.config.environment_name,
            trading_gate=self._gate(gate.open, gate.cause), strategy=self.strategy,
            entry_stop=any(name == Fence.ENTRY_STOP for name, _, _ in fences),
            entry_block=self._entry_block(gate.open, gate.cause, fences),
            fences=tuple(self._fence(name, cause, set_at) for name, cause, set_at in fences),
            position=positions[0] if positions else None, trades_today=trades,
            result_today_gross_usd=sum((t.result_gross_usd for t in trades), ZERO),
            freshness=(self.market.freshness(self.last_broker_read_at)
                       if self.market is not None
                       else api.Freshness(self.last_broker_read_at, None, api.StreamHealth.OFF)),
            recent_commands=self._recent_commands(),
            realized_loss_today_usd=loss, open_reservations_usd=reserved,
            daily_room_usd=room, positions=positions)

    def write_status(self) -> None:
        api.write_status(self.paths.status, self.status())

    def write_account(self) -> None:
        """After a successful reconciliation: what it read from the broker (docs/api.md)."""
        if self.paths.account is None:
            return
        document = account_document(self.engine, self.now())
        if document is None:
            return
        try:
            api.write_account(self.paths.account, document)
        except OSError as exc:
            # A display file: its failure never ends the loop. Journaled once per stretch.
            if not self._account_failing:
                with self.engine.store.transaction():
                    self.engine.store.append(ACCOUNT_WRITE_FAILED,
                                             {"error": type(exc).__name__, "detail": str(exc)},
                                             run_id=self.engine.run_id)
            self._account_failing = True
            return
        self._account_failing = False

    # --- loop ------------------------------------------------------------------------------

    def _observe(self, reconcile: bool) -> None:
        ok = self.engine.reconcile() if reconcile else True
        if not reconcile:
            self.engine.step()
        elif ok:
            self._disagreed = record_reconcile(self.engine, self._disagreed)
            self.write_account()
        else:
            self._disagreed = False  # a failed read: the next two must agree again
        if ok and not self._broker_read_failing():
            self.last_broker_read_at = self.now()

    def _broker_read_failing(self) -> bool:
        """A positions or open-orders read is failing (the fence, plain or keyed)."""
        return any(n.split(":", 1)[0] == Fence.BROKER_READ for n in self.engine.fences())

    def run(self, *, should_stop: Callable[[], bool],
            sleep: Callable[[float], None] = time.sleep,
            monotonic: Callable[[], float] = time.monotonic) -> bool:
        """Recover, loop until `should_stop()`, then drain (spec 11). Returns False when the
        drain hit shutdown_deadline_seconds: the caller exits non-zero and the supervisor
        alerts."""
        orders = self.config.orders
        self.write_status()
        self.engine.recover()
        # Nothing when the startup reconcile did not read.
        self._disagreed = record_reconcile(self.engine, False)
        if not self._broker_read_failing():
            self.last_broker_read_at = self.now()
            self.write_account()
        self.state = api.BotState.RUNNING
        last_reconcile = monotonic()
        while not should_stop():
            self.process_commands()
            if self.stopped_by is not None:
                break  # an operator stop-bot: nothing more this run, straight to the drain
            self.flatten_due()
            self.write_status()
            if self.watch is not None:
                self.watch.check()
                self.watch.backup_daily()
            sleep(float(orders.detail_poll_seconds))
            due = monotonic() - last_reconcile >= float(orders.reconcile_interval_seconds)
            self._observe(reconcile=due)
            self._record_latency()
            self._strategy_pass(monotonic)
            if due:
                last_reconcile = monotonic()
        self.state = api.BotState.STOPPING
        self.write_status()
        self.day_end.finish()
        clean = drain(self.engine, sleep=sleep, monotonic=monotonic)
        self.state = api.BotState.STOPPED
        self.write_status()
        if not clean and self.watch is not None:
            pending = self.engine.store.journal("shutdown-deadline")[-1].body["pending"]
            self.watch.alert(AlertKind.BOT_DID_NOT_STOP,
                             "shutdown deadline passed with work pending: " + "; ".join(pending))
        return clean
