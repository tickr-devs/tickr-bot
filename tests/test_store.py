from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from tickr.store import (
    DATABASE_FILE,
    LEGACY_DATABASE_FILE,
    MIGRATIONS,
    EnvironmentMismatch,
    LegacyDatabaseError,
    Store,
)


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "data" / "tickr.sqlite3", busy_timeout_seconds=0)
    yield s
    s.close()


def test_open_migrates_and_uses_wal(store):
    assert store.schema_version == len(MIGRATIONS)
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_reopen_is_idempotent(tmp_path):
    path = tmp_path / "a.sqlite3"
    Store.open(path, busy_timeout_seconds=0).close()
    s = Store.open(path, busy_timeout_seconds=0)
    assert s.schema_version == len(MIGRATIONS)
    s.close()


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "a.sqlite3"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store.open(path, busy_timeout_seconds=0)


def test_append_requires_transaction(store):
    with pytest.raises(RuntimeError):
        store.append("x", {})


def test_rollback_discards_rows(store):
    with pytest.raises(ValueError), store.transaction():
        store.append("intent", {"price": Decimal("1.23")})
        raise ValueError("boom")
    assert store.journal() == []


def test_journal_is_append_only(store):
    with store.transaction():
        store.append("intent", {"price": Decimal("1.23")}, key="ap-1")
    rows = store.journal(kind="intent", key="ap-1")
    assert rows[0].body == {"price": "1.23"}
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("UPDATE journal SET kind = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("DELETE FROM journal")


def test_nested_transaction_refused(store):
    with pytest.raises(RuntimeError, match="nested"), store.transaction(), store.transaction():
        pass


def test_runs_recorded(store):
    run_id = store.start_run(environment="env", config_digest="d", strategy="s",
                             strategy_version="1", code_version="abc")
    store.end_run(run_id, "stopped")
    assert store.run_count() == 1
    assert [r.kind for r in store.journal(key=run_id)] == ["run-started", "run-ended"]


def test_backup_is_verified(store, tmp_path):
    with store.transaction():
        store.append("intent", {"n": 1})
    target = tmp_path / "backup" / "copy.sqlite3"
    store.backup(target)
    copy = Store.open(target, busy_timeout_seconds=0)
    assert len(copy.journal()) == 1
    copy.close()
    with pytest.raises(FileExistsError):
        store.backup(target)


def test_busy_begin_does_not_wedge_the_store(store, tmp_path):
    other = sqlite3.connect(tmp_path / "data" / "tickr.sqlite3", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="locked"), store.transaction():
        pass
    other.execute("ROLLBACK")
    other.close()
    with store.transaction():
        store.append("intent", {"n": 1})
    assert len(store.journal()) == 1


def test_failed_commit_rolls_back(store):
    # A deferred foreign key violation makes COMMIT itself fail.
    with store.transaction():
        store._db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        store._db.execute("CREATE TABLE child (pid INTEGER REFERENCES parent(id) "
                          "DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError), store.transaction():
        store.append("intent", {"n": 1})
        store._db.execute("INSERT INTO child VALUES (42)")
    assert not store._db.in_transaction
    assert store.journal() == []
    with store.transaction():
        store.append("intent", {"n": 2})
    assert [r.body["n"] for r in store.journal()] == [2]


def test_migration_backs_up_existing_database(tmp_path, monkeypatch):
    import tickr.store as store_module
    path = tmp_path / "a.sqlite3"
    s = Store.open(path, busy_timeout_seconds=0)
    with s.transaction():
        s.append("intent", {"n": 1})
    s.close()
    added = []
    monkeypatch.setattr(store_module, "MIGRATIONS",
                        (*MIGRATIONS, lambda db: added.append(True)))
    upgraded = Store.open(path, busy_timeout_seconds=0)
    assert added == [True]
    assert upgraded.schema_version == len(MIGRATIONS) + 1
    upgraded.close()
    backup = tmp_path / f"a.pre-v{len(MIGRATIONS) + 1}.sqlite3"
    copy = sqlite3.connect(backup)
    assert copy.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert copy.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 1
    copy.close()


def test_new_database_needs_no_backup(tmp_path):
    Store.open(tmp_path / "a.sqlite3", busy_timeout_seconds=0).close()
    assert sorted(p.name for p in tmp_path.glob("*.pre-v*")) == []


def test_v2_database_gains_the_stop_percent_column(tmp_path):
    path = tmp_path / "a.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    for migration in MIGRATIONS[:2]:
        migration(db)
    db.execute("PRAGMA user_version = 2")
    db.execute("INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
               "entry_limit_price, stop_price, planned_loss_usd, opened_at) "
               "VALUES ('t1', 'r', 'ABC', 'open', '1', '10', '9.8', '0.2', 'x')")
    db.close()
    s = Store.open(path, busy_timeout_seconds=0)
    (row,) = s.query("SELECT trade_id, stop_percent_below_fill FROM trades")
    assert (row["trade_id"], row["stop_percent_below_fill"]) == ("t1", None)
    assert (tmp_path / "a.pre-v3.sqlite3").exists()
    s.close()


def test_a_database_with_runs_of_another_environment_is_refused(tmp_path):
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    store.require_environment("one")  # empty: any environment may start
    store.start_run(environment="one", config_digest="d", strategy="s",
                    strategy_version="1", code_version="x")
    store.require_environment("one")
    with pytest.raises(EnvironmentMismatch, match="'one'"):
        store.require_environment("two")
    store.close()


def test_open_refuses_a_new_database_next_to_the_pre_rename_one(tmp_path):
    Store.open(tmp_path / LEGACY_DATABASE_FILE, busy_timeout_seconds=0).close()
    with pytest.raises(LegacyDatabaseError, match="legacy-database-name"):
        Store.open(tmp_path / DATABASE_FILE, busy_timeout_seconds=0)
    assert not (tmp_path / DATABASE_FILE).exists()
    Store.open(tmp_path / "other.sqlite3", busy_timeout_seconds=0).close()
    (tmp_path / "other.sqlite3").rename(tmp_path / DATABASE_FILE)
    with pytest.raises(LegacyDatabaseError):  # both names present: still refused
        Store.open(tmp_path / DATABASE_FILE, busy_timeout_seconds=0)


def test_open_refuses_beside_a_leftover_pre_rename_wal_file(tmp_path):
    (tmp_path / (LEGACY_DATABASE_FILE + "-wal")).write_bytes(b"")
    with pytest.raises(LegacyDatabaseError):
        Store.open(tmp_path / DATABASE_FILE, busy_timeout_seconds=0)


def test_open_uses_the_renamed_database_once_it_exists(tmp_path):
    Store.open(tmp_path / LEGACY_DATABASE_FILE, busy_timeout_seconds=0).close()
    (tmp_path / LEGACY_DATABASE_FILE).rename(tmp_path / DATABASE_FILE)
    Store.open(tmp_path / DATABASE_FILE, busy_timeout_seconds=0).close()
