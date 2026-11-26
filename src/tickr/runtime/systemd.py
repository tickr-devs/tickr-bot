"""Render the systemd units for a Linux server from `ops/systemd/*.template`.

The counterpart of `launchd.py`: the same jobs, the same `[runtime]` times and weekdays, the same
validation (`launchd.schedule`, `launchd.check_timings`). The units run `docker compose` with the
services and profiles of the server's copy of `ops/docker/compose.yaml`, a host file named by its
absolute path (`/srv/tickr/etc/compose.yaml`, docs/deploy-server.md; the server has no clone):
the API always on; preflight, bot, watchdog and nightly fetch as scheduled jobs. Nothing is
installed by this code: it writes the units, the timers, `ports.env` and `limits.env` into an
output folder; `ops/systemd/deploy-server.sh` runs it inside the release image and installs
them, and only when the bot is flat.

Secrets: a job unit names the env file by path (`TICKR_ENV_FILE_HOST=`, mounted read-only into
the container by compose) and never holds a value. Every job unit requires and follows
`tickr-secrets.service`, the unit that writes that file (installed by the server bootstrap, not
rendered here).

Bot restart on request: `tickr-bot-restart.path` watches the request file the watchdog's restart
command writes inside its container (runtime/restartrequest.py) at the same place under the host
data folder, and `tickr-bot-restart.service` restarts the compose service `bot` once, with the
bot's own `stop_grace_period` read from that compose.yaml here (`bot_stop_grace`), never a number of
its own.

Times are wall-clock times in a zone the caller names (an IANA name): the server's own zone is
never assumed, so a host on UTC still starts the bot at 04:00 New York time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tickr.config import Config, ConfigError
from tickr.runtime import launchd, restartrequest

SERVICE_SUFFIX = ".service.template"
TIMER_SUFFIX = ".timer.template"
PATH_SUFFIX = ".path.template"
BOT_SERVICE = "bot"
PORTS_ENV = "ports.env"
COMPOSE_ENV = "limits.env"
# systemd weekday names, indexed by the launchd number (0 and 7 are both Sunday).
_WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEK_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A value written into a unit unquoted: no whitespace, quote, backslash, `$` or `%` (systemd
# expands them), so nothing in it can change what the unit runs.
_SAFE = re.compile(r"^[A-Za-z0-9._/@+:=,-]+$")
# An image reference compose runs: a repository and a tag (`<registry>/tickr-bot:<commit>` on
# the server), never a bare name, which Docker would read as `:latest`.
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Paths:
    """Server names that go into the units: absolute `compose` (the host's compose.yaml every
    unit runs), `config_host`, `env_file` and `data` (host paths compose mounts into the
    containers; `data` is the data folder, [run] data_dir in the containers), the service
    account `user`, and `image`, the image reference every compose service runs (TICKR_IMAGE in
    limits.env)."""

    compose: Path
    config_host: Path
    env_file: Path
    data: Path
    user: str
    image: str


def _safe(name: str, value: str) -> str:
    if not _SAFE.match(value):
        raise ConfigError(f"systemd {name} {value!r} has a character a unit cannot hold safely "
                          "(allowed: letters, digits and . _ / @ + : = , -)")
    return value


def _calendar(at: launchd.LocalTime, weekdays: tuple[int, ...], zone: str) -> str:
    days = {_WEEKDAYS[day] for day in weekdays}
    names = ",".join(day for day in _WEEK_ORDER if day in days)
    return f"{names} *-*-* {at.hour:02d}:{at.minute:02d}:00 {zone}"


def check_image(image: str) -> str:
    if not _IMAGE.match(image):
        raise ConfigError(f"image {image!r} is not a repository and tag "
                          "(for example <registry>/tickr-bot:<commit>)")
    return _safe("image", image)


def check_zone(zone: str) -> str:
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(f"timezone {zone!r} is not an IANA zone name "
                          "(for example America/New_York)") from exc
    return _safe("timezone", zone)


_GRACE_VARIABLE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::?[?-][^}]*)?\}s?$")
_GRACE_SECONDS = re.compile(r"^(\d+)s?$")


def bot_stop_grace(compose_text: str, compose_values: Mapping[str, str]) -> int:
    """Seconds of the bot service's `stop_grace_period` in compose.yaml: a whole number of
    seconds, or `${NAME...}s` resolved from `compose_values` (the limits.env compose reads).
    Refuses (ConfigError) when the bot service or its stop_grace_period is missing, or the value
    is any other form: there is no default."""
    lines = compose_text.splitlines()
    try:
        start = lines.index("services:")
    except ValueError:
        raise ConfigError("compose.yaml has no top-level services:") from None
    bot = None
    for index in range(start + 1, len(lines)):
        if re.match(r"^\S", lines[index]):
            break
        if lines[index].rstrip() == f"  {BOT_SERVICE}:":
            bot = index
            break
    if bot is None:
        raise ConfigError(f"compose.yaml has no service {BOT_SERVICE!r}")
    raw = None
    for line in lines[bot + 1:]:
        if line.strip() and not line.lstrip().startswith("#") and not line.startswith("   "):
            break  # the next service or top-level key: the bot's block ended
        found = re.match(r"^    stop_grace_period:\s*(.+?)\s*$", line)
        if found:
            raw = found.group(1).strip("\"'")
            break
    if raw is None:
        raise ConfigError(f"compose.yaml service {BOT_SERVICE!r} has no stop_grace_period; "
                          "the restart unit never invents one")
    variable = _GRACE_VARIABLE.match(raw)
    if variable:
        name = variable.group(1)
        if name not in compose_values:
            raise ConfigError(f"compose.yaml stop_grace_period reads {name}, which "
                              "render-systemd does not write")
        raw = compose_values[name]
    seconds = _GRACE_SECONDS.match(raw)
    if not seconds or int(seconds.group(1)) <= 0:
        raise ConfigError(f"compose.yaml stop_grace_period {raw!r} is not a positive whole "
                          "number of seconds")
    return int(seconds.group(1))


def values(config: Config, paths: Paths, zone: str, compose_text: str) -> dict[str, str]:
    """Placeholder values for the templates. `compose_text` is the text of `paths.compose` (the
    caller reads it): the source of the bot's stop grace."""
    runtime = config.runtime
    sched = launchd.schedule(runtime)
    for name in ("compose", "config_host", "env_file", "data"):
        if not Path(getattr(paths, name)).is_absolute():
            raise ConfigError(f"systemd path {name} must be absolute, got {getattr(paths, name)}")
    zone = check_zone(zone)
    data = _safe("data", str(paths.data))
    image = check_image(paths.image)
    request = _safe("server_restart_request_file",
                    str(paths.data / restartrequest.relative_request_file(config)))
    grace = bot_stop_grace(compose_text, compose_values(config, data, image))
    return {
        "COMPOSE": _safe("compose", str(paths.compose)),
        "CONFIG_HOST": _safe("config", str(paths.config_host)),
        "ENV_FILE": _safe("env_file", str(paths.env_file)),
        "COMPOSE_ENV": _safe("config", str(paths.config_host.parent / COMPOSE_ENV)),
        "USER": _safe("user", paths.user),
        "DATA_HOST": data,
        "IMAGE": image,
        "RESTART_REQUEST": request,
        "RESTART_TAKEN": request + restartrequest.TAKEN_SUFFIX,
        "BOT_STOP_GRACE": str(grace),
        "PREFLIGHT_CALENDAR": _calendar(sched.preflight, sched.weekdays, zone),
        "BOT_CALENDAR": _calendar(sched.bot, sched.weekdays, zone),
        "NIGHTLY_CALENDAR": _calendar(sched.nightly, sched.weekdays, zone),
        "WATCHDOG_CALENDAR": _calendar(sched.watchdog, sched.weekdays, zone),
    }


def ports_env(config: Config) -> str:
    """`ports.env` text: the API's two ports from [api], for the server's own tooling."""
    return (f"TICKR_API_PORT={config.api.port}\n"
            f"TICKR_API_REMOTE_PORT={config.api.remote_port}\n")


def compose_values(config: Config, data_host: str, image: str) -> dict[str, str]:
    """The values compose.yaml interpolates: from [runtime] (the bot's stop_grace_period is its
    exit timeout; the nightly fetch's folders), the host data folder and the image."""
    runtime = config.runtime
    return {"TICKR_BOT_EXIT_TIMEOUT": str(runtime.bot_exit_timeout_seconds),
            "TICKR_NIGHTLY_OUT": _safe("nightly_fetch_out", runtime.nightly_fetch_out),
            "TICKR_NIGHTLY_SEED": _safe("nightly_fetch_seed", runtime.nightly_fetch_seed),
            "TICKR_DATA_HOST": _safe("data", data_host),
            "TICKR_IMAGE": check_image(image)}


def compose_env(config: Config, data_host: str, image: str) -> str:
    """`limits.env` text: `compose_values`, one NAME=value line each."""
    return "".join(f"{name}={value}\n"
                   for name, value in compose_values(config, data_host, image).items())


def render_all(config: Config, template_dir: Path, out_dir: Path,
               filled: dict[str, str]) -> list[Path]:
    """Render every service, timer and path template in `template_dir`, and `ports.env`, into
    `out_dir` (created if missing)."""
    templates = sorted(p for p in template_dir.iterdir()
                       if p.name.endswith((SERVICE_SUFFIX, TIMER_SUFFIX, PATH_SUFFIX)))
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
    compose.write_text(compose_env(config, filled["DATA_HOST"], filled["IMAGE"]), "utf-8")
    return [*written, ports, compose]
