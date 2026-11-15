"""The operator's stop marker: a file the bot writes when it obeys a stop-bot command.

While it exists the bot does not start: preflight fails with its own cause (and the launchd bot
job alerts "bot did not start"), and `aperture run` refuses. Nothing removes it but the
operator, with `aperture clear-stop`. So a stop from the app sticks across the next scheduled
launchd start until the owner decides to trade again.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from aperture.api import contract
from aperture.config import Config


def path(config: Config) -> Path:
    return config.root / config.run.data_dir / config.runtime.stop_marker_file


def write(config: Config, command_id: str, at: datetime) -> None:
    """Durable (temp file + fsync + rename) before the command is journaled done."""
    contract.write_atomic(path(config), json.dumps(
        {"command_id": command_id, "stopped_at": contract.format_time(at)}) + "\n")


def describe(config: Config) -> str | None:
    """None when no marker exists; otherwise one line saying what stopped the bot. An unreadable
    marker still counts as present: its existence is what blocks the start."""
    marker = path(config)
    try:
        text = marker.read_text("utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"stop marker {marker} present but unreadable ({type(exc).__name__})"
    try:
        body = json.loads(text)
        what = f"command {body['command_id']} at {body['stopped_at']}"
    except (ValueError, TypeError, KeyError):
        what = "unrecognized content"
    return (f"stopped by an operator stop-bot command ({what}); run `aperture clear-stop` "
            "to allow starts again")


def clear(config: Config) -> bool:
    """Remove the marker; True if there was one."""
    marker = path(config)
    existed = marker.exists()
    marker.unlink(missing_ok=True)
    return existed
