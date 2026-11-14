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


def _ready(config_path: Path) -> dict[str, str]:
    """Create data and token folders; return an environment with the credential names set."""
    cfg = config_module.load(config_path)
    (cfg.root / cfg.run.data_dir / cfg.environment.token_dir).mkdir(parents=True)
    env = cfg.environment
    return {env.app_key_variable: CANARY, env.app_secret_variable: CANARY,
            env.account_variable: CANARY}


def _run(config_path: Path, environ: dict[str, str], now: datetime = MONDAY, free: int = PLENTY):
    return run_preflight(config_path, now=now, environ=environ, disk_free=lambda _: free)


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
    name = sorted(environ)[0]
    environ[name] = ""
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.CREDENTIALS_MISSING
    assert name in result.summary()


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
    assert failed == ["disk", "credentials", "token-dir"]


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


def test_bad_schedule_is_invalid_config(config_path):
    text = config_path.read_text("utf-8").replace('bot_start_local = "09:00"',
                                                  'bot_start_local = "08:00"')
    config_path.write_text(text, "utf-8")
    result = _run(config_path, {})
    assert result.cause is PreflightCause.CONFIG_INVALID
    assert "before bot_start_local" in result.summary()
