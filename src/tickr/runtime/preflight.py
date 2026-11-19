"""Preflight: may the bot start today? (spec section 11)

Checks, all reported, the first failure is the single cause:
1. config valid (loads, every key commented, active strategy file present and commented,
   launchd schedule valid);
1b. the running code version is known (tickr/version.py: the git commit, or the image's
   `TICKR_CODE_VERSION` without `.git`); without it the bot refuses to start, because every
   run and trade is tagged with it;
2. data folder present and free disk on it at least `[runtime] min_free_disk_mb`;
3. calendar knows today (New York date); a weekend, a market closure or a disabled half day is
   "no session today", which is not a failure;
4. credential environment variables present: only their NAMES are checked and reported, values
   are never read beyond "set and non-empty";
4b. this machine has an alert channel (`alerts.default_notifier`): today only macOS
   (notification and iMessage through osascript). Elsewhere (a Linux server, a container) the
   bot would run with no way to reach the owner, so the start is refused;
4c. the iMessage recipient variable (`[alerts] imessage_recipient_variable`) present, the same
   way: it is personal data;
5. broker token folder present;
5b. no operator stop marker (runtime/stopmarker.py): after a stop-bot command the bot stays
   stopped, and this check fails every morning, until the owner runs `tickr clear-stop`;
5c. the database, when it exists, holds runs of this environment only (spec 10: one database
   per environment); otherwise the bot would refuse at its start, hours later;
6. active strategy builds from its file and `[market_data] history_sessions` covers the prior
   sessions it needs. A failure here is reported but does not stop the bot from starting: the
   bot then protects positions with entries off (`blocks_start`);
7. broker token usable: only when no earlier check blocks the start and today has a session,
   the caller's `token_probe` creates or checks the Webull token, which may wait for the
   owner's confirmation in the app (the probe alerts token-needs-confirmation before it waits).

Exit codes: 0 ready, 1 failed, 2 config invalid, 3 no session today.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path

from tickr import config as config_module
from tickr.calendar import CalendarCause, CalendarRefused, SessionCalendar, ny_date_of
from tickr.observers.errors import ObserverError
from tickr.observers.loader import load_active
from tickr.runtime import stopmarker
from tickr.runtime.alerts import default_notifier, recipient_missing
from tickr.runtime.launchd import PRIVATE_MODE_MASK, check_timings
from tickr.store import (
    DATABASE_FILE,
    MIGRATIONS,
    EnvironmentMismatch,
    Store,
)
from tickr.version import CodeVersionUnknown, running_code_version

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
    CODE_VERSION_UNKNOWN = "code-version-unknown"
    DATA_DIR_MISSING = "data-dir-missing"
    DISK_LOW = "disk-low"
    CALENDAR_UNKNOWN = "calendar-unknown"
    CREDENTIALS_MISSING = "credentials-missing"
    ALERT_RECIPIENT_MISSING = "alert-recipient-missing"
    ALERT_CHANNEL_MISSING = "alert-channel-missing"
    TOKEN_DIR_MISSING = "token-dir-missing"
    TOKEN_DIR_OPEN = "token-dir-open"
    TOKEN_UNUSABLE = "token-unusable"
    STRATEGY_REFUSED = "strategy-refused"
    STOPPED_BY_OPERATOR = "stopped-by-operator"
    DATABASE_OTHER_ENVIRONMENT = "database-other-environment"
    DATABASE_UNREADABLE = "database-unreadable"


class CheckName(StrEnum):
    CONFIG = "config"
    CODE_VERSION = "code-version"
    DISK = "disk"
    CALENDAR = "calendar"
    CREDENTIALS = "credentials"
    ALERT_RECIPIENT = "alert-recipient"
    ALERT_CHANNEL = "alert-channel"
    TOKEN_DIR = "token-dir"
    TOKEN = "token"
    STRATEGY = "strategy"
    STOP_MARKER = "stop-marker"
    DATABASE = "database"


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
    config_path: Path  # the config file asked for, as given (journaled resolved)

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

    @property
    def blocks_start(self) -> bool:
        """True when the bot must not be started. A refused strategy alone does not block:
        the bot starts with entries off so it can still protect an existing position."""
        if self.exit_code is ExitCode.NO_SESSION:
            return True
        return any(not c.passed and c.cause is not PreflightCause.STRATEGY_REFUSED
                   for c in self.checks)

    def journal_body(self) -> dict[str, object]:
        """Also names the config it used (resolved path and full digest) and the data folder
        it checked (resolved): under a release (runtime/release.py) that is the release's own
        config and the one shared data folder its link names."""
        cfg = self.config
        return {
            "exit_code": int(self.exit_code),
            "cause": self.cause.value if self.cause else None,
            "environment": self.environment,
            "session_today": self.session_today,
            "config_path": str(self.config_path.resolve()),
            "config_digest": cfg.digest if cfg is not None else None,
            "data_dir": str((cfg.root / cfg.run.data_dir).resolve()) if cfg is not None else None,
            "checks": [
                {"name": c.name.value, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }

    def summary(self) -> str:
        """One line for an alert: the failed checks, in order."""
        failed = [f"{c.name.value}: {c.detail}" for c in self.checks if not c.passed]
        return "; ".join(failed) if failed else "all checks passed"


# Schema of the preflight record file ([runtime] preflight_result_file): GET /system reads it.
RECORD_SCHEMA_VERSION = 1
# Outcome words of the record, by exit code (docs/api.md, "GET /system").
OUTCOMES = {ExitCode.READY: "ready", ExitCode.FAILED: "failed",
            ExitCode.CONFIG_INVALID: "config-invalid", ExitCode.NO_SESSION: "no-session"}


class PreflightJob(StrEnum):
    """Which launchd job ran the preflight: the morning preflight job, or the bot job's own
    before it starts the bot."""

    PREFLIGHT = "preflight"
    BOT_START = "bot-start"


def record_text(result: PreflightResult, job: PreflightJob, started_at: datetime,
                finished_at: datetime) -> str:
    """The record file's text: when and by which job it ran, the outcome, the single cause and
    every check by name with its cause code. The checks' own words are left out: they hold
    paths, the config digest and folder names, and the file is served to the apps."""
    return json.dumps({
        "schema_version": RECORD_SCHEMA_VERSION,
        "job": job.value,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "ny_date": ny_date_of(started_at).isoformat(),
        "outcome": OUTCOMES[result.exit_code],
        "blocks_start": result.blocks_start,
        "cause": result.cause.value if result.cause else None,
        "checks": [{"name": c.name.value, "passed": c.passed,
                    "cause": c.cause.value if c.cause else None}
                   for c in result.checks],
    }, sort_keys=True) + "\n"


def record_path(cfg: config_module.Config) -> Path:
    return cfg.root / cfg.run.data_dir / cfg.runtime.preflight_result_file


DiskFree = Callable[[Path], int]
# Makes the broker token usable; returns None, or the cause it is not usable.
TokenProbe = Callable[[config_module.Config], str | None]


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
        check_timings(cfg)
    except (OSError, UnicodeDecodeError, config_module.ConfigError) as exc:
        return None, Check(CheckName.CONFIG, False, str(exc), PreflightCause.CONFIG_INVALID)
    detail = f"{path.resolve()} digest {cfg.digest}"
    if cfg.renamed_keys:
        detail += f"; old key names, rename them: {', '.join(cfg.renamed_keys)}"
    return cfg, Check(CheckName.CONFIG, True, detail)


def _check_code_version(cfg: config_module.Config, environ: Mapping[str, str]) -> Check:
    try:
        version = running_code_version(environ, cfg.run.code_version_timeout_seconds)
    except CodeVersionUnknown as exc:
        return Check(CheckName.CODE_VERSION, False, str(exc),
                     PreflightCause.CODE_VERSION_UNKNOWN)
    return Check(CheckName.CODE_VERSION, True, version)


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


ENV_FILE_HINT = ("add one NAME='value' line per missing name to the env file the launchd jobs load "
                 "(the --env-file given to render-launchd, e.g. ~/.config/tickr/env; create it "
                 "with chmod 600, see docs/operations.md, One-time setup, step 2)")


def _check_credentials(cfg: config_module.Config, environ: Mapping[str, str]) -> Check:
    env = cfg.environment
    names = (env.app_key_variable, env.app_secret_variable, env.account_variable)
    # Presence only: the value is compared with "" and never stored, printed or returned.
    missing = [name for name in names if not environ.get(name)]
    if missing:
        return Check(CheckName.CREDENTIALS, False,
                     "not set: " + ", ".join(missing) + "; " + ENV_FILE_HINT,
                     PreflightCause.CREDENTIALS_MISSING)
    return Check(CheckName.CREDENTIALS, True, "set: " + ", ".join(names))


def _check_alert_recipient(cfg: config_module.Config, environ: Mapping[str, str]) -> Check:
    # Presence only, like the credentials: the iMessage address is personal data.
    missing = recipient_missing(cfg.alerts, environ)
    if missing is not None:
        return Check(CheckName.ALERT_RECIPIENT, False,
                     f"not set: {missing} (your iMessage phone number or Apple ID email); "
                     + ENV_FILE_HINT, PreflightCause.ALERT_RECIPIENT_MISSING)
    return Check(CheckName.ALERT_RECIPIENT, True, f"set: {cfg.alerts.imessage_recipient_variable}")


def _check_alert_channel(cfg: config_module.Config, platform: str,
                         environ: Mapping[str, str]) -> Check:
    if default_notifier(cfg.alerts, platform, environ) is None:
        return Check(CheckName.ALERT_CHANNEL, False,
                     f"no alert channel on platform {platform!r}: the Mac notification and "
                     "iMessage need macOS (see docs/deploy-server.md)",
                     PreflightCause.ALERT_CHANNEL_MISSING)
    return Check(CheckName.ALERT_CHANNEL, True, f"platform {platform}")


def _check_token_dir(cfg: config_module.Config) -> Check:
    token_dir = cfg.root / cfg.run.data_dir / cfg.environment.token_dir
    try:
        info = token_dir.stat()
    except OSError as exc:
        return Check(CheckName.TOKEN_DIR, False, f"missing: {token_dir} ({type(exc).__name__})",
                     PreflightCause.TOKEN_DIR_MISSING)
    if not stat.S_ISDIR(info.st_mode):
        return Check(CheckName.TOKEN_DIR, False, f"missing: {token_dir} is not a folder",
                     PreflightCause.TOKEN_DIR_MISSING)
    # The SDK writes its token file with the process umask (usually readable by everyone), so
    # the folder itself must keep other accounts on this machine out of it. POSIX mode bits
    # only, not macOS ACLs: enough for a single-owner Mac.
    if stat.S_IMODE(info.st_mode) & PRIVATE_MODE_MASK:
        return Check(CheckName.TOKEN_DIR, False,
                     f"readable by other accounts: {token_dir} (run: chmod 700 {token_dir})",
                     PreflightCause.TOKEN_DIR_OPEN)
    return Check(CheckName.TOKEN_DIR, True, str(token_dir))


def _check_stop_marker(cfg: config_module.Config) -> Check:
    stopped = stopmarker.describe(cfg)
    if stopped is not None:
        return Check(CheckName.STOP_MARKER, False, stopped, PreflightCause.STOPPED_BY_OPERATOR)
    return Check(CheckName.STOP_MARKER, True, "none")


def _check_database(cfg: config_module.Config) -> Check:
    """Read-only: never creates, migrates or writes the bot's database."""
    database = cfg.root / cfg.run.data_dir / DATABASE_FILE
    if not database.exists():
        return Check(CheckName.DATABASE, True, "not created yet")
    try:
        store = Store.open_readonly(database)
        try:
            version = store.schema_version
            if version == 0:
                return Check(CheckName.DATABASE, True, "empty: the bot creates it")
            if version > len(MIGRATIONS):
                return Check(CheckName.DATABASE, False,
                             f"{database} has schema {version}, this code knows "
                             f"{len(MIGRATIONS)}: update the code",
                             PreflightCause.DATABASE_UNREADABLE)
            store.require_environment(cfg.environment_name)
        finally:
            store.close()
    except EnvironmentMismatch as exc:
        return Check(CheckName.DATABASE, False, str(exc),
                     PreflightCause.DATABASE_OTHER_ENVIRONMENT)
    except sqlite3.Error as exc:
        return Check(CheckName.DATABASE, False, f"cannot read {database}: {exc}",
                     PreflightCause.DATABASE_UNREADABLE)
    return Check(CheckName.DATABASE, True, f"runs of {cfg.environment_name} only")


def _check_strategy(cfg: config_module.Config) -> Check:
    try:
        loaded = load_active(cfg)
    except ObserverError as exc:
        return Check(CheckName.STRATEGY, False, str(exc), PreflightCause.STRATEGY_REFUSED)
    return Check(CheckName.STRATEGY, True,
                 f"{loaded.identity} needs {loaded.strategy.prior_sessions_needed} prior "
                 f"sessions, history_sessions = {cfg.market_data.history_sessions}")


def run_preflight(
    config_path: Path,
    *,
    now: datetime,
    environ: Mapping[str, str],
    disk_free: DiskFree = _disk_free,
    token_probe: TokenProbe | None = None,
    platform: str = sys.platform,
) -> PreflightResult:
    """Run every check. `now` must be timezone-aware; `environ` is usually os.environ.
    Without `token_probe` the token check is not run. `platform` picks the alert channels."""
    cfg, config_check = _load_config(config_path)
    if cfg is None:
        return PreflightResult((config_check,), session_today=False, environment=None, config=None,
                               config_path=config_path)
    disk = _check_disk(cfg, disk_free)
    calendar, session_today = _check_calendar(cfg, now)
    checks = (config_check, _check_code_version(cfg, environ), disk, calendar,
              _check_credentials(cfg, environ),
              _check_alert_channel(cfg, platform, environ), _check_alert_recipient(cfg, environ),
              _check_token_dir(cfg), _check_stop_marker(cfg), _check_database(cfg),
              _check_strategy(cfg))
    blocking = any(not c.passed and c.cause is not PreflightCause.STRATEGY_REFUSED
                   for c in checks)
    if token_probe is not None and session_today and not blocking:
        cause = token_probe(cfg)
        checks += (Check(CheckName.TOKEN, True, "usable") if cause is None else
                   Check(CheckName.TOKEN, False, cause, PreflightCause.TOKEN_UNUSABLE),)
    return PreflightResult(checks, session_today, cfg.environment_name, cfg, config_path)
