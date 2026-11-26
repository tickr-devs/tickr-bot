"""Settings API, read and validate only (backlog S4): GET /settings/schema, GET /settings/values,
POST /settings/validate. Nothing here writes."""

from __future__ import annotations

import hashlib
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
from tickr.api.server import make_server, settings_from_config
from tickr.api.settingsview import draft_document

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request
from .test_server import schema as contract_schema

ROOT = Path(__file__).resolve().parents[2]
PA3 = schema.PA3
STRATEGY = Path("strategies") / PA3 / "strategy.toml"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


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
    server = make_server(replace(settings_from_config(cfg, path), port=0),
                         Clock(T0 + timedelta(seconds=2)))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server, tmp_path
    server.shutdown()
    server.server_close()


def _validate(server, *changes):
    resp, payload = request(server, "POST", "/settings/validate", {"changes": [
        {"owner": o, "key": k, "value": v} for o, k, v in changes]})
    body = doc(payload)
    if resp.status == 200:
        assert errors(contract_schema("settings-draft-v1"), body) == []
    return resp, body


def _current(root: Path, owner: str) -> dict:
    path = root / ("config/bot.toml" if owner == schema.PLATFORM else STRATEGY)
    return schema.flatten(tomllib.loads(path.read_text("utf-8"), parse_float=Decimal))


def test_schema_lists_every_record_with_its_bounds_step_and_effects(served):
    server, _ = served
    resp, payload = request(server, "GET", "/settings/schema")
    body = doc(payload)
    assert resp.status == 200 and errors(contract_schema("settings-schema-v1"), body) == []
    loaded = schema.load(ROOT / "config" / "settings_schema.toml")
    assert [(s["owner"], s["key"]) for s in body["settings"]] == [(s.owner, s.key)
                                                                   for s in loaded]
    daily = body["settings"][0]
    assert daily["key"] == "risk.max_daily_loss_usd"
    assert (daily["kind"], daily["unit"], daily["applies"], daily["loosens"]) == (
        "decimal", "usd", "at_once", "raise")
    assert daily["needs_face_id"] is True and daily["requires_retraining"] is False
    assert (daily["minimum"], daily["minimum_inclusive"], daily["step"]) == ("0", False, "1")
    assert daily["description_en"] and daily["description_es"]
    assert [r["name"] for r in body["rules"]] == [r.name for r in schema.RULES]


def test_values_are_the_files_on_disk_now(served):
    server, root = served
    resp, payload = request(server, "GET", "/settings/values")
    body = doc(payload)
    assert resp.status == 200 and errors(contract_schema("settings-values-v1"), body) == []
    assert body["files"] == [
        {"owner": schema.PLATFORM, "file": "config/bot.toml",
         "digest": hashlib.sha256((root / "config/bot.toml").read_bytes()).hexdigest()},
        {"owner": PA3, "file": STRATEGY.as_posix(),
         "digest": hashlib.sha256((root / STRATEGY).read_bytes()).hexdigest()}]
    for value in body["values"]:
        expected = _current(root, value["owner"])[value["key"]]
        assert tomllib.loads(f"v = {value['value']}", parse_float=Decimal)["v"] == expected

    text = (root / STRATEGY).read_text("utf-8")
    (root / STRATEGY).write_text(text.replace("atr_period = ", "old_atr_period = "), "utf-8")
    shown = {(v["owner"], v["key"]): v["value"]
             for v in doc(request(server, "GET", "/settings/values")[1])["values"]}
    assert shown[(PA3, "indicators.atr_period")] is None


def test_a_draft_reports_when_each_change_applies_and_which_needs_face_id(served):
    server, root = served
    before = _snapshot(root)
    ceiling = _current(root, schema.PLATFORM)["risk.max_daily_loss_usd"]
    slots = _current(root, schema.PLATFORM)["risk.max_open_positions"]
    fast = _current(root, PA3)["indicators.fast_ema_period"]
    resp, body = _validate(
        server, (schema.PLATFORM, "risk.max_daily_loss_usd", str(ceiling + 1)),
        (schema.PLATFORM, "risk.max_open_positions", str(slots)),
        (PA3, "indicators.fast_ema_period", str(fast + 1)))
    assert resp.status == 200 and body["valid"] is True and body["refusals"] == []
    effects = {c["key"]: c for c in body["changes"]}
    loosen = effects["risk.max_daily_loss_usd"]
    assert (loosen["applies"], loosen["needs_face_id"], loosen["changed"]) == (
        "next_session", True, True)
    assert loosen["current"] == str(ceiling) and loosen["draft"] == str(ceiling + 1)
    same = effects["risk.max_open_positions"]
    assert (same["changed"], same["needs_face_id"]) == (False, False)
    strategy = effects["indicators.fast_ema_period"]
    assert (strategy["applies"], strategy["needs_face_id"]) == ("when_flat", False)

    _, body = _validate(server, (schema.PLATFORM, "risk.max_daily_loss_usd", str(ceiling - 1)))
    tighten = body["changes"][0]
    assert (tighten["applies"], tighten["needs_face_id"]) == ("at_once", False)
    assert _snapshot(root) == before  # nothing is written


@pytest.mark.parametrize(("owner", "key", "value", "cause"), [
    (schema.PLATFORM, "risk.max_open_positions", "1.5", "WRONG_TYPE"),
    (schema.PLATFORM, "risk.max_open_positions", "0", "BELOW_MINIMUM"),
    (schema.PLATFORM, "risk.max_daily_loss_usd", "abc", "WRONG_TYPE"),
    (schema.PLATFORM, "risk.max_daily_loss_usd", "1e3", "WRONG_TYPE"),
    (schema.PLATFORM, "risk.max_quote_age_seconds", "1" * 40 + ".5", "WRONG_TYPE"),
    (schema.PLATFORM, "risk.max_daily_loss_usd", "50.5", "OFF_STEP"),
    (schema.PLATFORM, "risk.symbol_cooldown_after_loss_enabled", "yes", "WRONG_TYPE"),
    (schema.PLATFORM, "risk.planned_loss_usd", "50", "ENGINEERING_KEY"),
    (schema.PLATFORM, "risk.no_such_key", "50", "UNKNOWN_KEY"),
    (schema.PLATFORM, "run.environment", '"production"', "CLOSED_KEY"),
    (schema.PLATFORM, "environments.production.trading_enabled", "true", "CLOSED_KEY"),
    (schema.PLATFORM, "environments.production.authorization_file", '""', "CLOSED_KEY"),
    (schema.PLATFORM, "massive.api_key_variable", '"X"', "CLOSED_KEY"),
    (schema.PLATFORM, "model.enabled", "true", "ENGINEERING_KEY"),
    # A closed table or key is refused at any depth and in any spelling of its path.
    (schema.PLATFORM, 'environments."production".trading_enabled', "true", "CLOSED_KEY"),
    (schema.PLATFORM, "environments . production . trading_enabled", "true", "CLOSED_KEY"),
    (schema.PLATFORM, "Environments.PRODUCTION.trading_enabled", "true", "CLOSED_KEY"),
    (schema.PLATFORM, "environments.production", "true", "CLOSED_KEY"),
    (schema.PLATFORM, "environments.production.a.b.c", "true", "CLOSED_KEY"),
    (schema.PLATFORM, "environments.sandbox.no_such_key", "1", "CLOSED_KEY"),
    (schema.PLATFORM, "run.environment.nested", '"x"', "CLOSED_KEY"),
    (schema.PLATFORM, "api.'remote_pairing_file'", '"x"', "CLOSED_KEY"),
    (schema.PLATFORM, "no_such_table.key", "1", "UNKNOWN_KEY"),
    (schema.PLATFORM, "risk..max_open_positions", "1", "UNKNOWN_KEY"),
    (schema.PLATFORM, 'risk."max_open_positions"', "1", "UNKNOWN_KEY"),
    (schema.PLATFORM, "risk.min_daily_room_usd", "100000", "RULE_BROKEN"),
    (PA3, "indicators.fast_ema_period", "1000", "RULE_BROKEN"),
    ("no-such-strategy", "indicators.fast_ema_period", "5", "UNKNOWN_KEY"),
])
def test_a_refused_draft_names_one_typed_cause_per_key(served, owner, key, value, cause):
    server, _ = served
    resp, body = _validate(server, (owner, key, value))
    assert resp.status == 200 and body["valid"] is False
    assert [(r["owner"], r["key"], r["cause"]) for r in body["refusals"]] == [
        (owner, key, cause)]


def test_a_key_missing_from_its_file_is_refused_not_guessed(served):
    server, root = served
    text = (root / STRATEGY).read_text("utf-8")
    (root / STRATEGY).write_text(text.replace("atr_period = ", "old_atr_period = "), "utf-8")
    _, body = _validate(server, (PA3, "indicators.atr_period", "14"))
    assert [r["cause"] for r in body["refusals"]] == ["VALUE_MISSING"]
    assert body["changes"] == []


@pytest.mark.parametrize("body", [
    {}, {"changes": []}, {"changes": {}}, {"changes": [{"owner": "platform", "key": "k"}]},
    {"changes": [{"owner": "platform", "key": "k", "value": 5}]},
    {"changes": [{"owner": "platform", "key": "k", "value": "5", "extra": "x"}]},
    {"changes": [{"owner": "platform", "key": "k", "value": "5"},
                 {"owner": "platform", "key": "k", "value": "6"}]},
    {"changes": [{"owner": "platform", "key": "k", "value": "5"}], "apply": True},
])
def test_a_draft_of_another_shape_is_a_bad_request(served, body):
    server, _ = served
    resp, payload = request(server, "POST", "/settings/validate", body)
    assert resp.status == 400 and doc(payload)["error"] == "bad_request"


def test_methods_and_query_strings(served):
    server, _ = served
    assert request(server, "GET", "/settings/validate")[0].status == 405
    assert request(server, "POST", "/settings/schema", {})[0].status == 405
    assert request(server, "POST", "/settings/values", {})[0].status == 405
    assert request(server, "GET", "/settings/values?x=1")[0].status == 400


def test_a_broken_schema_file_or_owner_file_answers_unavailable(served):
    server, root = served
    (root / STRATEGY).write_text("[indicators\n", "utf-8")
    resp, payload = request(server, "GET", "/settings/values")
    assert resp.status == 503 and STRATEGY.as_posix() in doc(payload)["cause"]
    assert str(root) not in doc(payload)["cause"]
    assert _validate(server, (PA3, "indicators.atr_period", "14"))[0].status == 503
    (root / "config" / "settings_schema.toml").write_text("[platform]\n", "utf-8")
    for path in ("/settings/schema", "/settings/values"):
        resp, payload = request(server, "GET", path)
        assert resp.status == 503 and doc(payload)["cause"].startswith(
            "settings schema unreadable")


def test_without_a_config_path_values_and_validate_are_unavailable(served):
    server, _ = served
    server.settings = replace(server.settings, config_file=None)
    assert request(server, "GET", "/settings/schema")[0].status == 200
    assert request(server, "GET", "/settings/values")[0].status == 503
    assert _validate(server, (schema.PLATFORM, "risk.max_open_positions", "1"))[0].status == 503


def test_a_record_under_a_closed_table_is_still_refused_by_the_draft(served, monkeypatch):
    # Review probe: a mistaken platform record under [environments.production] must not make
    # the key changeable from the apps, even when the loader's check is bypassed.
    _, root = served
    key = "environments.production.trading_enabled"
    base = next(r for r in schema.RECORDS if r.kind is schema.Kind.BOOL
                and r.owner == schema.PLATFORM)
    probe = replace(base, key=key)
    classes = schema.load_classes(root / "config" / "settings_schema.toml")
    monkeypatch.setattr(schema, "RECORDS", (*schema.RECORDS, probe))
    settings = (*schema.load(root / "config" / "settings_schema.toml"), probe)
    body = draft_document(settings, ((schema.PLATFORM, key, "true"),),
                          root / "config" / "bot.toml", root, classes)
    assert [(r.key, r.cause) for r in body.refusals] == [(key, schema.Cause.CLOSED_KEY)]
    assert body.changes == ()
