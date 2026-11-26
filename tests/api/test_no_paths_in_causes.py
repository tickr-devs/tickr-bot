"""A path or an OS message never reaches an API response, a document's cause or a log line.

Each test makes a read fail with an exception that carries an absolute path and an OS message,
then looks at what the API answers. tests/test_no_exception_text.py holds the general rule.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tickr.api import advisor, cleanday, contract, trade_whatif
from tickr.api.causes import RowRefused, safe_cause
from tickr.api.history import History, HistoryUnavailable
from tickr.data import capture
from tickr.research.errors import RecordFileError

from .samples import T0
from .test_history import (  # noqa: F401 - pytest fixtures
    PREVIOUS,
    _bar,
    _bars_file,
    paths,
    served,
)
from .test_server import api, doc, request, settings  # noqa: F401 - pytest fixtures

SECRET_DIR = "/Users/owner/secret-dir"
SECRET_FILE = "bars-secret.jsonl"
OS_MESSAGE = "Permission denied"


def leaky(kind: type[OSError] = PermissionError) -> OSError:
    return kind(13, OS_MESSAGE, f"{SECRET_DIR}/{SECRET_FILE}")


def clean(text: str) -> None:
    for leaked in (SECRET_DIR, SECRET_FILE, OS_MESSAGE, "Errno"):
        assert leaked not in text, text


# ------------------------------------------------------------------ the helper


def test_safe_cause_keeps_our_refusals_and_names_everything_else_by_type():
    assert safe_cause(leaky()) == "PermissionError"
    assert safe_cause(sqlite3.OperationalError(f"unable to open {SECRET_DIR}/db")) \
        == "OperationalError"
    assert safe_cause(ValueError(SECRET_DIR)) == "ValueError"
    assert safe_cause(RowRefused("move_atr is not a number")) == "move_atr is not a number"
    assert safe_cause(HistoryUnavailable("database unreadable (OperationalError)")) \
        == "database unreadable (OperationalError)"


def test_safe_cause_shows_a_record_file_error_as_file_name_and_line_only():
    refused = RecordFileError(f"{SECRET_DIR}/decisions.jsonl", 3, f"bad {SECRET_DIR}")
    assert safe_cause(refused) == "decisions.jsonl:3: the record is malformed"


# ------------------------------------------------------------------ responses


def test_status_file_that_cannot_be_read(api, settings, monkeypatch):  # noqa: F811
    server, _ = api

    def read(path):
        raise leaky()

    monkeypatch.setattr(contract, "read_status", read)
    body = doc(request(server, "GET", "/status")[1])
    assert body["unavailable_cause"] == "status file is invalid (PermissionError)"
    clean(str(body))


def test_a_capture_file_that_cannot_be_read_is_a_503_without_its_path(served, monkeypatch):  # noqa: F811
    server, paths = served  # noqa: F811
    _bars_file(paths, PREVIOUS, [_bar("ABCD", 0, day=PREVIOUS)])

    def by_symbol(self, captured, *, open_tail):
        raise leaky()

    monkeypatch.setattr(capture.CapturedBarsDays, "by_symbol", by_symbol)
    resp, payload = request(server, "GET", "/bars?symbol=ABCD&date=2026-11-13")
    assert resp.status == 503
    assert doc(payload)["cause"] == "capture file unreadable (PermissionError)"
    clean(payload.decode())


def test_a_database_that_cannot_be_opened_is_a_503_without_its_path(paths, monkeypatch):  # noqa: F811
    paths.database.write_bytes(b"")

    def open_readonly(path):
        raise sqlite3.OperationalError(f"unable to open database file {SECRET_DIR}/db")

    monkeypatch.setattr("tickr.api.history.Store.open_readonly", open_readonly)
    with pytest.raises(HistoryUnavailable) as refused:
        History(paths).day(PREVIOUS, PREVIOUS)
    assert str(refused.value) == "database unreadable (OperationalError)"


def test_the_trade_whatif_refuses_an_unreadable_calendar_by_its_type(monkeypatch):
    def load(directory):
        raise leaky()

    monkeypatch.setattr(trade_whatif.SessionCalendar, "load", staticmethod(load))
    unreadable = SimpleNamespace(outside_session_only=True,
                                 paths=SimpleNamespace(calendar=Path(SECRET_DIR)))
    with pytest.raises(trade_whatif.TradeWhatIfRefused) as refused:
        trade_whatif.refuse_in_session(unreadable, T0)
    assert str(refused.value) == "calendar unreadable: PermissionError"


def test_an_advisor_run_with_an_unreadable_file_names_the_type_only(tmp_path, monkeypatch):
    run = tmp_path / "20261113T150000Z-demo"
    run.mkdir()
    (run / advisor.files.RUN_FILE).write_text("{}", "utf-8")

    def read_text(self, *args, **kwargs):
        raise leaky()

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(advisor.Unreadable) as refused:
        advisor.read_run(run)
    assert str(refused.value) == f"{advisor.files.RUN_FILE}: PermissionError"
    clean(str(refused.value))


def test_a_journal_row_it_cannot_parse_is_named_by_its_type():
    row = {"seq": 7, "kind": "reconcile", "at_utc": "not a time", "body": "{}"}
    with pytest.raises(cleanday._Unreadable) as refused:  # noqa: SLF001
        cleanday._read(row)  # noqa: SLF001
    assert str(refused.value) == "journal row 7 (reconcile): ValueError"
