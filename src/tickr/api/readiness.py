"""GET /readiness: the readiness view for enabling another environment (readiness-v1), read-only.

Every `[environments.<name>]` section is evaluated as if it were loaded, without switching to
it: `trading_gate()` itself runs on a copy of the loaded config whose `[run] environment` names
that section, so the view shows exactly what the gate would say and never re-implements it. The
authorization record is shown field by field next to the gate's own authorization check; its
`statement` is never served.

The checklist only informs. Which environments it is about comes from the config alone: those
with `requires_owner_authorization`. Code never names an environment (CLAUDE.md rule 5). The
evidence checks (clean days, today's preflight, the watchdog) are of the loaded environment,
where the bot runs now.

Nothing here writes: not the config, not the authorization file (the owner creates it by hand),
not the bot's files. Credential variables are checked by NAME for presence only (rule 4).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from tickr.api import contract as api
from tickr.api import reasons
from tickr.api.causes import safe_cause
from tickr.api.history import HistoryUnavailable
from tickr.api.system import NO_HOST, _watchdog, morning_check
from tickr.calendar import ny_date_of
from tickr.config import (
    SYSTEM_SERVICES_MAC,
    Config,
    EnvironmentConfig,
    GateResult,
    _authorization_valid,
    trading_gate,
)
from tickr.runtime.preflight import record_path

NOT_HELD = ("this process holds none of them (the API job is started without other "
            "environments' credentials); that environment's preflight checks them")
SOME_HELD = "only some of them are set"
NEVER_STARTED = "the watchdog has not started yet"
PREFLIGHT_FAILED = (api.PreflightOutcome.FAILED, api.PreflightOutcome.CONFIG_INVALID)

# Counts the loaded environment's clean days now; raises HistoryUnavailable (or OSError) when
# history cannot be read. The server passes its History reader.
CleanDays = Callable[[date], int]


@dataclass(frozen=True)
class ReadinessSettings:
    config: Config  # as the API process loaded it: its digest is what a record must carry
    clean_days: int  # [api] readiness_clean_days
    preflight_record: Path
    watchdog_journal: Path
    host: str  # [api] system_services

    @classmethod
    def from_config(cls, config: Config) -> ReadinessSettings:
        return cls(config=config, clean_days=config.api.readiness_clean_days,
                   preflight_record=record_path(config),
                   watchdog_journal=config.root / config.run.data_dir
                   / config.runtime.watchdog_journal_file,
                   host=config.api.system_services)


def _chart_probe(cfg: Config) -> str | None:
    from tickr.probes import chart_client_gate  # the probes pull in the broker module

    return chart_client_gate(cfg)


def readiness_document(settings: ReadinessSettings, served_at: datetime,
                       clean_days: CleanDays, environ: Mapping[str, str] = os.environ,
                       chart_probe: Callable[[Config], str | None] = _chart_probe,
                       ) -> api.ReadinessV1:
    cfg = settings.config
    today = ny_date_of(served_at)
    environments = tuple(_environment(cfg, name, environ) for name in cfg.environments)
    checklist = [_clean(settings, today, clean_days), _preflight(settings, today),
                 _safeguard(settings, today)]
    for env in environments:
        if env.requires_owner_authorization:
            checklist += _target(_as_loaded(cfg, env.name), env, chart_probe)
    return api.ReadinessV1(
        served_at=served_at, environment=cfg.environment_name, config_digest=cfg.digest,
        gate=_gate(trading_gate(cfg)), environments=environments, checklist=tuple(checklist))


def _as_loaded(cfg: Config, name: str) -> Config:
    """The loaded config with `name` as its environment: nothing else changes."""
    return replace(cfg, run=replace(cfg.run, environment=name))


def _gate(result: GateResult) -> api.Gate:
    reason = reasons.gate_reason(result.cause)
    return api.Gate(result.open, result.cause, reason.code, reason.params)


def _keys(env: EnvironmentConfig, environ: Mapping[str, str]) -> bool | None:
    names = (env.app_key_variable, env.app_secret_variable, env.account_variable)
    present = [bool(environ.get(name)) for name in names]
    return True if all(present) else None if not any(present) else False


def _environment(cfg: Config, name: str,
                 environ: Mapping[str, str]) -> api.EnvironmentReadinessV1:
    env = cfg.environments[name]
    as_loaded = _as_loaded(cfg, name)
    return api.EnvironmentReadinessV1(
        name=name, loaded=name == cfg.environment_name, trading_enabled=env.trading_enabled,
        requires_owner_authorization=env.requires_owner_authorization,
        keys_present=_keys(env, environ), gate=_gate(trading_gate(as_loaded)),
        authorization=(_authorization(as_loaded, env) if env.requires_owner_authorization
                       else None))


def _shown(value: object) -> str | None:
    return None if value is None else str(value)


def _authorization(cfg: Config, env: EnvironmentConfig) -> api.AuthorizationRecordV1:
    """The gate's own check, plus what the file says (never its statement)."""
    check = _authorization_valid(cfg, env)
    path = cfg.root / env.authorization_file if env.authorization_file else None
    present = path is not None and path.is_file()
    record: dict[str, object] = {}
    if path is not None and present:
        try:
            record = tomllib.loads(path.read_text("utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            record = {}  # the gate's cause says why
    digest = record.get("config_digest")
    reason = reasons.gate_reason(check.cause)
    return api.AuthorizationRecordV1(
        file=env.authorization_file, present=present,
        environment=_shown(record.get("environment")),
        approved_on=_shown(record.get("approved_on")),
        digest_matches=None if digest is None else digest == cfg.digest,
        valid=check.open, cause=check.cause, code=reason.code, params=reason.params)


def _check(code: api.ReadinessCode, environment: str, state: api.ReadinessState,
           value: str | None = None, cause: str | None = None) -> api.ReadinessCheckV1:
    return api.ReadinessCheckV1(code, environment, state, value, cause)


PASS, FAIL, UNKNOWN = api.ReadinessState.PASS, api.ReadinessState.FAIL, api.ReadinessState.UNKNOWN


def _clean(settings: ReadinessSettings, today: date,
           clean_days: CleanDays) -> api.ReadinessCheckV1:
    name = settings.config.environment_name
    try:
        count = clean_days(today)
    except (HistoryUnavailable, OSError) as exc:
        return _check(api.ReadinessCode.CLEAN_DAYS, name, UNKNOWN,
                      cause=safe_cause(exc))
    value = f"{count} / {settings.clean_days}"
    if count >= settings.clean_days:
        return _check(api.ReadinessCode.CLEAN_DAYS, name, PASS, value)
    return _check(api.ReadinessCode.CLEAN_DAYS, name, FAIL, value,
                  f"{count} clean days of {settings.clean_days}")


def _preflight(settings: ReadinessSettings, today: date) -> api.ReadinessCheckV1:
    name = settings.config.environment_name
    morning, cause = morning_check(settings.preflight_record)
    if morning is None:
        return _check(api.ReadinessCode.PREFLIGHT, name, UNKNOWN, cause=cause)
    value = morning.day.isoformat()
    if morning.day != today:
        return _check(api.ReadinessCode.PREFLIGHT, name, UNKNOWN, value,
                      f"the latest preflight ran on {value}, not today")
    if morning.outcome is api.PreflightOutcome.READY:
        return _check(api.ReadinessCode.PREFLIGHT, name, PASS, value)
    if morning.outcome in PREFLIGHT_FAILED:
        return _check(api.ReadinessCode.PREFLIGHT, name, FAIL, value,
                      f"preflight {morning.outcome.value}: {morning.cause}")
    return _check(api.ReadinessCode.PREFLIGHT, name, UNKNOWN, value,
                  f"preflight {morning.outcome.value}")


def _safeguard(settings: ReadinessSettings, today: date) -> api.ReadinessCheckV1:
    name = settings.config.environment_name
    if settings.host != SYSTEM_SERVICES_MAC:
        return _check(api.ReadinessCode.UNATTENDED_SAFEGUARD, name, UNKNOWN, cause=NO_HOST)
    watchdog = _watchdog(settings.watchdog_journal, 0)
    if watchdog.cause is not None:
        return _check(api.ReadinessCode.UNATTENDED_SAFEGUARD, name, UNKNOWN,
                      cause=watchdog.cause)
    started = watchdog.started_day
    if started is None:
        return _check(api.ReadinessCode.UNATTENDED_SAFEGUARD, name, FAIL, cause=NEVER_STARTED)
    value = started.isoformat()
    if started == today:
        return _check(api.ReadinessCode.UNATTENDED_SAFEGUARD, name, PASS, value)
    return _check(api.ReadinessCode.UNATTENDED_SAFEGUARD, name, UNKNOWN, value,
                  f"the watchdog last started on {value}, not today")


def _target(cfg: Config, env: api.EnvironmentReadinessV1,
            chart_probe: Callable[[Config], str | None]) -> list[api.ReadinessCheckV1]:
    """The checks of one environment that requires owner authorization, `cfg` loaded as it."""
    name = env.name
    keys = (_check(api.ReadinessCode.KEYS, name, PASS) if env.keys_present else
            _check(api.ReadinessCode.KEYS, name, UNKNOWN, cause=NOT_HELD)
            if env.keys_present is None else
            _check(api.ReadinessCode.KEYS, name, FAIL, cause=SOME_HELD))
    probe = chart_probe(cfg)
    data = (_check(api.ReadinessCode.DATA_PROBE, name, PASS) if probe is None else
            _check(api.ReadinessCode.DATA_PROBE, name, FAIL, cause=probe))
    enabled = (_check(api.ReadinessCode.TRADING_ENABLED, name, PASS) if env.trading_enabled
               else _check(api.ReadinessCode.TRADING_ENABLED, name, FAIL,
                           cause=f"[environments.{name}] trading_enabled is false"))
    record = env.authorization
    if record is None:  # pragma: no cover - only environments that require one come here
        raise ValueError(f"{name} requires no owner authorization")
    authorization = (_check(api.ReadinessCode.AUTHORIZATION, name, PASS) if record.valid else
                     _check(api.ReadinessCode.AUTHORIZATION, name, FAIL, cause=record.cause))
    return [keys, data, enabled, authorization]
