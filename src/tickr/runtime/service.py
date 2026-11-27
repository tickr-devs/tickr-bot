"""The bot service behind `tickr run`: keep what the engine owns protected, act on operator
commands, flatten at the session's flatten time, write status.json, drain on SIGTERM.

With an evaluator the active strategy runs once per completed minute and its ENTER decisions
go through the entry desk; without one (tests, tools) the service never opens a trade and
status says so as the entry-blocking reason.
"""

from __future__ import annotations

import json
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
    recorded_fees,
    total_net,
    trade_metrics,
    trade_strategy,
)
from tickr.calendar import (
    NEW_YORK,
    CalendarCause,
    CalendarRefused,
    MarketDay,
    SessionCalendar,
    ny_date_of,
)
from tickr.config import Config, trading_gate
from tickr.engine.core import OPERATOR_FENCES, Engine, Fence, Leg, OrderRow, SendState, TradeRow
from tickr.engine.loop import drain
from tickr.engine.manual import flatten_deadline
from tickr.observers.runner import DROPPED
from tickr.risk.admission import Admission, AdmissionLimits, session_refusal
from tickr.risk.causes import AdmissionCause
from tickr.runtime import stopmarker
from tickr.runtime.account import (
    AccountReads,
    account_document,
    record_exact_fills,
    record_reconcile,
)
from tickr.runtime.alerts import AlertKind
from tickr.runtime.dayend import DayEnd
from tickr.runtime.liveprotection import ADOPTED as PROTECTION_ADOPTED
from tickr.runtime.liveprotection import IGNORED as PROTECTION_IGNORED
from tickr.runtime.liveprotection import Adoption, LiveProtection, toml_text
from tickr.runtime.protection import LatencyLog, busy
from tickr.runtime.watchdog import BYTES_PER_MB, DiskFree, disk_free, entry_restart_file
from tickr.settings_version import CONFLICT, SettingsVersion, SettingsVersionRefused
from tickr.store import SettingsVersionConflict

if TYPE_CHECKING:
    from tickr.observers.runner import ObserverSet
    from tickr.runtime.entries import EntryQuotes
    from tickr.runtime.evaluation import Evaluator
    from tickr.runtime.market import MarketRuntime
    from tickr.runtime.quotes import QuoteFile
    from tickr.runtime.watch import Watch
    from tickr.types import Quote

STRATEGIES_DIR = "strategies"
STRATEGY_FILE = "strategy.toml"
COMMAND = "command"
COMMAND_FILE_REFUSED = "command-file-refused"
COMMAND_EFFECT = "command-effect"
COMMAND_STEP = "command-step"  # one row per finished step of an emergency command
EMERGENCY_STEPS = ("entry-stop-on", "cancel-own-orders", "flatten")
CLOSE_IN_PROGRESS = "a close is in progress; send stop-bot again after it completes"
# One row per step of an obeyed or running stop-bot (api.StopStep), keyed by its command_id.
STOP_PROGRESS = "stop-progress"
# The fences that end a close_and_stop: the engine's operator fences (on the trade or on one
# of its orders), each one a point where the engine stops closing that trade on its own until
# the operator has looked: a close refused (exit-refused), not filled in [orders]
# max_exit_seconds (exit-not-filled), due outside the regular session and not sent
# (market-closed), a stop or entry still working after every cancel (cancel-failed), a stop
# replace unresolved, and so on. None of them is ever resent by the flow.
CLOSE_FAILED_FENCES = tuple(sorted(OPERATOR_FENCES))
STOP_RUNNING = "a close-and-stop is already running ({command})"
# A cancel-own-orders for one symbol with no entry of it working (Cancel entry, 2026-11-26).
NO_WORKING_ENTRY = "no working entry in {symbol}"
TRADES_LEFT_OPEN = ("{count} trade(s) left open: nothing closes them; their broker stops are "
                    "DAY orders")
# A close_and_stop that a fence ended: one `; `-joined part per kind of fence, saying what is
# then true at the broker, then STOP_CLOSE_KEPT.
STOP_CLOSE_REFUSED = ("closing order refused for {trades}: the bot places their stop again; "
                      "look in the Webull app")
STOP_CLOSE_NOT_FILLED = ("closing order not filled for {trades}: it is still working at the "
                         "broker and its shares are not under a stop; look in the Webull app")
STOP_CLOSE_NOT_SENT = ("close not sent for {trades} outside the regular session: nothing was "
                       "sent and the stop stays")
STOP_CLOSE_FAILED = "close held for {trades} ({fences}): look in the Webull app"
STOP_CLOSE_KEPT = "the bot keeps running and entry-stop stays on"
STOP_CLOSE_PARTS = {Fence.EXIT_REFUSED: STOP_CLOSE_REFUSED,
                    Fence.EXIT_NOT_FILLED: STOP_CLOSE_NOT_FILLED,
                    Fence.MARKET_CLOSED: STOP_CLOSE_NOT_SENT}
STOP_NEW_TRADE = ("trade(s) {trades} opened during close-and-stop: the bot keeps running and "
                  "the closes already asked go on; send stop-bot again")
STOP_INTERRUPTED = ("close-and-stop interrupted by a restart: the bot keeps running and "
                    "entry-stop stays on; send stop-bot again")
OBSERVER_FAILURE = "observer-failure"
OBSERVERS_STOPPED = "observers-stopped"
OBSERVERS_DROPPED = "observers-dropped"  # one row per pass with evaluations the observers dropped
OBSERVERS_NOT_LOADED = "observers-not-loaded"
EVALUATION_FAILED = "evaluation-failed"
EVALUATION_SKIPPED = "evaluation-skipped"  # one row per protect-first episode
EVALUATION_SLOW = "evaluation-slow"
LATENCY_FAILED = "protection-latency-failed"
STRATEGY_NOT_LOADED = "strategy-not-loaded"
ACCOUNT_WRITE_FAILED = "account-write-failed"  # one row per stretch of failed writes
RUN_FACTS_WRITE_FAILED = "run-facts-write-failed"  # one row per stretch of failed writes
# An adopted tightening whose settings version was refused: new entries record none.
SETTINGS_VERSION_REFUSED = "settings-version-refused"
STOP_TIMING_FAILED = "stop-timing-failed"  # one row per trade whose stop timing is unreadable
ENTRY_SEND_FAILED = "entry-send-failed"  # one row per trade whose entry send is unreadable
RESTART_ENTRY_CANCEL = "bot restarted: a working entry is never resumed"
FENCE_SET = "fence-set"
WATCHDOG_FENCE_TAIL = ("no new entry until the operator clears this "
                       "(tickr clear-fence watchdog-started)")
NOT_WIRED = api.EntryBlock("entries_not_wired",
                           "This build does not open trades on its own yet.")
ZERO = Decimal(0)


def stop_close_failed(failed: list[tuple[str, Fence]]) -> str:
    """The cause of a close_and_stop a fence ended, from its (trade, fence) pairs: what is true
    at the broker for each kind of fence (exit-refused, exit-not-filled, market-closed, then
    the other operator fences together), then that the bot runs on with entry-stop on."""
    parts = []
    for kind, text in STOP_CLOSE_PARTS.items():
        trades = sorted({trade for trade, fence in failed if fence is kind})
        if trades:
            parts.append(text.format(trades=", ".join(trades)))
    other = [(trade, fence) for trade, fence in failed if fence not in STOP_CLOSE_PARTS]
    if other:
        parts.append(STOP_CLOSE_FAILED.format(
            trades=", ".join(sorted({trade for trade, _ in other})),
            fences=", ".join(sorted({str(fence) for _, fence in other}))))
    return "; ".join([*parts, STOP_CLOSE_KEPT])


@dataclass(frozen=True)
class ServicePaths:
    status: Path
    inbox: Path
    processed: Path
    account: Path | None = None  # None: no account file is written (tools, some tests)
    # The watchdog's early-restart record; None: never read (tools, some tests).
    watchdog_entry_restart: Path | None = None
    # The data folder whose free disk space each loop pass checks; None: not checked.
    data: Path | None = None
    # The run facts file (contract.RunFactsV1); None: never written (tools, some tests).
    run_facts: Path | None = None

    @classmethod
    def from_config(cls, config: Config) -> ServicePaths:
        data = config.root / config.run.data_dir
        commands = data / config.api.commands_dir
        return cls(data / config.api.status_file, commands / api.INBOX,
                   commands / api.PROCESSED, data / config.api.account_file,
                   entry_restart_file(config), data, data / config.runtime.run_facts_file)


def _no_quote(symbol: str) -> Quote | None:  # noqa: ARG001 - no market: no quote for any
    return None


def strategy_id(config: Config) -> api.StrategyId:
    """Name and version of the active strategy, from its strategy.toml. Its settings version
    is set by the bot once the entry desk is wired (the one its records reference)."""
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
                     t.stop_confirmed_after_seconds, t.entry, t.entry_cause,
                     None if t.strategy is None else api.strategy_name(t.strategy),
                     t.code_version)


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
                 watch: Watch | None = None,
                 account_reads: AccountReads | None = None,
                 quote_file: QuoteFile | None = None,
                 disk_free: DiskFree = disk_free,
                 protection: LiveProtection | None = None) -> None:
        self.engine = engine
        self._disk_free = disk_free
        self.market = market
        self.evaluator = evaluator
        self.entries_off = entries_off  # why there is no evaluator (status says so)
        self.observers = observers
        self.watch = watch
        self.config = engine.config
        # Protection tightened while running (backlog S5); None: the started values throughout.
        self.protection = protection
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
        self._entry_quotes: EntryQuotes | None = None  # built on the first entry guard
        self.stopped_by: str | None = None  # command_id of an obeyed stop-bot command
        # A close_and_stop being carried out: its command and the trades open when it began
        # (backlog V4).
        self._closing: tuple[api.CommandV1, frozenset[str]] | None = None
        # The stop-bot this run obeyed or is carrying out (status `stop_progress`).
        self._progress: tuple[str, api.StopMode | None, api.StopStep, datetime] | None = None
        self._account_failing = False  # the last account file write failed (journaled once)
        self._run_facts_failing = False  # the last run facts write failed (journaled once)
        # The loss locks' counts the last run facts write carried (None: none written yet).
        self._loss_locks_written: tuple[Decimal | None, int] | None = None
        # The settings versions of the trades the latest status shows, and those the latest run
        # facts write held (None before the start write): a change writes the run facts again.
        self._trade_versions: tuple[api.TradeSettingsVersion, ...] = ()
        self._facts_trades: tuple[api.TradeSettingsVersion, ...] | None = None
        # Balances, account kind and today's fills, read on their own thread (display only).
        self.account_reads = account_reads
        # The live quotes file, written on its own thread (display only); the loop journals its
        # write failures.
        self.quote_file = quote_file
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
        if kind is api.CommandKind.ENTRY_STOP_OFF and self._closing is not None:
            # No new trade may open while a close_and_stop runs (its entry-stop fence).
            with store.transaction():
                self._record(command, api.CommandOutcome.REFUSED, STOP_RUNNING.format(
                    command=self._closing[0].command_id))
            return
        if kind in (api.CommandKind.ENTRY_STOP_ON, api.CommandKind.ENTRY_STOP_OFF):
            with store.transaction():
                self._record(command, api.CommandOutcome.DONE, None)
                engine.entry_stop_row(kind is api.CommandKind.ENTRY_STOP_ON,
                                      f"operator command {command.command_id}")
            return
        if kind is api.CommandKind.CANCEL_OWN_ORDERS:
            if command.symbol is not None and not any(
                    o.leg is Leg.ENTRY and o.live for t in engine.trades()
                    if t.symbol == command.symbol for o in engine.orders(t.trade_id)):
                with store.transaction():
                    self._record(command, api.CommandOutcome.REFUSED,
                                 NO_WORKING_ENTRY.format(symbol=command.symbol))
                return
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
        """Graceful stop. Without a mode it is obeyed only when flat: the marker first (durable,
        so no later start can miss it), then the record; the loop then ends and drains as on
        SIGTERM. Refused while a close is requested or working (an operator flatten, an
        emergency, the session flatten): stopping then would abandon an exit request that a
        later start would carry out without anyone asking. Refused while any trade is not
        closed, a working entry with no fill included: a broker stop is a DAY order and a
        stopped bot does not flatten at the flatten time, so a position (or an entry filling
        during the drain) could go overnight unprotected.

        With open trades the owner picks a mode (backlog V4, owner 2026-11-18):
        `close_and_stop` closes every own position through the flatten path and stops once
        flat (`carry_out_stop`); `leave_open` stops at once and leaves them under their broker
        stops (the drain still cancels a working entry and protects its fills), and nothing
        closes them later. Any stop-bot is refused while a close_and_stop is running."""
        engine = self.engine
        if self._closing is not None:
            with engine.store.transaction():
                self._record(command, api.CommandOutcome.REFUSED, STOP_RUNNING.format(
                    command=self._closing[0].command_id))
            return
        trades = engine.trades()
        if trades and command.mode is api.StopMode.CLOSE_AND_STOP:
            self._close_and_stop(command)
            return
        for trade in trades:
            orders = engine.orders(trade.trade_id)
            if trade.exit_requested or any(o.leg is Leg.EXIT and o.live for o in orders):
                with engine.store.transaction():
                    self._record(command, api.CommandOutcome.REFUSED, CLOSE_IN_PROGRESS)
                return
        if trades and command.mode is not api.StopMode.LEAVE_OPEN:
            with engine.store.transaction():
                self._record(command, api.CommandOutcome.REFUSED, (
                    f"{len(trades)} trade(s) open or entering; flatten first (or send "
                    "emergency), then stop-bot"))
            return
        self._obey_stop(command, TRADES_LEFT_OPEN.format(count=len(trades)) if trades else None)

    def _obey_stop(self, command: api.CommandV1, cause: str | None) -> None:
        """The marker first (durable), then the record and the `stopping` step in one write;
        the loop then ends and drains."""
        stopmarker.write(self.config, command.command_id, self.now())
        with self.engine.store.transaction():
            self._record(command, api.CommandOutcome.DONE, cause)
            self._step(command, api.StopStep.STOPPING)
        self.stopped_by = command.command_id

    def _step(self, command: api.CommandV1, step: api.StopStep) -> None:
        """Inside the caller's transaction: journal a stop-bot step and show it in status."""
        self.engine.store.append(STOP_PROGRESS, {
            "mode": command.mode, "step": step, "source": command.source,
            "requested_at": api.format_time(command.requested_at)},
            key=command.command_id, run_id=self.engine.run_id)
        self._progress = (command.command_id, command.mode, step, self.now())

    def _close_and_stop(self, command: api.CommandV1) -> None:
        """Start a close_and_stop: in one write the exit requests of every open trade (the
        flatten path, each close sent once by the engine and never resent), the entry-stop
        fence (no new trade while closing) and the `closing_positions` step; then cancel the
        working entries. Refused, with nothing changed, when the flatten is refused."""
        engine, cid = self.engine, command.command_id
        with engine.store.transaction():
            started = frozenset(t.trade_id for t in engine.trades())
            outcome, cause = self._flatten_rows(None, "operator-flatten")
            if outcome is api.CommandOutcome.REFUSED:
                self._record(command, outcome, f"flatten: {cause}")
                return
            engine.entry_stop_row(True, f"operator command {cid}")
            self._step(command, api.StopStep.CLOSING_POSITIONS)
        engine.cancel_entries()
        self._closing = (command, started)

    def carry_out_stop(self) -> None:
        """Each loop pass while a close_and_stop runs, in the bot (an app that disconnects
        changes nothing): once no own trade is open, `positions_closed`, then the stop as
        without a mode. Ended, refused, with the bot running and entry-stop on, as soon as a
        trade it began with holds a `CLOSE_FAILED_FENCES` fence (the engine then closes it no
        further without the operator, and never resends), or a trade it did not begin with is
        open. No time-out of its own: every close it waits on ends in a fill or in one of
        those fences (`[orders] max_exit_seconds` for a sent close, the session's close-order
        cutoff for one not sent); a close held by a condition that clears itself (a broker
        read, a Detail read, a quantity mismatch) is waited on like the session flatten waits
        on it, under that condition's own alert."""
        if self._closing is None:
            return
        command, started = self._closing
        engine = self.engine
        trades = engine.trades()
        if not trades:
            self._closing = None
            with engine.store.transaction():
                self._step(command, api.StopStep.POSITIONS_CLOSED)
            self._obey_stop(command, None)
            return
        cause = None
        new = sorted(t.trade_id for t in trades if t.trade_id not in started)
        if new:
            cause = STOP_NEW_TRADE.format(trades=", ".join(new))
        else:
            fences = engine.fences()
            failed = [(t.trade_id, kind) for t in trades
                      for key in (t.trade_id, *(o.client_order_id
                                                for o in engine.orders(t.trade_id)))
                      for kind in CLOSE_FAILED_FENCES if f"{kind}:{key}" in fences]
            if failed:
                cause = stop_close_failed(failed)
        if cause is not None:
            self._closing = None
            self._progress = None
            with engine.store.transaction():
                self._record(command, api.CommandOutcome.REFUSED, cause)

    def _close_interrupted_stop(self) -> None:
        """At startup: a close_and_stop a crash or restart interrupted (its latest step is
        before `stopping` and it has no record) is recorded refused, never resumed; the exit
        requests it made stay and the bot runs on."""
        store = self.engine.store
        if self._closing is not None:
            return  # this process is carrying it out
        rows = store.journal(STOP_PROGRESS, newest=1)
        if not rows or rows[0].key is None or rows[0].body["step"] not in (
                api.StopStep.CLOSING_POSITIONS, api.StopStep.POSITIONS_CLOSED):
            return
        cid, body = rows[0].key, rows[0].body
        if self._seen(cid):
            return
        command = api.CommandV1(cid, api.CommandKind.STOP_BOT,
                                api.parse_time(body["requested_at"]), body["source"],
                                mode=None if body["mode"] is None else api.StopMode(body["mode"]))
        with store.transaction():
            self._record(command, api.CommandOutcome.REFUSED, STOP_INTERRUPTED)

    def stop_progress(self) -> api.StopProgress | None:
        if self._progress is None:
            return None
        cid, mode, step, since = self._progress
        return api.StopProgress(cid, mode, step, since, len(self.engine.trades()))

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
        asked = self.engine.cancel_entries(command.symbol)
        with self.engine.store.transaction():
            self.engine.store.append(COMMAND_EFFECT, {"entries_cancel_requested": asked},
                                     key=command.command_id, run_id=self.engine.run_id)

    # --- protection tightened while running -------------------------------------------------

    def _entry_config(self) -> Config:
        """The config new entries are admitted under: the started one with the protection
        values in force."""
        return self.config if self.protection is None else self.protection.config

    def check_protection(self) -> None:
        """Once per loop pass, before any strategy work: adopt protection the owner tightened
        in the config file, for new entries only (runtime/liveprotection.py). Anything else is
        ignored for this run, journaled and alerted. An adoption is journaled, then its
        settings version is computed and recorded, then the desk takes the limits and the
        version together, then the run facts are written. Observers keep the settings version
        they started with: their values and the platform part they were given do not change."""
        if self.protection is None:
            return
        found = self.protection.check(self.now())
        if found is None:
            return
        store = self.engine.store
        if isinstance(found, Adoption):
            with store.transaction():  # journaled before admission uses it
                store.append(PROTECTION_ADOPTED, found.journal_body(), run_id=self.engine.run_id)
            if self.evaluator is not None:
                version = self._adopted_version(self.evaluator.desk.versions)
                self.evaluator.desk.adopt(self.protection.risk, version)
                self.strategy = replace(self.strategy, settings_version=version)
            self.write_run_facts()  # after the journal row and the adoption, never before
            return
        with store.transaction():
            store.append(PROTECTION_IGNORED, found.journal_body(), run_id=self.engine.run_id)
        if self.watch is not None:
            self.watch.alert(AlertKind.PROTECTION_CHANGE_IGNORED, found.alert_text())

    def _adopted_version(self, versions: Callable[[Config], SettingsVersion] | None) -> str | None:
        """The settings version of the config new entries are now admitted under, recorded.
        None when it cannot be computed or was recorded before with other contents: the
        adoption stands, never undone, and the version from before it is never kept; the
        refusal word is journaled (`settings-version-refused`). None without a way to compute
        one (tests)."""
        if versions is None or self.protection is None:
            return None
        try:
            version = versions(self.protection.config)
            self.engine.store.record_settings_version(version.value, version.contents)
        except SettingsVersionRefused as exc:
            cause, detail = exc.cause, exc.detail
        except SettingsVersionConflict as exc:
            cause, detail = CONFLICT, str(exc)
        else:
            return version.value
        with self.engine.store.transaction():
            self.engine.store.append(SETTINGS_VERSION_REFUSED,
                                     {"settings_version_refused": cause, "detail": detail},
                                     run_id=self.engine.run_id)
        return None

    def _protection_in_force(self) -> tuple[api.ProtectionValue, ...]:
        if self.protection is None:
            return ()
        return tuple(api.ProtectionValue(key, toml_text(value), at)
                     for key, value, at in self.protection.in_force())

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
        at the last-entry time, and while new entries are blocked (spec 9). Without a market
        there is no quote, so every working entry is cancelled ("no quote")."""
        from tickr.runtime.entries import (
            EntryQuotes,
            guard_entries,  # entries imports this module
        )

        if self._entry_quotes is None:
            market = self.market
            self._entry_quotes = EntryQuotes(
                self.engine, self.config,
                market.inbox.latest if market is not None else _no_quote,
                # read only when a stream quote is too old: a market's snapshot route
                None if market is None else (lambda symbols: market.source.snapshot(symbols)),
                self.engine.clock)
        now = self.now()
        quotes = self._entry_quotes
        quotes.refresh({o.symbol for o in self.engine.live_orders() if o.leg is Leg.ENTRY},
                       now)
        guard_entries(self.engine, self.config, self.calendar,
                      lambda symbol: quotes(symbol, now), now)

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
        """Market upkeep (a finished discovery pass collected) and the entry guard every pass;
        discovery starts and the strategy only when protection allows. The pass is timed
        against `[runtime] evaluation_budget_seconds`: discovery runs on its own thread
        (runtime/market.py), so this is the loop's own time; the pass's own duration is in its
        `discovery` row."""
        allowed = self._protect_first()
        started = monotonic()
        if self.market is not None:
            # After the engine's broker work of this pass. With an evaluator, discovery starts
            # inside its minute, after the positions' and WATCH symbols (Evaluator.tick).
            self.market.tick(discover=allowed and self.evaluator is None)
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
        try:
            self.evaluator.tick(self.market)
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
        work of this pass. Failures are journaled every pass. What the observers cannot take now
        (a full queue: the worker is behind; or the pass's cap on whole histories) is dropped,
        journaled as one `observers-dropped` row with the count; they go on with the next pass.
        A worker process that ended, or any error here, switches the observers off for the run,
        with one alert; trading goes on."""
        observers, evaluator = self.observers, self.evaluator
        if observers is None or evaluator is None:
            return
        try:
            self._hand_over(observers, evaluator)
            alive = observers.alive
        except Exception as exc:  # noqa: BLE001 - observers never end the trading loop
            self._stop_observers(observers, {"error": type(exc).__name__, "detail": str(exc)},
                                 f"observer hook failed ({type(exc).__name__}): observers "
                                 "switched off for this run")
            return
        if not alive:
            self._stop_observers(observers, {"error": "process-ended"},
                                 "observer process ended: observers switched off for this run")

    def _hand_over(self, observers: ObserverSet, evaluator: Evaluator) -> None:
        """Queue this pass's contexts (those the observers cannot take now are dropped) and
        journal the drops (one row with the count) and the worker's failures."""
        at = evaluator.evaluated_at
        dropped = 0
        if at is not None:
            for context in evaluator.contexts:
                if observers.on_evaluation(context, at):
                    dropped += 1
        failures = [f for f in observers.take_failures() if not f.cause.startswith(DROPPED)]
        if failures or dropped:
            store = self.engine.store
            with store.transaction():
                if dropped:
                    store.append(OBSERVERS_DROPPED, {"at": at.isoformat() if at else None,
                                                     "dropped": dropped},
                                 run_id=self.engine.run_id)
                for f in failures:
                    store.append(OBSERVER_FAILURE, {"folder": f.folder, "identity": f.identity,
                                                    "at": f.at.isoformat(), "cause": f.cause},
                                 run_id=self.engine.run_id)

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
        # Foreign positions and orders block only their own symbol (owner 2026-11-16): the
        # status names only what blocks every entry (`Engine.entry_fences`).
        blocking = self.engine.entry_fences(None)
        fences = [f for f in fences if f[0] in blocking]
        if fences:
            name, cause, _ = fences[0]
            fence = reasons.fence_reason(name, cause)
            return api.EntryBlock(name.split(":", 1)[0].replace("-", "_"), f"{name}: {cause}",
                                  params={"fence": fence.code, **fence.params})
        money = self._daily_money_block() or self._loss_lock_block()
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
        outside the entry window, or our own open trades take every position slot. Reported
        with `attention = False` so the apps can say "not now" without a warning. The answers
        are admission's own (its session check and its count of slots in use), so
        the status cannot say "allowed" while admission refuses. A calendar that cannot
        answer is a real block."""
        from tickr.runtime import entries  # entries imports this module

        now = self.now()
        day = entries.session_at(self.calendar, now)
        refused = session_refusal(day, now, AdmissionLimits.from_config(self.config))
        if refused is not None:
            return self._session_block(day, refused)
        if entries.slots_in_use(self.engine) >= self._entry_config().risk.max_open_positions:
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

        room = daily_loss_room(self.engine, self._entry_config(), self.now())
        if room is None:
            return api.EntryBlock("daily_money_unknown",
                                  "A trade closed today has fills without a known price.")
        risk = self._entry_config().risk
        if room < risk.min_daily_room_usd:
            return api.EntryBlock("daily_ceiling",
                                  f"Daily loss ceiling {risk.max_daily_loss_usd} USD: less than "
                                  f"{risk.min_daily_room_usd} USD of room left today.",
                                  params={"limit_usd": str(risk.max_daily_loss_usd),
                                          "min_room_usd": str(risk.min_daily_room_usd)})
        return None

    def _loss_lock_block(self) -> api.EntryBlock | None:
        """Entries the loss locks (backlog T11) refuse anyway: this week's money unknown, no
        room left under the weekly ceiling after the open reservations, or the stop-out streak
        at its limit. A lock at 0 is off. Admission still decides each entry."""
        from tickr.runtime.entries import (  # entries imports this module
            realized_loss_week,
            stop_out_streak,
        )

        risk, now = self._entry_config().risk, self.now()  # as tightened while running
        if risk.max_weekly_loss_usd != ZERO:
            loss = realized_loss_week(self.engine, now)
            if loss is None:
                return api.EntryBlock("weekly_money_unknown",
                                      "A trade closed this week has fills without a known price.")
            reserved = sum((t.reservation for t in self.engine.trades()), ZERO)
            if loss + reserved >= risk.max_weekly_loss_usd:
                return api.EntryBlock("weekly_ceiling",
                                      f"Weekly loss ceiling {risk.max_weekly_loss_usd} USD: no "
                                      "room left this week.",
                                      params={"limit_usd": str(risk.max_weekly_loss_usd)})
        if risk.stop_out_streak_limit:
            streak = stop_out_streak(self.engine, now)
            if streak >= risk.stop_out_streak_limit:
                return api.EntryBlock("stop_out_streak",
                                      f"{streak} stop-outs in a row today: no new entries "
                                      "until tomorrow.",
                                      params={"count": str(streak),
                                              "limit": str(risk.stop_out_streak_limit)})
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
        verified = [s for s in live if self.engine.protects(s)]
        if verified:
            return api.Stop(api.StopState.VERIFIED, trade.stop_price,
                            verified[0].client_order_id)
        # A stop once seen working whose broker row now disagrees or cannot be read is not
        # shown as verified: the engine does not count it either (`Engine.protects`).
        unknown = [s for s in live if s.send_state is SendState.UNKNOWN or s.verified]
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

    def _working_entries(self) -> tuple[api.WorkingEntry, ...]:
        """Every own trade whose entry order is working with nothing filled yet, oldest first;
        a fill of any part makes it a position (`_positions`)."""
        found = []
        for trade in self.engine.trades():
            orders = self.engine.orders(trade.trade_id)
            entries = [o for o in orders if o.leg is Leg.ENTRY]
            live = [o for o in entries if o.live]
            if not live or self.engine.expected_quantity(orders) > 0:
                continue
            entry, cause = self._entry_send(trade.trade_id, final=False)
            found.append(api.WorkingEntry(
                trade.symbol, trade.entry_quantity, trade.entry_limit_price, trade.stop_price,
                trade.planned_loss_usd, entry, cause,
                any(o.cancelling for o in live)))
        return tuple(found)

    def _daily_money(self) -> tuple[Decimal | None, Decimal, Decimal | None]:
        """(net realized loss today, open reservations, room) as admission computes them;
        loss and room are None when today's money is unknown."""
        from tickr.runtime import entries  # entries imports this module

        now = self.now()
        reserved = sum((t.reservation for t in self.engine.trades()), ZERO)
        return (entries.realized_loss_today(self.engine, now), reserved,
                entries.daily_loss_room(self.engine, self._entry_config(), now))

    def _loss_locks(self) -> tuple[Decimal | None, int]:
        """(this week's net realized loss, stop-outs in a row today) as admission counts them
        for the loss locks (backlog T11); the loss is None when this week's money is unknown."""
        from tickr.runtime import entries  # entries imports this module

        now = self.now()
        return (entries.realized_loss_week(self.engine, now),
                entries.stop_out_streak(self.engine, now))

    def _closed_today(self) -> list[api.DayTrade]:
        today = self.now().astimezone(NEW_YORK).date()
        return closed_trades(self.engine.store, closed_on=today)

    def _trades_today(self, closed: list[api.DayTrade]) -> tuple[api.Trade, ...]:
        """For status only: today's trades with their stop confirmation time and, once the
        broker's fees on every fill are recorded, their result after fees."""
        shown = [self._shown(t) for t in closed]
        self._trade_versions = tuple(
            api.TradeSettingsVersion(t.entry.client_order_id, t.strategy.settings_version)
            for t in shown if t.entry.client_order_id is not None and t.strategy is not None
            and t.strategy.settings_version is not None)
        return tuple(replace(_status_trade(t), result_exact_usd=t.result_net_usd) for t in shown)

    def _broker_result_today(self, closed: list[api.DayTrade]) -> Decimal | None:
        """Today's realized result as the broker counts it: its day profit or loss less the
        unrealized part, from the display reads' latest balance read (display only). The
        broker prices fills to the sub-cent and charges the fees, which the order rows do not
        show exactly (sub-dollar fill prices come back rounded to cents, probe 2026-11-16).
        None until a balance read made after the last of today's trades closed."""
        if self.account_reads is None:
            return None
        snapshot = self.account_reads.snapshot()
        balance, read_at = snapshot.balance, snapshot.read_at
        if balance is None or read_at is None:
            return None
        if any(trade.closed_at > read_at for trade in closed):
            return None
        return balance.day_profit_loss - balance.unrealized_profit_loss

    def _fees_today(self, closed: list[api.DayTrade]) -> Decimal | None:
        """The broker's fees on the fills of today's closed trades as recorded (the same the
        daily loss counts); None while any of them is not recorded, never counted as free."""
        total = ZERO
        for trade in closed:
            fees = recorded_fees(self.engine.orders(trade.trade_id))
            if fees is None:
                return None
            total += fees
        return total

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
        rows = self.engine.store.journal(COMMAND, newest=self.config.api.status_recent_commands)
        return tuple(api.CommandRecord(
            r.key or "", api.CommandKind(r.body["kind"]), _iso(r.body["requested_at"]),
            _iso(r.at_utc), api.CommandOutcome(r.body["outcome"]), r.body["cause"])
            for r in reversed(rows))

    def status(self) -> api.StatusV1:
        gate = trading_gate(self.config)
        fences = self.engine.fence_rows()
        closed = self._closed_today()
        trades = self._trades_today(closed)
        gross = sum((t.result_gross_usd for t in trades), ZERO)
        fees = self._fees_today(closed)
        positions = self._positions()
        loss, reserved, room = self._daily_money()
        return api.StatusV1(
            written_at=self.now(), bot_state=self.state, run_id=self.engine.run_id,
            environment=self.config.environment_name,
            trading_gate=self._gate(gate.open, gate.cause),
            strategy=api.strategy_name(self.strategy),
            entry_stop=any(name == Fence.ENTRY_STOP for name, _, _ in fences),
            entry_block=self._entry_block(gate.open, gate.cause, fences),
            fences=tuple(self._fence(name, cause, set_at) for name, cause, set_at in fences),
            position=positions[0] if positions else None, trades_today=trades,
            result_today_gross_usd=gross,
            freshness=(self.market.freshness(self.last_broker_read_at)
                       if self.market is not None
                       else api.Freshness(self.last_broker_read_at, None, api.StreamHealth.OFF)),
            recent_commands=self._recent_commands(),
            realized_loss_today_usd=loss, open_reservations_usd=reserved,
            daily_room_usd=room, positions=positions,
            code_version=self.engine.store.run_code_version(self.engine.run_id),
            fees_today_usd=fees, result_today_net_usd=total_net(closed),
            result_today_broker_usd=self._broker_result_today(closed),
            stop_progress=self.stop_progress(), working_entries=self._working_entries())

    def check_disk(self) -> None:
        """Each loop pass: below [runtime] min_free_disk_mb free on the data disk (or unknown),
        the disk-low fence stops new entries before a full disk stops the bot (2026-11-19 it
        crashed at 0 bytes free); protection, exits and the flatten go on. A set fence (low or
        unreadable) clears only at [runtime] disk_clear_free_mb, no flapping at the floor."""
        folder = self.paths.data
        if folder is None:
            return
        runtime = self.config.runtime
        try:
            free_mb = self._disk_free(folder) // BYTES_PER_MB
        except OSError as exc:
            self.engine.disk_room(f"free space on the data disk unreadable "
                                  f"({type(exc).__name__}): no new entry until it reads")
            return
        if free_mb < runtime.min_free_disk_mb:
            self.engine.disk_room(f"less than {runtime.min_free_disk_mb} MB free on the data "
                                  "disk: no new entry until there is room")
        elif free_mb >= runtime.disk_clear_free_mb:
            self.engine.disk_room(None)

    def write_status(self) -> None:
        api.write_status(self.paths.status, self.status())
        # Trades' settings versions and the loss locks' counts are run facts (status.json keeps
        # its older shape): written once when either changes (a trade closed today first shows,
        # a new day or week), never per pass.
        trades_changed = (self._facts_trades is not None
                          and self._trade_versions != self._facts_trades)
        locks_changed = (self._loss_locks_written is not None
                         and self._loss_locks() != self._loss_locks_written)
        if trades_changed or locks_changed:
            self.write_run_facts()
        if self.quote_file is not None:
            self.quote_file.journal_failures(self.engine)

    def run_facts(self) -> api.RunFactsV1:
        week_loss, streak = self._loss_locks_written = self._loss_locks()
        return api.RunFactsV1(written_at=self.now(), run_id=self.engine.run_id,
                              protection_in_force=self._protection_in_force(),
                              settings_version=self.strategy.settings_version,
                              trade_settings_versions=self._trade_versions,
                              realized_loss_week_usd=week_loss, stop_out_streak=streak)

    def write_run_facts(self) -> None:
        """At the start and after each change of what it holds (an adoption, a change of the
        settings versions of the trades the status shows or of the loss locks' counts), never
        per pass.
        Display only: a failed write never ends the loop nor undoes what changed, is never
        retried, and is journaled once per stretch of failures. The last file written stays: a
        failed write at the start leaves the previous run's file (the API shows no facts), but a
        failed write after an adoption leaves this run's earlier file, so the API keeps showing
        the values from before the adoption as in force until a later write succeeds; the
        journal (`protection-adopted`, `run-facts-write-failed`) is what holds."""
        if self.paths.run_facts is None:
            return
        self._facts_trades = self._trade_versions  # tried once, never again for the same
        try:
            api.write_run_facts(self.paths.run_facts, self.run_facts())
        except OSError as exc:
            if not self._run_facts_failing:
                with self.engine.store.transaction():
                    self.engine.store.append(RUN_FACTS_WRITE_FAILED,
                                             {"error": type(exc).__name__, "detail": str(exc)},
                                             run_id=self.engine.run_id)
            self._run_facts_failing = True
            return
        self._run_facts_failing = False

    def write_account(self) -> None:
        """After a successful reconciliation: what it read from the broker (docs/api.md)."""
        if self.paths.account is None:
            return
        balances = None
        if self.account_reads is not None:
            self.account_reads.journal_failures(self.engine)
            balances = self.account_reads.snapshot()
        document = account_document(self.engine, self.now(), balances)
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
            self._record_exact_fills()
            self.write_account()
        else:
            self._disagreed = False  # a failed read: the next two must agree again
        if ok and not self._broker_read_failing():
            self.last_broker_read_at = self.now()

    def _record_exact_fills(self) -> None:
        """After a successful reconciliation: our finished orders' exact fill prices from the
        display reads' latest snapshot (read on their thread, written here on the loop's)."""
        if self.account_reads is not None:
            record_exact_fills(self.engine, self.account_reads.snapshot())

    def _broker_read_failing(self) -> bool:
        """A positions or open-orders read is failing (the fence, plain or keyed)."""
        return any(n.split(":", 1)[0] == Fence.BROKER_READ for n in self.engine.fences())

    def _fence_after_early_watchdog_start(self) -> None:
        """A run the watchdog started early for a resting entry (its record says today)
        protects, cancels and flattens but takes no new entry: the `watchdog-started` fence,
        which only the operator clears (owner rule: a bot started unattended never opens a
        trade on its own). An unreadable record sets it too. Set once per record, so a later
        start the same day, after the operator cleared it, is not fenced again."""
        path = self.paths.watchdog_entry_restart
        if path is None:
            return
        try:
            record = json.loads(path.read_text("utf-8"))
            day, at = date.fromisoformat(record["ny_date"]), str(record["at"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            cause = (f"the watchdog's early restart record is unreadable ({type(exc).__name__})"
                     f": {WATCHDOG_FENCE_TAIL}")
        else:
            if day != ny_date_of(self.now()):
                return
            try:
                started = datetime.fromisoformat(at).astimezone(NEW_YORK).strftime("%H:%M")
            except (ValueError, TypeError):
                started = "an unreadable time"
            cause = (f"the watchdog started the bot at {started} for a resting entry: "
                     f"{WATCHDOG_FENCE_TAIL}")
        name = str(Fence.WATCHDOG_STARTED)
        if any(row.body.get("cause") == cause
               for row in self.engine.store.journal(FENCE_SET, key=name)):
            return
        self.engine.watchdog_started_fence(cause)

    def run(self, *, should_stop: Callable[[], bool],
            sleep: Callable[[float], None] = time.sleep,
            monotonic: Callable[[], float] = time.monotonic) -> bool:
        """Recover, loop until `should_stop()`, then drain (spec 11). Returns False when the
        drain hit shutdown_deadline_seconds: the caller exits non-zero and the supervisor
        alerts."""
        orders = self.config.orders
        self.write_status()
        self.write_run_facts()
        self.engine.recover()
        # A working entry is never resumed after a restart: its decision is stale and nothing
        # watched it while the bot was down. Cancelled now; a fill that races the cancel is
        # protected by the engine step like any fill.
        for trade in self.engine.trades():
            if trade.exit_requested is None:
                self.engine.cancel_entry(trade.trade_id, RESTART_ENTRY_CANCEL)
        self._fence_after_early_watchdog_start()
        self._close_interrupted_stop()
        # Nothing when the startup reconcile did not read.
        self._disagreed = record_reconcile(self.engine, False)
        if not self._broker_read_failing():
            self.last_broker_read_at = self.now()
            self._record_exact_fills()
            self.write_account()
        self.state = api.BotState.RUNNING
        last_reconcile = monotonic()
        while not should_stop():
            self.process_commands()
            self.carry_out_stop()
            if self.stopped_by is not None:
                break  # an operator stop-bot: nothing more this run, straight to the drain
            self.check_disk()
            self.check_protection()
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
        if self._progress is not None and self.stopped_by == self._progress[0]:
            cid, mode, _, _ = self._progress
            with self.engine.store.transaction():
                self.engine.store.append(STOP_PROGRESS, {"mode": mode,
                                                         "step": api.StopStep.STOPPED},
                                         key=cid, run_id=self.engine.run_id)
            self._progress = (cid, mode, api.StopStep.STOPPED, self.now())
        self.write_status()
        if not clean and self.watch is not None:
            pending = self.engine.store.journal("shutdown-deadline", newest=1)[-1].body["pending"]
            self.watch.alert(AlertKind.BOT_DID_NOT_STOP,
                             "shutdown deadline passed with work pending: " + "; ".join(pending))
        return clean
