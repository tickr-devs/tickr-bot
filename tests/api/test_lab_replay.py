"""GET and POST /lab/replay: the Lab's simulated replay runs the backtest as a child process."""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.lab_replay import LabReplay, LabReplayRefused
from tickr.api.server import make_remote_server, make_server, settings_from_config
from tickr.backtest.universe import PoolEntry, write_pool
from tickr.config import ConfigError

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]
TODAY = T0.date() - timedelta(days=3)  # the last fetched pool day: Friday 2026-11-13
SATURDAY = T0 + timedelta(days=5)  # T0 is 10:00 New York on Monday 2026-11-16
DEAD_PID = 2 ** 22 + 7  # above Linux's and macOS's pid limits: no such process


class Child:
    """A fake backtest process: alive (this test's pid) until `finish`."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.done = threading.Event()
        self.code = 0

    def wait(self) -> int:
        self.done.wait(5)
        return self.code


class Spawner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.children: list[Child] = []

    @property
    def child(self) -> Child:
        return self.children[-1]

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.children.append(Child())
        return self.child


@pytest.fixture
def spawner():
    """Every fake child ends with its test, so no waiter thread outlives it."""
    made: list[Spawner] = []

    def new() -> Spawner:
        made.append(Spawner())
        return made[-1]

    yield new
    for spawn in made:
        for child in spawn.children:
            child.done.set()


@pytest.fixture
def cfg(tmp_path):
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    # The versions and runs below are price-action-v1's, so it is the active one here.
    return replace(cfg, strategy=replace(cfg.strategy, active="price-action-v1"))


def _lab(cfg, now, spawn, config_path=ROOT / "config" / "bot.toml"):
    return LabReplay(settings_from_config(cfg, config_path).lab, Clock(now), spawn)


def _pool(cfg):
    lab = settings_from_config(cfg).lab
    lab.data.mkdir(parents=True)
    write_pool([PoolEntry(TODAY, 1, "ABCD", Decimal(20), Decimal(29), Decimal(50),
                          Decimal(10**8))], lab.data / "pool.csv")


def _wait_for_exit(lab):
    for _ in range(500):
        if lab.state().finished_at is not None:
            return
        threading.Event().wait(0.01)
    raise AssertionError("the waiter never wrote exit.json")


def test_idle_get_and_refusals_answer_409_with_one_cause(cfg):
    settings = replace(settings_from_config(cfg, ROOT / "config" / "bot.toml"), port=0)
    clock = Clock(T0)
    server = make_server(settings, clock)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        resp, payload = request(server, "GET", "/lab/replay")
        body = doc(payload)
        assert resp.status == 200 and body["state"] == "idle" and body["sessions"] == 20
        assert errors(contract.json_schema(contract.LabReplayV1), body) == []
        # In the session the document says when a start is allowed again, before any POST.
        assert body["available_at"] == "2026-11-16T21:00:00.000Z"
        resp, payload = request(server, "POST", "/lab/replay", {})
        refusal = doc(payload)
        assert resp.status == 409 and refusal["cause"] == (
            "refused during the regular session until 2026-11-16T21:00:00.000Z "
            "([api] lab_replay_outside_session_only)")
        assert refusal["cause_codes"] == [
            {"code": "outside_session_only", "params": {"until": "16:00"}}]
        clock.now = SATURDAY
        assert doc(request(server, "GET", "/lab/replay")[1])["available_at"] is None
        resp, payload = request(server, "POST", "/lab/replay", {})
        assert resp.status == 409 and "pool.csv is missing" in doc(payload)["cause"]
        assert doc(payload)["cause_codes"] == [{"code": "no_backtest_data", "params": {}}]
        assert request(server, "POST", "/lab/replay", {"sessions": 5})[0].status == 400
        assert request(server, "POST", "/lab/replay")[0].status == 415
    finally:
        server.shutdown()
        server.server_close()


def test_start_runs_the_niced_backtest_then_reports_done(cfg, spawner):
    _pool(cfg)
    spawn = spawner()
    lab = _lab(cfg, SATURDAY, spawn)
    started = lab.start()
    assert started.state is contract.LabReplayState.RUNNING
    assert (started.strategy, started.version) == ("price-action-v1", "1")
    command, kwargs = spawn.calls[0]
    assert command[:3] == ["nice", "-n", "19"]
    assert command[command.index("--last-sessions") + 1] == "20"
    assert command[command.index("--strategy") + 1] == "tickr.strategy.price_action_v1"
    values = Path(command[command.index("--values") + 1])
    assert values == cfg.root / "strategies" / "price-action-v1" / "strategy.toml"
    assert values.exists() and kwargs["cwd"] == cfg.root
    assert lab.state().state is contract.LabReplayState.RUNNING
    with pytest.raises(LabReplayRefused, match="already running"):
        lab.start()
    results = Path(command[command.index("--results") + 1])
    (results / "report.json").write_text(json.dumps(_report()))
    spawn.child.done.set()
    _wait_for_exit(lab)
    done = lab.state()
    assert done.state is contract.LabReplayState.DONE and done.run_id == started.run_id
    assert done.summary is not None and done.summary.trades == 3
    assert done.summary.mean_r_interval_low is None


def test_a_refused_backtest_fails_with_its_cause_without_the_home_folder(cfg, spawner):
    _pool(cfg)
    spawn = spawner()
    lab = _lab(cfg, SATURDAY, spawn)
    lab.start()
    command, _ = spawn.calls[0]
    results = Path(command[command.index("--results") + 1])
    (results / "log.txt").write_text(
        f"fetching nothing\nbacktest refused: {cfg.root}/data/backtest/minutes is missing\n")
    spawn.child.code = 2
    spawn.child.done.set()
    _wait_for_exit(lab)
    failed = lab.state()
    assert failed.state is contract.LabReplayState.FAILED
    assert failed.cause == "backtest refused: data/backtest/minutes is missing"
    # The next start keeps only its own run folder (and never touches other folders).
    keep = results.parent.parent / "notes"
    keep.mkdir()
    lab.start()
    assert not results.parent.exists() and keep.exists()


def _results(command):
    return Path(command[command.index("--results") + 1])


def test_compare_runs_the_second_version_after_the_first_over_the_same_window(cfg, spawner):
    _pool(cfg)
    spawn = spawner()
    lab = _lab(cfg, SATURDAY, spawn)
    started = lab.start(None, "price-action-v2")
    assert started.folder == "price-action-v1" and started.compare is not None
    assert (started.compare.folder, started.compare.version) == ("price-action-v2", "2")
    assert started.compare.state is contract.LabReplayState.RUNNING  # waits for the first
    assert len(spawn.calls) == 1
    (_results(spawn.calls[0][0]) / "report.json").write_text(json.dumps(_report()))
    first = spawn.child
    first.done.set()
    for _ in range(500):  # the waiter starts the second version
        if len(spawn.calls) == 2:
            break
        threading.Event().wait(0.01)
    command, _ = spawn.calls[1]
    assert command[command.index("--strategy") + 1] == "tickr.strategy.price_action_v2"
    assert command[command.index("--last-sessions") + 1] == "20"
    middle = lab.state()
    assert middle.state is contract.LabReplayState.RUNNING and middle.finished_at is None
    with pytest.raises(LabReplayRefused, match="already running"):
        lab.start()
    second = _report()
    second["all_signals"]["trades"] = 5
    (_results(command) / "report.json").write_text(json.dumps(second))
    spawn.child.done.set()
    for _ in range(500):
        if lab.state().compare.state is contract.LabReplayState.DONE:
            break
        threading.Event().wait(0.01)
    done = lab.state()
    assert done.state is contract.LabReplayState.DONE and done.finished_at is not None
    assert done.summary.trades == 3 and done.compare.summary.trades == 5
    assert errors(contract.json_schema(contract.LabReplayV1),
                  json.loads(contract.dumps(done))) == []


def test_the_moment_between_the_two_versions_still_reads_running(cfg, spawner):
    """The first child has exited and the second is not started yet: the run is running, and
    a start is refused instead of deleting the folder the second version is about to use."""
    _pool(cfg)
    spawn = spawner()
    lab = _lab(cfg, SATURDAY, spawn)
    gate, release = threading.Event(), threading.Event()
    plain = spawn.__call__

    def slow(command, **kwargs):
        if spawn.calls:  # the second version: hold its start
            gate.set()
            release.wait(5)
        return plain(command, **kwargs)

    lab.spawn = slow
    lab.start(None, "price-action-v2")
    (_results(spawn.calls[0][0]) / "report.json").write_text(json.dumps(_report()))
    spawn.child.done.set()  # the first child exits
    assert gate.wait(5)
    current_path = lab.settings.results / "current.json"
    current = json.loads(current_path.read_text())
    current["legs"][0]["pid"] = DEAD_PID  # as for a real child that has exited
    current_path.write_text(json.dumps(current))
    try:
        gap = lab.state()
        assert gap.state is contract.LabReplayState.RUNNING and gap.summary is None
        assert gap.compare.state is contract.LabReplayState.RUNNING
        with pytest.raises(LabReplayRefused, match="already running"):
            lab.start()
    finally:
        release.set()
    for _ in range(500):
        if len(spawn.calls) == 2:
            break
        threading.Event().wait(0.01)
    assert len(spawn.calls) == 2 and _results(spawn.calls[1][0]).parent.exists()


def test_an_unreadable_current_file_fails_and_the_next_start_replaces_it(cfg, spawner):
    _pool(cfg)
    lab = _lab(cfg, SATURDAY, spawner())
    lab.settings.results.mkdir(parents=True)
    for text in ("{not json", json.dumps({"run_id": "0" * 32, "legs": []})):
        (lab.settings.results / "current.json").write_text(text)
        broken = lab.state()
        assert broken.state is contract.LabReplayState.FAILED
        assert "current.json unreadable" in broken.cause
    assert lab.start().state is contract.LabReplayState.RUNNING


def test_one_day_replays_a_single_session_ending_at_that_day(cfg, spawner):
    _pool(cfg)
    spawn = spawner()
    lab = _lab(cfg, SATURDAY, spawn)
    started = lab.start("volume-shock-v1", None, TODAY)
    assert started.day == TODAY and started.sessions == 1 and started.compare is None
    assert (started.strategy, started.folder) == ("volume-shock-v1", "volume-shock-v1")
    command, _ = spawn.calls[0]
    assert command[command.index("--last-sessions") + 1] == "1"
    assert command[command.index("--end-day") + 1] == TODAY.isoformat()


def test_choices_list_every_version_and_the_pool_days(cfg, spawner):
    _pool(cfg)
    lab = _lab(cfg, SATURDAY, spawner())
    idle = lab.state()
    assert idle.days == (TODAY,)
    roles = {v.folder: (v.role, v.cause) for v in idle.versions}
    assert idle.versions[0].folder == "price-action-v1"
    assert roles["price-action-v1"] == (contract.LabVersionRole.ACTIVE, None)
    assert roles["price-action-v2"] == (contract.LabVersionRole.CANDIDATE, None)
    broken = cfg.root / "strategies" / "my-idea"
    broken.mkdir()
    (broken / "strategy.toml").write_text("[strategy]\nversion = \"1\"\n")
    cause = {v.folder: v.cause for v in lab.state().versions}["my-idea"]
    assert "no [strategy] name" in cause and str(cfg.root) not in cause
    with pytest.raises(LabReplayRefused, match="no \\[strategy\\] name"):
        lab.start("my-idea")


@pytest.mark.parametrize("args,match", [
    (("nope", None, None), "no strategy folder 'nope'"),
    ((None, "price-action-v1", None), "same version twice"),
    ((None, None, TODAY - timedelta(days=1)), "not a day of the fetched data"),
])
def test_start_refuses_a_bad_choice(cfg, spawner, args, match):
    _pool(cfg)
    spawn = spawner()
    with pytest.raises(LabReplayRefused, match=match):
        _lab(cfg, SATURDAY, spawn).start(*args)
    assert spawn.calls == []


def test_a_run_from_before_comparisons_still_reads(cfg, spawner):
    lab = _lab(cfg, SATURDAY, spawner())
    run = lab.settings.results / ("0" * 32)
    run.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps(_report()))
    (run / "exit.json").write_text(json.dumps({"finished_at": "2026-11-21T15:04:00.000Z",
                                                "code": 0}))
    (lab.settings.results / "current.json").write_text(json.dumps({
        "run_id": "0" * 32, "started_at": "2026-11-21T15:00:00.000Z", "pid": 1,
        "strategy": "price-action-v1", "version": "1", "sessions": 20}))
    old = lab.state()
    assert old.state is contract.LabReplayState.DONE and old.summary.trades == 3
    assert old.folder is None and old.compare is None and old.day is None


def test_post_body_shapes(cfg):
    settings = replace(settings_from_config(cfg, ROOT / "config" / "bot.toml"), port=0)
    server = make_server(settings, Clock(SATURDAY))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        for body in ({"day": "13-11-2026"}, {"version": 1}, {"other": "x"}, {"version": ""}):
            assert request(server, "POST", "/lab/replay", body)[0].status == 400
        resp, payload = request(server, "POST", "/lab/replay", {"compare": "price-action-v2"})
        assert resp.status == 409 and "pool.csv is missing" in doc(payload)["cause"]
    finally:
        server.shutdown()
        server.server_close()


def test_refused_without_config_path_or_strategy_module(cfg, spawner):
    _pool(cfg)
    with pytest.raises(LabReplayRefused, match="config file path"):
        _lab(cfg, SATURDAY, spawner(), None).start()
    no_module = replace(cfg, observers=replace(cfg.observers, strategy_modules=()))
    with pytest.raises(LabReplayRefused, match="no module"):
        _lab(no_module, SATURDAY, spawner()).start()


def test_in_session_allowed_when_the_switch_is_off(cfg, spawner):
    _pool(cfg)
    anytime = replace(cfg, api=replace(cfg.api, lab_replay_outside_session_only=False))
    lab = _lab(anytime, T0, spawner())
    assert lab.state().available_at is None
    assert lab.start().state is contract.LabReplayState.RUNNING


def test_both_listeners_share_one_replay(cfg):
    settings = replace(settings_from_config(cfg), port=0, remote_port=0)
    local = make_server(settings)
    remote = make_remote_server(local)
    try:
        assert remote.lab is local.lab
    finally:
        remote.server_close()
        local.server_close()


@pytest.mark.parametrize("key,value", [("lab_replay_sessions", 0),
                                       ("lab_replay_niceness", 20),
                                       ("lab_replay_results_dir", "nightly/current"),
                                       ("lab_replay_results_dir", "."),
                                       ("lab_replay_results_dir", ""),
                                       ("lab_replay_results_dir", "../lab"),
                                       ("lab_replay_results_dir", "/tmp/lab"),
                                       ("lab_replay_data_dir", "a/../b")])
def test_config_refuses_bad_lab_replay_values(cfg, key, value):
    with pytest.raises(ConfigError):
        settings_from_config(replace(cfg, api=replace(cfg.api, **{key: value})))


def _report():
    return {"all_signals": {"trades": 3, "win_rate_percent": "33.33", "mean_r": "0.1",
                            "total_r": "0.3", "profit_factor": "1.2", "mean_r_interval": None,
                            "confidence_percent": "95", "max_drawdown_r": "1"},
            "bot": {"trades": 2, "total_usd": "12.5", "max_drawdown_usd": "50"},
            "pool": {"replayed_symbol_days": 3},
            "last_sessions": {"requested": 20, "first": "2026-10-19", "last": "2026-11-13",
                              "sessions_without_replayed_days": 17}}
