"""render-systemd (golden files) and the server deploy script's flat gate."""

from __future__ import annotations

import json
import re
import stat
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config as config_module
from tickr.runtime import __main__ as runtime_main
from tickr.runtime import systemd
from tickr.runtime.__main__ import main
from tickr.types import Position

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden" / "systemd"
SCRIPT = ROOT / "ops" / "systemd" / "deploy-server.sh"
PATHS = systemd.Paths(repo=Path("/srv/tickr/repo"), config_host=Path("/srv/tickr/etc/bot.toml"),
                      env_file=Path("/run/tickr/env"), user="tickr")
ZONE = "America/New_York"
# The values the golden files were made with: set in the test config, so the golden files do
# not move when the owner edits config/bot.toml.
PINNED = {"preflight_start_local": '"03:30"', "bot_start_local": '"04:00"',
          "watchdog_start_local": '"03:30"', "nightly_fetch_start_local": '"19:00"',
          "launch_weekdays": "[1, 2, 3, 4, 5]", "bot_exit_timeout_seconds": "120",
          "port": "8766", "remote_port": "8767"}


def _pinned(config_path: Path) -> config_module.Config:
    text = config_path.read_text("utf-8")
    for key, value in PINNED.items():
        text, count = re.subn(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
        assert count >= 1, key
    config_path.write_text(text, "utf-8")
    return config_module.load(config_path)


def _render(config_path: Path, out: Path, paths: systemd.Paths = PATHS) -> list[Path]:
    cfg = _pinned(config_path)
    return systemd.render_all(cfg, config_path.parents[1] / "ops" / "systemd", out,
                              systemd.values(cfg, paths, ZONE))


def test_units_match_the_golden_files(config_path, repo, tmp_path):
    written = _render(config_path, tmp_path / "out")
    golden = sorted(p.name for p in GOLDEN.iterdir())
    assert sorted(p.name for p in written) == golden
    for path in written:
        assert path.read_text("utf-8") == (GOLDEN / path.name).read_text("utf-8"), path.name


def test_times_and_ports_come_from_config_only(config_path, repo, tmp_path):
    _pinned(config_path)
    text = config_path.read_text("utf-8")
    text = text.replace('bot_start_local = "04:00"', 'bot_start_local = "04:15"')
    text = text.replace("remote_port = 8767", "remote_port = 9999")
    config_path.write_text(text, "utf-8")
    cfg = config_module.load(config_path)
    out = tmp_path / "out"
    systemd.render_all(cfg, repo / "ops" / "systemd", out, systemd.values(cfg, PATHS, ZONE))
    assert "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 04:15:00 America/New_York" in (
        out / "tickr-bot.timer").read_text("utf-8")
    assert (out / "ports.env").read_text("utf-8") == (
        "TICKR_API_PORT=8766\nTICKR_API_REMOTE_PORT=9999\n")


def test_templates_hold_no_clock_time(repo):
    for template in (repo / "ops" / "systemd").glob("*.template"):
        assert not re.search(r"\b\d{1,2}:\d{2}\b", template.read_text("utf-8")), template.name


def test_every_job_requires_the_secrets_unit_and_holds_no_value(config_path, repo, tmp_path):
    out = tmp_path / "out"
    _render(config_path, out)
    for job in ("preflight", "bot", "watchdog", "nightly-fetch"):
        text = (out / f"tickr-{job}.service").read_text("utf-8")
        assert "Requires=docker.service tickr-secrets.service\n" in text
        assert "After=docker.service tickr-secrets.service\n" in text
        assert "Environment=TICKR_ENV_FILE_HOST=/run/tickr/env\n" in text
    api = (out / "tickr-api.service").read_text("utf-8")
    assert "tickr-secrets.service" not in api and "TICKR_ENV_FILE_HOST=" not in api


@pytest.mark.parametrize("field,value", [
    ("repo", "/srv/with space"), ("repo", "/srv/a$b"), ("config_host", "/srv/a%h"),
    ("env_file", '/srv/"q"'), ("user", "a b"), ("repo", "relative/repo")])
def test_unsafe_or_relative_values_are_refused(config_path, repo, field, value):
    cfg = _pinned(config_path)
    bad = systemd.Paths(**{**vars(PATHS), field: value if field == "user" else Path(value)})
    with pytest.raises(config_module.ConfigError):
        systemd.values(cfg, bad, ZONE)


def test_unknown_zone_is_refused(config_path, repo):
    with pytest.raises(config_module.ConfigError):
        systemd.values(_pinned(config_path), PATHS, "Mars/Base")


def test_a_bad_schedule_is_refused_like_launchd(config_path, repo):
    _pinned(config_path)
    text = config_path.read_text("utf-8").replace('bot_start_local = "04:00"',
                                                  'bot_start_local = "25:00"')
    config_path.write_text(text, "utf-8")
    cfg = config_module.load(config_path)
    with pytest.raises(config_module.ConfigError):
        systemd.values(cfg, PATHS, ZONE)


def test_render_systemd_command(config_path, repo, tmp_path, capsys):
    out = tmp_path / "units"
    assert main(["--config", str(config_path), "render-systemd", "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(p.name for p in GOLDEN.iterdir())
    assert "Environment=TICKR_CONFIG_HOST=/srv/tickr/etc/bot.toml" in (
        out / "tickr-bot.service").read_text("utf-8")


class _Broker:
    def __init__(self, positions=(), orders=()):
        self._positions, self._orders = positions, orders

    def positions(self):
        return list(self._positions)

    def open_orders(self):
        return list(self._orders)


def _require(config_path, monkeypatch, broker, capsys, open_trades=0):
    monkeypatch.setattr(runtime_main, "FLAT_BROKER", lambda cfg, pending: broker)
    monkeypatch.setattr(runtime_main.watchdog, "open_trades", lambda database: open_trades)
    code = main(["--config", str(config_path), "require-flat"])
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return code, row


def test_require_flat_passes_only_when_everything_is_flat(config_path, monkeypatch, capsys):
    code, row = _require(config_path, monkeypatch, _Broker(), capsys)
    assert code == 0 and row["flat"] is True


@pytest.mark.parametrize("broker,trades,cause", [
    (_Broker(positions=[Position("ABCD", Decimal("10"), None)]), 0, "positions ['ABCD']"),
    (_Broker(), 1, "1 open trade"),
])
def test_require_flat_refuses_when_not_flat(config_path, monkeypatch, capsys, broker, trades,
                                            cause):
    code, row = _require(config_path, monkeypatch, broker, capsys, open_trades=trades)
    assert code == 1 and row["flat"] is False and cause in row["cause"]


def test_require_flat_refuses_when_the_read_fails(config_path, monkeypatch, capsys):
    class Broken(_Broker):
        def positions(self):
            raise OSError("down")

    code, row = _require(config_path, monkeypatch, Broken(), capsys)
    assert code == 1 and "broker read failed" in row["cause"]


def _fake_server(tmp_path: Path, flat: bool) -> tuple[Path, Path, Path, Path, Path]:
    """A repo whose `.venv` python renders, a docker that answers `require-flat`, a systemctl
    that only logs, the units folder and the etc folder."""
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    python = repo / ".venv" / "bin" / "python"
    python.write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do [ "$1" = --out ] && out=$2; shift; done\n'
        'mkdir -p "$out"; : > "$out/tickr-bot.service"; : > "$out/tickr-bot.timer"\n'
        ': > "$out/ports.env"; echo TICKR_BOT_EXIT_TIMEOUT=1 > "$out/limits.env"\n', "utf-8")
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\nexit {0 if flat else 1}\n", "utf-8")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/systemctl.log\n', "utf-8")
    for script in (python, docker, systemctl):
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    units, etc = tmp_path / "units", tmp_path / "etc"
    units.mkdir()
    etc.mkdir()
    return repo, docker, systemctl, units, etc


def _deploy(tmp_path: Path, flat: bool) -> subprocess.CompletedProcess:
    repo, docker, systemctl, units, etc = _fake_server(tmp_path, flat)
    return subprocess.run(
        [str(SCRIPT), str(repo), str(etc)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "TICKR_UNITS_DIR": str(units),
             "TICKR_SYSTEMCTL": str(systemctl), "TICKR_DOCKER": str(docker)}, check=False)


def test_deploy_script_refuses_when_not_flat(tmp_path):
    done = _deploy(tmp_path, flat=False)
    assert done.returncode == 1 and "not proven flat" in done.stderr
    assert list((tmp_path / "units").iterdir()) == [] and list((tmp_path / "etc").iterdir()) == []
    assert not (tmp_path / "systemctl.log").exists()


def test_deploy_script_installs_the_units_when_flat(tmp_path):
    done = _deploy(tmp_path, flat=True)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in (tmp_path / "units").iterdir()) == [
        "tickr-bot.service", "tickr-bot.timer"]
    assert (tmp_path / "etc" / "ports.env").exists() and (tmp_path / "etc" / "limits.env").exists()
    log = (tmp_path / "systemctl.log").read_text("utf-8")
    assert "daemon-reload" in log and "tickr-bot.timer" in log
    # The bot unit is never started or restarted by a deploy.
    assert not re.search(r"(start|restart) .*tickr-bot\.service", log)


def test_the_bots_grace_period_is_the_exit_timeout_from_config_only(config_path, repo, tmp_path):
    cfg = _pinned(config_path)
    out = tmp_path / "out"
    systemd.render_all(cfg, repo / "ops" / "systemd", out, systemd.values(cfg, PATHS, ZONE))
    rendered = (out / "limits.env").read_text("utf-8")
    assert rendered == (f"TICKR_BOT_EXIT_TIMEOUT={cfg.runtime.bot_exit_timeout_seconds}\n"
                        f"TICKR_NIGHTLY_OUT={cfg.runtime.nightly_fetch_out}\n"
                        f"TICKR_NIGHTLY_SEED={cfg.runtime.nightly_fetch_seed}\n")
    compose = (ROOT / "ops" / "docker" / "compose.yaml").read_text("utf-8")
    # compose reads the rendered value and holds no number of its own.
    assert re.findall(r"(?m)^\s*stop_grace_period: (.*)$", compose) == [
        "${TICKR_BOT_EXIT_TIMEOUT:?run render-systemd and load its limits.env}s"]
    for job in ("api", "preflight", "bot", "watchdog", "nightly-fetch"):
        assert "EnvironmentFile=/srv/tickr/etc/limits.env\n" in (
            out / f"tickr-{job}.service").read_text("utf-8")


def test_the_nightly_fetch_unit_runs_the_compose_service_at_the_configured_time(
        config_path, repo, tmp_path):
    cfg = _pinned(config_path)
    out = tmp_path / "out"
    systemd.render_all(cfg, repo / "ops" / "systemd", out, systemd.values(cfg, PATHS, ZONE))
    assert "--profile jobs run --rm nightly-fetch\n" in (
        out / "tickr-nightly-fetch.service").read_text("utf-8")
    assert "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 19:00:00 America/New_York" in (
        out / "tickr-nightly-fetch.timer").read_text("utf-8")
    assert re.search(r"(?m)^  nightly-fetch:$", (ROOT / "ops/docker/compose.yaml").read_text())


def test_deploy_script_installs_nothing_when_a_copy_fails(tmp_path):
    repo, docker, systemctl, units, etc = _fake_server(tmp_path, True)
    missing = tmp_path / "no-such-etc"  # the units stage fine, then the etc files cannot be copied
    done = subprocess.run(
        [str(SCRIPT), str(repo), str(missing)], capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "TICKR_UNITS_DIR": str(units),
             "TICKR_SYSTEMCTL": str(systemctl), "TICKR_DOCKER": str(docker)})
    assert done.returncode == 1 and "nothing was installed" in done.stderr
    assert list(units.iterdir()) == []  # no unit, and no staged file left behind
    assert not (tmp_path / "systemctl.log").exists()  # no daemon-reload
