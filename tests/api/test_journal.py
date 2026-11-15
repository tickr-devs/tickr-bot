"""GET /journal: the latest journal rows with their numbers, from the database read-only."""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from aperture import config
from aperture.api import contract
from aperture.api.journal import NO_DATABASE, journal_document
from aperture.api.server import make_server, settings_from_config
from aperture.config import ConfigError
from aperture.store import Store

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfg(tmp_path):
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    return config.load(ROOT / "config" / "bot.toml", root=tmp_path)


def _fill(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    store = Store.open(path, busy_timeout_seconds=1)
    try:
        for i in range(count):
            body = {"symbol": "ABCD"} if i % 2 else {"symbols": ["ABCD"], "note": "x"}
            with store.transaction():
                store.append("trade-opened" if i % 2 else "reconcile-checked", body)
    finally:
        store.close()


def test_latest_rows_oldest_first_with_the_last_number(cfg):
    settings = replace(settings_from_config(cfg), port=0, journal_rows=3)
    server = make_server(settings, Clock(T0))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        resp, payload = request(server, "GET", "/journal")
        assert resp.status == 200 and doc(payload)["cause"] == NO_DATABASE
        _fill(settings.jobs.database, 5)
        resp, payload = request(server, "GET", "/journal")
        body = doc(payload)
        assert errors(contract.json_schema(contract.JournalV1), body) == []
        assert request(server, "POST", "/journal", {})[0].status == 405
    finally:
        server.shutdown()
        server.server_close()
    assert body["last_seq"] == 5 and body["cause"] is None
    assert [(r["seq"], r["kind"], r["symbol"]) for r in body["rows"]] == [
        (3, "reconcile-checked", None), (4, "trade-opened", "ABCD"),
        (5, "reconcile-checked", None)]
    assert all(set(r) == {"seq", "at", "kind", "symbol"} for r in body["rows"])  # no body


def test_empty_and_unreadable_database(tmp_path):
    database = tmp_path / "bot.sqlite"
    _fill(database, 0)
    assert journal_document(database, 3).cause == "the journal has no rows yet"
    database.write_bytes(b"not a database")
    broken = journal_document(database, 3)
    assert broken.last_seq is None and broken.cause.startswith("database unreadable")


def test_journal_rows_must_be_positive(cfg):
    with pytest.raises(ConfigError):
        settings_from_config(replace(cfg, api=replace(cfg.api, journal_rows=0)))
