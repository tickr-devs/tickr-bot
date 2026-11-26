"""Render the systemd units for a Linux server from `ops/systemd/*.template`.

The counterpart of `launchd.py`: the same jobs, the same `[runtime]` times and weekdays, the same
validation (`launchd.schedule`, `launchd.check_timings`). The units run `docker compose` with the
services and profiles of `ops/docker/compose.yaml` (docs/deploy-server.md): the API always on;
preflight, bot, watchdog and nightly fetch as scheduled jobs. Nothing is installed by this code:
it writes the
units, the timers, `ports.env` and `limits.env` into an output folder;
`ops/systemd/deploy-server.sh` installs
them, and only when the bot is flat.

Secrets: a job unit names the env file by path (`TICKR_ENV_FILE_HOST=`, mounted read-only into
the container by compose) and never holds a value. Every job unit requires and follows
`tickr-secrets.service`, the unit that writes that file (installed by the server bootstrap, not
rendered here).

Times are wall-clock times in a zone the caller names (an IANA name): the server's own zone is
never assumed, so a host on UTC still starts the bot at 04:00 New York time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tickr.config import Config, ConfigError
from tickr.runtime import launchd

SERVICE_SUFFIX = ".service.template"
TIMER_SUFFIX = ".timer.template"
PORTS_ENV = "ports.env"
COMPOSE_ENV = "limits.env"
# systemd weekday names, indexed by the launchd number (0 and 7 are both Sunday).
_WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEK_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A value written into a unit unquoted: no whitespace, quote, backslash, `$` or `%` (systemd
# expands them), so nothing in it can change what the unit runs.
_SAFE = re.compile(r"^[A-Za-z0-9._/@+:=,-]+$")


@dataclass(frozen=True)
class Paths:
    """Server names that go into the units: absolute `repo` (the clone with ops/docker),
    `config_host` and `env_file` (host paths compose mounts into the containers), and the
    service account `user`."""

    repo: Path
    config_host: Path
    env_file: Path
    user: str


def _safe(name: str, value: str) -> str:
    if not _SAFE.match(value):
        raise ConfigError(f"systemd {name} {value!r} has a character a unit cannot hold safely "
                          "(allowed: letters, digits and . _ / @ + : = , -)")
    return value


def _calendar(at: launchd.LocalTime, weekdays: tuple[int, ...], zone: str) -> str:
    days = {_WEEKDAYS[day] for day in weekdays}
    names = ",".join(day for day in _WEEK_ORDER if day in days)
    return f"{names} *-*-* {at.hour:02d}:{at.minute:02d}:00 {zone}"


def check_zone(zone: str) -> str:
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(f"timezone {zone!r} is not an IANA zone name "
                          "(for example America/New_York)") from exc
    return _safe("timezone", zone)


def values(config: Config, paths: Paths, zone: str) -> dict[str, str]:
    """Placeholder values for the templates."""
    runtime = config.runtime
    sched = launchd.schedule(runtime)
    for name in ("repo", "config_host", "env_file"):
        if not Path(getattr(paths, name)).is_absolute():
            raise ConfigError(f"systemd path {name} must be absolute, got {getattr(paths, name)}")
    zone = check_zone(zone)
    return {
        "REPO": _safe("repo", str(paths.repo)),
        "CONFIG_HOST": _safe("config", str(paths.config_host)),
        "ENV_FILE": _safe("env_file", str(paths.env_file)),
        "COMPOSE_ENV": _safe("config", str(paths.config_host.parent / COMPOSE_ENV)),
        "USER": _safe("user", paths.user),
        "PREFLIGHT_CALENDAR": _calendar(sched.preflight, sched.weekdays, zone),
        "BOT_CALENDAR": _calendar(sched.bot, sched.weekdays, zone),
        "NIGHTLY_CALENDAR": _calendar(sched.nightly, sched.weekdays, zone),
        "WATCHDOG_CALENDAR": _calendar(sched.watchdog, sched.weekdays, zone),
    }


def ports_env(config: Config) -> str:
    """`ports.env` text: the API's two ports from [api], for the server's own tooling."""
    return (f"TICKR_API_PORT={config.api.port}\n"
            f"TICKR_API_REMOTE_PORT={config.api.remote_port}\n")


def compose_env(config: Config) -> str:
    """`limits.env` text: the values compose.yaml interpolates, from [runtime] only (the bot's
    stop_grace_period is its exit timeout; the nightly fetch's folders)."""
    runtime = config.runtime
    return (f"TICKR_BOT_EXIT_TIMEOUT={runtime.bot_exit_timeout_seconds}\n"
            f"TICKR_NIGHTLY_OUT={_safe('nightly_fetch_out', runtime.nightly_fetch_out)}\n"
            f"TICKR_NIGHTLY_SEED={_safe('nightly_fetch_seed', runtime.nightly_fetch_seed)}\n")


def render_all(config: Config, template_dir: Path, out_dir: Path,
               filled: dict[str, str]) -> list[Path]:
    """Render every service and timer template in `template_dir`, and `ports.env`, into
    `out_dir` (created if missing)."""
    templates = sorted(p for p in template_dir.iterdir()
                       if p.name.endswith((SERVICE_SUFFIX, TIMER_SUFFIX)))
    if not templates:
        raise ConfigError(f"no unit templates in {template_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for template in templates:
        target = out_dir / template.name.removesuffix(".template")
        target.write_text(launchd.render_text(template.read_text("utf-8"), filled), "utf-8")
        written.append(target)
    ports = out_dir / PORTS_ENV
    ports.write_text(ports_env(config), "utf-8")
    compose = out_dir / COMPOSE_ENV
    compose.write_text(compose_env(config), "utf-8")
    return [*written, ports, compose]
