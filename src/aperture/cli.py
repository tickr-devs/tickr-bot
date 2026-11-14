"""Command line entry point. Commands that only read local files; trading commands come later."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aperture import config as config_module
from aperture.lock import AlreadyRunning, SingleInstanceLock
from aperture.store import Store

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
    try:
        with SingleInstanceLock(lock_path):
            print("bot: not running")
    except AlreadyRunning:
        print("bot: running")
    if not database.exists():
        print("database: not created yet")
        return 0
    store = Store.open(database, busy_timeout_seconds=cfg.run.database_busy_timeout_seconds)
    try:
        print(f"database: schema v{store.schema_version}, {store.run_count()} runs")
        last = store.last_run()
        if last is not None:
            ended = last["ended_at"] or "still open"
            print(f"last run: started {last['started_at']}, ended {ended}"
                  f" ({last['end_cause'] or 'no cause recorded'})")
    finally:
        store.close()
    return 0


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
    args = parser.parse_args(argv)
    try:
        if args.command == "check-config":
            return _check_config(args.config)
        if args.command == "status":
            return _status(args.config)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
