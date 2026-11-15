"""Command line entry point: read-only commands plus the stage-3 manual trade commands."""

from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aperture import config as config_module
from aperture.broker.base import Broker
from aperture.calendar import CalendarRefused, SessionCalendar, ny_date_of
from aperture.data.base import MarketData
from aperture.engine.core import Engine, EntryRefused
from aperture.engine.manual import (
    CLOSE_WAITS,
    ManualRefused,
    check_window,
    close_refusal,
    flatten_deadline,
    plan_probe,
    run_until_closed,
)
from aperture.lock import AlreadyRunning, SingleInstanceLock, recorded_holder
from aperture.runtime import stopmarker
from aperture.store import DATABASE_FILE, MIGRATIONS, EnvironmentMismatch, Store

if TYPE_CHECKING:
    from aperture.observers.runner import ObserverSet
    from aperture.runtime.evaluation import Evaluator
    from aperture.runtime.market import StreamInbox

LOCK_FILE = "aperture.lock"


def data_paths(cfg: config_module.Config) -> tuple[Path, Path]:
    data = cfg.root / cfg.run.data_dir
    return data / DATABASE_FILE, data / LOCK_FILE


def _require_environment(store: Store, cfg: config_module.Config) -> None:
    """Before a run starts: the database must be this environment's own."""
    try:
        store.require_environment(cfg.environment_name)
    except EnvironmentMismatch:
        store.close()
        raise


def _status(path: Path) -> int:
    cfg = config_module.load(path)
    gate = config_module.trading_gate(cfg)
    database, lock_path = data_paths(cfg)
    print(f"environment: {cfg.environment_name}")
    print(f"trading gate: {'open' if gate.open else 'closed'} ({gate.cause})")
    pid = recorded_holder(lock_path)
    print("bot: not running" if pid is None
          else f"bot: pid {pid} recorded, process exists (lock not verified)")
    stopped = stopmarker.describe(cfg)
    if stopped is not None:
        print(f"starts refused: {stopped}")
    if not database.exists():
        print("database: not created yet")
        return 0
    try:
        store = Store.open_readonly(database)
    except sqlite3.Error as exc:
        print(f"database: unreadable ({exc})")
        return 1
    try:
        version = store.schema_version
        if version != len(MIGRATIONS):
            print(f"database: schema v{version}, this code expects v{len(MIGRATIONS)}")
            return 1
        print(f"database: schema v{version}, {store.run_count()} runs")
        last = store.last_run()
        if last is not None:
            ended = last["ended_at"] or "still open"
            print(f"last run: started {last['started_at']}, ended {ended}"
                  f" ({last['end_cause'] or 'no cause recorded'})")
    finally:
        store.close()
    return 0


def _webull_broker(cfg: config_module.Config) -> Broker:
    from aperture.broker.webull import broker_from_config  # optional dependency
    from aperture.runtime.alerts import token_pending_text

    def token_pending() -> None:
        wait = cfg.environment.token_check_duration_seconds
        print(f"waiting: {token_pending_text(wait)}", file=sys.stderr, flush=True)

    return broker_from_config(cfg, on_token_pending=token_pending)


@dataclass(frozen=True)
class RunAdapters:
    """What `aperture run` talks to: the broker, market data, and the L1 stream's lifecycle."""

    broker: Broker
    market: MarketData
    start_stream: Callable[[], None]
    stop_stream: Callable[[], None]


def _webull_run_adapters(cfg: config_module.Config,
                         on_stream_error: Callable[[str, Exception], None],
                         on_token_pending: Callable[[], None]) -> RunAdapters:
    from aperture.broker.webull import bot_adapters  # optional dependency
    broker, market, stream = bot_adapters(cfg, on_stream_error, on_token_pending)
    return RunAdapters(broker, market, stream.start, stream.stop)


def _webull_probe_adapters(cfg: config_module.Config) -> tuple[Broker, MarketData]:
    from aperture.broker.webull import probe_adapters  # optional dependency
    from aperture.runtime.alerts import token_pending_text

    def token_pending() -> None:
        wait = cfg.environment.token_check_duration_seconds
        print(f"waiting: {token_pending_text(wait)}", file=sys.stderr, flush=True)

    return probe_adapters(cfg, token_pending)


# Replaced in tests with fakes.
PROBE_ADAPTERS: Callable[[config_module.Config], tuple[Broker, MarketData]] = \
    _webull_probe_adapters
# Replaced in tests with fakes. Arguments: config, stream error sink, token-pending hook.
RUN_ADAPTERS: Callable[[config_module.Config, Callable[[str, Exception], None],
                        Callable[[], None]], RunAdapters] = _webull_run_adapters


def _default_notifier(alerts: config_module.AlertsConfig) -> Any:
    from aperture.runtime.alerts import default_notifier
    return default_notifier(alerts)


# Replaced in tests: the notifier `aperture run` alerts through (None = journal only).
NOTIFIER: Callable[[config_module.AlertsConfig], Any] = _default_notifier
# Replaced in tests with a FakeBroker factory.
BROKER_FACTORY: Callable[[config_module.Config], Broker] = _webull_broker
NOW: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731
SLEEP: Callable[[float], None] = time.sleep
# Replaced in tests (with SLEEP and NOW) so `aperture run` can live a whole day on a fake clock.
MONOTONIC: Callable[[], float] = time.monotonic


class _Interrupt:
    """First Ctrl-C asks for an orderly exit; the second one stops the command (stops rest)."""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, frame: Any) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        print("exit requested: closing the position through the normal exit path "
              "(Ctrl-C again to stop and leave the stop resting)", file=sys.stderr)


@contextmanager
def _engine(path: Path, broker: Broker | None = None,
            precheck: Callable[[Engine], None] | None = None) -> Iterator[Engine]:
    """Lock, open the store, recover. `precheck` runs on the records before recovery (which
    may already send cancels or orders)."""
    cfg = config_module.load(path)
    database, lock_path = data_paths(cfg)
    with SingleInstanceLock(lock_path):
        store = Store.open(database, busy_timeout_seconds=cfg.run.database_busy_timeout_seconds)
        _require_environment(store, cfg)
        run_id = store.start_run(environment=cfg.environment_name, config_digest=cfg.digest,
                                 strategy="manual", strategy_version="-", code_version="-")
        cause = "crashed"
        try:
            try:
                calendar: SessionCalendar | None = SessionCalendar.load(
                    (cfg.root / cfg.session.calendar_file).parent)
            except CalendarRefused:
                calendar = None  # closes at once, as `flatten_deadline` without a calendar
            engine = Engine(store, broker if broker is not None else BROKER_FACTORY(cfg), cfg,
                            run_id, lambda: config_module.trading_gate(cfg),
                            close_refusal=lambda: close_refusal(cfg, calendar, NOW()))
            if precheck is not None:
                precheck(engine)
            engine.recover()
            yield engine
            cause = "finished"
        except KeyboardInterrupt:
            cause = "interrupted"
            raise
        except (EntryRefused, ManualRefused):
            cause = "refused"
            raise
        finally:
            store.end_run(run_id, cause)
            store.close()


def _drive(engine: Engine, flatten_at: datetime | None, hold: float | None) -> int:
    interrupt = _Interrupt()
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        waiting = run_until_closed(engine, sleep=SLEEP, monotonic=time.monotonic, now=NOW,
                                   flatten_at=flatten_at,
                                   interrupted=lambda: interrupt.requested, hold_seconds=hold,
                                   notify=lambda line: print(line, file=sys.stderr))
    finally:
        signal.signal(signal.SIGINT, previous)
    fences = engine.fences()
    for trade_id in waiting:
        name = next((f"{f}:{trade_id}" for f in CLOSE_WAITS if f"{f}:{trade_id}" in fences),
                    f"exit-refused:{trade_id}")
        cause = fences.get(name, "closing order refused")
        print(f"trade {trade_id}: {cause}. Look in the Webull app, then "
              f"`aperture clear-fence {name}` and `aperture exit`.", file=sys.stderr)
    return 4 if waiting else 0


def _report(engine: Engine) -> None:
    for trade in engine.trades(open_only=False)[-1:]:
        row = engine.store.query("SELECT close_cause FROM trades WHERE trade_id = ?",
                                 (trade.trade_id,))[0]
        print(f"trade {trade.trade_id} {trade.symbol}: {trade.state}"
              f" ({row['close_cause'] or 'open'})")
    for name, cause in sorted(engine.fences().items()):
        print(f"fence {name}: {cause}")


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from exc


def _trade(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    plan = plan_probe(cfg, args.symbol, args.limit, args.stop, args.quantity)
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    flatten_at = check_window(cfg, calendar, NOW())
    print(f"plan: BUY {plan.quantity} {plan.symbol} limit {plan.limit_price}, "
          f"stop {plan.stop_price}, planned loss {plan.planned_loss_usd} USD")
    with _engine(args.config) as engine:
        engine.open_trade(plan.symbol, plan.quantity, plan.limit_price, plan.stop_price,
                          plan.planned_loss_usd,
                          None if args.stop is not None else cfg.probe.stop_percent_below_fill)
        code = _drive(engine, flatten_at, float(cfg.probe.hold_seconds))
        _report(engine)
    return code


def _resume(args: argparse.Namespace, *, exit_all: bool) -> int:
    cfg = config_module.load(args.config)
    try:
        calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
        flatten_at = flatten_deadline(cfg, calendar, NOW())
    except CalendarRefused as exc:
        print(f"calendar unreadable ({exc}): closing open trades now", file=sys.stderr)
        flatten_at = NOW()
    with _engine(args.config) as engine:
        if exit_all:
            for trade in engine.trades():
                engine.request_exit(trade.trade_id, "operator-exit")
        code = _drive(engine, flatten_at, None)
        _report(engine)
    return code


def _probe_replace_stop(args: argparse.Namespace) -> int:
    """Probe F5: move the open trade's stop once (tighten only) and print what Webull shows.
    Exit 0 confirmed, 3 refused or nothing changed, 4 the operator must look."""
    from aperture.engine import replace_probe

    cfg = config_module.load(args.config)
    gate = config_module.trading_gate(cfg)
    if not gate.open:
        raise ManualRefused(f"trading gate closed: {gate.cause}")
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    check_window(cfg, calendar, NOW())
    broker, market = PROBE_ADAPTERS(cfg)
    with _engine(args.config, broker, replace_probe.precheck) as engine:
        plan = replace_probe.plan_replace(engine, market, args.to, NOW())
        print(f"plan: stop {plan.stop.client_order_id} ({plan.stop.quantity} {plan.symbol}) "
              f"from {plan.current} to {plan.new}; bid {plan.bid}")
        verdict = replace_probe.Verdict.NOT_SENT
        if replace_probe.send(engine, plan, print):
            verdict = replace_probe.observe(engine, plan, sleep=SLEEP,
                                            monotonic=time.monotonic, out=print)
        print(f"F5 result: {verdict}")
        engine.reconcile()
        _report(engine)
    if verdict is replace_probe.Verdict.CONFIRMED:
        return 0
    return 3 if verdict is replace_probe.Verdict.NOT_SENT else 4


def _check_quote(args: argparse.Namespace) -> int:
    """Read-only: one snapshot quote through the probes' market-data path, and whether its age
    passes the check `probe-replace-stop` applies. Sends no orders. Exit 0 fresh, 3 not."""
    from aperture.engine.replace_probe import read_quote, stale_cause

    cfg = config_module.load(args.config)
    _, market = PROBE_ADAPTERS(cfg)
    quote = read_quote(market, args.symbol)
    age, stale = stale_cause(cfg, quote, NOW())
    print(f"{quote.symbol}: bid {quote.bid} ask {quote.ask}, provider time "
          f"{quote.provider_time.isoformat()}, age {age} s, "
          f"limit {cfg.risk.max_quote_age_seconds} s")
    if stale is not None:
        print(f"STALE: {stale}: the probes will refuse; the quotes may be delayed")
        return 3
    print(f"OK: quote is {age} s old (limit {cfg.risk.max_quote_age_seconds} s)")
    return 0


def _explain(args: argparse.Namespace) -> int:
    """Read-only: the latest decision of the active strategy recorded today for one symbol,
    from the decision file. No broker, no database writes. Exit 0 found, 1 none or unreadable."""
    from aperture.api.contract import explain
    from aperture.observers.log import DecisionLog
    from aperture.research.errors import RecordFileError

    cfg = config_module.load(args.config)
    day = ny_date_of(NOW())
    try:
        record = DecisionLog.from_config(cfg).latest_active(day, args.symbol)
    except (RecordFileError, OSError) as exc:
        print(f"decision file unreadable: {exc}", file=sys.stderr)
        return 1
    if record is None:
        print(f"no decision recorded for {args.symbol} on {day.isoformat()} "
              f"({cfg.environment_name})")
        return 1
    e = explain(cfg.environment_name, day, record)
    print(f"{e.symbol} {e.day.isoformat()}: {e.verdict} by {e.strategy} {e.version} at "
          f"{e.decided_at.isoformat()} (bar {e.bar_start.isoformat()}), setup {e.setup or '-'}")
    print(f"admission: {e.admission}"
          + (f" ({e.refusal_cause})" if e.refusal_cause is not None else ""))
    for c in e.checks:
        value = ("" if c.measured is None and c.threshold is None
                 else f": {c.measured} {c.operator} {c.threshold}")
        note = f" [{c.note}]" if c.note else ""
        print(f"  {'pass' if c.passed else 'FAIL'} {c.rule}{value}{note}")
    print(f"first failed check: {e.first_failed_check or 'none'}")
    return 0


def _maintain(args: argparse.Namespace) -> int:
    """Archive finished days' capture and decision files, checkpoint the database (see
    `aperture.maintain`). Holds the bot's lock: refused while the bot runs. Dry run unless
    --apply. Exit 0 done, 1 an archive failed, 3 refused."""
    from aperture import maintain

    cfg = config_module.load(args.config)
    database, lock_path = data_paths(cfg)
    try:
        calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    except CalendarRefused as exc:
        raise ManualRefused(f"calendar unreadable: {exc}") from exc
    with SingleInstanceLock(lock_path):
        try:
            return maintain.run(cfg, calendar, database, ny_date_of(NOW()), apply=args.apply,
                                out=print)
        except maintain.MaintainRefused as exc:
            raise ManualRefused(str(exc)) from exc


def _clear_stop(path: Path) -> int:
    cfg = config_module.load(path)
    if not stopmarker.clear(cfg):
        print("no stop marker: starts were not refused")
        return 1
    print("stop marker removed: the bot may start again (the next scheduled launchd start, or "
          "launchctl kickstart)")
    return 0


def _clear_fence(args: argparse.Namespace) -> int:
    with _engine(args.config) as engine:
        if not engine.clear_operator_fence(args.name):
            print(f"not an operator fence or not set: {args.name}", file=sys.stderr)
            return 1
        print(f"cleared {args.name}")
    return 0


def _evaluator(cfg: config_module.Config, engine: Engine, source: MarketData,
               inbox: StreamInbox, calendar: SessionCalendar | None,
               last_broker_read_at: Callable[[], datetime | None],
               observers_refused: Callable[[str], None]
               ) -> tuple[Evaluator, ObserverSet] | None:
    """The active strategy wired to the entry desk, and the observers fed the same contexts
    (both write one decision log). None (entries off, status says why) when there is no
    trading calendar: entries are refused without one anyway. Observers that cannot be
    loaded are reported to `observers_refused` and left out: a shadow strategy never stops
    trading."""
    from aperture.data.history import HistoryReader
    from aperture.observers.errors import ObserverError
    from aperture.observers.loader import load_active, load_observers
    from aperture.observers.log import DecisionLog
    from aperture.observers.runner import ObserverSet
    from aperture.runtime.entries import EntryDesk
    from aperture.runtime.evaluation import Evaluator

    if calendar is None:
        return None
    loaded = load_active(cfg)  # ObserverError: caller refuses entries
    log = DecisionLog.from_config(cfg)
    desk = EntryDesk(engine, cfg, calendar=calendar, last_broker_read_at=last_broker_read_at,
                     log=log)
    evaluator = Evaluator(engine, cfg, source, strategy=loaded.strategy, desk=desk,
                          history=HistoryReader.from_config(cfg, source, calendar),
                          inbox=inbox, calendar=calendar, now=NOW)
    try:
        shadows = load_observers(cfg)
    except ObserverError as exc:
        observers_refused(str(exc))
        shadows = ()
    observers = ObserverSet(shadows, log, cfg.observers.max_evaluation_seconds,
                            cfg.observers.queue_size)
    return evaluator, observers


def _closer(observers: ObserverSet, cfg: config_module.Config) -> Callable[[], None]:
    """Shutdown step: let queued observer evaluations finish (bounded), close the log."""
    def close() -> None:
        observers.close(float(cfg.observers.max_evaluation_seconds))
    return close


def _run(path: Path) -> int:
    """The launchd bot job (via `aperture.runtime run-bot`): run until SIGTERM, then drain.
    Exit 0 after a clean drain (SIGTERM or a stop-bot command), 3 refused while the operator's
    stop marker exists, 5 when the drain hit its deadline."""
    from aperture.data.capture import CaptureWriter
    from aperture.data.health import StreamHealth
    from aperture.observers.errors import ObserverError
    from aperture.runtime import service
    from aperture.runtime.alerts import Alerter, AlertKind, token_pending_text
    from aperture.runtime.market import MarketRuntime, StreamInbox
    from aperture.runtime.signals import StopRequest, install_stop_handler, restore_handlers
    from aperture.runtime.watch import ALERT, Watch

    cfg = config_module.load(path)
    stopped = stopmarker.describe(cfg)
    if stopped is not None:
        print(f"refused: {stopped}", file=sys.stderr)
        return 3
    strategy = service.strategy_id(cfg)
    database, lock_path = data_paths(cfg)
    with SingleInstanceLock(lock_path):
        store = Store.open(database, busy_timeout_seconds=cfg.run.database_busy_timeout_seconds,
                           now=NOW)
        _require_environment(store, cfg)
        run_id = store.start_run(environment=cfg.environment_name, config_digest=cfg.digest,
                                 strategy=strategy.name, strategy_version=strategy.version,
                                 code_version="-")
        stop = StopRequest()
        previous = install_stop_handler(stop)
        cause = "crashed"
        inbox = StreamInbox()
        adapters: RunAdapters | None = None
        capture: CaptureWriter | None = None
        observers: ObserverSet | None = None
        watch: Watch | None = None
        try:
            alerter = Alerter(cfg.alerts, f"Aperture {cfg.environment_name}",
                              NOTIFIER(cfg.alerts), clock=NOW)

            def token_pending() -> None:  # before the SDK waits for the owner (no engine yet)
                outcome = alerter.alert(AlertKind.TOKEN_NEEDS_CONFIRMATION, token_pending_text(
                    cfg.environment.token_check_duration_seconds))
                with store.transaction():
                    store.append(ALERT, outcome.journal_body(), run_id=run_id)

            adapters = RUN_ADAPTERS(cfg, inbox.on_error, token_pending)
            calendar = service.load_calendar(cfg)
            engine = Engine(store, adapters.broker, cfg, run_id,
                            lambda: config_module.trading_gate(cfg),
                            clock=lambda: NOW().timestamp(),
                            close_refusal=lambda: close_refusal(cfg, calendar, NOW()))
            capture = CaptureWriter.from_config(cfg, adapters.market.provider)
            market = MarketRuntime(engine, cfg, adapters.market,
                                   health=StreamHealth.from_config(cfg), inbox=inbox,
                                   capture=capture, calendar=calendar, now=NOW,
                                   monotonic=MONOTONIC)
            watch = Watch(engine, cfg, alerter, NOW)

            def observers_refused(cause: str) -> None:
                with store.transaction():
                    store.append(service.OBSERVERS_NOT_LOADED, {"cause": cause}, run_id=run_id)
                watch.alert(AlertKind.OBSERVERS_STOPPED, f"observers not loaded: {cause}")

            entries_off = None
            evaluator, observers = None, None
            try:
                wired = _evaluator(cfg, engine, adapters.market, inbox, calendar,
                                   lambda: bot.last_broker_read_at, observers_refused)
            except ObserverError as exc:  # the bot still protects and exits; no entries
                wired, entries_off = None, str(exc)
                with store.transaction():
                    store.append(service.STRATEGY_NOT_LOADED, {"cause": entries_off},
                                 run_id=run_id)
            if wired is not None:
                evaluator, observers = wired
            bot = service.Service(engine, now=NOW, calendar=calendar, strategy=strategy,
                                  paths=service.ServicePaths.from_config(cfg), market=market,
                                  evaluator=evaluator, entries_off=entries_off,
                                  observers=observers, watch=watch)
            market.bind()
            adapters.start_stream()
            clean = bot.run(should_stop=lambda: stop.requested, sleep=SLEEP,
                            monotonic=MONOTONIC)
            how = (f"operator command {bot.stopped_by}" if bot.stopped_by is not None
                   else stop.signal_name)
            cause = f"stopped ({how})" if clean else "shutdown-deadline"
        finally:
            # Each step guarded: a failing SDK disconnect must not skip recording the run end.
            for step in ((adapters.stop_stream if adapters is not None else None),
                         (capture.close if capture is not None else None),
                         (_closer(observers, cfg) if observers is not None else None),
                         (watch.flush_alerts if watch is not None else None),
                         lambda: restore_handlers(previous),
                         lambda: store.end_run(run_id, cause),
                         store.close):
                if step is None:
                    continue
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 - finish the remaining steps
                    print(f"shutdown step failed: {type(exc).__name__}", file=sys.stderr)
    if not clean:
        print("shutdown deadline reached with work pending: check orders and position in the "
              "Webull app, then `aperture status`", file=sys.stderr)
    return 0 if clean else 5


def _check_config(path: Path) -> int:
    text = path.read_text("utf-8")
    problems = config_module.uncommented_keys(text)
    for line, key in problems:
        print(f"{path}:{line}: key {key!r} has no comment")
    cfg = config_module.load(path)
    gate = config_module.trading_gate(cfg)
    print(f"environment: {cfg.environment_name}")
    print(f"config digest: {cfg.digest}")
    print(f"active strategy: {cfg.strategy.active}")
    print(f"trading gate: {'open' if gate.open else 'closed'} ({gate.cause})")
    from aperture.observers.errors import ObserverError
    from aperture.observers.loader import load_active
    try:
        loaded = load_active(cfg)
    except ObserverError as exc:
        print(f"strategy refused: {exc}")
        return 1
    print(f"strategy: {loaded.identity} needs {loaded.strategy.prior_sessions_needed} prior "
          f"sessions, history_sessions = {cfg.market_data.history_sessions}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aperture")
    parser.add_argument("--config", type=Path, default=Path("config/bot.toml"),
                        help="path to bot.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="validate config and show the trading gate")
    sub.add_parser("status", help="show environment, gate, bot liveness and last run")
    trade = sub.add_parser("trade", help="one protected manual trade within the [probe] limits")
    trade.add_argument("symbol")
    trade.add_argument("--limit", type=_decimal, required=True, help="entry limit price, USD")
    trade.add_argument("--stop", type=_decimal, help="stop price, USD (default from [probe])")
    trade.add_argument("--quantity", type=_decimal, help="shares (default from [probe])")
    replace = sub.add_parser("probe-replace-stop",
                             help="probe F5: tighten the open trade's stop once by replace")
    replace.add_argument("--to", type=_decimal, required=True, help="new stop price, USD")
    sub.add_parser("run", help="the bot service (launchd): protect, obey commands, drain on "
                   "SIGTERM")
    sub.add_parser("resume", help="recover after a crash and keep protecting open trades")
    sub.add_parser("exit", help="close every open trade through the normal exit path")
    quote = sub.add_parser("check-quote", help="read-only: is the snapshot quote fresh enough "
                           "for the probes? Sends no orders")
    quote.add_argument("symbol")
    explain_parser = sub.add_parser("explain", help="read-only: the latest decision recorded "
                                    "today for a symbol, with every check")
    explain_parser.add_argument("symbol")
    maint = sub.add_parser("maintain", help="with the bot stopped: gzip finished days' capture "
                           "and decision files, checkpoint the database (dry run by default)")
    maint.add_argument("--apply", action="store_true", help="act; without it only print")
    fence = sub.add_parser("clear-fence", help="clear an operator fence after looking at it")
    fence.add_argument("name")
    sub.add_parser("clear-stop", help="allow starts again after an operator stop-bot command")
    sub.add_parser("demo", help="a made-up trading day in its own folder (config/demo.toml) "
                   "served to the apps; never talks to Webull")
    args = parser.parse_args(argv)
    try:
        if args.command == "trade":
            return _trade(args)
        if args.command == "run":
            return _run(args.config)
        if args.command == "resume":
            return _resume(args, exit_all=False)
        if args.command == "exit":
            return _resume(args, exit_all=True)
        if args.command == "probe-replace-stop":
            return _probe_replace_stop(args)
        if args.command == "check-quote":
            return _check_quote(args)
        if args.command == "explain":
            return _explain(args)
        if args.command == "maintain":
            return _maintain(args)
        if args.command == "clear-fence":
            return _clear_fence(args)
        if args.command == "clear-stop":
            return _clear_stop(args.config)
        if args.command == "check-config":
            return _check_config(args.config)
        if args.command == "status":
            return _status(args.config)
        if args.command == "demo":
            from aperture.demo.run import main as demo_main
            repo = config_module.load(args.config).root
            return demo_main(repo, repo / "config" / "demo.toml")
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (ManualRefused, EntryRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except EnvironmentMismatch as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except AlreadyRunning:
        print("refused: another aperture process holds the lock", file=sys.stderr)
        return 3
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
