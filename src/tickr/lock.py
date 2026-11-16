"""Single-instance lock: an OS file lock held for the whole bot process lifetime.

Exclusivity is the held lock, never a process-name or pid match: a stale pid file cannot make
a dead bot look alive to `acquire()`, and the OS releases the lock when the process dies.
`recorded_holder` reads the pid for display only and never decides anything.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK_FILE = "tickr.lock"  # the bot's lock file under [run] data_dir
# Lock file name of the code before the rename to Tickr; a bot holding it blocks every start.
LEGACY_LOCK_FILE = "aperture.lock"  # rename-keep


class AlreadyRunning(RuntimeError):
    """Another process holds the lock."""


class SingleInstanceLock:
    """`legacy`: a lock file under an older name (before the rename to Tickr) that a bot of the
    old code would still hold; `acquire()` refuses while any process holds it, and never removes it.
    """

    def __init__(self, path: Path | str, legacy: Path | str | None = None) -> None:
        self.path = Path(path)
        self.legacy = None if legacy is None else Path(legacy)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunning(str(self.path)) from exc
        if self.legacy is not None and _held(self.legacy):
            os.close(fd)
            raise AlreadyRunning(str(self.legacy))
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.ftruncate(self._fd, 0)  # a cleanly stopped bot leaves no pid behind
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _held(path: Path) -> bool:
    """True when another process holds the lock on `path` (a missing file is not held)."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def recorded_holder(path: Path | str) -> int | None:
    """Pid written by the current or last holder, if that process still exists.

    Informational only (for status displays): it never takes the lock, so it cannot race a
    starting bot. A crashed bot's pid may have been reused; exclusivity is still decided by
    the lock itself in `acquire()`.
    """
    try:
        text = Path(path).read_text().strip()
    except (FileNotFoundError, UnicodeDecodeError):
        return None
    if not text.isdigit():
        return None
    pid = int(text)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # exists, owned by another user
    return pid
