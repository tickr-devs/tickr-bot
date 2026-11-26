"""`tickr diagnostics`: the bundle holds what the allow-list names and no secret value."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tickr import cli, config
from tickr.store import DATABASE_FILE, Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
DUMMY = "dummy-secret-7f3a91c2"


def _alert(at: datetime) -> str:
    return json.dumps({"at": at.isoformat(), "title": "t", "text": "x"}) + "\n"


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "bot.toml", tmp_path / "config" / "bot.toml")
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    cfg = config.load(tmp_path / "config" / "bot.toml")
    env = cfg.environment
    for name in (env.app_key_variable, env.app_secret_variable, env.account_variable,
                 cfg.alerts.imessage_recipient_variable):
        monkeypatch.setenv(name, DUMMY)
    # Files that must never be read: env files next to the config, a token, the account file.
    (tmp_path / "config" / ".env").write_text(f"WEBULL_APP_KEY={DUMMY}\n")
    (tmp_path / "config" / "local.toml").write_text(f'token = "{DUMMY}"\n')
    (tmp_path / "config" / "env").write_text(f"WEBULL_APP_SECRET={DUMMY}\n")
    data = tmp_path / cfg.run.data_dir
    (data / env.token_dir).mkdir(parents=True)
    (data / env.token_dir / "token.txt").write_text(DUMMY)
    (data / "account.json").write_text(json.dumps({"account": DUMMY}))
    logs = data / cfg.run.diagnostics_logs_dir
    logs.mkdir()
    (logs / "bot.log").write_text("bot started\n")
    (logs / "bot.log.1").write_text("older\n")
    (logs / "notes.txt").write_text(f"{DUMMY}\n")
    (logs / "api.log").write_text("too old\n")
    for name, age in (("bot.log", 1), ("bot.log.1", 2), ("notes.txt", 1), ("api.log", 30)):
        stamp = (NOW - timedelta(days=age)).timestamp()
        os.utime(logs / name, (stamp, stamp))
    (data / cfg.alerts.feed_file).write_text(
        _alert(NOW - timedelta(days=30)) + _alert(NOW - timedelta(hours=1)) + "garbage\n")
    Store.open(data / DATABASE_FILE, busy_timeout_seconds=5).close()
    return tmp_path


def test_bundle_holds_the_allow_list_and_no_secret(folder: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    out = folder / "out" / "bundle.zip"
    assert cli.main(["--config", str(folder / "config" / "bot.toml"), "diagnostics", "--out",
                     str(out)]) == 0
    with zipfile.ZipFile(out) as bundle:
        blobs = {n: bundle.read(n) for n in bundle.namelist()}
    manifest = json.loads(blobs["manifest.json"])
    assert set(blobs) == {"version.txt", "alerts.jsonl", "database.json", "status.txt",
                          "manifest.json", "config/bot.toml", "logs/bot.log", "logs/bot.log.1"}
    for name, blob in blobs.items():
        assert DUMMY.encode() not in blob, name
    assert {e["path"]: e["bytes"] for e in manifest["files"]} == {
        n: len(b) for n, b in blobs.items() if n != "manifest.json"}
    assert manifest["alerts_lines_unreadable"] == 1
    assert len(blobs["alerts.jsonl"].decode().splitlines()) == 1
    counts = json.loads(blobs["database.json"])
    assert counts["schema_version"] == counts["expected_schema_version"]
    assert counts["row_counts"]["runs"] == 0
    assert DUMMY not in capsys.readouterr().out


def test_refuses_existing_output_and_bad_days(folder: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    out = folder / "bundle.zip"
    out.write_bytes(b"x")
    args = ["--config", str(folder / "config" / "bot.toml"), "diagnostics", "--out", str(out)]
    assert cli.main(args) == 3
    assert out.read_bytes() == b"x"
    out.unlink()
    assert cli.main([*args, "--days", "0"]) == 3
    assert not out.exists()
