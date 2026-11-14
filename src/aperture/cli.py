"""Command line entry point: read-only commands plus the stage-3 manual trade commands."""

from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aperture import config as config_module
from aperture.broker.base import Broker
from aperture.calendar import CalendarRefused, SessionCalendar
from aperture.engine.core import Engine, EntryRefused
from aperture.engine.manual import (
    ManualRefused,
    check_window,
    flatten_deadline,
    plan_probe,
    run_until_closed,
)
from aperture.lock import AlreadyRunning, SingleInstanceLock, recorded_holder
from aperture.store import MIGRATIONS, Store

DATABASE_FILE = "aperture.sqlite3"
LOCK_FILE = "aperture.lock"


def data_paths(cfg: config_module.Config) -> tuple[Path, Path]:
    data = cfg.root / cfg.run.data_dir
    return data / DATABASE_FILE, data / LOCK_FILE


def _status(path: Path) -> int:
    cfg = config_module.load(path)
    gate = config_module.trading_gate(cfg)
    database, lock_path = data_paths(cfg)
    print(f"environment: {cfg.environment_name}")
    print(f"trading gate: {'open' if gate.open else 'closed'} ({gate.cause})")
    pid = recorded_holder(lock_path)
    print("bot: not running" if pid is None
          else f"bot: pid {pid} recorded, process exists (lock not verified)")
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
    return broker_from_config(cfg)


# Replaced in tests with a FakeBroker factory.
BROKER_FACTORY: Callable[[config_module.Config], Broker] = _webull_broker
NOW: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731
SLEEP: Callable[[float], None] = time.sleep


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
def _engine(path: Path) -> Iterator[Engine]:
    cfg = config_module.load(path)
    database, lock_path = data_paths(cfg)
    with SingleInstanceLock(lock_path):
        store = Store.open(database, busy_timeout_seconds=cfg.run.database_busy_timeout_seconds)
        run_id = store.start_run(environment=cfg.environment_name, config_digest=cfg.digest,
                                 strategy="manual", strategy_version="-", code_version="-")
        cause = "crashed"
        try:
            engine = Engine(store, BROKER_FACTORY(cfg), cfg, run_id,
                            lambda: config_module.trading_gate(cfg))
            engine.recover()
            yield engine
            cause = "finished"
        except KeyboardInterrupt:
            cause = "interrupted"
            raise
        except EntryRefused:
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
    for trade_id in waiting:
        print(f"trade {trade_id}: closing order refused; the stop is back in place. Look in "
              f"the Webull app, then `aperture clear-fence exit-refused:{trade_id}` and "
              "`aperture exit`.", file=sys.stderr)
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


def _clear_fence(args: argparse.Namespace) -> int:
    with _engine(args.config) as engine:
        if not engine.clear_operator_fence(args.name):
            print(f"not an operator fence or not set: {args.name}", file=sys.stderr)
            return 1
        print(f"cleared {args.name}")
    return 0


def _run(path: Path) -> int:
    """The launchd bot job (via `aperture.runtime run-bot`): run until SIGTERM, then drain.
    Exit 0 after a clean drain, 5 when the drain hit its deadline."""
    from aperture.runtime import service
    from aperture.runtime.signals import StopRequest, install_stop_handler, restore_handlers

    cfg = config_module.load(path)
    strategy = service.strategy_id(cfg)
    database, lock_path = data_paths(cfg)
    with SingleInstanceLock(lock_path):
        store = Store.open(database, busy_timeout_seconds=cfg.run.database_busy_timeout_seconds)
        run_id = store.start_run(environment=cfg.environment_name, config_digest=cfg.digest,
                                 strategy=strategy.name, strategy_version=strategy.version,
                                 code_version="-")
        stop = StopRequest()
        previous = install_stop_handler(stop)
        cause = "crashed"
        try:
            engine = Engine(store, BROKER_FACTORY(cfg), cfg, run_id,
                            lambda: config_module.trading_gate(cfg))
            bot = service.Service(engine, now=NOW, calendar=service.load_calendar(cfg),
                                  strategy=strategy,
                                  paths=service.ServicePaths.from_config(cfg))
            clean = bot.run(should_stop=lambda: stop.requested, sleep=SLEEP)
            cause = f"stopped ({stop.signal_name})" if clean else "shutdown-deadline"
        finally:
            restore_handlers(previous)
            store.end_run(run_id, cause)
            store.close()
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
    sub.add_parser("run", help="the bot service (launchd): protect, obey commands, drain on "
                   "SIGTERM")
    sub.add_parser("resume", help="recover after a crash and keep protecting open trades")
    sub.add_parser("exit", help="close every open trade through the normal exit path")
    fence = sub.add_parser("clear-fence", help="clear an operator fence after looking at it")
    fence.add_argument("name")
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
        if args.command == "clear-fence":
            return _clear_fence(args)
        if args.command == "check-config":
            return _check_config(args.config)
        if args.command == "status":
            return _status(args.config)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (ManualRefused, EntryRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except AlreadyRunning:
        print("refused: another aperture process holds the lock", file=sys.stderr)
        return 3
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
