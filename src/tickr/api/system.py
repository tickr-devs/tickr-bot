"""GET /system: the apps' System card (read-only).

Portable parts, which a bot on any machine has: the link to Webull (the status file's last
broker read and quote stream), the latest preflight (its record file, written by the preflight
and bot launchd jobs) and model training (the training job's own progress file). Then `host`:
the machine's own services, behind `[api] system_services` so a server without launchd or
caffeinate reports none instead of guessing. On a Mac: the watchdog job's journal and restarts,
and caffeinate's no-sleep hold read from the power assertions. `model`: the trained model's switch
and bundle from `[model]`, and its state only where the API can know it. Nothing here is
written, and the bot neither reads nor writes anything for it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr.api import contract as api
from tickr.config import SYSTEM_SERVICES_MAC, Config
from tickr.runtime import watchdog
from tickr.runtime.preflight import RECORD_SCHEMA_VERSION, record_path

NO_PREFLIGHT = "no preflight recorded yet"
NO_TRAINING = "no training job is reporting"
NO_HOST = "no machine services on this host ([api] system_services = none)"
NO_BROKER_READ = "no broker read recorded yet"
TRAINING_SCHEMA_VERSION = 1  # training-progress-v1, docs/api.md "GET /system"
# The power assertion lines of `pmset -g assertions` that are caffeinate's no-sleep holds:
# "pid 523(caffeinate): [0x...] 00:15:02 PreventUserIdleSystemSleep named: ...".
_HOLD = re.compile(r"pid (?P<pid>\d+)\(caffeinate\):.*\b(PreventUserIdleSystemSleep|"
                   r"PreventSystemSleep)\b")

# Runs a command with a timeout in seconds: (exit code, standard output). Replaced in tests.
Runner = Callable[[Sequence[str], float], tuple[int, str]]


def run_command(command: Sequence[str], timeout: float) -> tuple[int, str]:
    done = subprocess.run(list(command), check=False, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout


@dataclass(frozen=True)
class SystemSettings:
    warn_seconds: Decimal  # [api] broker_read_warn_seconds
    max_age_seconds: Decimal  # [orders] broker_read_max_age_seconds
    preflight_record: Path
    training_progress: Path
    host: str  # [api] system_services
    watchdog_journal: Path
    sleep_command: tuple[str, ...]
    sleep_timeout_seconds: Decimal
    restart_rows: int
    model_enabled: bool  # [model] enabled
    model_bundle: str  # [model] bundle, "" = none chosen

    @classmethod
    def from_config(cls, config: Config) -> SystemSettings:
        data = config.root / config.run.data_dir
        return cls(
            warn_seconds=config.api.broker_read_warn_seconds,
            max_age_seconds=config.orders.broker_read_max_age_seconds,
            preflight_record=record_path(config),
            training_progress=data / Path(config.api.training_progress_file).expanduser(),
            host=config.api.system_services,
            watchdog_journal=data / config.runtime.watchdog_journal_file,
            sleep_command=config.api.sleep_check_command,
            sleep_timeout_seconds=config.api.sleep_check_timeout_seconds,
            restart_rows=config.api.system_restart_rows,
            model_enabled=config.model.enabled,
            model_bundle=config.model.bundle,
        )


def system_document(settings: SystemSettings, response: api.StatusResponseV1,
                    runner: Runner = run_command) -> api.SystemV1:
    """The System card at `response.served_at`, from the status the server just read."""
    status = response.status
    morning, morning_cause = morning_check(settings.preflight_record)
    training, training_cause = _training(settings.training_progress)
    mac = settings.host == SYSTEM_SERVICES_MAC
    host, host_cause = (_mac(settings, runner), None) if mac else (None, NO_HOST)
    return api.SystemV1(
        served_at=response.served_at,
        bot_state=status.bot_state if status is not None else None,
        status_stale=response.stale,
        broker_link=_link(settings, response),
        morning_check=morning,
        morning_check_cause=morning_cause,
        training=training,
        training_cause=training_cause,
        host=host,
        host_cause=host_cause,
        model=_model(settings),
    )


def _model(settings: SystemSettings) -> api.ModelV1:
    """Off while `[model] enabled` is false. While it is true the bot is the one that loads the
    bundle, and it records no load result yet, so the state is unknown here: null with its cause
    (rule 7), never `not_loaded` or `refused` guessed from the config."""
    bundle = settings.model_bundle or None
    if not settings.model_enabled:
        return api.ModelV1(False, bundle, api.ModelState.OFF, None)
    return api.ModelV1(True, bundle, None, api.MODEL_LOAD_NOT_RECORDED)


def _link(settings: SystemSettings, response: api.StatusResponseV1) -> api.BrokerLinkV1:
    status = response.status
    last = status.freshness.last_broker_read_at if status is not None else None
    if status is None:
        state, cause = api.LinkState.UNKNOWN, response.unavailable_cause
    elif last is None:
        state, cause = api.LinkState.UNKNOWN, NO_BROKER_READ
    else:
        age = api.seconds_between(last, response.served_at)
        state = (api.LinkState.STALE if age > settings.max_age_seconds else
                 api.LinkState.AGING if age > settings.warn_seconds else api.LinkState.FRESH)
        cause = None
    return api.BrokerLinkV1(last, settings.warn_seconds, settings.max_age_seconds, state,
                            status.freshness.stream if status is not None else None, cause)


def _read_json(path: Path, version: int) -> dict[str, Any] | None:
    """A JSON object file of this schema version, None when missing. Anything else raises."""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    body = json.loads(text)
    if not isinstance(body, dict) or body.get("schema_version") != version:
        raise ValueError("schema_version")
    return body


def _aware(text: object) -> datetime:
    value = datetime.fromisoformat(str(text))
    if value.tzinfo is None:
        raise ValueError("naive time")
    return value


def morning_check(path: Path) -> tuple[api.MorningCheckV1 | None, str | None]:
    try:
        body = _read_json(path, RECORD_SCHEMA_VERSION)
        if body is None:
            return None, NO_PREFLIGHT
        checks = tuple(api.PreflightCheckV1(str(c["name"]), _flag(c["passed"]),
                                            None if c["cause"] is None else str(c["cause"]))
                       for c in body["checks"])
        cause = body["cause"]
        return api.MorningCheckV1(
            job=api.PreflightJobName(body["job"]),
            started_at=_aware(body["started_at"]),
            finished_at=_aware(body["finished_at"]),
            day=date.fromisoformat(body["ny_date"]),
            outcome=api.PreflightOutcome(body["outcome"]),
            blocks_start=_flag(body["blocks_start"]),
            cause=None if cause is None else str(cause),
            checks=checks), None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"preflight record unreadable ({type(exc).__name__})"


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("not a boolean")
    return value


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("not a count")
    return value


def _training(path: Path) -> tuple[api.TrainingV1 | None, str | None]:
    try:
        body = _read_json(path, TRAINING_SCHEMA_VERSION)
        if body is None:
            return None, NO_TRAINING
        done, total = _count(body["done"]), _count(body["total"])
        if done > total:
            raise ValueError("done above total")
        cause = body["cause"]
        return api.TrainingV1(
            job=str(body["job"]), state=api.TrainingState(body["state"]), done=done,
            total=total, started_at=_aware(body["started_at"]),
            updated_at=_aware(body["updated_at"]),
            cause=None if cause is None else str(cause)), None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"training progress unreadable ({type(exc).__name__})"


def _mac(settings: SystemSettings, runner: Runner) -> api.HostV1:
    return api.HostV1(SYSTEM_SERVICES_MAC,
                      _watchdog(settings.watchdog_journal, settings.restart_rows),
                      _sleep(settings, runner))


def _watchdog(path: Path, rows: int) -> api.WatchdogV1:
    try:
        try:
            lines = path.read_text("utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        started: date | None = None
        check: dict[str, Any] | None = None
        restarts: list[api.WatchdogRestartV1] = []
        for line in lines:
            if not line.strip():
                continue
            record = json.loads(line)
            kind = record["record"]
            if kind == watchdog.START and "no_session" not in record:
                started = date.fromisoformat(record["day"])
            elif kind == watchdog.CHECK:
                check = record
            elif kind == watchdog.RESTART:
                restarts.append(_restart(record))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return api.WatchdogV1(None, None, None, None, (), 0,
                              f"watchdog journal unreadable ({type(exc).__name__})")
    read = None if check is None else _flag(check["broker_read"])
    flat = None if not read or check is None else (
        _count(check["positions"]) == 0 and _count(check["open_orders"]) == 0)
    return api.WatchdogV1(
        started_day=started,
        last_check_at=None if check is None else _aware(check["at"]),
        last_check_read=read,
        last_check_flat=flat,
        restarts=tuple(reversed(restarts))[:rows],
        restart_count=len(restarts),
        cause=None)


def _restart(record: dict[str, Any]) -> api.WatchdogRestartV1:
    """A restart line: `started` and `cause` always; `why`, `open_trades` and `early` only when
    the command ran (a refusal records neither)."""
    started = _flag(record["started"])
    ran = "open_trades" in record
    outcome = (api.RestartOutcome.STARTED if started else
               api.RestartOutcome.FAILED if ran else api.RestartOutcome.REFUSED)
    cause = record["cause"]
    return api.WatchdogRestartV1(
        at=_aware(record["at"]), outcome=outcome,
        early=_flag(record["early"]) if ran else False,
        open_trades=_count(record["open_trades"]) if ran else None,
        cause=None if cause is None else str(cause))


def _sleep(settings: SystemSettings, runner: Runner) -> api.SleepV1:
    try:
        code, out = runner(settings.sleep_command, float(settings.sleep_timeout_seconds))
    except (OSError, subprocess.SubprocessError) as exc:
        return api.SleepV1(api.SleepState.UNKNOWN, None,
                           f"sleep check failed ({type(exc).__name__})")
    if code != 0:
        return api.SleepV1(api.SleepState.UNKNOWN, None, f"sleep check exited {code}")
    holders = {match["pid"] for match in _HOLD.finditer(out)}
    return api.SleepV1(api.SleepState.HELD if holders else api.SleepState.NOT_HELD,
                       len(holders), None)

