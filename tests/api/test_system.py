"""GET /system: the System card, read-only (status, preflight record, training file, watchdog
journal and the Mac's power assertions)."""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.contract import Freshness, LinkState, StreamHealth
from tickr.api.server import make_server, settings_from_config
from tickr.api.system import (
    NO_BROKER_READ,
    NO_HOST,
    NO_PREFLIGHT,
    NO_TRAINING,
    SystemSettings,
    system_document,
)
from tickr.runtime.preflight import Check, CheckName, PreflightCause, PreflightJob, PreflightResult
from tickr.runtime.preflight import record_text as preflight_record

from .samples import T0, status
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]
# `pmset -g assertions` with the bot's and the watchdog's caffeinate holds (two processes,
# each holding two assertions) and one of another process.
NAMED = 'named: "caffeinate command-line tool"'
PMSET = "\n".join([
    "Assertion status system-wide:",
    "   PreventUserIdleSystemSleep     1",
    "Listed by owning process:",
    f"   pid 523(caffeinate): [0x0000a1b2000192f3] 00:15:02 PreventUserIdleSystemSleep {NAMED}",
    f"   pid 523(caffeinate): [0x0000a1b2000192f4] 00:15:02 PreventSystemSleep {NAMED}",
    f"   pid 611(caffeinate): [0x0000a1b2000192f5] 00:02:10 PreventUserIdleSystemSleep {NAMED}",
    '   pid 88(coreaudiod): [0x0000a1b2000192f6] 00:01:00 PreventUserIdleSystemSleep named: "a"',
    ""])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # The shipped training file is under ~/: keep it inside the test's folder.
    monkeypatch.setenv("HOME", str(tmp_path))
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    return config.load(ROOT / "config" / "bot.toml", root=tmp_path)


def _response(written: contract.StatusV1 | None, now=T0, cause=None) -> contract.StatusResponseV1:
    return contract.StatusResponseV1(now, written, None, False, cause, "", Decimal("3"))


def _runner(code=0, out=PMSET):
    return lambda command, timeout: (code, out)


RUNNING = status()


def _doc(cfg, written=RUNNING, runner=None) -> contract.SystemV1:
    return system_document(SystemSettings.from_config(cfg), _response(written),
                           runner or _runner())


def test_endpoint_answers_its_schema_with_nothing_recorded(cfg):
    settings = replace(settings_from_config(cfg), port=0)
    server = make_server(settings, Clock(T0))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        resp, payload = request(server, "GET", "/system")
        body = doc(payload)
        assert resp.status == 200
        assert errors(contract.json_schema(contract.SystemV1), body) == []
        assert request(server, "POST", "/system", {})[0].status == 405
    finally:
        server.shutdown()
        server.server_close()
    assert body["broker_link"]["state"] == "unknown"
    assert body["morning_check"] is None and body["morning_check_cause"] == NO_PREFLIGHT
    assert body["training"] is None and body["training_cause"] == NO_TRAINING
    assert body["host"]["kind"] == "mac" and body["host"]["watchdog"]["restarts"] == []


@pytest.mark.parametrize(("past", "state"), [("warn", LinkState.FRESH),
                                            ("warn+", LinkState.AGING),
                                            ("max+", LinkState.STALE)])
def test_link_state_follows_the_warning_and_block_ages(cfg, past, state):
    warn, block = cfg.api.broker_read_warn_seconds, cfg.orders.broker_read_max_age_seconds
    age = {"warn": warn, "warn+": warn + 1, "max+": block + 1}[past]
    written = status(freshness=Freshness(T0 - timedelta(seconds=float(age)), None,
                                         StreamHealth.QUIET))
    link = _doc(cfg, written).broker_link
    assert (link.state, link.stream, link.cause) == (state, StreamHealth.QUIET, None)
    assert (link.warn_seconds, link.max_age_seconds) == (warn, block)


def test_link_unknown_without_a_read_or_a_status(cfg):
    written = status(freshness=Freshness(None, None, StreamHealth.OFF))
    assert _doc(cfg, written).broker_link.cause == NO_BROKER_READ
    link = system_document(SystemSettings.from_config(cfg),
                           _response(None, cause="the bot has not written a status file"),
                           _runner()).broker_link
    assert (link.state, link.stream, link.last_read_at) == (LinkState.UNKNOWN, None, None)


def test_morning_check_is_the_preflight_record(cfg):
    result = PreflightResult(
        (Check(CheckName.CONFIG, True, "valid"),
         Check(CheckName.TOKEN, False, "TimeoutError", PreflightCause.TOKEN_UNUSABLE)),
        session_today=True, environment="sandbox", config=cfg, config_path=Path("bot.toml"))
    path = SystemSettings.from_config(cfg).preflight_record
    path.parent.mkdir(parents=True)
    path.write_text(preflight_record(result, PreflightJob.PREFLIGHT, T0,
                                     T0 + timedelta(seconds=40)))
    check = _doc(cfg).morning_check
    assert check is not None
    assert (check.outcome, check.blocks_start, check.cause, check.day) == (
        contract.PreflightOutcome.FAILED, True, "token-unusable", T0.date())
    assert [(c.name, c.passed) for c in check.checks] == [("config", True), ("token", False)]


@pytest.mark.parametrize("text", ["{", "[]", '{"schema_version": 2}',
                                  '{"schema_version": 1, "job": "preflight"}'])
def test_unreadable_preflight_record_is_a_cause_never_a_guess(cfg, text):
    path = SystemSettings.from_config(cfg).preflight_record
    path.parent.mkdir(parents=True)
    path.write_text(text)
    document = _doc(cfg)
    assert document.morning_check is None
    assert document.morning_check_cause.startswith("preflight record unreadable (")


def _training(cfg, **body):
    path = SystemSettings.from_config(cfg).training_progress
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {"schema_version": 1, "job": "wide model", "state": "running", "done": 130,
            "total": 209, "started_at": "2026-11-16T06:00:00+00:00",
            "updated_at": "2026-11-16T14:58:00+00:00", "cause": None}
    path.write_text(json.dumps(base | body))
    return _doc(cfg)


def test_training_progress_file(cfg):
    training = _training(cfg).training
    assert training is not None
    assert (training.job, training.state, training.done, training.total) == (
        "wide model", contract.TrainingState.RUNNING, 130, 209)


def test_training_progress_file_may_live_outside_the_release(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    api = replace(cfg.api, training_progress_file="~/research/progress.json")
    path = SystemSettings.from_config(replace(cfg, api=api)).training_progress
    assert path == tmp_path / "research" / "progress.json"


@pytest.mark.parametrize("body", [{"done": 210}, {"done": -1}, {"total": "209"},
                                  {"state": "paused"}, {"updated_at": "2026-11-16T14:58:00"}])
def test_training_file_out_of_shape_is_refused(cfg, body):
    document = _training(cfg, **body)
    assert document.training is None
    assert document.training_cause.startswith("training progress unreadable (")


def _journal(cfg, *records):
    path = SystemSettings.from_config(cfg).watchdog_journal
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_watchdog_journal_gives_start_check_and_restarts_newest_first(cfg):
    _journal(cfg,
             {"record": "watchdog-start", "day": "2026-11-13", "checks": []},
             {"record": "watchdog-restart", "at": "2026-11-13T20:40:00+00:00", "started": True,
              "why": "status stale", "open_trades": 1, "cause": None, "early": False,
              "command": ["/bin/launchctl"]},
             {"record": "watchdog-check", "at": "2026-11-13T20:54:00+00:00",
              "broker_read": True, "positions": 0, "open_orders": 0},
             {"record": "watchdog-start", "day": "2026-11-16", "checks": []},
             {"record": "watchdog-restart", "at": "2026-11-16T15:01:00+00:00",
              "started": False, "cause": "stop marker exists; the watchdog never starts a bot "
                                         "the operator stopped"},
             {"record": "watchdog-restart", "at": "2026-11-16T15:02:00+00:00", "started": False,
              "why": "status stale", "open_trades": 2, "cause": "restart command exited 113",
              "early": True, "command": ["/bin/launchctl"]})
    host = _doc(cfg).host
    assert host is not None
    dog = host.watchdog
    assert dog.started_day == T0.date() and dog.restart_count == 3 and dog.cause is None
    assert (dog.last_check_read, dog.last_check_flat) == (True, True)
    assert [(r.outcome, r.early, r.open_trades) for r in dog.restarts] == [
        (contract.RestartOutcome.FAILED, True, 2), (contract.RestartOutcome.REFUSED, False, None),
        (contract.RestartOutcome.STARTED, False, 1)]


def test_restart_rows_are_capped_by_config(cfg):
    restart = {"record": "watchdog-restart", "at": "2026-11-16T15:01:00+00:00", "started": False,
               "cause": "x"}
    _journal(cfg, *[restart] * (cfg.api.system_restart_rows + 1))
    dog = _doc(cfg).host.watchdog
    assert (len(dog.restarts), dog.restart_count) == (cfg.api.system_restart_rows,
                                                      cfg.api.system_restart_rows + 1)


def test_failed_watchdog_read_is_not_flat_but_unknown(cfg):
    _journal(cfg, {"record": "watchdog-check", "at": "2026-11-16T20:54:00+00:00",
                   "broker_read": False, "cause": "TimeoutError"})
    dog = _doc(cfg).host.watchdog
    assert (dog.last_check_read, dog.last_check_flat) == (False, None)


def test_unreadable_watchdog_journal(cfg):
    _journal(cfg, {"record": "watchdog-start", "day": "2026-11-16", "checks": []})
    path = SystemSettings.from_config(cfg).watchdog_journal
    path.write_text(path.read_text() + "not json\n")
    dog = _doc(cfg).host.watchdog
    assert dog.cause == "watchdog journal unreadable (JSONDecodeError)"
    assert (dog.started_day, dog.restarts, dog.restart_count) == (None, (), 0)


def test_sleep_hold_counts_caffeinate_processes_only(cfg):
    sleep = _doc(cfg).host.sleep
    assert (sleep.state, sleep.holds) == (contract.SleepState.HELD, 2)
    sleep = _doc(cfg, runner=_runner(out="Listed by owning process:\n")).host.sleep
    assert (sleep.state, sleep.holds) == (contract.SleepState.NOT_HELD, 0)


def test_sleep_check_failure_is_unknown_with_its_cause(cfg):
    assert _doc(cfg, runner=_runner(code=1)).host.sleep == contract.SleepV1(
        contract.SleepState.UNKNOWN, None, "sleep check exited 1")

    def missing(command, timeout):
        raise FileNotFoundError(command[0])

    assert _doc(cfg, runner=missing).host.sleep.cause == "sleep check failed (FileNotFoundError)"


def test_a_machine_without_host_services_reports_none(tmp_path):
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    text = (ROOT / "config" / "bot.toml").read_text("utf-8").replace(
        'system_services = "mac"', 'system_services = "none"')
    path = tmp_path / "bot.toml"
    path.write_text(text, "utf-8")
    cfg = config.load(path, root=tmp_path)

    def never(command, timeout):
        raise AssertionError("no command runs on a host without services")

    document = _doc(cfg, runner=never)
    assert (document.host, document.host_cause) == (None, NO_HOST)


def test_unknown_system_services_is_a_config_error(tmp_path):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8").replace(
        'system_services = "mac"', 'system_services = "linux"')
    path = tmp_path / "bot.toml"
    path.write_text(text, "utf-8")
    with pytest.raises(config.ConfigError, match="system_services"):
        config.load(path, root=tmp_path)
