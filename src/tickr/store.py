"""SQLite store: append-only journal plus state tables, one writer.

Commit-before-effect: callers write the journal rows (and state changes) in one transaction
and only then perform the side effect (for example an HTTP send).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.version import recorded

# Database file name inside [run] data_dir (structural: the bot, the CLI and the API all use it).
DATABASE_FILE = "tickr.sqlite3"

_SCHEMA_V1 = (
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        environment TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        strategy TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        code_version TEXT NOT NULL,
        ended_at TEXT,
        end_cause TEXT
    )""",
    """CREATE TABLE journal (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at_utc TEXT NOT NULL,
        run_id TEXT,
        kind TEXT NOT NULL,
        key TEXT,
        body TEXT NOT NULL
    )""",
    "CREATE INDEX journal_kind_key ON journal(kind, key)",
    """CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal
        BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END""",
    """CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal
        BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END""",
)


def _schema_v1(db: sqlite3.Connection) -> None:
    # Statements run one by one: executescript() would COMMIT the open transaction.
    for statement in _SCHEMA_V1:
        db.execute(statement)


# v2: engine state tables. Decimals are stored as text; times as UTC ISO-8601 text.
_SCHEMA_V2 = (
    """CREATE TABLE trades (
        trade_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        state TEXT NOT NULL,
        entry_quantity TEXT NOT NULL,
        entry_limit_price TEXT NOT NULL,
        stop_price TEXT NOT NULL,
        planned_loss_usd TEXT NOT NULL,
        exit_requested TEXT,
        stop_failures INTEGER NOT NULL DEFAULT 0,
        last_stop_failure_at REAL,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        close_cause TEXT
    )""",
    """CREATE TABLE orders (
        client_order_id TEXT PRIMARY KEY,
        trade_id TEXT NOT NULL REFERENCES trades(trade_id),
        leg TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        order_type TEXT NOT NULL,
        quantity TEXT NOT NULL,
        limit_price TEXT,
        stop_price TEXT,
        send_state TEXT NOT NULL,
        broker_state TEXT,
        filled_quantity TEXT NOT NULL DEFAULT '0',
        average_fill_price TEXT,
        broker_order_id TEXT,
        refusal TEXT,
        absent_reads INTEGER NOT NULL DEFAULT 0,
        cancel_state TEXT,
        cancel_attempts INTEGER NOT NULL DEFAULT 0,
        cancel_at REAL,
        last_detail_at REAL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX orders_trade ON orders(trade_id)",
    """CREATE TABLE fences (
        name TEXT PRIMARY KEY,
        cause TEXT NOT NULL,
        set_at TEXT NOT NULL,
        set_at_epoch REAL NOT NULL
    )""",
    """CREATE TABLE counters (
        name TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )""",
)


def _schema_v2(db: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V2:
        db.execute(statement)


def _schema_v3(db: sqlite3.Connection) -> None:
    # Per-trade stop distance below the actual fill (NULL: keep the stop price as given).
    db.execute("ALTER TABLE trades ADD COLUMN stop_percent_below_fill TEXT")


def _schema_v4(db: sqlite3.Connection) -> None:
    # Bottom of the strategy's entry price window (NULL: none given, e.g. a manual trade); a
    # resting entry is cancelled once the ask leaves the window (spec 9).
    db.execute("ALTER TABLE trades ADD COLUMN entry_low_price TEXT")


def _schema_v5(db: sqlite3.Connection) -> None:
    # A filled order's exact average fill price, from the account's cash activity of the fill
    # (NULL: not known; results use average_fill_price, which the broker's order rows round to
    # cents for sub-dollar prices). Written once by the loop, never overwritten.
    db.execute("ALTER TABLE orders ADD COLUMN exact_fill_price TEXT")


def _schema_v6(db: sqlite3.Connection) -> None:
    # A finished order's fees as the broker charged them (sum of the order history row's
    # `fees[].actual_value`; NULL: not read yet). The daily loss counts them (owner decision
    # 2026-11-16). Written once by the loop, never overwritten.
    db.execute("ALTER TABLE orders ADD COLUMN fees_usd TEXT")


def _schema_v7(db: sqlite3.Connection) -> None:
    # A finished order's settled cash amount from the account's cash activity of its fill,
    # signed (a buy is negative), fees included (NULL: not proven yet). Once every filled
    # order of a trade has one, their sum is the trade's result. Written once by the loop.
    db.execute("ALTER TABLE orders ADD COLUMN net_cash_usd TEXT")


def _schema_v8(db: sqlite3.Connection) -> None:
    # Until when a resting limit entry may work (UTC ISO; NULL: a marketable entry, cancelled
    # once the ask leaves its window). Decision.rest_bars, runtime.entries.guard_entries.
    db.execute("ALTER TABLE trades ADD COLUMN entry_rest_until TEXT")


def _schema_v9(db: sqlite3.Connection) -> None:
    # Scale-out (engine.core.request_reduce): the shares the entry decision plans to sell at its
    # first target (NULL: none; stops are then not split for it), and one partial sale: how
    # many shares, why, and its state (NULL: none asked; requested, sent, done).
    for column in ("scale_out_quantity", "reduce_quantity", "reduce_cause", "reduce_state"):
        db.execute(f"ALTER TABLE trades ADD COLUMN {column} TEXT")


# Forward-only migrations. Never edit a released entry; append a new one.
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _schema_v1, _schema_v2, _schema_v3, _schema_v4, _schema_v5, _schema_v6, _schema_v7,
    _schema_v8, _schema_v9)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EnvironmentMismatch(Exception):
    """The database belongs to another environment; nothing was started."""


@dataclass(frozen=True)
class JournalRow:
    seq: int
    at_utc: str
    run_id: str | None
    kind: str
    key: str | None
    body: dict[str, Any]


class Store:
    def __init__(self, connection: sqlite3.Connection,
                 now: Callable[[], datetime] = _utc_now) -> None:
        self._db = connection
        self._in_transaction = False
        self._now = now

    def stamp(self) -> str:
        """The time written into rows (journal, runs, orders, trades): the bot's clock, so a
        rehearsal on a fake clock dates its trades on the day it lives."""
        return self._now().astimezone(UTC).isoformat()

    @classmethod
    def open(cls, path: Path | str, *, busy_timeout_seconds: Decimal | float,
             now: Callable[[], datetime] = _utc_now) -> Store:
        """Open (and if needed migrate) the database.

        An existing database older than this code is first copied to
        `<name>.pre-v<N>.sqlite3` (verified) and only then migrated.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists() and path.stat().st_size > 0
        db = sqlite3.connect(str(path), isolation_level=None,
                             timeout=float(busy_timeout_seconds))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        if sys.platform == "darwin":
            # macOS: synchronous=FULL alone does not flush the disk cache.
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA checkpoint_fullfsync=ON")
        store = cls(db, now)
        current = store.schema_version
        if existed and 0 < current < len(MIGRATIONS):
            store.backup(path.with_name(f"{path.stem}.pre-v{current + 1}{path.suffix}"))
        store._migrate()
        return store

    @classmethod
    def open_readonly(cls, path: Path | str) -> Store:
        """Read-only connection for status readers: never migrates, never writes."""
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        return cls(sqlite3.connect(uri, uri=True, isolation_level=None))

    def close(self) -> None:
        self._db.close()

    @property
    def schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.schema_version
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"database schema {current} is newer than this code ({len(MIGRATIONS)})"
            )
        for version in range(current, len(MIGRATIONS)):
            with self.transaction():
                MIGRATIONS[version](self._db)
                self._db.execute(f"PRAGMA user_version = {version + 1}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One durable transaction. Nested use is refused to keep commit points explicit."""
        if self._in_transaction:
            raise RuntimeError("nested transaction")
        # BEGIN first: if it fails (for example the write lock is busy) nothing is open
        # and the flag stays clear, so the next transaction can still start.
        self._db.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            # Covers a failed body and a failed COMMIT: never leave a transaction open.
            if self._db.in_transaction:
                with suppress(sqlite3.Error):
                    self._db.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    def append(self, kind: str, body: dict[str, Any], *, key: str | None = None,
               run_id: str | None = None) -> int:
        """Append one journal row. Must be called inside `transaction()`."""
        if not self._in_transaction:
            raise RuntimeError("journal rows are written only inside a transaction")
        cursor = self._db.execute(
            "INSERT INTO journal(at_utc, run_id, kind, key, body) VALUES (?, ?, ?, ?, ?)",
            (self.stamp(), run_id, kind, key,
             json.dumps(body, default=_json_default, sort_keys=True)),
        )
        return int(cursor.lastrowid)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Write to a state table. Must be called inside `transaction()`; returns rowcount."""
        if not self._in_transaction:
            raise RuntimeError("state is written only inside a transaction")
        return self._db.execute(sql, params).rowcount

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        cursor = self._db.execute(sql, params)
        cursor.row_factory = sqlite3.Row
        return cursor.fetchall()

    def next_counter(self, name: str) -> int:
        """Durable monotonically increasing counter (inside a transaction)."""
        if not self._in_transaction:
            raise RuntimeError("counters are advanced only inside a transaction")
        self._db.execute(
            "INSERT INTO counters(name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,))
        return int(self._db.execute("SELECT value FROM counters WHERE name = ?",
                                    (name,)).fetchone()[0])

    def require_environment(self, environment: str) -> None:
        """Refuse a database that holds runs of another environment (spec 10: one database
        per environment). Its open trades, fences and day result would be taken as this
        environment's own."""
        row = self._db.execute("SELECT environment FROM runs WHERE environment != ? LIMIT 1",
                               (environment,)).fetchone()
        if row is not None:
            raise EnvironmentMismatch(
                f"the database holds runs of environment {row[0]!r}, the config says "
                f"{environment!r}: move the database aside (or point [run] data_dir at "
                "another folder) before running this environment")

    def start_run(self, *, environment: str, config_digest: str, strategy: str,
                  strategy_version: str, code_version: str) -> str:
        run_id = uuid.uuid4().hex
        with self.transaction():
            self._db.execute(
                "INSERT INTO runs(run_id, started_at, environment, config_digest, strategy, "
                "strategy_version, code_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, self.stamp(), environment, config_digest, strategy, strategy_version,
                 code_version),
            )
            self.append("run-started", {"environment": environment,
                                        "config_digest": config_digest,
                                        "strategy": strategy,
                                        "strategy_version": strategy_version,
                                        "code_version": code_version},
                        key=run_id, run_id=run_id)
        return run_id

    def end_run(self, run_id: str, cause: str) -> None:
        with self.transaction():
            self._db.execute(
                "UPDATE runs SET ended_at = ?, end_cause = ? WHERE run_id = ?",
                (self.stamp(), cause, run_id),
            )
            self.append("run-ended", {"cause": cause}, key=run_id, run_id=run_id)

    def journal(self, kind: str | None = None, key: str | None = None) -> list[JournalRow]:
        sql = "SELECT seq, at_utc, run_id, kind, key, body FROM journal"
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if key is not None:
            clauses.append("key = ?")
            params.append(key)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq"
        return [JournalRow(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]))
                for r in self._db.execute(sql, params)]

    def last_run(self) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT run_id, started_at, environment, strategy, strategy_version, ended_at, "
            "end_cause FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        names = ("run_id", "started_at", "environment", "strategy", "strategy_version",
                 "ended_at", "end_cause")
        return dict(zip(names, row, strict=True))

    def run_code_version(self, run_id: str) -> str | None:
        """The code version recorded on run `run_id`; None for an unknown run or one recorded
        before the version was determined (`-`), never guessed."""
        row = self._db.execute("SELECT code_version FROM runs WHERE run_id = ?",
                               (run_id,)).fetchone()
        return None if row is None else recorded(row[0])

    def run_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    def backup(self, target: Path | str) -> None:
        """Write a consistent copy and verify it with integrity_check."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        self._db.execute("VACUUM INTO ?", (str(target),))
        check = sqlite3.connect(str(target))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            raise RuntimeError(f"backup failed integrity_check: {result}")
