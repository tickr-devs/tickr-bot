"""Render the launchd job files from `ops/launchd/*.plist.template` for the owner to install.

Nothing is installed by this code: it writes plists into an output folder; the owner copies them
to ~/Library/LaunchAgents and loads them (docs/operations.md). Templates use `{{NAME}}`
placeholders; every value is XML-escaped; an unknown or unfilled placeholder is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from aperture.config import ConfigError, RuntimeConfig

TEMPLATE_SUFFIX = ".plist.template"
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_HHMM = re.compile(r"^(\d{2}):(\d{2})$")
# launchd calendar units (protocol constants): hours 0-23, minutes 0-59, weekdays 0-7 where
# both 0 and 7 are Sunday.
_HOURS = 24
_MINUTES = 60
_WEEKDAY_MAX = 7


@dataclass(frozen=True)
class LocalTime:
    hour: int
    minute: int


@dataclass(frozen=True)
class Schedule:
    preflight: LocalTime
    bot: LocalTime
    weekdays: tuple[int, ...]


def _local_time(key: str, value: str) -> LocalTime:
    match = _HHMM.match(value)
    if match is None:
        raise ConfigError(f"[runtime] {key}: expected HH:MM, got {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour >= _HOURS or minute >= _MINUTES:
        raise ConfigError(f"[runtime] {key}: not a time of day: {value!r}")
    return LocalTime(hour, minute)


def schedule(runtime: RuntimeConfig) -> Schedule:
    """Validated launchd schedule from [runtime]; refuses bad times, weekdays and timeouts."""
    preflight = _local_time("preflight_start_local", runtime.preflight_start_local)
    bot = _local_time("bot_start_local", runtime.bot_start_local)
    if (preflight.hour, preflight.minute) >= (bot.hour, bot.minute):
        raise ConfigError("[runtime] preflight_start_local must be before bot_start_local")
    days = runtime.launch_weekdays
    if not days or len(set(days)) != len(days) or any(d < 0 or d > _WEEKDAY_MAX for d in days):
        raise ConfigError(
            f"[runtime] launch_weekdays: expected distinct days 0..7, got {list(days)!r}")
    if runtime.bot_exit_timeout_seconds <= 0:
        raise ConfigError("[runtime] bot_exit_timeout_seconds must be positive")
    if not 0 < runtime.stop_alert_after_seconds < runtime.bot_exit_timeout_seconds:
        raise ConfigError("[runtime] stop_alert_after_seconds must be positive and below "
                          "bot_exit_timeout_seconds")
    if runtime.supervisor_poll_seconds <= 0:
        raise ConfigError("[runtime] supervisor_poll_seconds must be positive")
    if runtime.min_free_disk_mb <= 0:
        raise ConfigError("[runtime] min_free_disk_mb must be positive")
    return Schedule(preflight=preflight, bot=bot, weekdays=tuple(days))


def _calendar_xml(at: LocalTime, weekdays: tuple[int, ...]) -> str:
    entries = "".join(
        "<dict>"
        f"<key>Weekday</key><integer>{day}</integer>"
        f"<key>Hour</key><integer>{at.hour}</integer>"
        f"<key>Minute</key><integer>{at.minute}</integer>"
        "</dict>"
        for day in weekdays
    )
    return f"<array>{entries}</array>"


@dataclass(frozen=True)
class Paths:
    """Owner machine paths that go into the plists (absolute)."""

    repo: Path
    python: Path
    config: Path
    env_file: Path
    log_dir: Path


def values(runtime: RuntimeConfig, paths: Paths) -> dict[str, str]:
    """Placeholder values, already XML-safe."""
    sched = schedule(runtime)
    for name, path in vars(paths).items():
        if not Path(path).is_absolute():
            raise ConfigError(f"launchd path {name} must be absolute, got {path}")
    return {
        "REPO": escape(str(paths.repo)),
        "PYTHON": escape(str(paths.python)),
        "CONFIG": escape(str(paths.config)),
        "ENV_FILE": escape(str(paths.env_file)),
        "LOG_DIR": escape(str(paths.log_dir)),
        "PREFLIGHT_CALENDAR": _calendar_xml(sched.preflight, sched.weekdays),
        "BOT_CALENDAR": _calendar_xml(sched.bot, sched.weekdays),
        "BOT_EXIT_TIMEOUT": str(runtime.bot_exit_timeout_seconds),
    }


def render_text(template: str, filled: dict[str, str]) -> str:
    names = set(_PLACEHOLDER.findall(template))
    unknown = sorted(names - set(filled))
    if unknown:
        raise ConfigError(f"template has unknown placeholders: {', '.join(unknown)}")
    return _PLACEHOLDER.sub(lambda m: filled[m.group(1)], template)


def render_all(template_dir: Path, out_dir: Path, filled: dict[str, str]) -> list[Path]:
    """Render every template in `template_dir` into `out_dir` (created if missing)."""
    templates = sorted(template_dir.glob(f"*{TEMPLATE_SUFFIX}"))
    if not templates:
        raise ConfigError(f"no {TEMPLATE_SUFFIX} files in {template_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for template in templates:
        target = out_dir / template.name.removesuffix(".template")
        target.write_text(render_text(template.read_text("utf-8"), filled), "utf-8")
        written.append(target)
    return written
