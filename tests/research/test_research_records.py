from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from research_helpers import M1, record, write_jsonl

from tickr.research.errors import RecordFileError
from tickr.research.records import (
    Admission,
    load_records,
    record_from_mapping,
    record_to_json,
    write_records,
)
from tickr.strategy.base import Check, Role, Verdict


def _full():
    check = Check("relative_volume", Decimal("2.5"), ">=", Decimal("2.0"), True, "ok")
    return record("10:00", low="10.00", high="10.10", stop="9.80", quantity="15",
                  setup="breakout", checks=(check,))


def test_round_trip(tmp_path):
    records = [_full(), record("10:01", Verdict.WATCH), record("10:02", Verdict.IGNORE)]
    path = tmp_path / "d.jsonl"
    write_records(path, records)
    assert load_records([path]) == tuple(records)


def test_written_line_uses_strings_for_decimals():
    body = json.loads(record_to_json(_full()))
    assert body["entry_high"] == "10.10"
    assert body["checks"][0]["measured"] == "2.5"
    assert body["admission"] == "refused"
    assert body["refusal_cause"] == "daily loss ceiling"


def _body(**changes):
    body = json.loads(record_to_json(_full()))
    body.update(changes)
    return body


@pytest.mark.parametrize(
    ("changes", "cause"),
    [
        ({"entry_high": 10.1}, "entry_high must be a string"),
        ({"verdict": "BUY"}, "verdict 'BUY' is not one of"),
        ({"admission": "refused", "refusal_cause": None}, "refused without refusal_cause"),
        ({"admission": "taken"}, "refusal_cause set but admission is not refused"),
        ({"admission": "not_applicable", "refusal_cause": None}, "ENTER needs admission"),
        ({"verdict": "WATCH"}, "WATCH must have admission not_applicable"),
        ({"admission": "taken", "refusal_cause": None, "role": "observer"},
         "only the active role"),
        ({"time": "2026-11-10T15:00:30+00:00"}, "before the decision bar completed"),
        ({"bar_start": "2026-11-10T15:00:00"}, "bar_start has no UTC offset"),
        ({"stop": "NaN"}, "stop is not finite"),
        ({"checks": [{"rule": "x"}]}, "each check needs exactly the keys"),
    ],
)
def test_bad_records_are_refused(changes, cause):
    with pytest.raises(ValueError, match=cause):
        record_from_mapping(_body(**changes))


def test_missing_and_unknown_keys_are_named():
    body = _body(extra=1)
    del body["stop"]
    with pytest.raises(ValueError, match=r"missing=\['stop'\] unknown=\['extra'\]"):
        record_from_mapping(body)


def test_file_error_names_line(tmp_path):
    path = write_jsonl(tmp_path / "d.jsonl", [_body(), "", "{not json"])
    with pytest.raises(RecordFileError) as info:
        load_records([path])
    assert info.value.line == 3


def test_writer_refuses_invalid_record():
    bad = replace(_full(), role=Role.OBSERVER, admission=Admission.TAKEN, refusal_cause=None)
    with pytest.raises(ValueError, match="cannot write decision record"):
        record_to_json(bad)


def test_time_is_stored_as_utc():
    rec = record_from_mapping(_body(time="2026-11-10T10:05:00-05:00"))
    assert rec.time.utcoffset().total_seconds() == 0
    assert rec.time - rec.bar_start == 5 * M1
