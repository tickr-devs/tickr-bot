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


# Forward-only migrations. Never edit a released entry; append a new one.
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_schema_v1,)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class JournalRow:
    seq: int
    at_utc: str
    run_id: str | None
    kind: str
    key: str | None
    body: dict[str, Any]


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._in_transaction = False

    @classmethod
    def open(cls, path: Path | str, *, busy_timeout_seconds: Decimal | float) -> Store:
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
        store = cls(db)
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
            (now_utc(), run_id, kind, key,
             json.dumps(body, default=_json_default, sort_keys=True)),
        )
        return int(cursor.lastrowid)

    def start_run(self, *, environment: str, config_digest: str, strategy: str,
                  strategy_version: str, code_version: str) -> str:
        run_id = uuid.uuid4().hex
        with self.transaction():
            self._db.execute(
                "INSERT INTO runs(run_id, started_at, environment, config_digest, strategy, "
                "strategy_version, code_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, now_utc(), environment, config_digest, strategy, strategy_version,
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
                (now_utc(), cause, run_id),
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
