"""Render the launchd job files from `ops/launchd/*.plist.template` for the owner to install.

Nothing is installed by this code: it writes plists into an output folder; the owner copies them
to ~/Library/LaunchAgents and loads them (docs/operations.md). Templates use `{{NAME}}`
placeholders; every value is XML-escaped; an unknown or unfilled placeholder is refused.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from tickr.config import Config, ConfigError, RuntimeConfig

TEMPLATE_SUFFIX = ".plist.template"
# The nightly fetch writes its outcome here, in [runtime] nightly_fetch_out (backtest/nightly.py);
# GET /jobs reads it.
NIGHTLY_OUTCOME_FILE = "nightly.json"
# The nightly fetch's last good data, in [runtime] nightly_fetch_out; the Lab replays it.
NIGHTLY_CURRENT = "current"
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_HHMM = re.compile(r"^(\d{2}):(\d{2})$")
# launchd calendar units (protocol constants): hours 0-23, minutes 0-59, weekdays 0-7 where
# both 0 and 7 are Sunday.
_HOURS = 24
_MINUTES = 60
_WEEKDAY_MAX = 7
SECONDS_PER_MINUTE = 60


@dataclass(frozen=True)
class LocalTime:
    hour: int
    minute: int


@dataclass(frozen=True)
class Schedule:
    preflight: LocalTime
    bot: LocalTime
    watchdog: LocalTime
    nightly: LocalTime
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
    watchdog = _local_time("watchdog_start_local", runtime.watchdog_start_local)
    nightly = _local_time("nightly_fetch_start_local", runtime.nightly_fetch_start_local)
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
    if runtime.watchdog_bot_check_seconds <= 0:
        raise ConfigError("[runtime] watchdog_bot_check_seconds must be positive")
    for key in ("nightly_fetch_out", "nightly_fetch_seed"):
        if not getattr(runtime, key) or Path(getattr(runtime, key)).is_absolute():
            raise ConfigError(f"[runtime] {key} must be a folder relative to the repo root")
    return Schedule(preflight=preflight, bot=bot, watchdog=watchdog, nightly=nightly,
                    weekdays=tuple(days))


def check_lab_folder(config: Config) -> None:
    """Refuse unless the Lab replays the nightly job's current/ folder, so its data is refreshed.
    Only render-launchd and check-config call this: a Lab setting never stops the bot's preflight.
    """
    lab = (config.root / config.run.data_dir / config.api.lab_replay_data_dir).resolve()
    nightly = (config.root / config.runtime.nightly_fetch_out / NIGHTLY_CURRENT).resolve()
    if lab != nightly:
        raise ConfigError(f"[api] lab_replay_data_dir must be the nightly job's "
                          f"{NIGHTLY_CURRENT}/ folder ([runtime] nightly_fetch_out), or the Lab "
                          "replays data never refreshed")


def check_timings(config: Config) -> None:
    """Refuse unless the engine drain ends before the supervisor alert, which ends before SIGKILL:
    0 < [orders] shutdown_deadline_seconds < [runtime] stop_alert_after_seconds
      < [runtime] bot_exit_timeout_seconds;
    and unless a reconcile pass fits inside the day-end grace:
    [orders] reconcile_interval_seconds < [session] flatten_grace_seconds;
    and unless both watchdog checks fall between the bot's day-end check and the close:
    flatten_grace_seconds < watchdog_after_flatten_seconds and
    watchdog_after_flatten_seconds + watchdog_before_close_seconds
      < flatten_minutes_before_close (in seconds), watchdog_before_close_seconds > 0;
    and unless the watchdog's restart of a down bot comes after the last entry time:
    0 < watchdog_restart_before_flatten_seconds
      < (last_entry_minutes_before_close - flatten_minutes_before_close) in seconds,
    with a restart command and a positive timeout for it;
    and unless the session flatten can still send its close:
    0 <= [session] close_order_cutoff_seconds < flatten_minutes_before_close (in seconds)."""
    schedule(config.runtime)
    drain = config.orders.shutdown_deadline_seconds
    alert_after = config.runtime.stop_alert_after_seconds
    if not 0 < drain < alert_after:
        raise ConfigError(
            f"[orders] shutdown_deadline_seconds ({drain}) must be positive and below "
            f"[runtime] stop_alert_after_seconds ({alert_after})")
    grace = config.session.flatten_grace_seconds
    if not config.orders.reconcile_interval_seconds < grace:
        raise ConfigError(
            f"[session] flatten_grace_seconds ({grace}) must be above "
            f"[orders] reconcile_interval_seconds ({config.orders.reconcile_interval_seconds})")
    runtime = config.runtime
    stale = runtime.watchdog_status_stale_seconds
    floor = max(config.orders.detail_poll_seconds, config.orders.reconcile_interval_seconds)
    if not stale > floor:
        raise ConfigError(
            f"[runtime] watchdog_status_stale_seconds ({stale}) must be above [orders] "
            f"detail_poll_seconds and reconcile_interval_seconds ({floor})")
    after, before = runtime.watchdog_after_flatten_seconds, runtime.watchdog_before_close_seconds
    if not grace < after:
        raise ConfigError(f"[runtime] watchdog_after_flatten_seconds ({after}) must be above "
                          f"[session] flatten_grace_seconds ({grace})")
    window = config.session.flatten_minutes_before_close * SECONDS_PER_MINUTE
    if not (before > 0 and after + before < window):
        raise ConfigError(
            f"[runtime] watchdog_after_flatten_seconds ({after}) + watchdog_before_close_seconds "
            f"({before}) must be below the flatten-to-close time ({window} s), both positive")
    cutoff = config.session.close_order_cutoff_seconds
    if not 0 <= cutoff < window:
        raise ConfigError(f"[session] close_order_cutoff_seconds ({cutoff}) must be from 0 to "
                          f"below the flatten-to-close time ({window} s)")
    restart = runtime.watchdog_restart_before_flatten_seconds
    entry_gap = (config.session.last_entry_minutes_before_close
                 - config.session.flatten_minutes_before_close) * SECONDS_PER_MINUTE
    if not 0 < restart < entry_gap:
        raise ConfigError(
            f"[runtime] watchdog_restart_before_flatten_seconds ({restart}) must be positive and "
            f"below the last-entry-to-flatten time ({entry_gap} s): a restarted bot must never "
            "be able to open a trade")
    if not runtime.watchdog_restart_command or runtime.watchdog_restart_timeout_seconds <= 0:
        raise ConfigError("[runtime] watchdog_restart_command must be non-empty and "
                          "watchdog_restart_timeout_seconds positive")


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


# Permission bits of the group and others: a file or folder holding secrets has none of them.
PRIVATE_MODE_MASK = stat.S_IRWXG | stat.S_IRWXO


def check_env_file(path: Path) -> None:
    """The credentials file the jobs source must exist and be readable by its owner only. Only
    its metadata is looked at, never its contents."""
    try:
        info = path.stat()
    except OSError as exc:
        raise ConfigError(f"env file {path} not usable ({type(exc).__name__}); create it with "
                          "chmod 600 (docs/operations.md, One-time setup, step 2)") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"env file {path} is not a regular file")
    if stat.S_IMODE(info.st_mode) & PRIVATE_MODE_MASK:
        raise ConfigError(f"env file {path} is readable by other accounts (run: chmod 600 {path})")


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
        "WATCHDOG_CALENDAR": _calendar_xml(sched.watchdog, sched.weekdays),
        "NIGHTLY_CALENDAR": _calendar_xml(sched.nightly, sched.weekdays),
        "NIGHTLY_OUT": escape(runtime.nightly_fetch_out),
        "NIGHTLY_SEED": escape(runtime.nightly_fetch_seed),
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
