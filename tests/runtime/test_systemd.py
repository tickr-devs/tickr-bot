"""render-systemd (golden files) and the server deploy script's flat gate."""

from __future__ import annotations

import json
import os
import pwd
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
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# A made-up registry: a real one holds the AWS account id, which never enters the repo.
REGISTRY = "registry.example/tickr-bot"
IMAGE = f"{REGISTRY}:{COMMIT}"
PATHS = systemd.Paths(compose=Path("/srv/tickr/etc/compose.yaml"),
                      config_host=Path("/srv/tickr/etc/bot.toml"),
                      env_file=Path("/run/tickr/env"), data=Path("/srv/tickr/data"),
                      user="tickr", image=IMAGE)
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


def _compose_text(config_path: Path) -> str:
    """The text of the compose.yaml in the test repo (the server's copy names PATHS.compose)."""
    return (config_path.parents[1] / "ops" / "docker" / "compose.yaml").read_text("utf-8")


def _values(cfg: config_module.Config, paths: systemd.Paths = PATHS) -> dict[str, str]:
    return systemd.values(cfg, paths, ZONE, _compose_text(cfg.root / "config" / "bot.toml"))


def _render(config_path: Path, out: Path, paths: systemd.Paths = PATHS) -> list[Path]:
    cfg = _pinned(config_path)
    return systemd.render_all(cfg, config_path.parents[1] / "ops" / "systemd", out,
                              _values(cfg, paths))


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
    systemd.render_all(cfg, repo / "ops" / "systemd", out, _values(cfg))
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
    ("compose", "/srv/with space/compose.yaml"), ("compose", "/srv/a$b/compose.yaml"),
    ("config_host", "/srv/a%h"), ("env_file", '/srv/"q"'), ("user", "a b"),
    ("compose", "relative/compose.yaml"),
    ("data", "relative/data"), ("data", "/srv/da ta"), ("image", "tickr-bot"),
    ("image", "registry.example/tickr-bot:a b"), ("image", "Registry/tickr-bot:x"),
    ("image", "registry.example/tickr-bot@sha256:00")])
def test_unsafe_or_relative_values_are_refused(config_path, repo, field, value):
    cfg = _pinned(config_path)
    bad = systemd.Paths(**{**vars(PATHS),
                           field: value if field in ("user", "image") else Path(value)})
    with pytest.raises(config_module.ConfigError):
        _values(cfg, bad)


def test_unknown_zone_is_refused(config_path, repo):
    with pytest.raises(config_module.ConfigError):
        systemd.values(_pinned(config_path), PATHS, "Mars/Base", _compose_text(config_path))


def test_a_bad_schedule_is_refused_like_launchd(config_path, repo):
    _pinned(config_path)
    text = config_path.read_text("utf-8").replace('bot_start_local = "04:00"',
                                                  'bot_start_local = "25:00"')
    config_path.write_text(text, "utf-8")
    cfg = config_module.load(config_path)
    with pytest.raises(config_module.ConfigError):
        _values(cfg)


def test_render_systemd_command(config_path, repo, tmp_path, capsys):
    out = tmp_path / "units"
    compose = repo / "ops" / "docker" / "compose.yaml"
    assert main(["--config", str(config_path), "render-systemd", "--image", IMAGE,
                 "--compose", str(compose), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(p.name for p in GOLDEN.iterdir())
    bot = (out / "tickr-bot.service").read_text("utf-8")
    assert "Environment=TICKR_CONFIG_HOST=/srv/tickr/etc/bot.toml" in bot
    # The units run the compose file named by --compose, never a clone's ops/docker.
    assert f"ExecStart=/usr/bin/docker compose -f {compose} --profile jobs up -d bot\n" in bot
    assert f"TICKR_IMAGE={IMAGE}\n" in (out / "limits.env").read_text("utf-8")


def test_render_systemd_refuses_a_missing_compose_file(config_path, repo, tmp_path, capsys):
    out = tmp_path / "units"
    code = main(["--config", str(config_path), "render-systemd", "--image", IMAGE,
                 "--compose", str(tmp_path / "no-compose.yaml"), "--out", str(out)])
    assert code == int(runtime_main.ExitCode.CONFIG_INVALID)
    assert "cannot read the compose file" in capsys.readouterr().err
    assert not out.exists()


def test_no_unit_needs_a_clone(config_path, repo, tmp_path):
    out = tmp_path / "out"
    _render(config_path, out)
    for unit in out.iterdir():
        text = unit.read_text("utf-8")
        assert "WorkingDirectory=" not in text and "ops/docker" not in text, unit.name
        assert "/srv/tickr/repo" not in text, unit.name
    for job in ("api", "preflight", "bot", "watchdog", "nightly-fetch", "bot-restart"):
        assert "docker compose -f /srv/tickr/etc/compose.yaml " in (
            out / f"tickr-{job}.service").read_text("utf-8"), job


def test_render_systemd_has_no_default_image(config_path, repo, tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--config", str(config_path), "render-systemd", "--out", str(tmp_path / "units")])
    assert exit_info.value.code == 2 and "--image" in capsys.readouterr().err
    assert not (tmp_path / "units").exists()


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


def _fake_server(tmp_path: Path, flat: bool) -> tuple[Path, Path, Path, Path]:
    """A server with no clone: a docker that runs only the release image (`docker run --pull
    never`: render-systemd writes into the folder mounted at /out, require-flat answers `flat`),
    a systemctl that only logs, the units folder and the etc folder with the copied bot.toml and
    compose.yaml."""
    # The fake docker: `image inspect REF` succeeds only for the images in PRESENT (default: the
    # release image of COMMIT); any other command but `run --pull never ... IMAGE ...` fails;
    # `--entrypoint id IMAGE -u|-g` answers IMAGE_UID/IMAGE_GID (default: the data folder's
    # owner, this test's own ids; ID_FAILS makes it fail), `--entrypoint cat` prints the image's
    # compose.yaml (IMAGE_COMPOSE, default the etc copy), render-systemd writes the units of
    # the --data and --image it is given (RENDER_IMAGE overrides the image), require-flat
    # answers the flat result. Every call is logged.
    docker = tmp_path / "docker"
    docker.write_text(
        f'#!/bin/sh\necho "$@" >> {tmp_path}/docker.log\n'
        'if [ "$1 $2" = "image inspect" ]; then\n'
        f'  for ref in ${{PRESENT-{IMAGE}}}; do [ "$ref" = "$3" ] && exit 0; done; exit 1\nfi\n'
        '[ "$1" = run ] || exit 1\n'
        'case " $* " in *" --pull never "*) ;; *) exit 1 ;; esac\n'
        f'case " $* " in *" {IMAGE} "*) ;; *) exit 1 ;; esac\n'
        'out=; data=; image=; prev=\n'
        'for arg in "$@"; do\n'
        '  case "$prev" in\n'
        '    --mount) case "$arg" in *,target=/out)\n'
        '      out=${arg#*source=}; out=${out%,target=/out} ;; esac ;;\n'
        '    --data) data=$arg ;;\n'
        '    --image) image=$arg ;;\n'
        '  esac\n'
        '  prev=$arg\n'
        'done\n'
        'case "$*" in\n'
        f'  *"--entrypoint id {IMAGE} -u")\n'
        f'    [ -z "${{ID_FAILS:-}}" ] || exit 1; echo "${{IMAGE_UID:-{os.getuid()}}}" ;;\n'
        f'  *"--entrypoint id {IMAGE} -g")\n'
        f'    [ -z "${{ID_FAILS:-}}" ] || exit 1; echo "${{IMAGE_GID:-{os.getgid()}}}" ;;\n'
        f'  *"--entrypoint cat {IMAGE} /app/ops/docker/compose.yaml")\n'
        f'    cat "${{IMAGE_COMPOSE:-{tmp_path}/etc/compose.yaml}}" ;;\n'
        '  *" render-systemd "*)\n'
        '    [ -d "$out" ] || exit 1\n'
        '    : > "$out/tickr-bot.service"; : > "$out/tickr-bot.timer"\n'
        '    echo "PathExists=$data/control/restart-request" > "$out/tickr-bot-restart.path"\n'
        '    : > "$out/ports.env"\n'
        '    printf "TICKR_BOT_EXIT_TIMEOUT=1\\nTICKR_IMAGE=%s\\n" "${RENDER_IMAGE:-$image}" \\\n'
        '      > "$out/limits.env" ;;\n'
        f'  *" require-flat") exit {0 if flat else 1} ;;\n'
        '  *) exit 1 ;;\nesac\n', "utf-8")
    mounted = tmp_path / "mountpoint"
    mounted.write_text('#!/bin/sh\n[ "$1" = -q ] && [ ! -e "$2/.not-mounted" ]\n', "utf-8")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/systemctl.log\n', "utf-8")
    for script in (docker, systemctl, mounted):
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    units, etc = tmp_path / "units", tmp_path / "etc"
    units.mkdir()
    etc.mkdir()
    (etc / "bot.toml").write_text("# the server's copy\n", "utf-8")
    (etc / "compose.yaml").write_text("name: tickr\n", "utf-8")
    # As on the server: the folder is the service user's only, its files read-only to others.
    etc.chmod(0o700)
    for name in ("bot.toml", "compose.yaml"):
        (etc / name).chmod(0o644)
    (tmp_path / "registry.env").write_text(f"TICKR_IMAGE_REPOSITORY={REGISTRY}\n", "utf-8")
    return docker, systemctl, units, etc


def _script_env(units: Path, systemctl: Path, docker: Path, **extra: str) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "TICKR_UNITS_DIR": str(units),
            "TICKR_SYSTEMCTL": str(systemctl), "TICKR_DOCKER": str(docker),
            "TICKR_MOUNTPOINT": str(docker.parent / "mountpoint"),
            "TICKR_REGISTRY_FILE": str(docker.parent / "registry.env"),
            "TICKR_SERVICE_USER": pwd.getpwuid(os.getuid()).pw_name, **extra}


def _deploy(tmp_path: Path, flat: bool, commit: str = COMMIT, registry: str | None = None,
            no_registry: bool = False, **extra: str) -> subprocess.CompletedProcess:
    docker, systemctl, units, etc = _fake_server(tmp_path, flat)
    if registry is not None:
        (tmp_path / "registry.env").write_text(registry, "utf-8")
    if no_registry:
        (tmp_path / "registry.env").unlink()
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return subprocess.run(
        [str(SCRIPT), commit, str(etc), str(data)], capture_output=True, text=True,
        env=_script_env(units, systemctl, docker, **extra), check=False)


COPIED = ["bot.toml", "compose.yaml"]  # the host files in ETC_DIR before any deploy


def _nothing_changed(tmp_path: Path) -> None:
    assert list((tmp_path / "units").iterdir()) == []
    assert sorted(p.name for p in (tmp_path / "etc").iterdir()) == COPIED
    assert not (tmp_path / "systemctl.log").exists()
    assert not (tmp_path / "data" / "control").exists()


@pytest.mark.parametrize("make", [None, "file", "link"])
def test_deploy_script_refuses_and_never_creates_a_missing_data_folder(tmp_path, make):
    data = tmp_path / "data"
    if make == "file":
        data.write_text("", "utf-8")
    elif make == "link":
        (tmp_path / "elsewhere").mkdir()
        data.symlink_to(tmp_path / "elsewhere")
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    done = subprocess.run(
        [str(SCRIPT), COMMIT, str(etc), str(data)], capture_output=True, text=True,
        env=_script_env(units, systemctl, docker), check=False)
    assert done.returncode == 1 and "refused: data-missing" in done.stderr
    assert list(units.iterdir()) == [] and sorted(p.name for p in etc.iterdir()) == COPIED
    assert not (tmp_path / "systemctl.log").exists()
    assert make is not None or not data.exists()
    assert not (tmp_path / "elsewhere" / "control").exists()


def _other_user() -> str:
    """A real account of this machine that is not the one running the tests."""
    return next(p.pw_name for p in pwd.getpwall() if p.pw_uid != os.getuid())


def _deploy_etc(tmp_path: Path, change, **extra: str) -> subprocess.CompletedProcess:
    """Deploy after `change(etc)` altered the etc folder made by the fake server."""
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    change(etc)
    (tmp_path / "data").mkdir()
    return subprocess.run(
        [str(SCRIPT), COMMIT, str(etc), str(tmp_path / "data")], capture_output=True,
        text=True, check=False, env=_script_env(units, systemctl, docker, **extra))


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o711, 0o701, 0o770, 0o500, 0o1700])
def test_deploy_script_refuses_an_etc_folder_that_is_not_mode_700(tmp_path, mode):
    done = _deploy_etc(tmp_path, lambda etc: etc.chmod(mode))
    assert done.returncode == 1 and "refused: etc-dir-unsafe" in done.stderr
    etc = tmp_path / "etc"
    assert stat.S_IMODE(etc.stat().st_mode) == mode  # never fixed by the script
    etc.chmod(0o700)
    _nothing_changed(tmp_path)


@pytest.mark.parametrize("name", ["bot.toml", "compose.yaml"])
@pytest.mark.parametrize("mode", [0o664, 0o666, 0o600, 0o640, 0o444, 0o755, 0o2644])
def test_deploy_script_refuses_an_etc_file_that_is_not_mode_644(tmp_path, name, mode):
    done = _deploy_etc(tmp_path, lambda etc: (etc / name).chmod(mode))
    assert done.returncode == 1 and "refused: etc-file-unsafe" in done.stderr
    assert name in done.stderr
    assert stat.S_IMODE((tmp_path / "etc" / name).stat().st_mode) == mode  # never fixed
    _nothing_changed(tmp_path)


@pytest.mark.parametrize("name", ["bot.toml", "compose.yaml"])
def test_deploy_script_refuses_an_etc_file_that_is_a_link(tmp_path, name):
    def link(etc: Path) -> None:
        target = tmp_path / f"real-{name}"
        target.write_text((etc / name).read_text("utf-8"), "utf-8")
        target.chmod(0o644)
        (etc / name).unlink()
        (etc / name).symlink_to(target)
    done = _deploy_etc(tmp_path, link)
    assert done.returncode == 1 and "refused: etc-file-unsafe" in done.stderr
    assert list((tmp_path / "units").iterdir()) == []
    assert not (tmp_path / "systemctl.log").exists()


def test_deploy_script_refuses_an_etc_folder_that_is_a_link(tmp_path):
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    real = tmp_path / "real-etc"
    etc.rename(real)
    etc.symlink_to(real)
    (tmp_path / "data").mkdir()
    done = subprocess.run(
        [str(SCRIPT), COMMIT, str(etc), str(tmp_path / "data")], capture_output=True,
        text=True, check=False, env=_script_env(units, systemctl, docker))
    assert done.returncode == 1 and "refused: etc-dir-unsafe" in done.stderr
    assert list(units.iterdir()) == [] and not (tmp_path / "systemctl.log").exists()


def test_deploy_script_refuses_an_etc_folder_owned_by_another_user(tmp_path):
    # The folder and files are this test's user's, the service user is another account.
    done = _deploy_etc(tmp_path, lambda etc: None, TICKR_SERVICE_USER=_other_user())
    assert done.returncode == 1 and "refused: etc-dir-unsafe" in done.stderr
    _nothing_changed(tmp_path)


@pytest.mark.parametrize("name", ["bot.toml", "compose.yaml"])
def test_deploy_script_refuses_an_etc_file_owned_by_another_user(tmp_path, name):
    if os.getuid() != 0:
        pytest.skip("giving a file to another account needs root")
    other = pwd.getpwnam(_other_user())
    done = _deploy_etc(tmp_path, lambda etc: os.chown(etc / name, other.pw_uid, other.pw_gid))
    assert done.returncode == 1 and "refused: etc-file-unsafe" in done.stderr
    _nothing_changed(tmp_path)


def test_deploy_script_refuses_an_unknown_service_user(tmp_path):
    done = _deploy_etc(tmp_path, lambda etc: None, TICKR_SERVICE_USER="tickr-no-such-user")
    assert done.returncode == 1 and "refused: service-user-unknown" in done.stderr
    _nothing_changed(tmp_path)


def test_deploy_script_refuses_when_the_data_disk_is_not_mounted(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".not-mounted").touch()
    done = _deploy(tmp_path, flat=True)
    assert done.returncode == 1 and "refused: data-not-mounted" in done.stderr
    _nothing_changed(tmp_path)


@pytest.mark.parametrize("extra", [{"IMAGE_UID": "1000"}, {"IMAGE_GID": "1000"}])
def test_deploy_script_refuses_when_the_image_user_does_not_own_the_data(tmp_path, extra):
    if os.getuid() == 1000 or os.getgid() == 1000:
        extra = {name: "1500" for name in extra}
    done = _deploy(tmp_path, flat=True, **extra)
    assert done.returncode == 1 and "refused: data-owner-mismatch" in done.stderr
    _nothing_changed(tmp_path)


def test_every_unit_that_uses_the_data_folder_requires_its_mount(config_path, repo, tmp_path):
    out = tmp_path / "out"
    _render(config_path, out)
    for unit in [*out.glob("*.service"), *out.glob("*.path")]:
        assert "RequiresMountsFor=/srv/tickr/data\n" in unit.read_text("utf-8"), unit.name


def test_deploy_script_refuses_when_not_flat(tmp_path):
    done = _deploy(tmp_path, flat=False)
    assert done.returncode == 1 and "not proven flat" in done.stderr
    _nothing_changed(tmp_path)


def test_deploy_script_installs_the_units_when_flat(tmp_path):
    done = _deploy(tmp_path, flat=True)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in (tmp_path / "units").iterdir()) == [
        "tickr-bot-restart.path", "tickr-bot.service", "tickr-bot.timer"]
    assert (tmp_path / "etc" / "ports.env").exists() and (tmp_path / "etc" / "limits.env").exists()
    log = (tmp_path / "systemctl.log").read_text("utf-8")
    assert "daemon-reload" in log and "tickr-bot.timer" in log
    assert re.search(r"(?m)^enable .*tickr-bot-restart\.path", log)
    assert re.search(r"(?m)^start .*tickr-bot-restart\.path", log)
    # The control folder: created by the deploy, the service account's only.
    control = tmp_path / "data" / "control"
    assert control.is_dir() and stat.S_IMODE(control.stat().st_mode) == 0o700
    assert control.stat().st_uid == os.getuid()
    # The bot unit is never started or restarted by a deploy.
    assert not re.search(r"(start|restart) .*tickr-bot\.service", log)


def test_the_bots_grace_period_is_the_exit_timeout_from_config_only(config_path, repo, tmp_path):
    cfg = _pinned(config_path)
    out = tmp_path / "out"
    systemd.render_all(cfg, repo / "ops" / "systemd", out, _values(cfg))
    rendered = (out / "limits.env").read_text("utf-8")
    assert rendered == (f"TICKR_BOT_EXIT_TIMEOUT={cfg.runtime.bot_exit_timeout_seconds}\n"
                        f"TICKR_NIGHTLY_OUT={cfg.runtime.nightly_fetch_out}\n"
                        f"TICKR_NIGHTLY_SEED={cfg.runtime.nightly_fetch_seed}\n"
                        "TICKR_DATA_HOST=/srv/tickr/data\n"
                        f"TICKR_IMAGE={IMAGE}\n")
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
    systemd.render_all(cfg, repo / "ops" / "systemd", out, _values(cfg))
    assert "--profile jobs run --rm nightly-fetch\n" in (
        out / "tickr-nightly-fetch.service").read_text("utf-8")
    assert "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 19:00:00 America/New_York" in (
        out / "tickr-nightly-fetch.timer").read_text("utf-8")
    assert re.search(r"(?m)^  nightly-fetch:$", (ROOT / "ops/docker/compose.yaml").read_text())


def test_deploy_script_installs_nothing_when_a_copy_fails(tmp_path):
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    # The units stage fine, then the etc files cannot be copied (a cp that fails into etc).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cp = bin_dir / "cp"
    cp.write_text(f'#!/bin/sh\ncase "$2" in {etc}/*) exit 1 ;; esac\nexec /bin/cp "$@"\n',
                  "utf-8")
    cp.chmod(0o755)
    (tmp_path / "data").mkdir()
    done = subprocess.run(
        [str(SCRIPT), COMMIT, str(etc), str(tmp_path / "data")],
        capture_output=True, text=True, check=False,
        env={**_script_env(units, systemctl, docker), "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert done.returncode == 1 and "nothing was installed" in done.stderr
    assert list(units.iterdir()) == []  # no unit, and no staged file left behind
    assert not (tmp_path / "systemctl.log").exists()  # no daemon-reload


def test_deploy_script_refuses_over_a_waiting_restart_request(tmp_path):
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    control = tmp_path / "data" / "control"
    control.mkdir(parents=True)
    (control / "restart-request").write_text("{}\n", "utf-8")
    done = subprocess.run(
        [str(SCRIPT), COMMIT, str(etc), str(tmp_path / "data")], capture_output=True,
        text=True, check=False, env=_script_env(units, systemctl, docker))
    assert done.returncode == 1 and "restart request is waiting" in done.stderr
    assert list(units.iterdir()) == [] and not (tmp_path / "systemctl.log").exists()
    assert (control / "restart-request").exists()  # left for the owner to read


def test_the_restart_unit_restarts_once_with_the_bots_grace_and_never_retries(
        config_path, repo, tmp_path):
    cfg = _pinned(config_path)
    out = tmp_path / "out"
    systemd.render_all(cfg, repo / "ops" / "systemd", out, _values(cfg))
    service = (out / "tickr-bot-restart.service").read_text("utf-8")
    steps = re.findall(r"(?m)^ExecStart=(.*)$", service)
    assert steps == [
        "/bin/mv -fT /srv/tickr/data/control/restart-request "
        "/srv/tickr/data/control/restart-request.taken",
        "/usr/bin/docker compose -f /srv/tickr/etc/compose.yaml -p tickr "
        "--profile jobs up -d --force-recreate --no-deps --pull never --no-build "
        f"-t {cfg.runtime.bot_exit_timeout_seconds} bot",
        "/bin/cat /srv/tickr/data/control/restart-request.taken"]
    assert "Type=oneshot\n" in service
    assert not re.search(r"(?m)^(Restart|StartLimit|ExecStartPre|ExecStopPost)", service)
    path = (out / "tickr-bot-restart.path").read_text("utf-8")
    assert "PathExists=/srv/tickr/data/control/restart-request\n" in path
    assert "Unit=tickr-bot-restart.service\n" in path


def test_the_compose_project_name_is_tickr_and_data_is_the_host_folder():
    compose = (ROOT / "ops" / "docker" / "compose.yaml").read_text("utf-8")
    assert re.findall(r"(?m)^name: (.*)$", compose) == ["tickr"]
    # No Docker named volume: the host units see the containers' data folder.
    assert not re.search(r"(?m)^volumes:", compose)
    assert compose.count("source: ${TICKR_DATA_HOST:?") == 2
    assert compose.count("target: /app/data\n") == 2


COMPOSE_BOT = """name: tickr
services:
  api:
    stop_grace_period: 5s
  bot:
    image: tickr:local
    # a comment
{grace}    pid: host

  watchdog:
    stop_grace_period: 7s
"""


@pytest.mark.parametrize("grace,values,seconds", [
    ("    stop_grace_period: ${TICKR_BOT_EXIT_TIMEOUT:?load limits.env}s\n",
     {"TICKR_BOT_EXIT_TIMEOUT": "120"}, 120),
    ("    stop_grace_period: 45s\n", {}, 45),
    ('    stop_grace_period: "30"\n', {}, 30),
])
def test_the_bots_stop_grace_is_read_from_compose(grace, values, seconds):
    assert systemd.bot_stop_grace(COMPOSE_BOT.format(grace=grace), values) == seconds


@pytest.mark.parametrize("text,values", [
    (COMPOSE_BOT.format(grace=""), {}),  # none on the bot (api and watchdog's do not count)
    (COMPOSE_BOT.format(grace="    stop_grace_period: ${OTHER}s\n"), {}),  # not rendered
    (COMPOSE_BOT.format(grace="    stop_grace_period: 1m30s\n"), {}),  # another form
    (COMPOSE_BOT.format(grace="    stop_grace_period: 0s\n"), {}),
    (COMPOSE_BOT.format(grace="    stop_grace_period: ${T}s\n"), {"T": "soon"}),
    ("name: tickr\nservices:\n  api:\n    image: x\n", {}),  # no bot service
    ("name: tickr\n", {}),
])
def test_a_missing_or_unreadable_stop_grace_is_refused(text, values):
    with pytest.raises(config_module.ConfigError):
        systemd.bot_stop_grace(text, values)


def test_render_refuses_when_compose_gives_the_bot_no_grace(config_path, repo, tmp_path):
    cfg = _pinned(config_path)
    compose = repo / "ops" / "docker" / "compose.yaml"
    text = compose.read_text("utf-8")
    compose.write_text(re.sub(r"(?m)^\s*stop_grace_period: .*\n", "", text), "utf-8")
    with pytest.raises(config_module.ConfigError, match="no stop_grace_period"):
        _values(cfg)


@pytest.mark.parametrize("commit", [
    "", "main", COMMIT[:12], COMMIT + "0", COMMIT.upper(), COMMIT[:-1] + "g",
    f"{COMMIT[:20]} {COMMIT[21:]}"])
def test_deploy_script_refuses_a_bad_commit(tmp_path, commit):
    done = _deploy(tmp_path, flat=True, commit=commit)
    assert done.returncode == 1 and "refused: bad-commit" in done.stderr
    _nothing_changed(tmp_path)
    assert not (tmp_path / "docker.log").exists()


def test_deploy_script_needs_the_commit(tmp_path):
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    done = subprocess.run([str(SCRIPT)], capture_output=True, text=True, check=False,
                          env=_script_env(units, systemctl, docker))
    assert done.returncode == 2 and "usage:" in done.stderr and "COMMIT" in done.stderr


@pytest.mark.parametrize("registry", [
    None,  # no file
    "",  # empty file
    "AWS_DEFAULT_REGION=us-east-1\n",  # another variable only
    "TICKR_IMAGE_REPOSITORY=\n",
    f'TICKR_IMAGE_REPOSITORY="{REGISTRY}"\n',
    f"TICKR_IMAGE_REPOSITORY={REGISTRY}\nTICKR_IMAGE_REPOSITORY={REGISTRY}\n",
    f"TICKR_IMAGE_REPOSITORY={REGISTRY} extra\n",
    "TICKR_IMAGE_REPOSITORY=tickr-bot\n",  # no registry host
])
def test_deploy_script_refuses_an_unknown_registry(tmp_path, registry):
    done = _deploy(tmp_path, flat=True, registry=registry, no_registry=registry is None)
    assert done.returncode == 1 and "refused: registry-unknown" in done.stderr
    _nothing_changed(tmp_path)
    assert not (tmp_path / "docker.log").exists()


def test_deploy_script_reads_only_the_repository_from_the_registry_file(tmp_path):
    other = "TICKR_SOMETHING_ELSE=do-not-print-0451\n"
    done = _deploy(tmp_path, flat=True,
                   registry=f"# from user-data\n{other}TICKR_IMAGE_REPOSITORY={REGISTRY}\n")
    assert done.returncode == 0, done.stderr
    assert "do-not-print-0451" not in done.stdout + done.stderr
    # The registry address (an AWS account id on the server) is never printed either.
    assert REGISTRY not in done.stdout + done.stderr
    assert (tmp_path / "etc" / "limits.env").read_text("utf-8").endswith(f"TICKR_IMAGE={IMAGE}\n")


@pytest.mark.parametrize("present", ["", f"{REGISTRY}:{'f' * 40}", f"tickr:local {REGISTRY}"])
def test_deploy_script_refuses_when_the_release_image_is_not_on_the_host(tmp_path, present):
    done = _deploy(tmp_path, flat=True, PRESENT=present)
    assert done.returncode == 1 and "refused: image-missing" in done.stderr
    assert f"tickr-image-pull {COMMIT}" in done.stderr and REGISTRY not in done.stderr
    _nothing_changed(tmp_path)
    # Only the inspect ran: no pull, no container.
    assert (tmp_path / "docker.log").read_text("utf-8") == f"image inspect {IMAGE}\n"


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "docker.log").read_text("utf-8").splitlines()


def test_deploy_script_runs_every_python_step_in_the_release_image_and_never_pulls(tmp_path):
    done = _deploy(tmp_path, flat=True)
    assert done.returncode == 0, done.stderr
    calls = _calls(tmp_path)
    assert calls[0] == f"image inspect {IMAGE}"
    # The fake docker fails every command that is not `run --pull never` of the release image:
    # the uid check, render-systemd and require-flat all ran in it; none may pull.
    runs = calls[1:]
    assert len(runs) == 5 and all(c.startswith("run --rm --pull never ") for c in runs)
    assert sum(f"--entrypoint id {IMAGE} -" in c for c in runs) == 2
    etc, data = tmp_path / "etc", tmp_path / "data"
    [render] = [c for c in runs if " render-systemd " in c]
    assert "--network none --entrypoint python " in render
    assert f"type=bind,source={etc}/bot.toml,target=/app/config/bot.toml,readonly" in render
    assert f"type=bind,source={etc}/compose.yaml,target={etc}/compose.yaml,readonly" in render
    assert f" {IMAGE} -m tickr.runtime --config /app/config/bot.toml render-systemd " in render
    assert f"--compose {etc}/compose.yaml --etc {etc} " in render
    assert f"--data {data} " in render and f"--image {IMAGE} --out /out" in render
    [flat] = [c for c in runs if c.endswith(" require-flat")]
    assert "--network host -e TICKR_ENV_FILE=/run/tickr/env " in flat
    assert f"type=bind,source={etc}/bot.toml,target=/app/config/bot.toml,readonly" in flat
    assert "type=bind,source=/run/tickr/env,target=/run/tickr/env,readonly" in flat
    assert f"type=bind,source={data},target=/app/data " in flat
    assert "--entrypoint" not in flat  # the image's entrypoint sources the env file
    assert flat.endswith(f" {IMAGE} python -m tickr.runtime --config /app/config/bot.toml "
                         "require-flat")


def test_deploy_script_needs_no_clone(tmp_path):
    # The server has no clone and no virtualenv: the deploy runs from an empty folder with only
    # ETC_DIR (bot.toml, compose.yaml) and DATA_DIR.
    work = tmp_path / "empty"
    work.mkdir()
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    (tmp_path / "data").mkdir()
    done = subprocess.run(
        [str(SCRIPT), COMMIT, str(etc), str(tmp_path / "data")], capture_output=True,
        text=True, check=False, cwd=work, env=_script_env(units, systemctl, docker))
    assert done.returncode == 0, done.stderr
    assert list(work.iterdir()) == []
    assert not any(p.name in ("repo", ".venv") for p in tmp_path.rglob("*"))
    script = SCRIPT.read_text("utf-8")
    assert ".venv" not in script and "/srv/tickr/repo" not in script
    assert "ops/docker/compose.yaml\"" not in script


@pytest.mark.parametrize("missing,cause", [("bot.toml", "config-missing"),
                                           ("compose.yaml", "compose-missing")])
def test_deploy_script_refuses_without_the_copied_host_files(tmp_path, missing, cause):
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    (etc / missing).unlink()
    (tmp_path / "data").mkdir()
    done = subprocess.run(
        [str(SCRIPT), COMMIT, str(etc), str(tmp_path / "data")], capture_output=True,
        text=True, check=False, env=_script_env(units, systemctl, docker))
    assert done.returncode == 1 and f"refused: {cause}" in done.stderr
    assert list(units.iterdir()) == [] and not (tmp_path / "systemctl.log").exists()
    assert _calls(tmp_path) == [f"image inspect {IMAGE}"]


@pytest.mark.parametrize("which", ["etc", "data"])
def test_deploy_script_refuses_a_relative_folder(tmp_path, which):
    docker, systemctl, units, etc = _fake_server(tmp_path, True)
    (tmp_path / "data").mkdir()
    args = ["etc", str(tmp_path / "data")] if which == "etc" else [str(etc), "data"]
    done = subprocess.run([str(SCRIPT), COMMIT, *args], capture_output=True, text=True,
                          check=False, cwd=tmp_path, env=_script_env(units, systemctl, docker))
    assert done.returncode == 1 and "refused: path-not-absolute" in done.stderr
    _nothing_changed(tmp_path)


def test_deploy_script_refuses_a_compose_file_that_is_not_the_images(tmp_path):
    other = tmp_path / "image-compose.yaml"
    other.write_text("name: tickr\n# another commit\n", "utf-8")
    done = _deploy(tmp_path, flat=True, IMAGE_COMPOSE=str(other))
    assert done.returncode == 1 and "refused: compose-mismatch" in done.stderr
    _nothing_changed(tmp_path)
    assert not any(" render-systemd " in c for c in _calls(tmp_path))
    (tmp_path / "b").mkdir()
    unreadable = _deploy(tmp_path / "b", flat=True, IMAGE_COMPOSE=str(tmp_path / "none"))
    assert unreadable.returncode == 1 and "refused: compose-mismatch" in unreadable.stderr


def test_deploy_script_refuses_when_the_uid_check_cannot_run_the_release_image(tmp_path):
    done = _deploy(tmp_path, flat=True, ID_FAILS="1")
    assert done.returncode == 1 and "refused: image-user-unknown" in done.stderr
    _nothing_changed(tmp_path)
    assert not any(" render-systemd " in c for c in _calls(tmp_path))


def test_deploy_script_refuses_units_rendered_for_another_image(tmp_path):
    done = _deploy(tmp_path, flat=True, RENDER_IMAGE="tickr:local")
    assert done.returncode == 1 and "does not name the release image" in done.stderr
    _nothing_changed(tmp_path)
    assert not any(c.endswith(" require-flat") for c in _calls(tmp_path))


def test_compose_runs_only_the_image_it_is_given():
    compose = (ROOT / "ops" / "docker" / "compose.yaml").read_text("utf-8")
    assert not re.search(r"(?m)^\s*build:", compose)
    assert re.findall(r"(?m)^\s*image: (.*)$", compose) == [
        "${TICKR_IMAGE:?image to run; on the server deploy-server.sh writes it into limits.env}"]
    assert re.findall(r"(?m)^\s*pull_policy: (.*)$", compose) == ["never"]


COMPOSE_VALUES = {"TICKR_CONFIG_HOST": "/srv/tickr/etc/bot.toml",
                  "TICKR_ENV_FILE_HOST": "/run/tickr/env", "TICKR_BOT_EXIT_TIMEOUT": "1",
                  "TICKR_NIGHTLY_OUT": "data/nightly", "TICKR_NIGHTLY_SEED": "data/backtest",
                  "TICKR_DATA_HOST": "/srv/tickr/data"}


def _compose_config(*args: str, **values: str) -> subprocess.CompletedProcess:
    # Only named variables reach compose (CLAUDE.md rule 10): never this process's environment.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", **values}
    return subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "ops" / "docker" / "compose.yaml"),
         "--profile", "jobs", "config", *args], capture_output=True, text=True, check=False,
        env=env)


def _has_compose() -> bool:
    try:
        return _compose_config("--help").returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _has_compose(), reason="docker compose is not installed here "
                    "(ci.yml's docker-image job runs the same check)")
def test_compose_config_resolves_every_service_to_tickr_image():
    done = _compose_config("--images", **COMPOSE_VALUES, TICKR_IMAGE=IMAGE)
    assert done.returncode == 0, done.stderr
    assert set(done.stdout.split()) == {IMAGE}
    missing = _compose_config("-q", **COMPOSE_VALUES)
    assert missing.returncode != 0 and "TICKR_IMAGE" in missing.stderr
