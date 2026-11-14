"""Preflight: may the bot start today? (spec section 11)

Checks, all reported, the first failure is the single cause:
1. config valid (loads, every key commented, active strategy file present and commented,
   launchd schedule valid);
2. data folder present and free disk on it at least `[runtime] min_free_disk_mb`;
3. calendar knows today (New York date); a weekend, a market closure or a disabled half day is
   "no session today", which is not a failure;
4. credential environment variables present: only their NAMES are checked and reported, values
   are never read beyond "set and non-empty";
5. broker token folder present.

Exit codes: 0 ready, 1 failed, 2 config invalid, 3 no session today.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path

from aperture import config as config_module
from aperture.calendar import CalendarCause, CalendarRefused, SessionCalendar, ny_date_of
from aperture.runtime.launchd import check_shutdown_deadlines

STRATEGY_FILE = "strategy.toml"
STRATEGIES_DIR = "strategies"
BYTES_PER_MB = 1024 * 1024
# Calendar refusals that mean "the market is closed today" rather than "we do not know".
_CLOSED_CAUSES = frozenset({CalendarCause.WEEKEND, CalendarCause.HOLIDAY})


class ExitCode(IntEnum):
    READY = 0
    FAILED = 1
    CONFIG_INVALID = 2
    NO_SESSION = 3


class PreflightCause(StrEnum):
    CONFIG_INVALID = "config-invalid"
    DATA_DIR_MISSING = "data-dir-missing"
    DISK_LOW = "disk-low"
    CALENDAR_UNKNOWN = "calendar-unknown"
    CREDENTIALS_MISSING = "credentials-missing"
    TOKEN_DIR_MISSING = "token-dir-missing"


class CheckName(StrEnum):
    CONFIG = "config"
    DISK = "disk"
    CALENDAR = "calendar"
    CREDENTIALS = "credentials"
    TOKEN_DIR = "token-dir"


@dataclass(frozen=True)
class Check:
    name: CheckName
    passed: bool
    detail: str
    cause: PreflightCause | None = None


@dataclass(frozen=True)
class PreflightResult:
    checks: tuple[Check, ...]
    session_today: bool
    environment: str | None
    config: config_module.Config | None

    @property
    def cause(self) -> PreflightCause | None:
        """The single recorded cause: the first failed check, or None."""
        return next((c.cause for c in self.checks if not c.passed), None)

    @property
    def exit_code(self) -> ExitCode:
        cause = self.cause
        if cause is PreflightCause.CONFIG_INVALID:
            return ExitCode.CONFIG_INVALID
        if cause is not None:
            return ExitCode.FAILED
        if not self.session_today:
            return ExitCode.NO_SESSION
        return ExitCode.READY

    def journal_body(self) -> dict[str, object]:
        return {
            "exit_code": int(self.exit_code),
            "cause": self.cause.value if self.cause else None,
            "environment": self.environment,
            "session_today": self.session_today,
            "checks": [
                {"name": c.name.value, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }

    def summary(self) -> str:
        """One line for an alert: the failed checks, in order."""
        failed = [f"{c.name.value}: {c.detail}" for c in self.checks if not c.passed]
        return "; ".join(failed) if failed else "all checks passed"


DiskFree = Callable[[Path], int]


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _load_config(path: Path) -> tuple[config_module.Config | None, Check]:
    try:
        text = path.read_text("utf-8")
        cfg = config_module.load(path)
        problems = config_module.uncommented_keys(text)
        if problems:
            keys = ", ".join(key for _, key in problems)
            raise config_module.ConfigError(f"keys without a comment: {keys}")
        strategy = cfg.root / STRATEGIES_DIR / cfg.strategy.active / STRATEGY_FILE
        if not strategy.is_file():
            raise config_module.ConfigError(f"active strategy file missing: {strategy}")
        if config_module.uncommented_keys(strategy.read_text("utf-8")):
            raise config_module.ConfigError(f"{strategy} has keys without a comment")
        check_shutdown_deadlines(cfg)
    except (OSError, UnicodeDecodeError, config_module.ConfigError) as exc:
        return None, Check(CheckName.CONFIG, False, str(exc), PreflightCause.CONFIG_INVALID)
    return cfg, Check(CheckName.CONFIG, True, f"{path} digest {cfg.digest[:12]}")


def _check_disk(cfg: config_module.Config, disk_free: DiskFree) -> Check:
    data_dir = cfg.root / cfg.run.data_dir
    if not data_dir.is_dir():
        return Check(CheckName.DISK, False, f"data folder missing: {data_dir}",
                     PreflightCause.DATA_DIR_MISSING)
    free_mb = disk_free(data_dir) // BYTES_PER_MB
    need = cfg.runtime.min_free_disk_mb
    if free_mb < need:
        return Check(CheckName.DISK, False, f"{free_mb} MB free, need {need} MB",
                     PreflightCause.DISK_LOW)
    return Check(CheckName.DISK, True, f"{free_mb} MB free (need {need} MB)")


def _check_calendar(cfg: config_module.Config, now: datetime) -> tuple[Check, bool]:
    today = ny_date_of(now)
    directory = (cfg.root / cfg.session.calendar_file).parent
    try:
        day = SessionCalendar.load(directory).day(today)
    except CalendarRefused as exc:
        if exc.cause in _CLOSED_CAUSES:
            return Check(CheckName.CALENDAR, True, f"no session {today}: {exc.cause}"), False
        return Check(CheckName.CALENDAR, False, f"{today}: {exc}",
                     PreflightCause.CALENDAR_UNKNOWN), False
    if day.half_day and not cfg.session.half_days_enabled:
        return Check(CheckName.CALENDAR, True,
                     f"no session {today}: half day, half_days_enabled = false"), False
    return Check(CheckName.CALENDAR, True,
                 f"{today} regular {day.regular_open:%H:%M}-{day.regular_close:%H:%M} ET"), True


def _check_credentials(cfg: config_module.Config, environ: Mapping[str, str]) -> Check:
    env = cfg.environment
    names = (env.app_key_variable, env.app_secret_variable, env.account_variable)
    # Presence only: the value is compared with "" and never stored, printed or returned.
    missing = [name for name in names if not environ.get(name)]
    if missing:
        return Check(CheckName.CREDENTIALS, False, "not set: " + ", ".join(missing),
                     PreflightCause.CREDENTIALS_MISSING)
    return Check(CheckName.CREDENTIALS, True, "set: " + ", ".join(names))


def _check_token_dir(cfg: config_module.Config) -> Check:
    token_dir = cfg.root / cfg.run.data_dir / cfg.environment.token_dir
    if not token_dir.is_dir():
        return Check(CheckName.TOKEN_DIR, False, f"missing: {token_dir}",
                     PreflightCause.TOKEN_DIR_MISSING)
    return Check(CheckName.TOKEN_DIR, True, str(token_dir))


def run_preflight(
    config_path: Path,
    *,
    now: datetime,
    environ: Mapping[str, str],
    disk_free: DiskFree = _disk_free,
) -> PreflightResult:
    """Run every check. `now` must be timezone-aware; `environ` is usually os.environ."""
    cfg, config_check = _load_config(config_path)
    if cfg is None:
        return PreflightResult((config_check,), session_today=False, environment=None, config=None)
    disk = _check_disk(cfg, disk_free)
    calendar, session_today = _check_calendar(cfg, now)
    checks = (config_check, disk, calendar, _check_credentials(cfg, environ),
              _check_token_dir(cfg))
    return PreflightResult(checks, session_today, cfg.environment_name, cfg)
