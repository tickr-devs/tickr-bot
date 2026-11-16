"""`python -m tickr.runtime release` / `rollback` (runtime/release.py).

Git runs for real on temporary repositories (an origin with `main`, a clone, worktrees);
uv, the release's preflight, launchctl and ps are a fake runner: no launchd is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config as config_module
from tickr.api.contract import AccountPosition, AccountV1, write_account
from tickr.runtime import release
from tickr.runtime.__main__ import main
from tickr.store import DATABASE_FILE, Store
from tickr.version import code_version

from .conftest import ROOT

T0 = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)  # the account file written at T0 is fresh
BOT, WATCHDOG, API = release.BOT_JOB, release.WATCHDOG_JOB, release.API_JOB
GIT_ID = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false")


def _git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, "-C", str(folder), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, *, push: bool = True) -> str:
    (repo / "src" / "tickr" / f"{name}.py").write_text(f"# {name}\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    if push:
        _git(repo, "push", "-q", "origin", "HEAD:main")
    return _git(repo, "rev-parse", "HEAD")


def _account(**overrides) -> AccountV1:
    values = dict(read_at=T0, equity_usd=None, buying_power_usd=None, positions=(), orders=(),
                  matches_records=True, mismatch=None)
    values.update(overrides)
    return AccountV1(**values)


class World:
    """An origin, the operator's checkout (with THE data folder) and a releases folder."""

    def __init__(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.repo = tmp_path / "Tickr"
        (self.repo / "src" / "tickr").mkdir(parents=True)
        for part in ("config", "strategies", "ops"):
            shutil.copytree(ROOT / part, self.repo / part)
        (self.repo / ".gitignore").write_text("/data/\n.venv/\n", "utf-8")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "remote", "add", "origin", str(origin))
        self.first = _commit(self.repo, "first")
        self.config_path = self.repo / "config" / "bot.toml"
        self.cfg = config_module.load(self.config_path)
        self.data = self.repo / "data"
        self.data.mkdir()
        Store.open(self.data / DATABASE_FILE, busy_timeout_seconds=Decimal(5)).close()
        (self.data / "keep.txt").write_text("accumulated data", "utf-8")
        self.account = self.data / self.cfg.api.account_file
        write_account(self.account, _account())
        self.releases = tmp_path / "releases"
        self.env_file = tmp_path / "env"
        self.env_file.write_text("", "utf-8")
        self.fake = FakeRunner(self)
        self.lines: list[str] = []

    def context(self) -> release.Context:
        ctx = release.context(self.cfg, self.config_path, self.releases, self.env_file,
                              self.fake, os.environ, self.lines.append)
        return replace(ctx, sleep=self.fake.sleep, clock=self.fake.clock, now=lambda: NOW)

    def live(self) -> str | None:
        link = self.releases / release.LIVE
        return Path(os.readlink(link)).name if link.is_symlink() else None

    def previous(self) -> str | None:
        link = self.releases / release.PREVIOUS
        return Path(os.readlink(link)).name if link.is_symlink() else None


class FakeRunner:
    """git for real; uv, the preflight, launchctl and ps faked and recorded."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[list[str]] = []
        self.pids: dict[str, int | None] = {BOT: 101, WATCHDOG: None, API: 103}
        self.stuck: set[str] = set()
        self.preflight_exit = 0
        self.preflight_override: dict[str, object] = {}
        self.processes: list[str] = []
        # What the broker shows to `broker-flat`; a string is the cause its read fails with.
        self.broker: dict[str, list[str]] | str = {"positions": [], "open_orders": []}
        self.now = 0.0
        self.next_pid = 500

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def kinds(self) -> list[str]:
        return [call[0] if call[0] != release.LAUNCHCTL else f"launchctl {call[1]}"
                for call in self.calls if call[0] != release.GIT]

    def __call__(self, argv, cwd, env, timeout) -> release.Done:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == release.GIT:
            return release.run_process(argv, cwd, env, timeout)
        if tuple(argv) == release.UV_SYNC:
            python = cwd / release.VENV_PYTHON
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
            return release.Done(0, "", "")
        if argv[0] == release.SH and argv[7] == release.BROKER_FLAT:
            if isinstance(self.broker, str):
                body = {"record": release.BROKER_FLAT, "error": self.broker}
                return release.Done(1, json.dumps(body) + "\n", "")
            body = {"record": release.BROKER_FLAT, "error": None, **self.broker}
            return release.Done(0, json.dumps(body) + "\n", "")
        if argv[0] == release.SH:
            assert argv[7] == release.PREFLIGHT
            return self._preflight(Path(argv[4]), argv[6])
        if argv[:2] == [release.LAUNCHCTL, "list"]:
            pid = self.pids.get(argv[2])
            out = '{\n\t"Label" = "x";\n' + (f'\t"PID" = {pid};\n' if pid else "") + "};\n"
            return release.Done(0, out, "")
        label = argv[-1].rsplit("/", 1)[-1]
        if argv[:2] == [release.LAUNCHCTL, "kill"]:
            if label not in self.stuck:
                self.pids[label] = None
            return release.Done(0, "", "")
        if argv[:2] == [release.LAUNCHCTL, "kickstart"]:
            self.next_pid += 1
            self.pids[label] = self.next_pid
            return release.Done(0, "", "")
        if argv[0] == release.PS[0]:
            return release.Done(0, "\n".join(self.processes), "")
        raise AssertionError(f"unexpected command {argv}")

    def _preflight(self, folder: Path, config: str) -> release.Done:
        real = folder.resolve()
        cfg = config_module.load(real / config)
        body = {"record": "preflight", "exit_code": self.preflight_exit,
                "cause": None if self.preflight_exit == 0 else "token-unusable",
                "config_path": str((real / config).resolve()),
                "config_digest": cfg.digest,
                "data_dir": str((cfg.root / cfg.run.data_dir).resolve()),
                "checks": [{"name": "code-version", "passed": True,
                            "detail": code_version(real, {}, Decimal(5))}]}
        body.update(self.preflight_override)
        return release.Done(self.preflight_exit, json.dumps(body) + "\n", "")


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# --- refusals: nothing running is touched -------------------------------------------------


def test_a_fresh_account_file_with_a_position_refuses_early(world):
    write_account(world.account,
                  _account(positions=(AccountPosition("ABCD", Decimal("10"), Decimal("12")),)))
    with pytest.raises(release.ReleaseRefused, match="5 s ago shows 1 position"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.fake.calls == [] and world.live() is None


def test_a_stale_account_file_proves_nothing_the_broker_decides(world):
    # The file says flat, but it is older than broker_read_max_age_seconds: the broker read
    # just before the switch shows a position and refuses; nothing was stopped or switched.
    stale = NOW - timedelta(seconds=int(world.cfg.orders.broker_read_max_age_seconds) + 1)
    write_account(world.account, _account(read_at=stale))
    world.fake.broker = {"positions": ["ABCD"], "open_orders": ["ABCD"]}
    with pytest.raises(release.ReleaseRefused, match="broker shows positions"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.live() is None and "launchctl kill" not in world.fake.kinds()
    assert world.fake.kinds()[-1] == release.SH  # refused at the broker read


def test_a_stale_position_in_the_account_file_is_not_a_refusal(world):
    stale = NOW - timedelta(seconds=int(world.cfg.orders.broker_read_max_age_seconds) + 1)
    write_account(world.account, _account(
        read_at=stale, positions=(AccountPosition("ABCD", Decimal("10"), Decimal("12")),)))
    release.release(world.context(), release.DEFAULT_COMMIT)  # the broker shows flat
    assert world.live() == world.first


def test_a_failed_broker_read_refuses(world):
    world.fake.broker = "RateLimited: request budget exhausted"
    with pytest.raises(release.ReleaseRefused, match="broker read failed: RateLimited"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.live() is None and "launchctl kill" not in world.fake.kinds()


def test_rollback_also_reads_the_broker(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    _commit(world.repo, "second")
    release.release(world.context(), release.DEFAULT_COMMIT)
    live = world.live()
    world.fake.broker = {"positions": ["ABCD"], "open_orders": []}
    with pytest.raises(release.ReleaseRefused, match="broker shows positions"):
        release.rollback(world.context())
    assert world.live() == live


def test_an_open_trade_in_the_database_refuses(world):
    store = Store.open(world.data / DATABASE_FILE, busy_timeout_seconds=Decimal(5))
    store._db.execute(
        "INSERT INTO trades (trade_id, run_id, symbol, state, entry_quantity, entry_limit_price,"
        " stop_price, planned_loss_usd, opened_at) VALUES ('t1', 'r1', 'ABCD', 'open', '10',"
        " '10', '9.5', '5', '2026-11-16T15:00:00+00:00')")
    store.close()
    with pytest.raises(release.ReleaseRefused, match="open trade"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.fake.calls == []


def test_no_account_file_leaves_the_proof_to_the_broker(world):
    world.account.unlink()
    release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.live() == world.first
    assert any(c[0] == release.SH and c[7] == release.BROKER_FLAT for c in world.fake.calls)


def test_a_commit_not_on_main_is_refused(world):
    side = _commit(world.repo, "side", push=False)
    with pytest.raises(release.ReleaseRefused, match="not on origin/main"):
        release.release(world.context(), side)
    assert world.fake.kinds() == [] and not (world.releases / side).exists()


def test_uncommitted_config_in_the_checkout_is_refused(world):
    world.config_path.write_text(world.config_path.read_text("utf-8") + "\n", "utf-8")
    with pytest.raises(release.ReleaseRefused, match="uncommitted changes under config"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.fake.kinds() == []


def test_preflight_failure_leaves_the_running_release_untouched(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    second = _commit(world.repo, "second")
    world.fake.calls.clear()
    world.fake.preflight_exit = 1
    with pytest.raises(release.ReleaseRefused, match="preflight failed"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.live() == world.first and world.previous() is None
    assert world.fake.kinds() == ["uv", release.SH]  # prepared and checked; nothing stopped
    assert world.fake.calls[-1][7] == release.PREFLIGHT  # the broker was not even read
    assert (world.releases / second / ".venv").is_dir()  # left for a look; pruned later


@pytest.mark.parametrize("override,match", [
    ({"checks": [{"name": "code-version", "detail": "abc-dirty"}]}, "code version"),
    ({"data_dir": "/elsewhere/data"}, "not .*data"),
    ({"config_path": "/elsewhere/bot.toml"}, "not the release config"),
])
def test_preflight_of_other_code_config_or_data_is_refused(world, override, match):
    world.fake.preflight_override = override
    with pytest.raises(release.ReleaseRefused, match=match):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.live() is None and "launchctl kill" not in world.fake.kinds()


def test_context_refuses_an_empty_data_folder(world):
    (world.data / DATABASE_FILE).unlink()
    with pytest.raises(release.ReleaseRefused, match="existing data folder"):
        world.context()


def test_context_refuses_data_inside_the_releases_folder(world):
    with pytest.raises(release.ReleaseRefused, match="contain each other"):
        release.context(world.cfg, world.config_path, world.data / "releases", world.env_file,
                        world.fake, os.environ, world.lines.append)


def test_context_refuses_a_stop_wait_below_the_bot_exit_timeout(world):
    runtime = replace(world.cfg.runtime, release_stop_wait_seconds=60)
    with pytest.raises(release.ReleaseRefused, match="release_stop_wait_seconds"):
        release.context(replace(world.cfg, runtime=runtime), world.config_path, world.releases,
                        world.env_file, world.fake, os.environ, world.lines.append)


# --- release, restart, prune, rollback ------------------------------------------------------


def test_release_pins_the_commit_and_shares_the_one_data_folder(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    folder = world.releases / world.first
    assert world.live() == world.first
    assert (folder / ".git").is_file()  # a worktree, detached at the commit
    assert code_version(folder, {}, Decimal(5)) == world.first  # the data link is not "dirty"
    link = folder / "data"
    assert link.is_symlink() and link.resolve() == world.data.resolve()
    assert (world.releases / release.LIVE / "data" / "keep.txt").read_text() == "accumulated data"
    # Prepared and checked before anything was stopped; running jobs were then stopped,
    # waited for and started again, a stopped one left to its schedule.
    kinds = world.fake.kinds()
    assert kinds[:3] == ["uv", release.SH, release.SH] and kinds.index("launchctl kill") > 2
    sh = [c[7] for c in world.fake.calls if c[0] == release.SH]
    assert sh == [release.PREFLIGHT, release.BROKER_FLAT]  # the broker read last
    killed = [c[-1] for c in world.fake.calls if c[:2] == [release.LAUNCHCTL, "kill"]]
    started = [c[-1] for c in world.fake.calls if c[:2] == [release.LAUNCHCTL, "kickstart"]]
    uid = os.getuid()
    assert killed == started == [f"gui/{uid}/{BOT}", f"gui/{uid}/{API}"]
    assert any(f"config release {(folder / 'config' / 'bot.toml').resolve()}" in line
               for line in world.lines)
    # Again: nothing to do.
    world.fake.calls.clear()
    release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.fake.kinds() == [] and "already live" in world.lines[-1]


def test_switches_keep_live_and_previous_and_prune_the_rest_never_the_data(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    second = _commit(world.repo, "second")
    release.release(world.context(), release.DEFAULT_COMMIT)
    assert (world.live(), world.previous()) == (second, world.first)
    third = _commit(world.repo, "third")
    world.fake.processes = [f"{(world.releases / world.first).resolve()}/.venv/bin/python -m x"]
    release.release(world.context(), release.DEFAULT_COMMIT)
    assert (world.releases / world.first).is_dir()  # a process still runs from it: kept
    fourth = _commit(world.repo, "fourth")
    world.fake.processes = []
    release.release(world.context(), release.DEFAULT_COMMIT)
    assert (world.live(), world.previous()) == (fourth, third)
    left = sorted(p.name for p in world.releases.iterdir() if not p.name.startswith("."))
    assert left == sorted([release.LIVE, release.PREVIOUS, third, fourth])
    assert world.first not in _git(world.repo, "worktree", "list")
    assert (world.data / "keep.txt").read_text() == "accumulated data"
    assert (world.data / DATABASE_FILE).is_file()


def test_rollback_swaps_live_and_previous(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    second = _commit(world.repo, "second")
    release.release(world.context(), release.DEFAULT_COMMIT)
    release.rollback(world.context())
    assert (world.live(), world.previous()) == (world.first, second)
    release.rollback(world.context())
    assert (world.live(), world.previous()) == (second, world.first)


def test_rollback_needs_no_network(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    second = _commit(world.repo, "second")
    release.release(world.context(), release.DEFAULT_COMMIT)
    _git(world.repo, "remote", "set-url", "origin", str(world.repo.parent / "gone.git"))
    with pytest.raises(release.ReleaseRefused, match="git fetch origin failed"):
        release.release(world.context(), release.DEFAULT_COMMIT)  # a release does fetch
    world.fake.calls.clear()
    release.rollback(world.context())
    assert (world.live(), world.previous()) == (world.first, second)
    git = [c for c in world.fake.calls if c[0] == release.GIT]
    assert git and not any("fetch" in c for c in git)
    assert "uv" not in world.fake.kinds()  # verified, not synced again


def test_rollback_refuses_a_changed_previous_folder(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    _commit(world.repo, "second")
    release.release(world.context(), release.DEFAULT_COMMIT)
    (world.releases / world.first / "src" / "tickr" / "first.py").write_text("# x\n", "utf-8")
    with pytest.raises(release.ReleaseRefused, match="changes to tracked files"):
        release.rollback(world.context())
    assert world.previous() == world.first


@pytest.mark.parametrize("command", ["release", "rollback"])
def test_a_command_from_another_data_folder_is_refused(world, tmp_path, command):
    release.release(world.context(), release.DEFAULT_COMMIT)
    _commit(world.repo, "second")
    release.release(world.context(), release.DEFAULT_COMMIT)
    other = tmp_path / "other"  # another checkout with its own data and database
    shutil.copytree(world.repo / "config", other / "config")
    (other / "data").mkdir()
    Store.open(other / "data" / DATABASE_FILE, busy_timeout_seconds=Decimal(5)).close()
    cfg = config_module.load(other / "config" / "bot.toml")
    write_account(other / "data" / cfg.api.account_file, _account())
    ctx = release.context(cfg, other / "config" / "bot.toml", world.releases, world.env_file,
                          world.fake, os.environ, world.lines.append)
    world.fake.calls.clear()
    live = world.live()
    with pytest.raises(release.ReleaseRefused, match="live release uses the data folder"):
        release.release(ctx, release.DEFAULT_COMMIT) if command == "release" else \
            release.rollback(ctx)
    assert world.fake.calls == [] and world.live() == live


def test_link_failure_is_reported_with_the_links(world, monkeypatch):
    release.release(world.context(), release.DEFAULT_COMMIT)
    second = _commit(world.repo, "second")

    def broken(link, name):
        raise PermissionError("denied")
    monkeypatch.setattr(release, "_point", broken)
    with pytest.raises(release.ReleaseFailed, match=f"live -> {world.first}, previous -> none"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.live() == world.first and (world.releases / second).is_dir()


def test_watchdog_stops_first_and_starts_last(world):
    world.fake.pids[WATCHDOG] = 102
    release.release(world.context(), release.DEFAULT_COMMIT)
    killed = [c[-1].rsplit("/", 1)[-1] for c in world.fake.calls
              if c[:2] == [release.LAUNCHCTL, "kill"]]
    started = [c[-1].rsplit("/", 1)[-1] for c in world.fake.calls
               if c[:2] == [release.LAUNCHCTL, "kickstart"]]
    assert killed == [WATCHDOG, BOT, API] and started == [BOT, API, WATCHDOG]
    last_kill = max(i for i, c in enumerate(world.fake.calls) if c[1:2] == ["kill"])
    first_start = min(i for i, c in enumerate(world.fake.calls) if c[1:2] == ["kickstart"])
    assert last_kill < first_start  # every job stopped before any starts


def test_preflight_shell_errors_are_never_printed(world, capsys):
    original = world.fake._preflight
    world.fake._preflight = lambda folder, config: release.Done(
        2, "", "env: line 3: SECRET=abc: not found")
    with pytest.raises(release.ReleaseRefused) as refused:
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert "SECRET" not in str(refused.value) and "exit 2" in str(refused.value)
    assert not any("SECRET" in line for line in world.lines)
    world.fake._preflight = original


def test_rollback_without_a_previous_release_is_refused(world):
    release.release(world.context(), release.DEFAULT_COMMIT)
    with pytest.raises(release.ReleaseRefused, match="no live and previous"):
        release.rollback(world.context())


def test_a_job_that_does_not_stop_is_reported(world):
    world.fake.stuck = {BOT}
    with pytest.raises(release.ReleaseFailed, match=f"{BOT}: pid 101 still running"):
        release.release(world.context(), release.DEFAULT_COMMIT)
    assert world.live() == world.first  # switched; the old process keeps its own release
    assert world.fake.now >= world.cfg.runtime.release_stop_wait_seconds
    started = [c[-1] for c in world.fake.calls if c[:2] == [release.LAUNCHCTL, "kickstart"]]
    assert started == [f"gui/{os.getuid()}/{API}"]


def test_a_developer_environment_never_reaches_the_release(world):
    ctx = replace(world.context(), environ={"PATH": "/usr/bin:/bin", "VIRTUAL_ENV": "/dev/venv",
                                            "PYTHONPATH": "/dev/src"})
    seen: list[dict] = []
    ctx = replace(ctx, run=lambda argv, cwd, env, timeout: (seen.append(dict(env)),
                                                           world.fake(argv, cwd, env, timeout))[1])
    release.release(ctx, release.DEFAULT_COMMIT)
    assert seen and all("VIRTUAL_ENV" not in env and "PYTHONPATH" not in env for env in seen)


# --- version and CLI ------------------------------------------------------------------------


def test_version_of_a_release_worktree(world):
    folder = world.releases / world.first
    _git(world.repo, "worktree", "add", "-q", "--detach", str(folder), world.first)
    (folder / "data").symlink_to(world.data)
    assert code_version(folder, {}, Decimal(5)) == world.first
    (folder / "src" / "tickr" / "first.py").write_text("# changed\n", "utf-8")
    assert code_version(folder, {}, Decimal(5)) == world.first + "-dirty"


def test_cli_refusal_says_the_running_release_is_unchanged(world, monkeypatch, capsys):
    from tickr.runtime import __main__ as runtime_main

    monkeypatch.setattr(runtime_main, "RELEASE_RUNNER", world.fake)
    write_account(world.account,
                  _account(read_at=datetime.now(UTC),
                           positions=(AccountPosition("ABCD", Decimal("1"), None),)))
    code = main(["--config", str(world.config_path), "release", "--releases",
                 str(world.releases), "--env-file", str(world.env_file)])
    assert code == 1
    assert "running release is unchanged" in capsys.readouterr().err
    assert world.fake.calls == []


def test_broker_flat_command_reads_positions_and_orders_only(world, monkeypatch, capsys):
    from tickr.runtime import __main__ as runtime_main
    from tickr.types import Position

    class Broker:
        def positions(self):
            return [Position("ABCD", Decimal("10"), None)]

        def open_orders(self):
            return []

        def place(self, *a, **k):  # pragma: no cover - never reachable through the wrapper
            raise AssertionError("send")

    monkeypatch.setattr(runtime_main, "FLAT_BROKER", lambda cfg, pending: Broker())
    assert main(["--config", str(world.config_path), release.BROKER_FLAT]) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert row == {"record": release.BROKER_FLAT, "error": None, "positions": ["ABCD"],
                   "open_orders": []}


def test_broker_flat_command_reports_a_failed_read(world, monkeypatch, capsys):
    from tickr.broker.base import RateLimited
    from tickr.runtime import __main__ as runtime_main

    class Broker:
        def positions(self):
            raise RateLimited("request budget exhausted")

    monkeypatch.setattr(runtime_main, "FLAT_BROKER", lambda cfg, pending: Broker())
    assert main(["--config", str(world.config_path), release.BROKER_FLAT]) == 1
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert row["error"] == "request budget exhausted"
