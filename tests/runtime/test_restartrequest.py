"""The server's bot restart request: `python -m tickr.runtime request-restart`."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tickr import config as config_module
from tickr.runtime import restartrequest
from tickr.runtime.__main__ import main
from tickr.runtime.restartrequest import RequestCause, RestartRequestRefused

AT = datetime(2026, 11, 26, 15, 35, 0, tzinfo=UTC)


def test_the_request_holds_its_utc_time_and_cause(tmp_path):
    path = tmp_path / "restart-request"
    restartrequest.write_request(path, AT, "watchdog")
    assert json.loads(path.read_text("utf-8")) == {
        "requested_at": "2026-11-26T15:35:00+00:00", "cause": "watchdog"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["restart-request"]  # no temp file left


def test_the_write_is_a_rename_of_a_complete_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "restart-request"
    seen = []
    real = os.replace

    def spy(src, dst):
        # The temp file is in the same folder (so the rename is atomic), never named like the
        # request the path unit watches, and already complete when it is renamed.
        seen.append((Path(src).parent, Path(src).name, Path(src).read_text("utf-8")))
        assert not path.exists()
        real(src, dst)

    monkeypatch.setattr(restartrequest.os, "replace", spy)
    restartrequest.write_request(path, AT, "watchdog")
    assert len(seen) == 1
    folder, name, text = seen[0]
    assert folder == tmp_path and name != path.name and name.startswith(".")
    assert text == restartrequest.request_text(AT, "watchdog")


def test_a_later_request_replaces_a_waiting_one(tmp_path):
    path = tmp_path / "restart-request"
    restartrequest.write_request(path, AT, "first")
    restartrequest.write_request(path, AT + timedelta(minutes=1), "second")
    assert json.loads(path.read_text("utf-8"))["cause"] == "second"


def test_a_missing_control_folder_is_refused_and_never_created(tmp_path):
    path = tmp_path / "control" / "restart-request"
    with pytest.raises(RestartRequestRefused) as refused:
        restartrequest.write_request(path, AT, "watchdog")
    assert refused.value.cause is RequestCause.CONTROL_FOLDER_MISSING
    assert not (tmp_path / "control").exists()


@pytest.mark.parametrize("cause", ["", "two words", "line\nbreak", 'quo"te'])
def test_an_unsafe_cause_is_refused(tmp_path, cause):
    with pytest.raises(RestartRequestRefused) as refused:
        restartrequest.write_request(tmp_path / "restart-request", AT, cause)
    assert refused.value.cause is RequestCause.CAUSE_INVALID
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    def broken(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(restartrequest.os, "replace", broken)
    with pytest.raises(RestartRequestRefused) as refused:
        restartrequest.write_request(tmp_path / "restart-request", AT, "watchdog")
    assert refused.value.cause is RequestCause.WRITE_FAILED
    assert list(tmp_path.iterdir()) == []


def test_the_time_must_be_utc():
    with pytest.raises(ValueError):
        restartrequest.request_text(AT.astimezone(timezone(timedelta(hours=-5))), "x")
    with pytest.raises(ValueError):
        restartrequest.request_text(AT.replace(tzinfo=None), "x")


def _set_request_file(config_path: Path, value: str) -> None:
    text = config_path.read_text("utf-8")
    old = 'server_restart_request_file = "control/restart-request"'
    assert old in text
    config_path.write_text(text.replace(old, f"server_restart_request_file = {value!r}".replace(
        "'", '"')), "utf-8")


@pytest.mark.parametrize("value", ["/abs/restart-request", "../restart-request",
                                   "restart-request"])
def test_the_configured_file_must_sit_in_a_folder_inside_data_dir(config_path, value):
    _set_request_file(config_path, value)
    cfg = config_module.load(config_path)
    with pytest.raises(config_module.ConfigError):
        restartrequest.request_path(cfg)


def test_request_restart_command_writes_under_the_data_folder(config_path, capsys):
    cfg = config_module.load(config_path)
    path = cfg.root / cfg.run.data_dir / "control" / "restart-request"
    path.parent.mkdir(parents=True)
    assert main(["--config", str(config_path), "request-restart", "--cause", "watchdog"]) == 0
    row = json.loads(capsys.readouterr().out.strip())
    assert row["record"] == "request-restart" and row["requested"] is True
    assert json.loads(path.read_text("utf-8"))["cause"] == "watchdog"


def test_request_restart_command_refuses_without_the_control_folder(config_path, capsys):
    assert main(["--config", str(config_path), "request-restart", "--cause", "watchdog"]) == 1
    row = json.loads(capsys.readouterr().out.strip())
    assert row["requested"] is False and row["cause"] == "control-folder-missing"


def test_the_shipped_watchdog_restart_command_is_this_request(config_path, capsys):
    """config/bot.toml holds the server's command (the cutover): `python -m tickr.runtime`
    with the bot.toml compose mounts in every container, then request-restart."""
    cfg = config_module.load(config_path)
    command = list(cfg.runtime.watchdog_restart_command)
    assert command[:5] == ["python", "-m", "tickr.runtime", "--config", "/app/config/bot.toml"]
    compose = (Path(__file__).resolve().parents[2] / "ops/docker/compose.yaml").read_text("utf-8")
    assert "target: /app/config/bot.toml" in compose
    path = cfg.root / cfg.run.data_dir / "control" / "restart-request"
    path.parent.mkdir(parents=True)
    assert main(["--config", str(config_path), *command[5:]]) == 0
    assert json.loads(capsys.readouterr().out.strip())["requested"] is True
    assert json.loads(path.read_text("utf-8"))["cause"] == "watchdog"
