"""POST /commands/start-bot: start the bot job through launchd, never inside the API process.

The API runs `[api] start_bot_command` (launchd's kickstart of the bot job, without -k, so a job
that already runs is never restarted). The job then runs the same preflight and supervisor as a
scheduled start: preflight may still refuse (no session, token, disk ...), and GET /status shows
what happened. Nothing here talks to the broker.

The command runs without any credential variable (`config.credential_variables`): while
`[chart] webull_enabled` the API holds the Webull app key and secret (runtime/launchd.py), and
the kickstart needs none of them. The bot job never takes its environment from the caller:
launchd starts it with the job's own environment, and its shell sources the env file itself
(ops/launchd/local.tickr.bot.plist.template).

Checks, each a typed refusal with one cause and nothing changed:
- no start command configured (Docker, the demo): `unavailable`;
- the bot's fresh status says it is starting, running or stopping: `conflict` (a stale or
  missing status proves nothing, and the kickstart is harmless for a running job);
- a live process is recorded in the bot's single-instance lock file: `conflict`. A fresh
  `stopped` status does not prove the process has ended (it still closes its stream, observers
  and alerts after writing it), and `tickr trade` holds the same lock; a kickstart then would
  be a no-op or end in `AlreadyRunning`, and the owner's stop would be gone for nothing.

Then the operator's stop marker (runtime/stopmarker.py) is removed: pressing Start is the
owner's decision to trade again, the same as `tickr clear-stop`. It is removed before the
command runs, because preflight refuses while it exists; if the command then fails, the marker
stays removed and the next scheduled start runs as usual (the answer says so). Removing it is
written to the API's log (stderr, api.log under launchd) with the time, the only trace of why the
bot started again later.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tickr.api.contract import BotStartV1, BotState, StatusResponseV1
from tickr.config import Config, credential_variables, environ_without
from tickr.lock import LOCK_FILE, recorded_holder

UID_PLACEHOLDER = "{uid}"

# Runs a command with a timeout in seconds in the given environment and returns its exit code;
# replaced in tests.
Runner = Callable[[Sequence[str], float, Mapping[str, str]], int]


def run_command(command: Sequence[str], timeout: float, environ: Mapping[str, str]) -> int:
    """Run the start command in `environ`; its output is discarded, only the exit code
    counts."""
    return subprocess.run(list(command), check=False, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout, env=dict(environ)).returncode


class StartRefused(Exception):
    def __init__(self, error: str, cause: str) -> None:
        super().__init__(cause)
        self.error = error
        self.cause = cause


@dataclass(frozen=True)
class StartSettings:
    command: tuple[str, ...]  # with this user's id filled in; empty = not available here
    timeout_seconds: float
    stop_marker: Path
    lock_file: Path  # the bot's single-instance lock (lock.LOCK_FILE under data_dir)
    scrub: frozenset[str]  # credential variable NAMES the command never gets

    @classmethod
    def from_config(cls, cfg: Config) -> StartSettings:
        uid = str(os.getuid())
        data = cfg.root / cfg.run.data_dir
        return cls(tuple(part.replace(UID_PLACEHOLDER, uid) for part in cfg.api.start_bot_command),
                   float(cfg.api.start_bot_timeout_seconds),
                   data / cfg.runtime.stop_marker_file, data / LOCK_FILE,
                   credential_variables(cfg))


def _note(line: str) -> None:
    """One line in the API's log (its stderr)."""
    print(line, file=sys.stderr, flush=True)


def start_bot(settings: StartSettings, state: StatusResponseV1, now: datetime,
              runner: Runner = run_command) -> BotStartV1:
    """Start the bot job, or raise StartRefused with what blocks it."""
    if not settings.command:
        raise StartRefused("unavailable", "no start command is configured on this machine "
                           "([api] start_bot_command is empty): start the bot where it runs")
    status = state.status
    if status is not None and not state.stale and status.bot_state is not BotState.STOPPED:
        raise StartRefused("conflict", f"the bot is {status.bot_state.value}: nothing to start")
    try:
        pid = recorded_holder(settings.lock_file)
    except OSError as exc:
        raise StartRefused("unavailable", f"the bot's lock file could not be read "
                           f"({type(exc).__name__}): nothing was started") from exc
    if pid is not None:
        raise StartRefused("conflict", f"a bot process (pid {pid}) still holds the lock: wait "
                           "for it to end, then start again")
    try:
        cleared = settings.stop_marker.exists()
        settings.stop_marker.unlink(missing_ok=True)
    except OSError as exc:
        raise StartRefused("unavailable", f"the operator stop could not be cleared "
                           f"({type(exc).__name__}): nothing was started") from exc
    if cleared:
        _note(f"{now.isoformat()} start-bot: operator stop cleared by Start")
    after = " (the operator stop was cleared, so the next scheduled start runs)" if cleared else ""
    try:
        code = runner(settings.command, settings.timeout_seconds,
                      environ_without(settings.scrub))
    except subprocess.TimeoutExpired:
        raise StartRefused("unavailable", f"the start command did not return within "
                           f"{settings.timeout_seconds:g} s{after}") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartRefused("unavailable", f"the start command failed ({type(exc).__name__})"
                           f"{after}") from exc
    if code != 0:
        raise StartRefused("unavailable", f"the start command exited {code}{after}")
    return BotStartV1(requested_at=now, stop_marker_cleared=cleared)
