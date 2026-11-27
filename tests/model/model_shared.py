"""An expensive fixture built once per test session, also across pytest-xdist workers (the
pattern of the xdist docs, "Making session-scoped fixtures execute only once", with fcntl in
place of filelock): without it every worker that runs one test of a module builds that
module's synthetic day again."""

from __future__ import annotations

import fcntl
import os
import pickle
from collections.abc import Callable
from pathlib import Path
from typing import Any

WORKER_VARIABLE = "PYTEST_XDIST_WORKER"  # set by pytest-xdist in each worker process


def once(tmp_path_factory: Any, name: str, build: Callable[[Path], Any]) -> Any:
    """`build(folder)` writes its files under `folder` and returns a picklable value (which may
    name those files). Under xdist the first worker to ask builds it in the session's shared
    folder and the others wait for it and load the value; the files are only read afterwards.
    Without xdist it is built in a fresh folder of this session."""
    if os.environ.get(WORKER_VARIABLE) is None:
        return build(tmp_path_factory.mktemp(name))
    root = tmp_path_factory.getbasetemp().parent / f"shared-{name}"
    with open(f"{root}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            done = root / "value.pickle"
            if done.exists():
                return pickle.loads(done.read_bytes())
            root.mkdir(exist_ok=True)  # a failed build of an earlier worker is built again
            value = build(root)
            part = root / "value.pickle.part"
            part.write_bytes(pickle.dumps(value))
            part.replace(done)
            return value
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
