"""POST /commands/start-bot: launchd's start of the bot job, never the bot inside the API."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.contract import BotState
from tickr.api.server import make_server, settings_from_config
from tickr.lock import SingleInstanceLock

from .samples import T0, status
from .schema_check import errors
from .test_server import ROOT, Clock, doc, request, schema


@pytest.fixture
def settings(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    return replace(settings_from_config(cfg), port=0)


@pytest.fixture
def api(settings):
    clock = Clock(T0 + timedelta(seconds=2))
    server = make_server(settings, clock)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server, clock
    server.shutdown()
    server.server_close()


class Runner:
    """Records the commands it is asked to run and answers with `code` (or raises)."""

    def __init__(self, code: int = 0, raises: Exception | None = None) -> None:
        self.code, self.raises, self.calls = code, raises, []

    def __call__(self, command, timeout):
        self.calls.append((tuple(command), timeout))
        if self.raises is not None:
            raise self.raises
        return self.code


def _start(server, runner: Runner, body=None):
    server.start_runner = runner
    resp, payload = request(server, "POST", "/commands/start-bot", {} if body is None else body)
    return resp, doc(payload)


def test_shipped_command_is_launchd_kickstart_of_the_bot_job_with_this_uid(settings):
    command = settings.start.command
    assert command[:2] == ("/bin/launchctl", "kickstart")
    assert "-k" not in command  # never restarts a running job
    assert command[2].startswith("gui/") and command[2].endswith("/local.tickr.bot")
    assert "{uid}" not in command[2] and command[2].split("/")[1].isdigit()


def test_start_without_a_status_runs_the_command_once(api, settings):
    server, clock = api
    runner = Runner()
    resp, body = _start(server, runner)
    assert resp.status == 202
    assert body == {"requested_at": contract.format_time(clock.now), "stop_marker_cleared": False,
                    "api_version": contract.API_VERSION}
    assert errors(schema("bot-start-v1"), body) == []
    assert runner.calls == [(settings.start.command, settings.start.timeout_seconds)]
    assert not server.settings.inbox.exists() or not any(server.settings.inbox.iterdir())


def test_start_clears_the_operator_stop_marker_first(api, settings):
    server, _ = api
    marker = settings.start.stop_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"command_id": "c", "stopped_at": "x"}\n', "utf-8")
    seen = []
    runner = Runner()
    real = runner.__call__

    def checking(command, timeout):
        seen.append(marker.exists())  # preflight would refuse while it exists
        return real(command, timeout)

    resp, body = _start(server, checking)
    assert resp.status == 202 and body["stop_marker_cleared"] is True
    assert seen == [False] and not marker.exists()


@pytest.mark.parametrize("state", [BotState.STARTING, BotState.RUNNING, BotState.STOPPING])
def test_refused_while_a_fresh_status_says_the_bot_is_alive(api, settings, state):
    server, _ = api
    contract.write_status(settings.status_path, status(bot_state=state))
    marker = settings.start.stop_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}\n", "utf-8")
    runner = Runner()
    resp, body = _start(server, runner)
    assert resp.status == 409 and body["error"] == "conflict"
    assert body["cause"] == f"the bot is {state.value}: nothing to start"
    assert runner.calls == [] and marker.exists()  # nothing changed


def test_stopped_or_stale_status_does_not_block(api, settings):
    server, clock = api
    contract.write_status(settings.status_path, status(bot_state=BotState.STOPPED))
    assert _start(server, Runner())[0].status == 202
    contract.write_status(settings.status_path, status())  # running, but stale
    clock.now = T0 + timedelta(seconds=int(settings.status_stale_seconds) + 1)
    assert _start(server, Runner())[0].status == 202


def test_refused_while_a_bot_process_holds_the_lock_even_if_stopped(api, settings):
    # A fresh `stopped` status is written before the process ends; `tickr trade` holds the
    # same lock. The live pid in the lock file is the proof, checked before touching the marker.
    server, _ = api
    contract.write_status(settings.status_path, status(bot_state=BotState.STOPPED))
    marker = settings.start.stop_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}\n", "utf-8")
    with SingleInstanceLock(settings.start.lock_file):
        runner = Runner()
        resp, body = _start(server, runner)
        assert resp.status == 409 and body["error"] == "conflict"
        assert body["cause"].startswith(f"a bot process (pid {os.getpid()}) still holds the lock")
        assert runner.calls == [] and marker.exists()
    assert _start(server, Runner())[0].status == 202  # released: a clean stop leaves no pid


def test_an_unreadable_lock_file_is_a_typed_refusal(api, settings):
    server, _ = api
    settings.start.lock_file.mkdir(parents=True)  # reading a folder fails with an OSError
    runner = Runner()
    resp, body = _start(server, runner)
    assert resp.status == 503 and body["cause"].startswith("the bot's lock file could not be read")
    assert runner.calls == []


def test_a_marker_that_cannot_be_removed_is_a_typed_refusal(api, settings, monkeypatch):
    server, _ = api
    marker = settings.start.stop_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}\n", "utf-8")

    def denied(self, missing_ok=False):
        raise PermissionError(str(self))

    monkeypatch.setattr(type(marker), "unlink", denied)
    runner = Runner()
    resp, body = _start(server, runner)
    assert resp.status == 503 and body["error"] == "unavailable"
    assert body["cause"] == ("the operator stop could not be cleared (PermissionError): "
                             "nothing was started")
    assert runner.calls == []


def test_clearing_the_operator_stop_is_logged_with_the_time(api, settings, capsys):
    server, clock = api
    marker = settings.start.stop_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}\n", "utf-8")
    assert _start(server, Runner())[0].status == 202
    assert f"{clock.now.isoformat()} start-bot: operator stop cleared by Start" \
        in capsys.readouterr().err


def test_a_hanging_start_never_holds_back_queued_commands(api):
    server, _ = api
    # The start runs under its own lock: with it held, a command still queues at once.
    with server.start_lock:
        assert request(server, "POST", "/commands/entry-stop", {"state": "on"})[0].status == 202


@pytest.mark.parametrize(("runner", "cause"), [
    (Runner(code=113), "the start command exited 113"),
    (Runner(raises=FileNotFoundError("launchctl")), "the start command failed (FileNotFoundError)"),
    (Runner(raises=subprocess.TimeoutExpired("launchctl", 10)),
     "the start command did not return within 10 s"),
])
def test_failed_command_is_unavailable_with_its_cause(api, runner, cause):
    server, _ = api
    resp, body = _start(server, runner)
    assert resp.status == 503 and body == {"error": "unavailable", "cause": cause,
                                           "api_version": contract.API_VERSION}


def test_failure_after_clearing_the_marker_says_so(api, settings):
    server, _ = api
    marker = settings.start.stop_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}\n", "utf-8")
    resp, body = _start(server, Runner(code=1))
    assert resp.status == 503
    assert body["cause"] == ("the start command exited 1 (the operator stop was cleared, so the "
                             "next scheduled start runs)")


def test_no_command_configured_is_unavailable(api):
    server, _ = api
    server.settings = replace(server.settings,
                              start=replace(server.settings.start, command=()))
    runner = Runner()
    resp, body = _start(server, runner)
    assert resp.status == 503 and "start_bot_command is empty" in body["cause"]
    assert runner.calls == []


@pytest.mark.parametrize("body", [{"now": True}, [], "x"])
def test_body_must_be_empty(api, body):
    server, _ = api
    runner = Runner()
    resp, _ = _start(server, runner, body)
    assert resp.status == 400 and runner.calls == []


def test_get_is_not_allowed_and_bad_key_refused(api):
    server, _ = api
    resp, _ = request(server, "GET", "/commands/start-bot")
    assert resp.status == 405 and resp.getheader("Allow") == "POST"
    server.start_runner = Runner()
    resp, _ = request(server, "POST", "/commands/start-bot", {}, {"Idempotency-Key": "bad key"})
    assert resp.status == 400 and server.start_runner.calls == []
