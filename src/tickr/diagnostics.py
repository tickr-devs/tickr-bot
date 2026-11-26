"""`tickr diagnostics --out PATH.zip`: a bundle the owner can fetch from a server and send on,
holding no secret by construction (CLAUDE.md rules 4 and 10).

An ALLOW-LIST, not a deny-list: the bundle holds exactly these parts and nothing else is ever
opened.

- `version.txt`: the code version (`tickr.version`) or its one cause when unknown;
- `config/<name>.toml`: the config file in use and the repo's known config files
  (`CONFIG_FILES`) next to it, when present; no other file of that folder (they hold variable
  NAMES only, rule 4);
- `logs/<name>`: `*.log` and rotated `*.log.<digits>` files in `<data_dir>/<[run]
  diagnostics_logs_dir>` changed within the window (one folder level, links are skipped);
- `alerts.jsonl`: the lines of the alerts feed (`[alerts] feed_file`) whose `at` is within the
  window; lines that cannot be read are counted in the manifest, not copied;
- `database.json`: schema version, the version this code expects, and the row COUNT of every
  table (never a row), read through a read-only connection;
- `status.txt`: the text `tickr status` prints (passed in by the CLI);
- `manifest.json`: every file in the zip with its size, the window and the creation time.

Never opened: the process environment, `.env` or any env file, the secrets file, token folders,
authorization files, APNs keys, the account file, capture or decision files, the database rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tickr.config import Config
from tickr.store import DATABASE_FILE, MIGRATIONS, Store

# The repo's config files (config/ on main); with the config file in use they are the only
# config files a bundle takes. Any other file in that folder (a local override, an
# authorization or credentials file) is never read.
CONFIG_FILES = ("backtest.toml", "bot.toml", "demo.toml", "replay.toml", "research.toml",
                "settings_schema.toml")
LOG_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.log(\.\d+)?$")
MANIFEST = "manifest.json"


class DiagnosticsRefused(Exception):
    """The bundle cannot be written; the message is the single cause."""


def _within(path: Path, since: datetime) -> bool:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC) >= since


def _plain_files(folder: Path) -> list[Path]:
    """Regular files directly in `folder` (links are skipped, never followed), by name."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.is_symlink())


def _alerts(path: Path, since: datetime) -> tuple[bytes, int]:
    if not path.is_file() or path.is_symlink():
        return b"", 0
    kept: list[str] = []
    skipped = 0
    for line in path.read_text("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            at = datetime.fromisoformat(str(json.loads(line)["at"]))
            if at.tzinfo is None:
                raise ValueError("no timezone")
        except (ValueError, KeyError, TypeError):
            skipped += 1
            continue
        if at >= since:
            kept.append(line)
    return "".join(f"{line}\n" for line in kept).encode(), skipped


def _database(database: Path) -> dict[str, object]:
    if not database.is_file():
        return {"database": "not created yet"}
    try:
        store = Store.open_readonly(database)
    except sqlite3.Error as exc:
        return {"database": f"unreadable ({exc})"}
    try:
        names = [row[0] for row in store.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        counts: dict[str, int] = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = int(store.query(f"SELECT COUNT(*) FROM {quoted}")[0][0])
        return {"schema_version": store.schema_version,
                "expected_schema_version": len(MIGRATIONS), "row_counts": counts}
    except sqlite3.Error as exc:
        return {"database": f"unreadable ({exc})"}
    finally:
        store.close()


def build(cfg: Config, config_path: Path, out: Path, *, now: datetime, days: int,
          version: str, status_text: str) -> list[dict[str, object]]:
    """Write the zip at `out` and return its manifest entries. Refuses (DiagnosticsRefused)
    when `days` is below 1 or `out` already exists; nothing is overwritten."""
    if days < 1:
        raise DiagnosticsRefused("--days must be at least 1")
    if out.exists():
        raise DiagnosticsRefused(f"{out} already exists; choose a new file")
    since = now - timedelta(days=days)
    data = cfg.root / cfg.run.data_dir
    parts: dict[str, bytes] = {"version.txt": f"{version}\n".encode()}
    config_dir = config_path.resolve().parent
    for name in sorted({config_path.name, *CONFIG_FILES}):
        path = config_dir / name
        if path.is_file() and not path.is_symlink():
            parts[f"config/{name}"] = path.read_bytes()
    for path in _plain_files(data / cfg.run.diagnostics_logs_dir):
        if LOG_NAME.match(path.name) and _within(path, since):
            parts[f"logs/{path.name}"] = path.read_bytes()
    alerts, skipped = _alerts(data / cfg.alerts.feed_file, since)
    parts["alerts.jsonl"] = alerts
    parts["database.json"] = (json.dumps(_database(data / DATABASE_FILE), indent=2,
                                         sort_keys=True) + "\n").encode()
    parts["status.txt"] = status_text.encode()
    entries: list[dict[str, object]] = [
        {"path": name, "bytes": len(body)} for name, body in sorted(parts.items())]
    manifest = {"created_at": now.isoformat(), "environment": cfg.environment_name,
                "config_digest": cfg.digest, "days": days, "since": since.isoformat(),
                "alerts_lines_unreadable": skipped, "files": entries}
    body = (json.dumps(manifest, indent=2) + "\n").encode()
    entries.append({"path": MANIFEST, "bytes": len(body)})
    parts[MANIFEST] = body
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in sorted(parts.items()):
            bundle.writestr(name, content)
    return entries
