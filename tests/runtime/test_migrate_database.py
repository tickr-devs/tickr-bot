"""`python -m tickr.runtime migrate-database`: the server deploy's one migration, so the API of
a new release reads the days an older release wrote (2026-11-28: after release cd25eeb the API
refused "database schema v10, this API reads v12" until the bot's next start)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tickr import config as config_module
from tickr.api.history import History, HistoryPaths
from tickr.lock import LOCK_FILE, SingleInstanceLock
from tickr.runtime.__main__ import main
from tickr.store import DATABASE_FILE, MIGRATIONS, Store

OLD = len(MIGRATIONS) - 2  # a database an older release wrote


def _database(config_path: Path) -> Path:
    cfg = config_module.load(config_path)
    return cfg.root / cfg.run.data_dir / DATABASE_FILE


def _write(path: Path, version: int, environment: str | None = None) -> None:
    """A database at `version`, as the release that knew only that many migrations left it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    for migration in MIGRATIONS[:version]:
        migration(db)
    db.execute(f"PRAGMA user_version = {version}")
    if environment is not None:
        db.execute("INSERT INTO runs(run_id, started_at, environment, config_digest, strategy, "
                   "strategy_version, code_version) VALUES ('r1', '2026-11-25T09:00:00+00:00', "
                   "?, 'd', 's', '1', 'c')", (environment,))
    db.commit()
    db.close()


def _version(path: Path) -> int:
    store = Store.open_readonly(path)
    try:
        return store.schema_version
    finally:
        store.close()


def _run(config_path: Path, capsys) -> tuple[int, dict]:
    code = main(["--config", str(config_path), "migrate-database"])
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert row["record"] == "migrate-database"
    return code, row


def test_an_older_database_is_migrated_with_a_verified_copy_of_the_old_schema(
        config_path, capsys):
    database = _database(config_path)
    _write(database, OLD, environment=config_module.load(config_path).environment_name)
    code, row = _run(config_path, capsys)
    assert code == 0 and row["from"] == OLD and row["to"] == len(MIGRATIONS)
    assert row["cause"] is None
    assert _version(database) == len(MIGRATIONS)
    copy = database.with_name(f"{database.stem}.pre-v{OLD + 1}{database.suffix}")
    assert copy.name in row["detail"] and _version(copy) == OLD
    # The readers of the new release read it now: the API's history no longer refuses.
    cfg = config_module.load(config_path)
    store = History(HistoryPaths.from_config(cfg))._open()
    assert store is not None
    store.close()


def test_a_current_database_is_left_as_it_is(config_path, capsys):
    database = _database(config_path)
    _write(database, len(MIGRATIONS))
    before = database.read_bytes()
    code, row = _run(config_path, capsys)
    assert code == 0 and row["from"] == row["to"] == len(MIGRATIONS)
    assert row["detail"] == "already current" and database.read_bytes() == before
    assert sorted(p.name for p in database.parent.iterdir()) == sorted([database.name, LOCK_FILE])


def test_no_database_is_never_created(config_path, capsys):
    code, row = _run(config_path, capsys)
    assert code == 0 and row["from"] is None and row["cause"] is None
    assert not _database(config_path).exists()


def test_a_newer_database_is_refused_untouched(config_path, capsys):
    database = _database(config_path)
    _write(database, len(MIGRATIONS))
    db = sqlite3.connect(database)
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    db.close()
    code, row = _run(config_path, capsys)
    assert code == 1 and "newer than this code" in row["cause"]
    assert _version(database) == len(MIGRATIONS) + 1


def test_a_database_of_another_environment_is_refused_untouched(config_path, capsys):
    database = _database(config_path)
    _write(database, OLD, environment="some-other-environment")
    code, row = _run(config_path, capsys)
    assert code == 1 and "some-other-environment" in row["cause"]
    assert _version(database) == OLD
    assert not database.with_name(f"{database.stem}.pre-v{OLD + 1}{database.suffix}").exists()


def test_an_existing_copy_of_the_old_schema_is_never_overwritten(config_path, capsys):
    database = _database(config_path)
    _write(database, OLD)
    copy = database.with_name(f"{database.stem}.pre-v{OLD + 1}{database.suffix}")
    copy.write_bytes(b"an earlier copy")
    code, row = _run(config_path, capsys)
    assert code == 1 and "FileExistsError" in row["cause"]
    assert copy.read_bytes() == b"an earlier copy" and _version(database) == OLD


def test_a_running_bot_is_never_migrated_under(config_path, capsys):
    # The bot holds its lock around its own Store.open for its whole run: a migration then
    # would change the schema under a live process.
    database = _database(config_path)
    _write(database, OLD)
    with SingleInstanceLock(database.parent / LOCK_FILE):
        code, row = _run(config_path, capsys)
    assert code == 1 and "the bot is running" in row["cause"]
    assert _version(database) == OLD
    assert not database.with_name(f"{database.stem}.pre-v{OLD + 1}{database.suffix}").exists()


def test_the_lock_is_released_after_the_migration(config_path, capsys):
    database = _database(config_path)
    _write(database, OLD)
    assert _run(config_path, capsys)[0] == 0
    with SingleInstanceLock(database.parent / LOCK_FILE):
        pass  # a bot starting next takes it


def test_a_file_that_is_not_a_tickr_database_is_refused_untouched(config_path, capsys):
    database = _database(config_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(database).execute("CREATE TABLE other (x)").connection.close()
    before = database.read_bytes()
    code, row = _run(config_path, capsys)
    assert code == 1 and "not a Tickr database" in row["cause"]
    assert database.read_bytes() == before
