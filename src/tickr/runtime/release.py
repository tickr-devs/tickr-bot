"""Pinned releases: launchd runs Tickr from one exact commit, switched only at restart time.

Layout (absolute `<releases>` folder, given on the command line; docs/operations.md, "Releases"):

    <releases>/<sha>/      a detached git worktree of the repository at that commit, with its own
                           `.venv` (`uv sync --frozen`, so `tickr` is imported from this
                           folder's src/) and `<[run] data_dir>` a link to THE data folder
    <releases>/live        link -> <sha>: what every launchd job runs (launchd.py: `{{RELEASE}}`)
    <releases>/previous    link -> the release live named before the last switch (rollback)

Decisions:
- Data: exactly one data folder, the one the `--config` given to this command resolves
  (`config.root / [run] data_dir`, symlinks resolved): the existing folder, never a new one and
  never a copy. A release reaches it the way the Docker image reaches its volume: mounted at
  `<root>/<data_dir>`, here as a link. Code keeps addressing data only as root/data_dir.
  It is refused when it holds no database (a new, empty folder) or lies inside `<releases>`.
- Config: a release reads its own committed `config/bot.toml` (a config change is a reviewed
  commit, tagged by its digest on every run). A switch never changes it silently: this command
  prints both configs' paths and digests and the `git diff --stat` of config/ between them,
  refuses while the checkout it runs from has uncommitted config changes, and preflight
  journals the resolved config path, its digest and the resolved data folder.
- Switch: the link `live` is replaced atomically (a new link renamed over it). Each job
  resolves it once at its start (`cd -P`), so code under a running process never changes; the
  jobs that were running (bot, watchdog, api) are stopped with SIGTERM, waited for, and started
  again from the new release. Plists never change, so nothing is reinstalled.
- Retention: `live` and `previous` are kept; every other release folder is removed (repo rule:
  keep only the latest backup), unless a running process still runs from it (`ps`), and only
  with `git worktree remove` without force, after unlinking its data link (never a delete
  that could follow the link into the data).

Order of `release`: lock; an early refusal (an open trade in the database, or the bot's
account file at most [orders] broker_read_max_age_seconds old showing a position: that file is
never trusted as proof of flat); refuse unless the commit is on origin/main; prepare the folder
and run its preflight BEFORE anything running is touched; then the proof of flat: a fresh
read-only broker read (`python -m tickr.runtime broker-flat` of the new release, positions and
open orders) showing neither, and no open trade in the database, a failed read refusing (a
refusal up to here leaves the running release as it was); switch; restart; prune.
`rollback` switches to `previous` without GitHub (no fetch): it verifies that folder (HEAD
at its commit, clean, data link) and runs its preflight. Both first refuse when the live
release uses another data folder than this command. Every step is printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tickr import config as config_module
from tickr.api.contract import ContractError, read_account
from tickr.lock import AlreadyRunning, SingleInstanceLock
from tickr.runtime.launchd import VENV_PYTHON
from tickr.runtime.preflight import ExitCode
from tickr.runtime.watchdog import open_trades
from tickr.store import DATABASE_FILE

LIVE = "live"
PREVIOUS = "previous"
LOCK = ".release.lock"  # one release command at a time, in <releases>
DEFAULT_COMMIT = "origin/main"
MAIN = "origin/main"  # a release commit must be on it
REMOTE = "origin"
CONFIG_DIR = "config"
LAUNCHD_DIR = "ops/launchd"
# The jobs a release restarts when they run. Stopped watchdog first (it would see the stopping
# bot missing and alert or spend its one restart a day), then the bot, then the API (launchd's
# KeepAlive starts it again by itself); started bot, API, then the watchdog.
BOT_JOB, WATCHDOG_JOB, API_JOB = "local.tickr.bot", "local.tickr.watchdog", "local.tickr.api"
STOP_ORDER = (WATCHDOG_JOB, BOT_JOB, API_JOB)
START_ORDER = (BOT_JOB, API_JOB, WATCHDOG_JOB)
# Commands (protocol: fixed argument lists, no shell, no user input beyond paths and a sha).
GIT = "git"
UV_SYNC = ("uv", "sync", "--frozen", "--extra", "webull")
LAUNCHCTL = "/bin/launchctl"
PS = ("/bin/ps", "-axo", "command=")
SH = "/bin/sh"
# A `python -m tickr.runtime` command of a release, run as the launchd jobs run it (templates):
# the credentials file sourced by sh, the folder resolved, its own interpreter and config; $4
# is the command (preflight, broker-flat).
_JOB_SCRIPT = ('set -a; . "$0"; set +a; cd -P "$1" || exit; '
               'exec "$PWD/$2" -m tickr.runtime --config "$PWD/$3" "$4"')
PREFLIGHT = "preflight"
BROKER_FLAT = "broker-flat"
# Environment variables that would make a release's interpreter or uv use another checkout's
# code or environment (an activated developer venv): removed for every command run here.
_FOREIGN_ENVIRONMENT = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV",
                        "UV_PROJECT_ENVIRONMENT")
_PREFLIGHT_PASSES = (ExitCode.READY, ExitCode.NO_SESSION)
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_PID = re.compile(r'^\s*"PID"\s*=\s*(\d+);', re.M)


class ReleaseRefused(Exception):
    """Refused before anything running was touched; the message is the single cause."""


class ReleaseFailed(Exception):
    """Failed after the switch began; the message says what runs where."""


@dataclass(frozen=True)
class Done:
    code: int
    out: str
    err: str


# Runs argv in a folder with an environment and a timeout in seconds; replaced in tests.
Runner = Callable[[Sequence[str], Path | None, Mapping[str, str], float], Done]


def run_process(argv: Sequence[str], cwd: Path | None, env: Mapping[str, str],
                timeout: float) -> Done:
    try:
        done = subprocess.run(list(argv), cwd=cwd, env=dict(env), capture_output=True,
                              text=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Done(-1, "", f"{argv[0]} took longer than {timeout:g} s")
    except OSError as exc:
        return Done(-1, "", f"{argv[0]} could not run: {exc}")
    return Done(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class Context:
    """Everything a release needs; built from the config by `context()`, replaced in tests."""

    repo: Path  # a checkout of the repository: release worktrees are added to it
    releases: Path
    data: Path  # THE data folder, resolved
    data_dir: Path  # [run] data_dir: the link's name inside a release
    backups: Path  # <data>/<[run] backups_dir>: the verified pre-release backup goes here
    config_rel: Path  # the config inside a release, relative to its root
    account_file: Path
    env_file: Path
    runtime: config_module.RuntimeConfig
    uid: int
    run: Runner
    environ: Mapping[str, str]
    say: Callable[[str], None]
    account_max_age: Decimal  # [orders] broker_read_max_age_seconds
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    @property
    def live(self) -> Path:
        return self.releases / LIVE

    @property
    def previous(self) -> Path:
        return self.releases / PREVIOUS

    def env(self) -> dict[str, str]:
        return {k: v for k, v in self.environ.items() if k not in _FOREIGN_ENVIRONMENT}

    def step(self, argv: Sequence[str], cwd: Path | None = None) -> Done:
        return self.run(argv, cwd, self.env(), float(self.runtime.release_step_timeout_seconds))

    def git(self, *args: str, folder: Path | None = None) -> Done:
        return self.step([GIT, "--no-optional-locks", "-C", str(folder or self.repo), *args])


def context(cfg: config_module.Config, config_path: Path, releases: Path, env_file: Path,
            run: Runner, environ: Mapping[str, str], say: Callable[[str], None]) -> Context:
    """Check the settings and paths; ReleaseRefused names the first problem."""
    runtime = cfg.runtime
    if runtime.release_poll_seconds <= 0:
        raise ReleaseRefused("[runtime] release_poll_seconds must be positive")
    if not runtime.release_stop_wait_seconds > runtime.bot_exit_timeout_seconds:
        raise ReleaseRefused("[runtime] release_stop_wait_seconds must be above "
                             "bot_exit_timeout_seconds")
    if not runtime.release_step_timeout_seconds > cfg.environment.token_check_duration_seconds:
        raise ReleaseRefused("[runtime] release_step_timeout_seconds must be above "
                             "token_check_duration_seconds (preflight may wait that long)")
    if not releases.is_absolute() or not env_file.is_absolute():
        raise ReleaseRefused("--releases and --env-file must be absolute paths")
    data_dir = Path(cfg.run.data_dir)
    if data_dir.is_absolute() or ".." in data_dir.parts:
        raise ReleaseRefused(f"[run] data_dir must be relative to the root, got {data_dir}")
    data = (cfg.root / data_dir).resolve()
    if not (data / DATABASE_FILE).is_file():
        raise ReleaseRefused(f"no {DATABASE_FILE} in {data}: a release only ever runs on the "
                             "existing data folder")
    releases_real = releases.resolve()
    if data.is_relative_to(releases_real) or releases_real.is_relative_to(data):
        raise ReleaseRefused(f"the data folder {data} and {releases} must not contain each other")
    config_rel = config_path.resolve().relative_to(cfg.root)
    return Context(repo=cfg.root, releases=releases, data=data, data_dir=data_dir,
                   backups=data / cfg.run.backups_dir,
                   config_rel=config_rel, account_file=data / cfg.api.account_file,
                   env_file=env_file, runtime=runtime, uid=os.getuid(), run=run,
                   environ=environ, say=say,
                   account_max_age=cfg.orders.broker_read_max_age_seconds)


# --- checks -------------------------------------------------------------------------------


def early_cause(account_file: Path, database: Path, now: datetime,
                max_age: Decimal) -> str | None:
    """An early refusal before anything is prepared, or None. It never proves flat: the
    account file counts only when it is at most `max_age` seconds old ([orders]
    broker_read_max_age_seconds) and shows a position; an open trade in the database counts
    always. The proof is `_require_broker_flat`, a fresh broker read, just before the switch."""
    try:
        account = read_account(account_file)
    except (FileNotFoundError, ContractError, OSError, ValueError):
        account = None
    if account is not None and account.positions:
        age = (now - account.read_at).total_seconds()
        if age <= max_age:
            return (f"the bot's broker read {age:.0f} s ago shows "
                    f"{len(account.positions)} position(s)")
    count = open_trades(database)
    if count:
        return f"the database has {count} open trade(s)"
    return None


def _require_early_flat(ctx: Context) -> None:
    cause = early_cause(ctx.account_file, ctx.data / DATABASE_FILE, ctx.now(),
                        ctx.account_max_age)
    if cause is not None:
        raise ReleaseRefused(f"not flat: {cause}")
    ctx.say("no open trade in the database, no fresh position in the bot's account file")


def _require_broker_flat(ctx: Context, folder: Path) -> None:
    """The proof, just before the switch: the broker itself, read now (positions and open
    orders only, through the release's own adapter and config, run as launchd runs a job: the
    env file sourced by sh), and no open trade in the database. A failed read refuses."""
    ctx.say(f"broker read (positions, open orders) from {folder}")
    done = ctx.step([SH, "-c", _JOB_SCRIPT, str(ctx.env_file), str(folder),
                     VENV_PYTHON.as_posix(), ctx.config_rel.as_posix(), BROKER_FLAT])
    body = _record(done.out, BROKER_FLAT)
    if body is None:
        # Only the exit code: the shell's error text could quote the credentials file.
        raise ReleaseRefused(f"broker read printed no result (exit {done.code})")
    if done.code != 0 or body.get("error"):
        raise ReleaseRefused(f"broker read failed: {body.get('error')}")
    positions, orders = body.get("positions"), body.get("open_orders")
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise ReleaseRefused("broker read result has no positions or open_orders list")
    if positions or orders:
        raise ReleaseRefused(f"not flat: the broker shows positions {positions} and open "
                             f"orders {orders}")
    count = open_trades(ctx.data / DATABASE_FILE)
    if count:
        raise ReleaseRefused(f"not flat: the database has {count} open trade(s)")
    ctx.say("flat: the broker shows no position and no open order; no open trade")


def _record(out: str, name: str) -> dict | None:
    """The last JSON line of `out` whose record is `name`."""
    found = None
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("record") == name:
            found = row
    return found


def _commit(ctx: Context, ref: str) -> str:
    ctx.say(f"fetch {REMOTE}")
    done = ctx.git("fetch", "--quiet", REMOTE)
    if done.code != 0:
        raise ReleaseRefused(f"git fetch {REMOTE} failed: {_first(done.err)}")
    done = ctx.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = done.out.strip()
    if done.code != 0 or not _SHA.match(sha):
        raise ReleaseRefused(f"{ref!r} is not a commit")
    if ctx.git("merge-base", "--is-ancestor", sha, MAIN).code != 0:
        raise ReleaseRefused(f"{sha} is not on {MAIN}")
    ctx.say(f"commit {sha} is on {MAIN}")
    return sha


def _first(text: str) -> str:
    return (text.strip().splitlines() or ["no message"])[0]


def _target(link: Path) -> str | None:
    """The release a link names (its folder name), or None when there is no link."""
    if not link.is_symlink():
        if link.exists():
            raise ReleaseRefused(f"{link} exists and is not a link")
        return None
    return Path(os.readlink(link)).name


def _require_live_data(ctx: Context) -> None:
    """Refuse unless the running release uses the same data folder as this command: started
    from another checkout, it would prove "flat" from another folder and switch the bot onto
    another database. The first release has nothing to compare with (it prints its link)."""
    live = ctx.live
    if not live.is_symlink():
        return
    used = (live / ctx.data_dir).resolve()
    if used != ctx.data:
        raise ReleaseRefused(f"the live release uses the data folder {used}, this command "
                             f"{ctx.data}: run it with the config of the checkout whose data "
                             "the bot uses")
    ctx.say(f"data folder {ctx.data} is the live release's")


def _require_committed_config(ctx: Context) -> None:
    done = ctx.git("status", "--porcelain", "--untracked-files=no", "--", CONFIG_DIR)
    if done.code != 0:
        raise ReleaseRefused(f"git status in {ctx.repo} failed: {_first(done.err)}")
    if done.out.strip():
        raise ReleaseRefused(f"{ctx.repo} has uncommitted changes under {CONFIG_DIR}/: a "
                             "release runs only committed config; commit or revert them")


# --- prepare ------------------------------------------------------------------------------


def _prepare(ctx: Context, sha: str) -> Path:
    folder = ctx.releases / sha
    if folder.exists():
        ctx.say(f"reuse {folder}")
        head = ctx.git("rev-parse", "HEAD", folder=folder)
        if head.code != 0 or head.out.strip() != sha:
            raise ReleaseRefused(f"{folder} exists but is not a worktree at {sha}")
    else:
        ctx.say(f"add worktree {folder} at {sha}")
        ctx.releases.mkdir(parents=True, exist_ok=True)
        done = ctx.git("worktree", "add", "--detach", str(folder), sha)
        if done.code != 0:
            raise ReleaseRefused(f"git worktree add failed: {_first(done.err)}")
    dirty = ctx.git("status", "--porcelain", "--untracked-files=no", folder=folder)
    if dirty.code != 0 or dirty.out.strip():
        raise ReleaseRefused(f"{folder} has changes to tracked files")
    link = folder / ctx.data_dir
    if link.is_symlink():
        if link.resolve() != ctx.data:
            raise ReleaseRefused(f"{link} points at {link.resolve()}, not {ctx.data}")
    elif link.exists():
        raise ReleaseRefused(f"{link} is a real folder: a release must use {ctx.data}")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(ctx.data, target_is_directory=True)
    ctx.say(f"data {link} -> {ctx.data}")
    ctx.say(f"uv sync in {folder}")
    done = ctx.step(UV_SYNC, cwd=folder.resolve())
    if done.code != 0:
        raise ReleaseRefused(f"uv sync failed: {_first(done.err)}")
    return folder


def _verify(ctx: Context, sha: str) -> Path:
    """An already prepared release, checked without any network: a worktree at `sha`, no
    changes to tracked files, its data link naming THE data folder, its environment present."""
    folder = ctx.releases / sha
    if not _SHA.match(sha) or not folder.is_dir():
        raise ReleaseRefused(f"{folder} is not a prepared release")
    head = ctx.git("rev-parse", "HEAD", folder=folder)
    if head.code != 0 or head.out.strip() != sha:
        raise ReleaseRefused(f"{folder} is not a worktree at {sha}")
    dirty = ctx.git("status", "--porcelain", "--untracked-files=no", folder=folder)
    if dirty.code != 0 or dirty.out.strip():
        raise ReleaseRefused(f"{folder} has changes to tracked files")
    link = folder / ctx.data_dir
    if not link.is_symlink() or link.resolve() != ctx.data:
        raise ReleaseRefused(f"{link} is not a link to {ctx.data}")
    if not (folder / VENV_PYTHON).exists():
        raise ReleaseRefused(f"{folder / VENV_PYTHON} is missing")
    ctx.say(f"verified {folder}: at {sha}, clean, data -> {ctx.data}")
    return folder


def _config_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_config(ctx: Context, folder: Path, sha: str, running: str | None) -> None:
    """Both configs and what changed between the commits (never silent)."""
    old_root = ctx.live.resolve() if running else ctx.repo
    old, new = old_root / ctx.config_rel, folder / ctx.config_rel
    ctx.say(f"config now     {old.resolve()} digest {_config_digest(old)}")
    ctx.say(f"config release {new.resolve()} digest {_config_digest(new)}")
    base = running or ctx.git("rev-parse", "HEAD").out.strip()
    stat = ctx.git("diff", "--stat", base, sha, "--", CONFIG_DIR, LAUNCHD_DIR)
    if stat.out.strip():
        ctx.say(f"changed between {base} and {sha}:\n{stat.out.rstrip()}")
        ctx.say("if [runtime] times or ops/launchd changed, render and reinstall the plists "
                "after this release (docs/operations.md, Install the jobs)")
    else:
        ctx.say(f"config/ and ops/launchd/ unchanged since {base}")


def _preflight(ctx: Context, folder: Path, sha: str) -> None:
    """Run the release's own preflight as launchd will; refuse unless it passes (or says no
    session today) with this commit, this config and the shared data folder."""
    ctx.say(f"preflight from {folder}")
    done = ctx.step([SH, "-c", _JOB_SCRIPT, str(ctx.env_file), str(folder),
                     VENV_PYTHON.as_posix(), ctx.config_rel.as_posix(), PREFLIGHT])
    for line in done.out.splitlines():
        ctx.say(f"  {line}")
    body = _record(done.out, PREFLIGHT)
    if body is None:
        # Only the exit code: the shell's error text could quote the credentials file.
        raise ReleaseRefused(f"preflight printed no result (exit {done.code})")
    if done.code not in _PREFLIGHT_PASSES or body.get("exit_code") != done.code:
        raise ReleaseRefused(f"preflight failed (exit {done.code}): {body.get('cause')}")
    version = next((c.get("detail") for c in body.get("checks", [])
                    if c.get("name") == "code-version"), None)
    if version != sha:
        raise ReleaseRefused(f"preflight reports code version {version!r}, not {sha}")
    if body.get("config_path") != str((folder / ctx.config_rel).resolve()):
        raise ReleaseRefused(f"preflight read {body.get('config_path')}, not the release config")
    if body.get("data_dir") != str(ctx.data):
        raise ReleaseRefused(f"preflight checked {body.get('data_dir')}, not {ctx.data}")
    ctx.say(f"preflight passed (exit {done.code}), code version {sha}")


# --- switch, restart, prune ---------------------------------------------------------------


def _point(link: Path, name: str) -> None:
    """Make `link` name `<releases>/<name>` atomically (a new link renamed over the old)."""
    fresh = link.with_name(f".{link.name}.new")
    if fresh.is_symlink():
        fresh.unlink()
    fresh.symlink_to(name, target_is_directory=True)
    os.replace(fresh, link)


def job_pid(ctx: Context, label: str) -> int | None:
    """The job's running pid; None when it is not running or not loaded."""
    done = ctx.step([LAUNCHCTL, "list", label])
    if done.code != 0:
        return None
    match = _PID.search(done.out)
    return int(match.group(1)) if match else None


def _stop(ctx: Context, label: str, pid: int) -> str | None:
    """SIGTERM to the job, then wait until that process has ended; the problem, or None."""
    ctx.say(f"{label}: stop pid {pid} (SIGTERM)")
    done = ctx.step([LAUNCHCTL, "kill", "TERM", f"gui/{ctx.uid}/{label}"])
    if done.code != 0:
        return f"{label}: kill failed (exit {done.code})"
    deadline = ctx.clock() + ctx.runtime.release_stop_wait_seconds
    while job_pid(ctx, label) == pid:
        if ctx.clock() >= deadline:
            return (f"{label}: pid {pid} still running after "
                    f"{ctx.runtime.release_stop_wait_seconds} s")
        ctx.sleep(float(ctx.runtime.release_poll_seconds))
    return None


def _restart(ctx: Context) -> list[str]:
    """Stop the running jobs (STOP_ORDER: the watchdog first, so it never sees the bot missing
    while it stops), each waited for, then start them again (START_ORDER, kickstart without -k).
    Returns the problems; a job not running is left to its schedule, which then starts the new
    release, and a job that did not stop is not started."""
    problems = []
    stopped = []
    for label in STOP_ORDER:
        pid = job_pid(ctx, label)
        if pid is None:
            ctx.say(f"{label}: not running; its next start runs the new release")
            continue
        problem = _stop(ctx, label, pid)
        if problem is None:
            stopped.append(label)
        else:
            problems.append(problem)
    for label in START_ORDER:
        if label not in stopped:
            continue
        done = ctx.step([LAUNCHCTL, "kickstart", f"gui/{ctx.uid}/{label}"])
        if done.code != 0:
            problems.append(f"{label}: kickstart failed (exit {done.code})")
            continue
        ctx.say(f"{label}: started, pid {job_pid(ctx, label)}")
    return problems


def _prune(ctx: Context, keep: set[str]) -> None:
    ps = ctx.step(list(PS))
    if ps.code != 0:
        ctx.say(f"keep old releases: ps failed ({_first(ps.err)})")
        return
    for folder in sorted(ctx.releases.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or folder.name in keep:
            continue
        if not _SHA.match(folder.name):
            continue
        real = str(folder.resolve())
        if any(real + os.sep in line for line in ps.out.splitlines()):
            ctx.say(f"keep {folder}: a process still runs from it (removed next release)")
            continue
        link = folder / ctx.data_dir
        if link.exists() and not link.is_symlink():
            ctx.say(f"keep {folder}: {link} is a real folder, not the data link")
            continue
        if link.is_symlink():
            link.unlink()  # the link only, never what it names
        done = ctx.git("worktree", "remove", str(folder))
        ctx.say(f"removed {folder}" if done.code == 0 else
                f"keep {folder}: git worktree remove failed: {_first(done.err)}")


PRE_RELEASE_PREFIX = "pre-release-"
BACKUP_SUFFIX = ".sqlite3"
TEMP_PREFIX = ".tmp-"
SHORT_SHA = 12  # hex digits of the commit in a backup's name (a name part, not a setting)


def _backup(ctx: Context, sha: str, *, prune: bool) -> Path:
    """A verified copy of the database, taken right before the switch, while the running bot
    may still write: SQLite's online backup from a read-only connection, so no code's schema
    knowledge is involved (2026-11-16: a backup through the developer checkout's Store refused a
    schema newer than its code, and the release went on without one). Written under a temporary
    name, `integrity_check` must say ok, then renamed. A release (`prune`) then removes older
    pre-release backups (keep only the latest); a rollback removes none, so the backup from
    before the release it rolls back is never lost. Any failure, of the backup or of removing
    an older one, refuses before a link moves."""
    stamp = ctx.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = ctx.backups / f"{PRE_RELEASE_PREFIX}{stamp}-{sha[:SHORT_SHA]}{BACKUP_SUFFIX}"
    temporary = ctx.backups / f"{TEMP_PREFIX}{target.name}"
    try:
        ctx.backups.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        source = sqlite3.connect(f"file:{ctx.data / DATABASE_FILE}?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(temporary)
            try:
                source.backup(copy)
                result = copy.execute("PRAGMA integrity_check").fetchone()[0]
                version = copy.execute("PRAGMA user_version").fetchone()[0]
            finally:
                copy.close()
        finally:
            source.close()
        if result != "ok":
            raise ReleaseRefused(f"the pre-release backup failed integrity_check: {result}")
        os.replace(temporary, target)
    except ReleaseRefused:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error) as exc:
        temporary.unlink(missing_ok=True)
        raise ReleaseRefused(f"the pre-release backup failed ({type(exc).__name__}: {exc})") \
            from exc
    ctx.say(f"backup {target} (integrity ok, schema {version})")
    if not prune:
        return target
    for old in ctx.backups.glob(f"{PRE_RELEASE_PREFIX}*{BACKUP_SUFFIX}"):
        if old == target:
            continue
        try:
            old.unlink()
        except OSError as exc:
            raise ReleaseRefused(f"removing the older backup {old.name} failed "
                                 f"({type(exc).__name__}: {exc}); the new backup {target.name} "
                                 "is kept, nothing was switched") from exc
        ctx.say(f"removed older backup {old.name}")
    return target


def _switch(ctx: Context, folder: Path, sha: str, running: str | None, *,
            prune_backups: bool) -> None:
    _require_broker_flat(ctx, folder)  # read now: preflight may have taken minutes
    _backup(ctx, sha, prune=prune_backups)  # after the last flat proof, before a link moves
    try:
        if running is not None:
            _point(ctx.previous, running)
            ctx.say(f"previous -> {running}")
        _point(ctx.live, sha)
    except OSError as exc:
        raise ReleaseFailed(f"switching the links failed ({type(exc).__name__}: {exc}); live "
                            f"-> {_link_state(ctx.live)}, previous -> "
                            f"{_link_state(ctx.previous)}; no job was restarted") from exc
    ctx.say(f"live -> {sha}")
    problems = _restart(ctx)
    keep = {sha} | ({running} if running else set())
    _prune(ctx, keep)
    if problems:
        raise ReleaseFailed("; ".join(problems) + f". live is {sha}; a job still running "
                            "runs the release it started from")
    ctx.say(f"released {sha}")


def _link_state(link: Path) -> str:
    try:
        return os.readlink(link) if link.is_symlink() else "none"
    except OSError:
        return "unreadable"


def _locked(ctx: Context, work: Callable[[], None]) -> None:
    ctx.releases.mkdir(parents=True, exist_ok=True)
    lock = SingleInstanceLock(ctx.releases / LOCK)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        raise ReleaseRefused(f"another release command holds {exc}") from exc
    try:
        work()
    finally:
        lock.release()


def release(ctx: Context, ref: str) -> None:
    """Release `ref` (see the module docstring for the order)."""
    def work() -> None:
        _require_live_data(ctx)
        _require_early_flat(ctx)
        _require_committed_config(ctx)
        sha = _commit(ctx, ref)
        running = _target(ctx.live)
        _target(ctx.previous)
        if running == sha:
            ctx.say(f"{sha} already live; nothing to do")
            return
        folder = _prepare(ctx, sha)
        _report_config(ctx, folder, sha, running)
        _preflight(ctx, folder, sha)
        _switch(ctx, folder, sha, running, prune_backups=True)
    _locked(ctx, work)


def rollback(ctx: Context) -> None:
    """Switch back to `previous`: live and previous swap, so a rollback can be rolled back."""
    def work() -> None:
        _require_live_data(ctx)
        _require_early_flat(ctx)
        running = _target(ctx.live)
        back = _target(ctx.previous)
        if running is None or back is None:
            raise ReleaseRefused("no live and previous release to roll back between")
        sha = back  # fetched, checked on origin/main and synced when it was released
        folder = _verify(ctx, sha)
        _report_config(ctx, folder, sha, running)
        _preflight(ctx, folder, sha)
        _switch(ctx, folder, sha, running, prune_backups=False)
    _locked(ctx, work)
