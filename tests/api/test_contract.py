"""Contract tests: the shapes the bot writes and the macOS app will read."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr.api import contract
from tickr.api.contract import CommandKind, ContractError, StatusV1

from .samples import T0, status
from .schema_check import errors

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = FIXTURES / "status-v1.example.json"


def test_committed_schemas_match_the_code(tmp_path):
    contract.write_schemas(tmp_path)
    for name in contract.SCHEMA_TYPES:
        committed = contract.SCHEMA_DIR / f"{name}.schema.json"
        generated = tmp_path / f"{name}.schema.json"
        assert committed.read_text("utf-8") == generated.read_text("utf-8"), (
            f"{name}: contract changed; run `python -m tickr.api.contract` and review the "
            "diff (removing or renaming a key needs a new version)")


def test_golden_example_still_parses_and_round_trips():
    """Breaking this test means the macOS app's decoder would break too."""
    raw = json.loads(EXAMPLE.read_text("utf-8"))
    parsed = contract.from_jsonable(StatusV1, raw)
    assert contract.to_jsonable(parsed) == raw
    assert errors(contract.json_schema(StatusV1), raw) == []


def test_round_trip_minimal_status():
    minimal = status(entry_block=None, fences=(), position=None, trades_today=(),
                     recent_commands=(), run_id=None)
    assert contract.loads(StatusV1, contract.dumps(minimal)) == minimal
    assert errors(contract.json_schema(StatusV1), json.loads(contract.dumps(minimal))) == []


def test_wire_formats():
    raw = json.loads(contract.dumps(status()))
    assert raw["schema_version"] == contract.STATUS_SCHEMA_VERSION
    assert raw["written_at"] == "2026-11-16T15:00:00.123Z"
    assert raw["position"]["entry_price"] == "12.34"
    assert raw["result_today_gross_usd"] == "-1.60"


@pytest.mark.parametrize("mutate, cause", [
    (lambda d: d.update(extra=1), "unknown keys: extra"),
    (lambda d: d.pop("environment"), "missing keys: environment"),
    (lambda d: d.update(schema_version=2), "expected 1"),
    (lambda d: d.update(schema_version=True), "expected 1"),
    (lambda d: d.update(entry_stop=1), "expected true/false"),
    (lambda d: d.update(result_today_gross_usd=1.5), "decimal string"),
    (lambda d: d.update(result_today_gross_usd="NaN"), "decimal string"),
    (lambda d: d.update(written_at="2026-11-16T15:00:00Z"), "UTC time"),
    (lambda d: d.update(written_at="2026-11-16T15:00:00.000+00:00"), "UTC time"),
    (lambda d: d.update(bot_state="sleeping"), "not one of"),
    (lambda d: d["position"]["stop"].update(state="maybe"), "position.stop.state"),
    (lambda d: d.update(fences={}), "expected a list"),
    (lambda d: d["trading_gate"].pop("cause"), "trading_gate: missing keys: cause"),
])
def test_status_refuses_bad_documents(mutate, cause):
    raw = json.loads(EXAMPLE.read_text("utf-8"))
    mutate(raw)
    with pytest.raises(ContractError, match=cause):
        contract.from_jsonable(StatusV1, raw)


@pytest.mark.parametrize("text, cause", [
    ('{"a": 1, "a": 2}', "duplicate key"),
    ('{"a": NaN}', "NaN is not allowed"),
    ("{", "not valid JSON"),
])
def test_strict_json(text, cause):
    with pytest.raises(ContractError, match=cause):
        contract.parse_json(text)


def test_naive_time_refused():
    with pytest.raises(ContractError, match="naive"):
        contract.dumps(status(written_at=T0.replace(tzinfo=None)))


def test_write_status_is_atomic_and_leaves_no_temp(tmp_path):
    path = tmp_path / "data" / "status.json"
    contract.write_status(path, status())
    contract.write_status(path, status(entry_stop=True))
    assert contract.read_status(path).entry_stop is True
    assert [p.name for p in path.parent.iterdir()] == ["status.json"]


def test_write_status_keeps_old_file_when_encoding_fails(tmp_path):
    path = tmp_path / "status.json"
    contract.write_status(path, status())
    with pytest.raises(ContractError):
        contract.write_status(path, status(written_at=T0.replace(tzinfo=None)))
    assert contract.read_status(path) == status()


def test_read_status_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        contract.read_status(tmp_path / "status.json")


def test_summary_in_plain_words():
    ok = status(entry_block=None)
    assert contract.summarize(ok, Decimal("1"), False, None) == "Entries allowed."
    blocked = status()
    assert contract.summarize(blocked, Decimal("1"), False, None) == blocked.entry_block.text
    assert "may be down" in contract.summarize(blocked, Decimal("40"), True, None)
    assert "unavailable: no file" in contract.summarize(None, None, False, "no file")


# ---------------------------------------------------------------- commands


MAX_AGE = Decimal("60")


def _write(inbox: Path, kind: CommandKind, at=T0) -> contract.CommandV1:
    command = contract.new_command(kind, at, "api")
    contract.write_command(inbox, command)
    return command


def test_commands_round_trip_oldest_first(tmp_path):
    inbox = tmp_path / "inbox"
    later = _write(inbox, CommandKind.FLATTEN, T0 + timedelta(seconds=2))
    earlier = _write(inbox, CommandKind.ENTRY_STOP_ON, T0)
    items = contract.read_commands(inbox, T0 + timedelta(seconds=3), MAX_AGE)
    assert [i.command for i in items] == [earlier, later]
    assert all(i.refusal is None for i in items)
    assert contract.pending_commands(inbox) == [earlier, later]
    assert contract.count_inbox(inbox) == 2
    raw = json.loads((inbox / f"{earlier.command_id}.json").read_text("utf-8"))
    assert errors(contract.json_schema(contract.CommandV1), raw) == []


def test_missing_inbox_is_empty(tmp_path):
    assert contract.read_commands(tmp_path / "nope", T0, MAX_AGE) == []
    assert contract.pending_commands(tmp_path / "nope") == []
    assert contract.count_inbox(tmp_path / "nope") == 0


def test_expired_and_future_commands_are_refused_not_executed(tmp_path):
    inbox = tmp_path / "inbox"
    _write(inbox, CommandKind.FLATTEN, T0 - timedelta(seconds=61))
    _write(inbox, CommandKind.FLATTEN, T0 + timedelta(seconds=1))
    items = contract.read_commands(inbox, T0, MAX_AGE)
    assert [i.command for i in items] == [None, None]
    causes = sorted(i.refusal for i in items)
    assert causes[0].startswith("expired: requested 61.000 s ago")
    assert "in the future" in causes[1]


def test_bad_command_files_are_refused_and_temp_files_ignored(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    good = _write(inbox, CommandKind.CANCEL_OWN_ORDERS)
    (inbox / ".x.json.tmp").write_text("{", "utf-8")
    (inbox / "notes.txt").write_text("hi", "utf-8")
    broken = "11111111-1111-1111-1111-111111111111.json"
    (inbox / broken).write_text('{"kind": "flatten"}', "utf-8")
    renamed = "22222222-2222-2222-2222-222222222222.json"
    (inbox / renamed).write_text(contract.dumps(replace(good)), "utf-8")
    items = contract.read_commands(inbox, T0, MAX_AGE)
    refusals = {i.path.name: i.refusal for i in items if i.command is None}
    assert set(refusals) == {broken, renamed}
    assert "unreadable command file" in refusals[broken]
    assert "does not match command_id" in refusals[renamed]
    assert [i.command for i in items if i.command] == [good]
    # refusals come first so the bot journals them before acting on anything
    assert items[-1].command == good


def test_retire_moves_out_of_inbox(tmp_path):
    inbox = tmp_path / "commands" / "inbox"
    command = _write(inbox, CommandKind.ENTRY_STOP_OFF)
    (item,) = contract.read_commands(inbox, T0, MAX_AGE)
    target = contract.retire_command(item, tmp_path / "commands" / contract.PROCESSED)
    assert target.name == f"{command.command_id}.json"
    assert contract.read_commands(inbox, T0, MAX_AGE) == []


def test_write_command_refuses_bad_id(tmp_path):
    bad = replace(contract.new_command(CommandKind.FLATTEN, T0, "api"), command_id="../x")
    with pytest.raises(ContractError, match="uuid"):
        contract.write_command(tmp_path, bad)


def test_send_outcome_mirrors_the_engine_send_state():
    from tickr.engine.core import SendState

    assert [m.value for m in contract.SendOutcome] == [m.value for m in SendState]


def test_a_status_written_before_a_key_was_added_still_reads():
    """An update leaves the old bot's status file on disk; the watchdog and the API read it."""
    raw = json.loads(EXAMPLE.read_text("utf-8"))
    assert raw["entry_block"].pop("attention") is False
    parsed = contract.from_jsonable(StatusV1, raw)
    assert parsed.entry_block.attention is True  # an old block was always a warning
    del raw["positions"]  # a key without a default stays required
    with pytest.raises(contract.ContractError, match="missing keys: positions"):
        contract.from_jsonable(StatusV1, raw)


def test_a_field_default_does_not_make_its_key_optional():
    """`symbol: null` on a flatten means close everything, so a command file that lacks the
    key is refused, never read as that."""
    command = contract.new_command(CommandKind.FLATTEN, T0, "test")
    raw = contract.to_jsonable(command)
    assert contract.from_jsonable(contract.CommandV1, raw) == command
    del raw["symbol"]
    with pytest.raises(contract.ContractError, match="missing keys: symbol"):
        contract.from_jsonable(contract.CommandV1, raw)
    del raw["schema_version"]
    with pytest.raises(contract.ContractError, match="missing keys: symbol, schema_version"):
        contract.from_jsonable(contract.CommandV1, raw)
