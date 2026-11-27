"""The server's bot restart and start requests (docs/deploy-server.md, "Bot restart on the
server" and "Bot start on the server").

On the server the watchdog runs in its own container and cannot run a host `systemctl`. Its
restart command (`[runtime] watchdog_restart_command`) is `python -m tickr.runtime
request-restart --cause <cause>`, which only writes the request file `[runtime]
server_restart_request_file` under the data folder (mounted in every container). The host unit
`tickr-bot-restart.path` sees the file and starts `tickr-bot-restart.service`, which takes the
request, restarts the bot container once and logs the request to the journal.

The API's Start (`[api] start_bot_command`) is `python -m tickr.runtime request-start --cause
start-bot`, which writes `[runtime] server_start_request_file` the same way. The host unit
`tickr-bot-start.path` starts `tickr-bot-start.service`, which takes the request and runs
`docker compose up -d --no-recreate` for the bot: a running bot container is never touched, a
stopped one is started, a missing one is created. A start request never restarts a running bot.

The write is atomic (a temp file in the same folder, fsynced, then renamed), so the path unit
never sees a partial file. The control folder is never created here: deploy-server.sh creates it
with its owner and permissions, and a missing folder is a typed refusal, never a guess.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath

from tickr.config import Config, ConfigError

# The suffix the restart and start units rename a request to before they act on the bot, so a
# request written meanwhile is a new event.
TAKEN_SUFFIX = ".taken"
# A cause is one word the journal can hold as is: no whitespace, quotes or line breaks.
_CAUSE = re.compile(r"^[A-Za-z0-9._:-]+$")


class RequestCause(StrEnum):
    CAUSE_INVALID = "cause-invalid"
    CONTROL_FOLDER_MISSING = "control-folder-missing"
    WRITE_FAILED = "write-failed"


class RestartRequestRefused(Exception):
    """No request was written; `cause` is the one recorded reason."""

    def __init__(self, cause: RequestCause, detail: str) -> None:
        super().__init__(f"{cause.value}: {detail}")
        self.cause = cause
        self.detail = detail


def _relative(key: str, raw: str) -> PurePosixPath:
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) < 2:
        raise ConfigError(f"[runtime] {key} must be a relative path "
                          f"inside data_dir with a folder part, got {raw!r}")
    return rel


def relative_request_file(config: Config) -> PurePosixPath:
    """`[runtime] server_restart_request_file`, checked: relative, inside data_dir, in a
    folder of its own (the folder deploy-server.sh creates)."""
    return _relative("server_restart_request_file", config.runtime.server_restart_request_file)


def relative_start_request_file(config: Config) -> PurePosixPath:
    """`[runtime] server_start_request_file`, checked like the restart request, and in the
    restart request's folder (deploy-server.sh creates that one folder only) under another
    name (each host path unit watches one file)."""
    rel = _relative("server_start_request_file", config.runtime.server_start_request_file)
    restart = relative_request_file(config)
    if rel.parent != restart.parent or rel == restart:
        raise ConfigError("[runtime] server_start_request_file must be another file in the "
                          f"folder of server_restart_request_file ({restart.parent}), "
                          f"got {str(rel)!r}")
    return rel


def request_path(config: Config) -> Path:
    return config.root / config.run.data_dir / relative_request_file(config)


def start_request_path(config: Config) -> Path:
    return config.root / config.run.data_dir / relative_start_request_file(config)


def request_text(at: datetime, cause: str) -> str:
    """The request: its time (UTC, ISO 8601) and its cause, one JSON line."""
    if at.utcoffset() != timedelta(0):
        raise ValueError("the request time must be UTC")
    return json.dumps({"requested_at": at.isoformat(), "cause": cause}, sort_keys=True) + "\n"


def write_request(path: Path, at: datetime, cause: str) -> None:
    """Write the request at `path` atomically. Raises RestartRequestRefused (nothing written)
    on an invalid cause, a missing control folder or a failed write."""
    if not _CAUSE.match(cause):
        raise RestartRequestRefused(RequestCause.CAUSE_INVALID,
                                    "a cause is one word of letters, digits and . _ : -")
    folder = path.parent
    if not folder.is_dir():
        raise RestartRequestRefused(RequestCause.CONTROL_FOLDER_MISSING,
                                    f"{folder} does not exist (deploy-server.sh creates it)")
    text = request_text(at, cause)
    temp = folder / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        dir_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise RestartRequestRefused(RequestCause.WRITE_FAILED, type(exc).__name__) from exc
    finally:
        if temp.exists():
            temp.unlink()
