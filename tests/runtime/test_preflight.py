from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aperture import config as config_module
from aperture.runtime.preflight import (
    BYTES_PER_MB,
    ExitCode,
    PreflightCause,
    run_preflight,
)

MONDAY = datetime(2026, 11, 16, 13, 30, tzinfo=UTC)  # 08:30 ET, a normal trading day
SATURDAY = datetime(2026, 11, 14, 13, 30, tzinfo=UTC)
THANKSGIVING = datetime(2026, 11, 26, 13, 30, tzinfo=UTC)
HALF_DAY = datetime(2026, 11, 27, 13, 30, tzinfo=UTC)
NEXT_YEAR = datetime(2028, 1, 4, 13, 30, tzinfo=UTC)  # past the last calendar file
CANARY = "canary-secret-value-7f3a"
PLENTY = 10**6 * BYTES_PER_MB
MAC = "darwin"  # the owner's machine: notification and iMessage are available
LINUX = "linux"  # a server or container: no alert channel today


def _ready(config_path: Path) -> dict[str, str]:
    """Create data and token folders; return an environment with the credential names set."""
    cfg = config_module.load(config_path)
    (cfg.root / cfg.run.data_dir / cfg.environment.token_dir).mkdir(parents=True)
    env = cfg.environment
    return {env.app_key_variable: CANARY, env.app_secret_variable: CANARY,
            env.account_variable: CANARY, cfg.alerts.imessage_recipient_variable: CANARY}


def _run(config_path: Path, environ: dict[str, str], now: datetime = MONDAY, free: int = PLENTY,
         platform: str = MAC):
    return run_preflight(config_path, now=now, environ=environ, disk_free=lambda _: free,
                         platform=platform)


def test_ready_on_a_trading_day(config_path):
    result = _run(config_path, _ready(config_path))
    assert result.exit_code is ExitCode.READY and result.cause is None
    assert result.session_today and all(c.passed for c in result.checks)


def test_secret_values_never_appear_in_result(config_path):
    environ = _ready(config_path)
    result = _run(config_path, environ)
    assert CANARY not in json.dumps(result.journal_body()) + result.summary()
    missing = _run(config_path, {next(iter(environ)): CANARY})
    assert CANARY not in json.dumps(missing.journal_body()) + missing.summary()


@pytest.mark.parametrize("now", [SATURDAY, THANKSGIVING, HALF_DAY])
def test_no_session_is_not_a_failure(config_path, now):
    result = _run(config_path, _ready(config_path), now=now)
    assert result.cause is None
    assert result.exit_code is ExitCode.NO_SESSION


def test_half_day_is_a_session_when_enabled(config_path):
    text = config_path.read_text("utf-8").replace("half_days_enabled = false",
                                                  "half_days_enabled = true")
    config_path.write_text(text, "utf-8")
    assert _run(config_path, _ready(config_path), now=HALF_DAY).exit_code is ExitCode.READY


def test_day_outside_calendar_fails(config_path):
    result = _run(config_path, _ready(config_path), now=NEXT_YEAR)
    assert result.cause is PreflightCause.CALENDAR_UNKNOWN
    assert result.exit_code is ExitCode.FAILED


def test_missing_credentials_name_the_variables(config_path):
    environ = _ready(config_path)
    name = config_module.load(config_path).environment.app_key_variable
    environ[name] = ""
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.CREDENTIALS_MISSING
    assert name in result.summary()
    assert "~/.config/aperture/env" in result.summary()  # says where to add it


def test_missing_imessage_recipient_refuses_with_its_own_cause(config_path):
    environ = _ready(config_path)
    name = config_module.load(config_path).alerts.imessage_recipient_variable
    del environ[name]
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.ALERT_RECIPIENT_MISSING
    assert result.blocks_start and name in result.summary()
    assert "Apple ID" in result.summary() and "~/.config/aperture/env" in result.summary()
    assert CANARY not in json.dumps(result.journal_body()) + result.summary()


def test_no_alert_channel_refuses_with_its_own_cause(config_path):
    calls: list[str] = []
    result = run_preflight(config_path, now=MONDAY, environ=_ready(config_path),
                           disk_free=lambda _: PLENTY, platform=LINUX,
                           token_probe=_probe(None, calls))
    assert result.cause is PreflightCause.ALERT_CHANNEL_MISSING
    assert result.exit_code is ExitCode.FAILED and result.blocks_start
    assert "alert-channel" in result.summary() and "'linux'" in result.summary()
    assert calls == []  # a blocked start never reaches the token probe


def test_no_alert_channel_is_reported_before_a_missing_recipient(config_path):
    environ = _ready(config_path)
    del environ[config_module.load(config_path).alerts.imessage_recipient_variable]
    result = _run(config_path, environ, platform=LINUX)
    assert result.cause is PreflightCause.ALERT_CHANNEL_MISSING  # the real blocker first
    failed = [c.name.value for c in result.checks if not c.passed]
    assert failed == ["alert-channel", "alert-recipient"]


def test_missing_token_dir(config_path):
    environ = _ready(config_path)
    cfg = config_module.load(config_path)
    (cfg.root / cfg.run.data_dir / cfg.environment.token_dir).rmdir()
    assert _run(config_path, environ).cause is PreflightCause.TOKEN_DIR_MISSING


def test_low_disk(config_path):
    environ = _ready(config_path)
    need = config_module.load(config_path).runtime.min_free_disk_mb
    result = _run(config_path, environ, free=(need - 1) * BYTES_PER_MB)
    assert result.cause is PreflightCause.DISK_LOW


def test_missing_data_dir_is_first_cause_and_all_checks_reported(config_path):
    result = _run(config_path, {})
    assert result.cause is PreflightCause.DATA_DIR_MISSING
    failed = [c.name.value for c in result.checks if not c.passed]
    assert failed == ["disk", "credentials", "alert-recipient", "token-dir"]


def test_invalid_config(config_path):
    config_path.write_text("[run]\n", "utf-8")
    result = _run(config_path, {})
    assert result.cause is PreflightCause.CONFIG_INVALID
    assert result.exit_code is ExitCode.CONFIG_INVALID


def test_uncommented_key_is_invalid_config(config_path):
    text = re.sub(r"(?m)^# Minimum free disk.*\n", "", config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    assert _run(config_path, {}).cause is PreflightCause.CONFIG_INVALID


def test_missing_strategy_file_is_invalid_config(config_path, repo):
    for path in (repo / "strategies").rglob("strategy.toml"):
        path.unlink()
    assert _run(config_path, {}).cause is PreflightCause.CONFIG_INVALID


def _set_history(config_path: Path, sessions: int) -> None:
    text = config_path.read_text("utf-8").replace("history_sessions = 5",
                                                  f"history_sessions = {sessions}", 1)
    config_path.write_text(text, "utf-8")


def test_history_shorter_than_the_strategy_needs_fails_but_does_not_block_start(config_path):
    _set_history(config_path, 4)
    result = _run(config_path, _ready(config_path))
    assert result.cause is PreflightCause.STRATEGY_REFUSED
    assert result.exit_code is ExitCode.FAILED
    assert "needs 5 prior sessions, [market_data] history_sessions = 4" in result.summary()
    assert not result.blocks_start  # the bot starts with entries off to protect positions


def test_unbuildable_strategy_fails_the_strategy_check(config_path, repo):
    strategy = repo / "strategies" / "price-action-v1" / "strategy.toml"
    strategy.write_text(strategy.read_text("utf-8").replace(
        "relative_volume_sessions = 5", "relative_volume_sessions = -5"), "utf-8")
    assert _run(config_path, _ready(config_path)).cause is PreflightCause.STRATEGY_REFUSED


def test_other_failures_and_no_session_block_start(config_path):
    assert _run(config_path, {}).blocks_start
    environ = _ready(config_path)
    assert not _run(config_path, environ).blocks_start
    assert _run(config_path, environ, now=SATURDAY).blocks_start


def test_bad_schedule_is_invalid_config(config_path):
    text = config_path.read_text("utf-8").replace('bot_start_local = "09:00"',
                                                  'bot_start_local = "08:00"')
    config_path.write_text(text, "utf-8")
    result = _run(config_path, {})
    assert result.cause is PreflightCause.CONFIG_INVALID
    assert "before bot_start_local" in result.summary()


def test_drain_deadline_after_supervisor_alert_is_invalid_config(config_path):
    text = re.sub(r"(?m)^shutdown_deadline_seconds = .*$", "shutdown_deadline_seconds = 95",
                  config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    result = _run(config_path, {})
    assert result.cause is PreflightCause.CONFIG_INVALID
    assert "shutdown_deadline_seconds" in result.summary()


def _probe(answer: str | None, calls: list[str]):
    def probe(cfg: config_module.Config) -> str | None:
        calls.append(cfg.environment_name)
        return answer
    return probe


def test_usable_token_is_the_last_check(config_path):
    calls: list[str] = []
    result = run_preflight(config_path, now=MONDAY, environ=_ready(config_path),
                           disk_free=lambda _: PLENTY, platform=MAC,
                           token_probe=_probe(None, calls))
    assert result.exit_code is ExitCode.READY and len(calls) == 1
    assert result.checks[-1].name.value == "token" and result.checks[-1].passed


def test_unusable_token_fails_with_its_cause(config_path):
    cause = "token initialisation failed: token check failed or token INVALID/EXPIRED"
    result = run_preflight(config_path, now=MONDAY, environ=_ready(config_path),
                           disk_free=lambda _: PLENTY, platform=MAC,
                           token_probe=_probe(cause, []))
    assert result.exit_code is ExitCode.FAILED
    assert result.cause is PreflightCause.TOKEN_UNUSABLE
    assert result.summary() == f"token: {cause}"


@pytest.mark.parametrize("now", [SATURDAY, MONDAY])
def test_token_is_not_probed_without_a_session_or_after_a_failure(config_path, now):
    calls: list[str] = []
    environ = _ready(config_path) if now is SATURDAY else {}  # MONDAY: credentials missing
    result = run_preflight(config_path, now=now, environ=environ,
                           disk_free=lambda _: PLENTY, platform=MAC,
                           token_probe=_probe(None, calls))
    assert calls == [] and "token" not in [c.name.value for c in result.checks]


@pytest.mark.parametrize("kind", ["missing", "text", "bool", "negative", "raises"])
def test_misbehaving_strategy_is_refused_never_crashes(config_path, break_strategy, kind):
    break_strategy(kind)
    result = _run(config_path, _ready(config_path))
    assert result.cause is PreflightCause.STRATEGY_REFUSED
    assert not result.blocks_start


def test_token_is_probed_when_only_the_strategy_is_refused(config_path, break_strategy):
    break_strategy("raises")
    calls: list[str] = []
    result = run_preflight(config_path, now=MONDAY, environ=_ready(config_path),
                           disk_free=lambda _: PLENTY, platform=MAC,
                           token_probe=_probe(None, calls))
    assert result.cause is PreflightCause.STRATEGY_REFUSED and not result.blocks_start
    assert len(calls) == 1 and result.checks[-1].name.value == "token"


def test_operator_stop_marker_blocks_the_start_until_cleared(config_path):
    from aperture.runtime import stopmarker

    environ = _ready(config_path)
    cfg = config_module.load(config_path)
    stopmarker.write(cfg, "3f2c9a7e-5b1d-4c8e-9a0f-1e2d3c4b5a69", MONDAY)
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.STOPPED_BY_OPERATOR
    assert result.exit_code is ExitCode.FAILED and result.blocks_start
    assert "clear-stop" in result.summary()
    assert stopmarker.clear(cfg)
    assert _run(config_path, environ).exit_code is ExitCode.READY
