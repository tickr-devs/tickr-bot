from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from aperture.store import MIGRATIONS, Store


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "data" / "aperture.sqlite3", busy_timeout_seconds=0)
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
    other = sqlite3.connect(tmp_path / "data" / "aperture.sqlite3", isolation_level=None)
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
    import aperture.store as store_module
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
