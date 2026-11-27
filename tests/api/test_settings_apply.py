"""Settings API writes (backlog S4 apply, history, rollback; S5 apply rules)."""

from __future__ import annotations

import fcntl
import shutil
import threading
import tomllib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr import settings_schema as schema
from tickr.api import contract
from tickr.api.contract import BotState
from tickr.api.server import make_server, settings_from_config
from tickr.api.settingsapply import (
    SettingsPaths,
    edited,
    promote,
    promote_at_start,
    read_history,
)
from tickr.api.settingsview import SettingsUnavailable

from .samples import T0, status
from .schema_check import errors
from .test_server import Clock, doc, request
from .test_server import schema as contract_schema

ROOT = Path(__file__).resolve().parents[2]
PA3 = schema.PA3
STRATEGY = Path("strategies") / PA3 / "strategy.toml"
SLOTS = "risk.max_open_positions"  # 6 in config/bot.toml; lower tightens (applies at once)
QUOTE_AGE = "risk.max_quote_age_seconds"  # 5; raise loosens (next session, Face ID)
ATR = "indicators.atr_period"  # 14; a strategy setting, applies when flat


@pytest.fixture
def served(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    for name in ("bot.toml", "settings_schema.toml"):
        (tmp_path / "config" / name).write_bytes((ROOT / "config" / name).read_bytes())
    (tmp_path / STRATEGY).parent.mkdir(parents=True)
    (tmp_path / STRATEGY).write_bytes((ROOT / STRATEGY).read_bytes())
    path = tmp_path / "config" / "bot.toml"
    cfg = config.load(path)
    settings = replace(settings_from_config(cfg, path), port=0)
    server = make_server(settings, Clock(T0 + timedelta(seconds=2)))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server, tmp_path
    server.shutdown()
    server.server_close()


def _flat(server, **overrides) -> None:
    contract.write_status(server.settings.status_path, status(
        position=None, positions=(), open_reservations_usd=Decimal("0"), **overrides))


def _value(root: Path, owner: str, key: str):
    path = root / ("config/bot.toml" if owner == schema.PLATFORM else STRATEGY)
    return schema.flatten(tomllib.loads(path.read_text("utf-8"), parse_float=Decimal))[key]


def _base(server) -> list[dict]:
    resp, payload = request(server, "GET", "/settings/values")
    assert resp.status == 200
    return [{"owner": f["owner"], "digest": f["digest"]} for f in doc(payload)["files"]]


def _apply(server, *changes, biometric=False, base=None):
    resp, payload = request(server, "POST", "/settings/apply", {
        "changes": [{"owner": o, "key": k, "value": v} for o, k, v in changes],
        "base": _base(server) if base is None else base, "biometric_confirmed": biometric})
    body = doc(payload)
    if resp.status == 200:
        assert errors(contract_schema("settings-applied-v1"), body) == []
    return resp, body


def _rollback(server, version, biometric=False):
    resp, payload = request(server, "POST", "/settings/rollback", {
        "version": version, "base": _base(server), "biometric_confirmed": biometric})
    return resp, doc(payload)


def _history(server) -> list[dict]:
    resp, payload = request(server, "GET", "/settings/history")
    body = doc(payload)
    assert resp.status == 200 and errors(contract_schema("settings-history-v1"), body) == []
    return body["versions"]


def _paths(server) -> SettingsPaths:
    s = server.settings
    return SettingsPaths(s.settings_schema_file, s.config_file, s.config_root, s.settings_history)


def test_tightening_is_written_at_once_and_journaled_with_time_and_device(served):
    server, root = served
    before = (root / "config/bot.toml").read_text("utf-8")
    resp, body = _apply(server, ("platform", SLOTS, "5"))
    assert resp.status == 200
    version = body["version"]
    assert (version["version"], version["kind"], version["device"], version["state"]) == (
        1, "apply", "local", "done")
    assert version["at"] == contract.format_time(T0 + timedelta(seconds=2))
    assert version["changes"] == [{
        "owner": "platform", "key": SLOTS, "before": "6", "after": "5", "applies": "at_once",
        "state": "written", "effective_after": None, "settled_by": None}]
    # A tightening needs no restart: the running bot adopts it at its next loop pass.
    assert body["restart_needed"] is False
    assert _value(root, "platform", SLOTS) == 5
    after = (root / "config/bot.toml").read_text("utf-8")
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True)
               if a != b]
    assert changed == [("max_open_positions = 6", "max_open_positions = 5")]
    assert _history(server) == [version]
    assert version["files"][0]["digest_after"] == _base(server)[0]["digest"]


def test_loosening_without_the_biometric_flag_is_refused_and_nothing_changes(served):
    server, root = served
    before = (root / "config/bot.toml").read_bytes()
    resp, body = _apply(server, ("platform", QUOTE_AGE, "6"))
    assert resp.status == 403 and body["error"] == "biometric_required"
    assert QUOTE_AGE in body["cause"]
    assert (root / "config/bot.toml").read_bytes() == before
    assert _history(server) == []


def test_loosening_waits_for_the_bots_first_start_on_a_later_date(served):
    server, root = served
    resp, body = _apply(server, ("platform", QUOTE_AGE, "6"), biometric=True)
    assert resp.status == 200 and body["restart_needed"] is False
    change = body["version"]["changes"][0]
    assert (change["state"], change["applies"], change["effective_after"]) == (
        "pending", "next_session", T0.date().isoformat())
    assert body["version"]["files"] == [] and body["version"]["biometric_confirmed"] is True
    assert _value(root, "platform", QUOTE_AGE) == 5
    path = root / "config/bot.toml"
    cfg = config.load(path)
    # A restart the same New York day keeps the tighter value.
    assert promote_at_start(cfg, path, T0 + timedelta(hours=3)) == (None, False)
    assert _value(root, "platform", QUOTE_AGE) == 5
    note, written = promote_at_start(cfg, path, T0 + timedelta(days=1))
    assert written and "risk.max_quote_age_seconds 5 -> 6" in note
    assert _value(root, "platform", QUOTE_AGE) == 6
    first, second = _history(server)
    assert (first["changes"][0]["state"], first["changes"][0]["settled_by"]) == ("promoted", 2)
    assert (second["kind"], second["device"], second["state"]) == ("promote", "bot", "done")
    assert promote(_paths(server), T0 + timedelta(days=2)) is None


def test_a_later_change_supersedes_a_pending_loosening(served):
    server, root = served
    assert _apply(server, ("platform", QUOTE_AGE, "6"), biometric=True)[0].status == 200
    resp, body = _apply(server, ("platform", QUOTE_AGE, "4"))  # tightens: at once, no Face ID
    assert resp.status == 200
    first, _ = _history(server)
    assert (first["changes"][0]["state"], first["changes"][0]["settled_by"]) == ("superseded", 2)
    assert promote(_paths(server), T0 + timedelta(days=1)) is None
    assert _value(root, "platform", QUOTE_AGE) == 4


def test_a_strategy_setting_applies_only_while_the_bot_is_flat(served):
    server, root = served
    contract.write_status(server.settings.status_path, status())  # a position is open
    resp, body = _apply(server, (PA3, ATR, "15"))
    assert resp.status == 409 and "flat" in body["cause"]
    assert _value(root, PA3, ATR) == 14
    _flat(server)
    resp, body = _apply(server, (PA3, ATR, "15"))
    assert resp.status == 200 and body["version"]["changes"][0]["applies"] == "when_flat"
    assert body["restart_needed"] is True  # stop-bot then start-bot: a safe restart
    assert _value(root, PA3, ATR) == 15
    _flat(server, bot_state=BotState.STOPPED)
    resp, body = _apply(server, (PA3, ATR, "16"))
    assert resp.status == 200 and body["restart_needed"] is False


def test_rollback_restores_the_values_of_an_earlier_version(served):
    server, root = served
    original = (root / "config/bot.toml").read_bytes()
    assert _apply(server, ("platform", SLOTS, "5"))[0].status == 200
    assert _apply(server, ("platform", SLOTS, "4"))[0].status == 200
    assert _apply(server, ("platform", QUOTE_AGE, "6"), biometric=True)[0].status == 200
    # Back to version 1: slots 4 -> 5 loosens, so it needs Face ID and waits like any loosening;
    # the pending quote age is dropped (its value at version 1 is the file's).
    resp, body = _rollback(server, 1)
    assert resp.status == 403 and body["error"] == "biometric_required"
    resp, body = _rollback(server, 1, biometric=True)
    assert resp.status == 200
    version = body["version"]
    assert (version["version"], version["kind"], version["rollback_to"]) == (4, "rollback", 1)
    assert [(c["key"], c["before"], c["after"], c["state"]) for c in version["changes"]] == [
        (SLOTS, "4", "5", "pending")]
    assert _value(root, "platform", SLOTS) == 4
    # Back to before any change: slots 6 replaces the pending 5.
    resp, body = _rollback(server, 0, biometric=True)
    assert resp.status == 200
    assert [(c["key"], c["after"]) for c in body["version"]["changes"]] == [(SLOTS, "6")]
    versions = _history(server)
    assert [(c["state"], c["settled_by"]) for v in versions[2:4] for c in v["changes"]] == [
        ("superseded", 4), ("superseded", 5)]
    assert promote(_paths(server), T0 + timedelta(days=1)) is not None
    assert (root / "config/bot.toml").read_bytes() == original


def test_rollback_that_loosens_needs_the_biometric_flag(served):
    server, _ = served
    assert _apply(server, ("platform", SLOTS, "5"))[0].status == 200
    resp, body = _rollback(server, 0)
    assert resp.status == 403 and body["error"] == "biometric_required"
    resp, body = _rollback(server, 7)
    assert resp.status == 404
    resp, body = _rollback(server, 1)
    assert resp.status == 409 and "latest" in body["cause"]


def test_a_draft_made_against_an_older_file_is_refused(served):
    server, root = served
    stale = _base(server)
    assert _apply(server, ("platform", SLOTS, "5"))[0].status == 200
    resp, body = _apply(server, ("platform", SLOTS, "4"), base=stale)
    assert resp.status == 409 and "changed since it was read" in body["cause"]
    assert _value(root, "platform", SLOTS) == 5
    resp, body = _apply(server, ("platform", SLOTS, "4"), base=[])
    assert resp.status == 409 and "no base digest" in body["cause"]


def test_a_concurrent_writer_holding_the_lock_is_refused(served):
    server, root = served
    paths = _paths(server)
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        resp, body = _apply(server, ("platform", SLOTS, "5"))
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    assert resp.status == 409 and "being written now" in body["cause"]
    assert _value(root, "platform", SLOTS) == 6


def test_refusals_change_nothing(served):
    server, root = served
    before = (root / "config/bot.toml").read_bytes()
    resp, body = _apply(server, ("platform", SLOTS, "6"))
    assert resp.status == 409 and "nothing to change" in body["cause"]
    resp, body = _apply(server, ("platform", SLOTS, "0"))
    assert resp.status == 400 and "BELOW_MINIMUM" in body["cause"]
    resp, body = _apply(server, ("platform", "run.data_dir", "x"))
    assert resp.status == 400 and "ENGINEERING_KEY" in body["cause"]  # S2 classes
    resp, body = _apply(server, ("platform", "run.environment", "\"production\""))
    assert resp.status == 400 and "CLOSED_KEY" in body["cause"]
    resp, body = _apply(server, ("platform", "risk.no_such_key", "1"))
    assert resp.status == 400 and "UNKNOWN_KEY" in body["cause"]
    resp, payload = request(server, "POST", "/settings/apply", {"changes": []})
    assert resp.status == 400
    assert (root / "config/bot.toml").read_bytes() == before
    assert _history(server) == []


def test_a_writer_that_stopped_after_journaling_is_settled_from_the_digests(served):
    server, root = served
    assert _apply(server, ("platform", SLOTS, "5"))[0].status == 200
    paths = _paths(server)
    (version,) = read_history(paths.history)
    left = replace(version, state=contract.SettingsVersionState.WRITING)
    contract.write_atomic(paths.history, contract.dumps(contract.SettingsHistoryV1((left,))))
    path = root / "config/bot.toml"
    path.write_text(path.read_text("utf-8").replace("max_open_positions = 5",
                                                    "max_open_positions = 6"), "utf-8")
    assert _apply(server, ("platform", SLOTS, "4"))[0].status == 200
    first, second = _history(server)
    assert first["state"] == "not_written" and second["changes"][0]["before"] == "6"


def test_edit_keeps_comments_and_refuses_an_ambiguous_key():
    text = "# head\n[risk]\n# what\nmax = 5  # inline\n[other]\nmax = 7\n"
    assert edited(text, "risk.max", "4") == text.replace("max = 5  #", "max = 4  #")
    with pytest.raises(SettingsUnavailable):
        edited(text, "risk.missing", "4")
    with pytest.raises(SettingsUnavailable):
        edited("[risk]\nmax = [\n  5]\n", "risk.max", "4")


# A value the schema passes but the bot's loader refuses: each test drops one schema rule that
# mirrors a loader check, so only the loader can catch it.

ROOM = "risk.min_daily_room_usd"  # 10; raise tightens; must stay below the daily loss ceiling
LOSS = "risk.max_daily_loss_usd"  # 5000; raise loosens


def _without_rule(monkeypatch, name: str) -> None:
    monkeypatch.setattr(schema, "RULES", tuple(r for r in schema.RULES if r.name != name))


def _untouched(server, root):
    return ((root / "config/bot.toml").read_bytes(), (root / STRATEGY).read_bytes(),
            _history(server), sorted(p.name for p in (root / "config").iterdir()),
            sorted(p.name for p in (root / "strategies").iterdir()))


def test_apply_refuses_a_value_the_config_loader_rejects(served, monkeypatch):
    server, root = served
    _without_rule(monkeypatch, "daily_room_below_ceiling")
    before = _untouched(server, root)
    resp, body = _apply(server, ("platform", ROOM, "5000"))
    assert resp.status == 400 and body["cause"].startswith("config/bot.toml would not load: ")
    assert "min_daily_room_usd must be >= 0 and below max_daily_loss_usd" in body["cause"]
    assert _untouched(server, root) == before


def test_apply_refuses_a_strategy_value_its_loader_rejects(served, monkeypatch):
    server, root = served
    _without_rule(monkeypatch, "fast_below_slow_ema")
    _flat(server)
    before = _untouched(server, root)
    resp, body = _apply(server, (PA3, "indicators.fast_ema_period", "20"))
    assert resp.status == 400 and "strategy.toml would not load: " in body["cause"]
    assert "fast_ema_period must be below slow_ema_period" in body["cause"]
    assert _untouched(server, root) == before


def test_rollback_refuses_values_the_config_loader_rejects(served, monkeypatch):
    server, root = served
    _without_rule(monkeypatch, "daily_room_below_ceiling")
    assert _apply(server, ("platform", LOSS, "6000"), biometric=True)[0].status == 200
    assert promote(_paths(server), T0 + timedelta(days=1)) is not None
    assert _apply(server, ("platform", ROOM, "5500"))[0].status == 200
    before = _untouched(server, root)
    # Back to 0: the ceiling 5000 is written at once, under the room of 5500 still in force.
    resp, body = _rollback(server, 0, biometric=True)
    assert resp.status == 400 and "would not load" in body["cause"]
    assert _untouched(server, root) == before


def test_promote_refuses_a_pending_value_the_config_loader_rejects(served, monkeypatch):
    server, root = served
    _without_rule(monkeypatch, "slots_fit_stream")
    assert _apply(server, ("platform", SLOTS, "21"), biometric=True)[0].status == 200
    before = _untouched(server, root)
    path = root / "config/bot.toml"
    note, written = promote_at_start(config.load(path), path, T0 + timedelta(days=1))
    assert not written and "would not load" in note and "stream_reserved_symbols" in note
    assert _untouched(server, root) == before
    assert _history(server)[0]["changes"][0]["state"] == "pending"


@pytest.mark.parametrize("key", [
    "environments.production.trading_enabled", 'environments."production".trading_enabled',
    "Environments.Production.x", "run.environment", "no_such_table.key"])
def test_apply_refuses_a_closed_or_unclassified_key_and_writes_nothing(served, key):
    # S2: apply checks through draft_document, so a closed key (any depth or spelling) and a key
    # with no class are refused before anything is journaled or written.
    server, root = served
    before = (root / "config/bot.toml").read_bytes()
    resp, body = _apply(server, ("platform", key, "true"), biometric=True)
    assert resp.status == 400
    assert ("CLOSED_KEY" if key != "no_such_table.key" else "UNKNOWN_KEY") in body["cause"]
    assert (root / "config/bot.toml").read_bytes() == before
    assert _history(server) == []
