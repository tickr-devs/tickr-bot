"""GET /readiness: the readiness view, read-only. It shows what `trading_gate()` says for every
environment without switching to it, and never writes the config or the authorization file."""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.contract import ReadinessCode, ReadinessState
from tickr.api.history import HistoryUnavailable
from tickr.api.readiness import NOT_HELD, ReadinessSettings, readiness_document
from tickr.api.server import make_server, settings_from_config
from tickr.api.system import NO_HOST
from tickr.runtime.preflight import Check, CheckName, PreflightCause, PreflightJob, PreflightResult
from tickr.runtime.preflight import record_text as preflight_record

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]
PASS, FAIL, UNKNOWN = ReadinessState.PASS, ReadinessState.FAIL, ReadinessState.UNKNOWN
STATEMENT = "I approve trading with real money at the configured limits."


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    return config.load(ROOT / "config" / "bot.toml", root=tmp_path)


def _target(cfg: config.Config) -> str:
    """The configured environment that requires owner authorization (named by config only)."""
    names = [n for n, e in cfg.environments.items() if e.requires_owner_authorization]
    assert len(names) == 1 and names[0] != cfg.environment_name
    return names[0]


def _enabled(cfg: config.Config, name: str) -> config.Config:
    env = replace(cfg.environments[name], trading_enabled=True)
    return replace(cfg, environments={**cfg.environments, name: env})


def _authorize(cfg: config.Config, name: str, **fields: str) -> Path:
    record = {"environment": name, "config_digest": cfg.digest, "approved_on": "2026-11-20",
              "statement": STATEMENT, **fields}
    path = cfg.root / cfg.environments[name].authorization_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in record.items()), "utf-8")
    return path


def _doc(cfg, clean=lambda today: 0, environ=None, probe=lambda c: None):
    return readiness_document(ReadinessSettings.from_config(cfg), T0, clean, environ or {},
                              probe)


def _check(document, code):
    return next(c for c in document.checklist if c.code is code)


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def test_endpoint_answers_its_schema_and_refuses_post(cfg):
    settings = replace(settings_from_config(cfg), port=0)
    server = make_server(settings, Clock(T0))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        resp, payload = request(server, "GET", "/readiness")
        body = doc(payload)
        assert resp.status == 200
        assert errors(contract.json_schema(contract.ReadinessV1), body) == []
        assert request(server, "POST", "/readiness", {})[0].status == 405
    finally:
        server.shutdown()
        server.server_close()
    assert body["environment"] == cfg.environment_name
    assert body["config_digest"] == cfg.digest
    clean = next(c for c in body["checklist"] if c["code"] == "clean_days")
    assert (clean["state"], clean["value"]) == ("fail", f"0 / {cfg.api.readiness_clean_days}")


def test_every_environment_shows_the_gate_itself_without_switching(cfg):
    target = _target(cfg)
    document = _doc(cfg)
    assert [e.name for e in document.environments] == list(cfg.environments)
    loaded = config.trading_gate(cfg)
    assert (document.gate.open, document.gate.cause) == (loaded.open, loaded.cause)
    other = next(e for e in document.environments if e.name == target)
    assert not other.loaded and not other.gate.open
    assert other.gate.code == "trading_disabled"
    assert other.gate.params == {"environment": target}
    assert cfg.environment_name == document.environment  # nothing switched


def test_checklist_order_and_a_shipped_config_is_not_ready(cfg):
    target = _target(cfg)
    document = _doc(cfg, probe=lambda c: "chart probe C1 is not recorded")
    assert [(c.code, c.environment) for c in document.checklist] == [
        (ReadinessCode.CLEAN_DAYS, cfg.environment_name),
        (ReadinessCode.PREFLIGHT, cfg.environment_name),
        (ReadinessCode.UNATTENDED_SAFEGUARD, cfg.environment_name),
        (ReadinessCode.KEYS, target), (ReadinessCode.DATA_PROBE, target),
        (ReadinessCode.TRADING_ENABLED, target), (ReadinessCode.AUTHORIZATION, target)]
    assert _check(document, ReadinessCode.TRADING_ENABLED).state is FAIL
    authorization = _check(document, ReadinessCode.AUTHORIZATION)
    assert authorization.state is FAIL and "not found" in (authorization.cause or "")
    record = next(e for e in document.environments if e.name == target).authorization
    assert record is not None and (record.present, record.valid) == (False, False)
    assert record.code == "authorization_file_missing"
    assert _check(document, ReadinessCode.DATA_PROBE).cause == "chart probe C1 is not recorded"


def test_a_valid_record_is_shown_field_by_field_and_never_written(cfg):
    target = _target(cfg)
    _authorize(cfg, target)
    enabled = _enabled(cfg, target)
    before = _files(cfg.root)
    document = _doc(enabled)
    assert _files(cfg.root) == before
    env = next(e for e in document.environments if e.name == target)
    assert env.gate.open and env.gate.code == "authorization_valid"
    record = env.authorization
    assert record is not None
    assert (record.present, record.environment, record.approved_on, record.digest_matches,
            record.valid) == (True, target, "2026-11-20", True, True)
    assert STATEMENT not in contract.dumps(document)
    assert _check(document, ReadinessCode.AUTHORIZATION).state is PASS
    assert _check(document, ReadinessCode.TRADING_ENABLED).state is PASS
    assert config.trading_gate(enabled).open  # the loaded environment's gate is unchanged


@pytest.mark.parametrize(("fields", "code", "environment", "matches"), [
    ({"config_digest": "0" * 64}, "authorization_digest_mismatch", None, False),
    ({"environment": "another"}, "authorization_other_environment", "another", True),
])
def test_a_record_that_does_not_match_fails_with_the_gates_cause(cfg, fields, code,
                                                                  environment, matches):
    target = _target(cfg)
    _authorize(cfg, target, **fields)
    document = _doc(_enabled(cfg, target))
    record = next(e for e in document.environments if e.name == target).authorization
    assert record is not None and record.present and not record.valid
    assert record.code == code and record.digest_matches is matches
    assert record.environment == (environment or target)
    assert _check(document, ReadinessCode.AUTHORIZATION).state is FAIL


def test_an_unreadable_record_shows_nothing_from_it(cfg):
    target = _target(cfg)
    path = _authorize(cfg, target)
    path.write_text("not = = toml", "utf-8")
    record = next(e for e in _doc(cfg).environments if e.name == target).authorization
    assert record is not None and record.present and record.code == "authorization_not_toml"
    assert (record.environment, record.approved_on, record.digest_matches) == (None, None, None)


@pytest.mark.parametrize(("set_", "state"), [(3, PASS), (1, FAIL), (0, UNKNOWN)])
def test_keys_are_checked_by_name_for_presence_only(cfg, set_, state):
    target = cfg.environments[_target(cfg)]
    names = (target.app_key_variable, target.app_secret_variable, target.account_variable)
    environ = {name: "x" for name in names[:set_]}
    check = _check(_doc(cfg, environ=environ), ReadinessCode.KEYS)
    assert check.state is state and check.value is None
    if state is UNKNOWN:
        assert check.cause == NOT_HELD


def test_clean_days_against_the_config(cfg):
    wanted = cfg.api.readiness_clean_days
    check = _check(_doc(cfg, clean=lambda today: wanted), ReadinessCode.CLEAN_DAYS)
    assert (check.state, check.value) == (PASS, f"{wanted} / {wanted}")

    def unreadable(today):
        raise HistoryUnavailable("database unreadable: locked")

    check = _check(_doc(cfg, clean=unreadable), ReadinessCode.CLEAN_DAYS)
    assert (check.state, check.cause) == (UNKNOWN, "database unreadable: locked")


def _preflight(cfg, passed: bool, at=T0) -> None:
    checks = (Check(CheckName.CONFIG, True, "valid"),) if passed else (
        Check(CheckName.TOKEN, False, "TimeoutError", PreflightCause.TOKEN_UNUSABLE),)
    result = PreflightResult(checks, session_today=True, environment=cfg.environment_name,
                             config=cfg, config_path=Path("bot.toml"))
    path = ReadinessSettings.from_config(cfg).preflight_record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(preflight_record(result, PreflightJob.PREFLIGHT, at,
                                     at + timedelta(seconds=40)))


@pytest.mark.parametrize(("passed", "at", "state"), [
    (True, T0, PASS), (False, T0, FAIL), (True, T0 - timedelta(days=3), UNKNOWN)])
def test_preflight_is_todays_record(cfg, passed, at, state):
    _preflight(cfg, passed, at)
    check = _check(_doc(cfg), ReadinessCode.PREFLIGHT)
    assert check.state is state and check.value == at.date().isoformat()
    if state is FAIL:
        assert check.cause == "preflight failed: token-unusable"


def _watchdog(cfg, day: str) -> None:
    path = ReadinessSettings.from_config(cfg).watchdog_journal
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"record": "watchdog-start", "day": day, "checks": []}) + "\n")


@pytest.mark.parametrize(("day", "state"), [(None, FAIL), ("2026-11-13", UNKNOWN),
                                            (T0.date().isoformat(), PASS)])
def test_unattended_safeguard_is_the_watchdog_started_today(cfg, day, state):
    # The shipped config names no host services (the server); this case is a Mac.
    mac = replace(cfg, api=replace(cfg.api, system_services=config.SYSTEM_SERVICES_MAC))
    if day is not None:
        _watchdog(mac, day)
    check = _check(_doc(mac), ReadinessCode.UNATTENDED_SAFEGUARD)
    assert check.state is state and check.value == day


def test_a_machine_without_host_services_cannot_prove_the_safeguard(cfg):
    plain = replace(cfg, api=replace(cfg.api, system_services="none"))
    check = _check(_doc(plain), ReadinessCode.UNATTENDED_SAFEGUARD)
    assert (check.state, check.cause) == (UNKNOWN, NO_HOST)


def test_without_an_environment_that_requires_authorization_only_evidence_is_listed(cfg):
    environments = {n: replace(e, requires_owner_authorization=False)
                    for n, e in cfg.environments.items()}
    document = _doc(replace(cfg, environments=environments))
    assert [c.code for c in document.checklist] == [
        ReadinessCode.CLEAN_DAYS, ReadinessCode.PREFLIGHT, ReadinessCode.UNATTENDED_SAFEGUARD]
    assert all(e.authorization is None for e in document.environments)


def test_the_probe_is_asked_about_that_environment(cfg):
    target = _target(cfg)
    asked = []
    _doc(cfg, probe=lambda c: asked.append(c.environment_name))
    assert asked == [target]
