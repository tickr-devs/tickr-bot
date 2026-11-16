"""The code version a bot process runs: the git commit, recorded on its run and every trade.

Determined once per process, before the run starts (`tickr run`, the manual commands, and
preflight as a check), for the checkout the running code is loaded from (`CODE_ROOT`, the folder
above `src/`), never the config's folder (`tickr demo` runs the bot on a copied config folder):

- in a git checkout (that folder holds `.git`, a folder or, in a worktree, a file): the full
  commit SHA from `git rev-parse HEAD`, with `-dirty` when a TRACKED file under src/ or
  strategies/ differs from HEAD (`git status --porcelain --untracked-files=no -- src
  strategies`); untracked files, docs, apps and config/ never count (the config is tagged by
  the run's config_digest);
- with no `.git` (the Docker image, docs/deploy-server.md): the `TICKR_CODE_VERSION`
  environment variable the image bakes in from its build argument;
- neither: `CodeVersionUnknown`, and the bot refuses to start (every trade is tagged).

Git is run with a fixed argument list (no shell, no user input) and a timeout from
`[run] code_version_timeout_seconds`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

# The environment variable the Docker image sets from its build argument (Dockerfile).
ENV_VARIABLE = "TICKR_CODE_VERSION"
DIRTY_SUFFIX = "-dirty"
# A commit id (SHA-1 or SHA-256, abbreviated to at least 7 hex digits), optionally dirty.
_VERSION = re.compile(r"^[0-9a-f]{7,64}(-dirty)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
# The value `runs.code_version` holds for runs recorded before the version was determined.
UNRECORDED = "-"
# The checkout the running code is loaded from: <root>/src/tickr/version.py (an editable install,
# as on the Mac and in the Docker image). Replaced in tests.
CODE_ROOT = Path(__file__).resolve().parents[2]


class CodeVersionUnknown(Exception):
    """The running code version cannot be determined; the message is the single cause."""


CODE_PATHS = ("src", "strategies")  # tracked folders whose changes mark a version dirty


def _git(root: Path, timeout: float, *args: str) -> str:
    argv = ["git", "--no-optional-locks", "-C", str(root), *args]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except FileNotFoundError as exc:
        raise CodeVersionUnknown(f"{root} is a git checkout but git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeVersionUnknown(f"git {args[0]} in {root} took longer than {timeout:g} s "
                                 "([run] code_version_timeout_seconds)") from exc
    except OSError as exc:  # not runnable (permissions, broken install): a cause, no crash
        raise CodeVersionUnknown(f"git {args[0]} in {root} could not run: {exc}") from exc
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or ["no message"])[0]
        raise CodeVersionUnknown(f"git {args[0]} in {root} failed "
                                 f"(exit {done.returncode}): {first}")
    return done.stdout


def code_version(root: Path, environ: Mapping[str, str], timeout_seconds: Decimal) -> str:
    """The code version of the checkout at `root` (see the module docstring). Raises
    CodeVersionUnknown with the cause when it cannot be determined."""
    if (root / ".git").exists():
        timeout = float(timeout_seconds)
        commit = _git(root, timeout, "rev-parse", "HEAD").strip()
        if not _COMMIT.match(commit):
            raise CodeVersionUnknown(f"git rev-parse HEAD in {root} gave {commit!r}, "
                                     "not a commit id")
        # Only the code that runs (src/, strategies/) makes it dirty: the config is tagged by
        # the run's own config_digest, and docs or app sources do not run in the bot.
        changed = _git(root, timeout, "status", "--porcelain", "--untracked-files=no", "--",
                       *CODE_PATHS).strip()
        return commit + (DIRTY_SUFFIX if changed else "")
    baked = environ.get(ENV_VARIABLE, "")
    if not baked:
        raise CodeVersionUnknown(
            f"{root} is not a git checkout and {ENV_VARIABLE} is not set (build the image with "
            f"--build-arg {ENV_VARIABLE}=$(git rev-parse HEAD), docs/deploy-server.md)")
    if not _VERSION.match(baked):
        raise CodeVersionUnknown(f"{ENV_VARIABLE} is {baked!r}, not a commit id")
    return baked


def running_code_version(environ: Mapping[str, str], timeout_seconds: Decimal) -> str:
    """The version of the code this process runs (`CODE_ROOT`); CodeVersionUnknown when unknown."""
    return code_version(CODE_ROOT, environ, timeout_seconds)


def recorded(value: str) -> str | None:
    """A run's stored `code_version` for display: None for a run recorded before the version
    was determined (`-`), never guessed."""
    return None if value == UNRECORDED else value
