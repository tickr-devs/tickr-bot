"""api.decision_cache: today's decision records read on incrementally, same answers as
DecisionLog.records."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from tickr.api import decision_cache
from tickr.api.decision_cache import DecisionCache
from tickr.archive import compress
from tickr.observers.log import DecisionLog
from tickr.research.errors import RecordFileError
from tickr.strategy.base import Role

from .test_decisions import DAY, _observer, _record

FILTERS = [{}, {"role": Role.ACTIVE}, {"role": Role.OBSERVER}, {"symbol": "ABCD"},
           {"symbol": "WXYZ", "role": Role.ACTIVE}]


@pytest.fixture
def log(tmp_path):
    return DecisionLog(tmp_path / "decisions")


def _same(cache, log, day=DAY, today=DAY):
    for f in FILTERS:
        assert cache.records(day, today, **f) == log.records(day, **f), f


def _append(log, *records, tail=""):
    for record in records:
        log.append(record)
    log.close()
    if tail:
        with open(log.path(DAY), "a", encoding="utf-8") as handle:
            handle.write(tail)


def _counting(monkeypatch):
    parsed = []
    original = decision_cache.record_from_mapping

    def count(data):
        parsed.append(data["symbol"])
        return original(data)

    monkeypatch.setattr(decision_cache, "record_from_mapping", count)
    return parsed


def test_reads_on_from_where_it_stopped(log, monkeypatch):
    parsed = _counting(monkeypatch)
    cache = DecisionCache(log)
    assert cache.records(DAY, DAY) == ()  # no file yet
    _append(log, _record(), _observer(minute=1), _record(symbol="WXYZ", minute=2))
    _same(cache, log)
    assert len(parsed) == 3  # five reads, each line parsed once
    _append(log, _record(minute=3), _observer(symbol="WXYZ", minute=4))
    _same(cache, log)
    assert len(parsed) == 5


def test_a_line_being_written_waits_for_its_newline(log):
    cache = DecisionCache(log)
    _append(log, _record())
    line = log.path(DAY).read_text("utf-8")
    with open(log.path(DAY), "a", encoding="utf-8") as handle:
        handle.write(line[:40])
    _same(cache, log)
    assert len(cache.records(DAY, DAY)) == 1
    with open(log.path(DAY), "a", encoding="utf-8") as handle:
        handle.write(line[40:] + "\n")
    _same(cache, log)
    assert len(cache.records(DAY, DAY)) == 2  # the finished line counts; the blank after it not


def test_a_replaced_or_shorter_file_is_read_from_the_top(log, tmp_path):
    cache = DecisionCache(log)
    _append(log, _record(), _record(minute=1), _record(minute=2))
    _same(cache, log)
    path = log.path(DAY)
    lines = path.read_text("utf-8").splitlines(keepends=True)
    with open(path, "w", encoding="utf-8") as handle:  # same file, shorter
        handle.write(lines[0])
    _same(cache, log)
    other = tmp_path / "other.jsonl"  # another file in its place
    other.write_text(lines[2] + lines[1], "utf-8")
    os.replace(other, path)
    _same(cache, log)
    assert [r.bar_start for r in cache.records(DAY, DAY)] == \
        [_record(minute=2).bar_start, _record(minute=1).bar_start]


def test_other_days_and_archived_files_are_read_as_before(log, monkeypatch):
    _append(log, _record(), _observer(minute=1))
    parsed = _counting(monkeypatch)
    cache = DecisionCache(log)
    later = DAY + timedelta(days=1)
    _same(cache, log, today=later)  # a past day: never cached
    assert parsed == []
    compress(log.path(DAY))
    assert not log.path(DAY).exists()
    _same(cache, log)  # archived today's file: read by DecisionLog
    assert parsed == []


def test_a_malformed_line_refuses_what_decision_log_refuses(log):
    cache = DecisionCache(log)
    _append(log, _record(), _observer(minute=1),
            tail='{"role": "observer", "symbol": "ABCD", broken\n')
    for f in FILTERS:
        try:
            expected = log.records(DAY, **f)
        except RecordFileError as exc:
            with pytest.raises(RecordFileError) as refused:
                cache.records(DAY, DAY, **f)
            assert (refused.value.line, str(refused.value)) == (exc.line, str(exc))
        else:
            assert cache.records(DAY, DAY, **f) == expected, f
    _append(log, _record(symbol="WXYZ", minute=2))  # lines after it are still read
    assert len(cache.records(DAY, DAY, symbol="WXYZ")) == 1


def test_a_file_rewritten_in_place_with_another_first_line_is_read_from_the_top(log):
    cache = DecisionCache(log)
    _append(log, _record())
    _same(cache, log)
    path = log.path(DAY)
    other = DecisionLog(path.parent.parent.parent / "other")
    _append(other, _record(symbol="WXYZ"), _record(symbol="WXYZ", minute=1))
    path.write_bytes(other.path(DAY).read_bytes())  # same inode, longer, other records
    _same(cache, log)
    assert {r.symbol for r in cache.records(DAY, DAY)} == {"WXYZ"}


def test_a_failed_read_keeps_nothing_of_it(log, monkeypatch):
    cache = DecisionCache(log)
    _append(log, _record(), _record(minute=1))
    original = decision_cache.record_from_mapping
    calls = []

    def fails_second(data):
        calls.append(data)
        if len(calls) == 2:
            raise RecursionError("too deep")
        return original(data)

    monkeypatch.setattr(decision_cache, "record_from_mapping", fails_second)
    with pytest.raises(RecursionError):
        cache.records(DAY, DAY)
    monkeypatch.setattr(decision_cache, "record_from_mapping", original)
    _same(cache, log)  # no record twice
