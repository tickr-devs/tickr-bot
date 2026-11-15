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

from aperture import config
from aperture.api import contract
from aperture.api.lab_replay import LabReplay, LabReplayRefused
from aperture.api.server import make_remote_server, make_server, settings_from_config
from aperture.backtest.universe import PoolEntry, write_pool
from aperture.config import ConfigError

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request

ROOT = Path(__file__).resolve().parents[2]
TODAY = T0.date() - timedelta(days=3)  # the last fetched pool day: Friday 2026-11-13
SATURDAY = T0 + timedelta(days=5)  # T0 is 10:00 New York on Monday 2026-11-16


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
    return config.load(ROOT / "config" / "bot.toml", root=tmp_path)


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
        resp, payload = request(server, "POST", "/lab/replay", {})
        assert resp.status == 409 and "regular session" in doc(payload)["cause"]
        clock.now = SATURDAY
        resp, payload = request(server, "POST", "/lab/replay", {})
        assert resp.status == 409 and "pool.csv is missing" in doc(payload)["cause"]
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
    assert command[command.index("--strategy") + 1] == "aperture.strategy.price_action_v1"
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
    assert failed.cause == "data/backtest/minutes is missing"
    # The next start keeps only its own run folder (and never touches other folders).
    keep = results.parent / "notes"
    keep.mkdir()
    lab.start()
    assert not results.exists() and keep.exists()


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
    assert _lab(anytime, T0, spawner()).start().state is contract.LabReplayState.RUNNING


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
                                       ("lab_replay_results_dir", "backtest"),
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
