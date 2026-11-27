from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tickr import config as config_module
from tickr.runtime.preflight import (
    BYTES_PER_MB,
    ExitCode,
    PreflightCause,
    run_preflight,
)
from tickr.store import DATABASE_FILE, Store
from tickr.version import ENV_VARIABLE

MONDAY = datetime(2026, 11, 16, 13, 30, tzinfo=UTC)  # 08:30 ET, a normal trading day
SATURDAY = datetime(2026, 11, 14, 13, 30, tzinfo=UTC)
THANKSGIVING = datetime(2026, 11, 26, 13, 30, tzinfo=UTC)
HALF_DAY = datetime(2026, 11, 27, 13, 30, tzinfo=UTC)
NEXT_YEAR = datetime(2028, 1, 4, 13, 30, tzinfo=UTC)  # past the last calendar file
CANARY = "canary-secret-value-7f3a"
PLENTY = 10**6 * BYTES_PER_MB
MAC = "darwin"  # the owner's Mac
LINUX = "linux"  # a server or container
SHA = "0123456789abcdef0123456789abcdef01234567"  # the image's code version (no .git here)


def _ready(config_path: Path) -> dict[str, str]:
    """Create data and token folders; return an environment with the credential names set."""
    cfg = config_module.load(config_path)
    (cfg.root / cfg.run.data_dir / cfg.environment.token_dir).mkdir(parents=True, mode=0o700)
    env = cfg.environment
    return {env.app_key_variable: CANARY, env.app_secret_variable: CANARY,
            env.account_variable: CANARY, cfg.alerts.bot_token_variable: CANARY,
            cfg.alerts.chat_id_variable: CANARY, ENV_VARIABLE: SHA}


def _run(config_path: Path, environ: dict[str, str], now: datetime = MONDAY, free: int = PLENTY,
         ):
    return run_preflight(config_path, now=now, environ=environ, disk_free=lambda _: free)


def test_ready_on_a_trading_day(config_path):
    result = _run(config_path, _ready(config_path))
    assert result.exit_code is ExitCode.READY and result.cause is None
    assert result.session_today and all(c.passed for c in result.checks)


def test_ready_reports_the_code_version(config_path):
    result = _run(config_path, _ready(config_path))
    (check,) = [c for c in result.checks if c.name.value == "code-version"]
    assert check.passed and check.detail == SHA


def test_unknown_code_version_fails_and_blocks_the_start(config_path):
    environ = _ready(config_path)
    del environ[ENV_VARIABLE]
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.CODE_VERSION_UNKNOWN
    assert result.exit_code is ExitCode.FAILED and result.blocks_start
    assert ENV_VARIABLE in result.summary()


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
    assert "~/.config/tickr/env" in result.summary()  # says where to add it


def test_no_alert_channel_refuses_with_its_own_cause(config_path):
    calls: list[str] = []
    alerts = config_module.load(config_path).alerts
    environ = _ready(config_path)
    del environ[alerts.bot_token_variable], environ[alerts.chat_id_variable]
    result = run_preflight(config_path, now=MONDAY, environ=environ,
                           disk_free=lambda _: PLENTY, token_probe=_probe(None, calls))
    assert result.cause is PreflightCause.ALERT_CHANNEL_MISSING
    assert result.exit_code is ExitCode.FAILED and result.blocks_start
    assert "alert-channel" in result.summary() and "Telegram" in result.summary()
    assert alerts.bot_token_variable in result.summary()
    assert calls == []  # a blocked start never reaches the token probe


@pytest.mark.parametrize("platform", [MAC, LINUX, "win32"])
def test_preflight_refuses_without_telegram_on_any_platform(config_path, monkeypatch, platform):
    """No channel depends on the operating system: Telegram alone is the alert channel."""
    monkeypatch.setattr("sys.platform", platform)
    alerts = config_module.load(config_path).alerts
    environ = _ready(config_path)
    ready = _run(config_path, environ)
    del environ[alerts.bot_token_variable], environ[alerts.chat_id_variable]
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.ALERT_CHANNEL_MISSING and result.blocks_start
    assert ready.cause is None


def test_missing_token_dir(config_path):
    environ = _ready(config_path)
    cfg = config_module.load(config_path)
    (cfg.root / cfg.run.data_dir / cfg.environment.token_dir).rmdir()
    assert _run(config_path, environ).cause is PreflightCause.TOKEN_DIR_MISSING


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755])
def test_token_dir_open_to_other_accounts_is_refused(config_path, mode):
    environ = _ready(config_path)
    cfg = config_module.load(config_path)
    token_dir = cfg.root / cfg.run.data_dir / cfg.environment.token_dir
    token_dir.chmod(mode)
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.TOKEN_DIR_OPEN
    (check,) = [c for c in result.checks if c.name.value == "token-dir"]
    assert f"chmod 700 {token_dir}" in check.detail


def test_token_dir_that_is_a_file_is_refused(config_path):
    environ = _ready(config_path)
    cfg = config_module.load(config_path)
    token_dir = cfg.root / cfg.run.data_dir / cfg.environment.token_dir
    token_dir.rmdir()
    token_dir.write_text("")
    assert _run(config_path, environ).cause is PreflightCause.TOKEN_DIR_MISSING


def test_low_disk(config_path):
    environ = _ready(config_path)
    need = config_module.load(config_path).runtime.min_free_disk_mb
    result = _run(config_path, environ, free=(need - 1) * BYTES_PER_MB)
    assert result.cause is PreflightCause.DISK_LOW


def test_missing_data_dir_is_first_cause_and_all_checks_reported(config_path):
    result = _run(config_path, {ENV_VARIABLE: SHA})
    assert result.cause is PreflightCause.DATA_DIR_MISSING
    failed = [c.name.value for c in result.checks if not c.passed]
    assert failed == ["disk", "credentials", "alert-channel", "token-dir"]


def test_invalid_config(config_path):
    config_path.write_text("[run]\n", "utf-8")
    result = _run(config_path, {})
    assert result.cause is PreflightCause.CONFIG_INVALID
    assert result.exit_code is ExitCode.CONFIG_INVALID


def test_uncommented_key_is_invalid_config(config_path):
    text = re.sub(r"(?m)^# Minimum free disk.*\n(?:#.*\n)*", "", config_path.read_text("utf-8"))
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
    active = config_module.load(config_path).strategy.active
    strategy = repo / "strategies" / active / "strategy.toml"
    strategy.write_text(strategy.read_text("utf-8").replace(
        "relative_volume_sessions = 5", "relative_volume_sessions = -5"), "utf-8")
    assert _run(config_path, _ready(config_path)).cause is PreflightCause.STRATEGY_REFUSED


def test_other_failures_and_no_session_block_start(config_path):
    assert _run(config_path, {}).blocks_start
    environ = _ready(config_path)
    assert not _run(config_path, environ).blocks_start
    assert _run(config_path, environ, now=SATURDAY).blocks_start


def test_bad_schedule_is_invalid_config(config_path):
    text = config_path.read_text("utf-8").replace('bot_start_local = "04:00"',
                                                  'bot_start_local = "03:00"')
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
                           disk_free=lambda _: PLENTY,
                           token_probe=_probe(None, calls))
    assert result.exit_code is ExitCode.READY and len(calls) == 1
    assert result.checks[-1].name.value == "token" and result.checks[-1].passed


def test_unusable_token_fails_with_its_cause(config_path):
    cause = "token initialisation failed: token check failed or token INVALID/EXPIRED"
    result = run_preflight(config_path, now=MONDAY, environ=_ready(config_path),
                           disk_free=lambda _: PLENTY,
                           token_probe=_probe(cause, []))
    assert result.exit_code is ExitCode.FAILED
    assert result.cause is PreflightCause.TOKEN_UNUSABLE
    assert result.summary() == f"token: {cause}"


@pytest.mark.parametrize("now", [SATURDAY, MONDAY])
def test_token_is_not_probed_without_a_session_or_after_a_failure(config_path, now):
    calls: list[str] = []
    environ = _ready(config_path) if now is SATURDAY else {}  # MONDAY: credentials missing
    result = run_preflight(config_path, now=now, environ=environ,
                           disk_free=lambda _: PLENTY,
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
                           disk_free=lambda _: PLENTY,
                           token_probe=_probe(None, calls))
    assert result.cause is PreflightCause.STRATEGY_REFUSED and not result.blocks_start
    assert len(calls) == 1 and result.checks[-1].name.value == "token"


def test_operator_stop_marker_blocks_the_start_until_cleared(config_path):
    from tickr.runtime import stopmarker

    environ = _ready(config_path)
    cfg = config_module.load(config_path)
    stopmarker.write(cfg, "3f2c9a7e-5b1d-4c8e-9a0f-1e2d3c4b5a69", MONDAY)
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.STOPPED_BY_OPERATOR
    assert result.exit_code is ExitCode.FAILED and result.blocks_start
    assert "clear-stop" in result.summary()
    assert stopmarker.clear(cfg)
    assert _run(config_path, environ).exit_code is ExitCode.READY


def _database(config_path: Path, environment: str) -> Path:
    cfg = config_module.load(config_path)
    path = cfg.root / cfg.run.data_dir / DATABASE_FILE
    store = Store.open(path, busy_timeout_seconds=0)
    store.start_run(environment=environment, config_digest="d", strategy="s",
                    strategy_version="1", code_version="x")
    store.close()
    return path


def test_a_database_of_this_environment_passes_and_is_not_written(config_path):
    environ = _ready(config_path)
    path = _database(config_path, config_module.load(config_path).environment_name)
    before = path.read_bytes()
    result = _run(config_path, environ)
    assert result.exit_code is ExitCode.READY
    assert path.read_bytes() == before


def test_a_database_of_another_environment_blocks_the_start_with_its_own_cause(config_path):
    environ = _ready(config_path)
    _database(config_path, "another")
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.DATABASE_OTHER_ENVIRONMENT and result.blocks_start
    assert "move the database aside" in result.summary()


def test_an_unreadable_database_blocks_the_start(config_path):
    environ = _ready(config_path)
    path = _database(config_path, "another")
    path.write_bytes(b"not a database")
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.DATABASE_UNREADABLE and result.blocks_start


def test_an_empty_database_file_passes_and_a_newer_schema_blocks(config_path):
    environ = _ready(config_path)
    path = _database(config_path, config_module.load(config_path).environment_name)
    path.write_bytes(b"")
    assert _run(config_path, environ).exit_code is ExitCode.READY
    path.unlink()
    path = _database(config_path, config_module.load(config_path).environment_name)
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 9999")
    db.commit()
    db.close()
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.DATABASE_UNREADABLE and "schema 9999" in result.summary()


def test_old_config_key_name_is_reported_in_the_journal(config_path):
    """An old key name loads (config.RENAMED_KEYS) and preflight's journal row names it, so the
    owner sees it under launchd too, not only as a Python warning on stderr."""
    environ = _ready(config_path)
    text = re.sub(r"(?m)^check_decimal_places = ", "watching_check_decimal_places = ",
                  config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    with pytest.warns(config_module.RenamedKeyWarning):
        result = _run(config_path, environ)
    assert result.exit_code is ExitCode.READY
    body = json.dumps(result.journal_body())
    assert "[api] watching_check_decimal_places -> check_decimal_places" in body


def test_telegram_check_exists_only_once_set_up_and_checks_presence_only(config_path):
    cfg = config_module.load(config_path).alerts
    environ = _ready(config_path)
    names = {c.name.value for c in _run(config_path, environ).checks}
    assert "alert-telegram" in names and "alert-channel" in names
    del environ[cfg.bot_token_variable], environ[cfg.chat_id_variable]
    names = {c.name.value for c in _run(config_path, environ).checks}
    assert "alert-telegram" not in names  # off: neither variable set (alert-channel refuses)
    environ[cfg.bot_token_variable] = CANARY
    refused = _run(config_path, environ)
    assert refused.cause is PreflightCause.ALERT_TELEGRAM_MISSING
    assert cfg.chat_id_variable in refused.summary() and CANARY not in refused.summary()
    environ[cfg.chat_id_variable] = CANARY
    ok = _run(config_path, environ)
    assert ok.cause is None and CANARY not in ok.summary()


def _p8(path: Path) -> Path:
    """A P-256 private key in PEM, as Apple's AuthKey_<key id>.p8 holds one."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return path


def _apns_only(config_path: Path, key: Path) -> dict[str, str]:
    """Ready credentials and the three APNs variables; no Telegram."""
    cfg = config_module.load(config_path).alerts
    environ = _ready(config_path)
    del environ[cfg.bot_token_variable], environ[cfg.chat_id_variable]
    environ[cfg.apns_key_id_variable] = CANARY
    environ[cfg.apns_team_id_variable] = CANARY
    environ[cfg.apns_key_path_variable] = str(key)
    return environ


def test_push_check_exists_only_once_set_up_and_refuses_a_half_setup(config_path, tmp_path):
    cfg = config_module.load(config_path).alerts
    environ = _ready(config_path)
    names = {c.name.value for c in _run(config_path, environ).checks}
    assert "alert-push" not in names  # off: no APNs variable set
    environ[cfg.apns_key_id_variable] = CANARY
    refused = _run(config_path, environ)
    assert refused.cause is PreflightCause.ALERT_PUSH_MISSING
    summary = refused.summary()
    assert cfg.apns_team_id_variable in summary and cfg.apns_key_path_variable in summary
    assert CANARY not in summary
    environ[cfg.apns_team_id_variable] = CANARY
    environ[cfg.apns_key_path_variable] = str(tmp_path / "no-such-key.p8")
    missing_key = _run(config_path, environ)
    assert missing_key.cause is PreflightCause.ALERT_PUSH_KEY_UNREADABLE
    assert "no-such-key" not in missing_key.summary()  # the variable is named, not its value
    environ[cfg.apns_key_path_variable] = str(_p8(tmp_path / "AuthKey.p8"))
    ok = _run(config_path, environ)
    assert ok.cause is None and CANARY not in ok.summary()


@pytest.mark.parametrize("content", [b"x", b"-----BEGIN PRIVATE KEY-----\nAAAA\n"])
def test_push_check_loads_the_key_so_a_wrong_file_refuses_the_start(config_path, tmp_path,
                                                                    content):
    key = tmp_path / "AuthKey.p8"
    key.write_bytes(content)
    environ = _ready(config_path)  # Telegram set up, every other check passing
    cfg = config_module.load(config_path).alerts
    environ[cfg.apns_key_id_variable] = CANARY
    environ[cfg.apns_team_id_variable] = CANARY
    environ[cfg.apns_key_path_variable] = str(key)
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.ALERT_PUSH_KEY_INVALID and result.blocks_start
    push = next(c for c in result.checks if c.name.value == "alert-push")
    assert not push.passed and "P-256" in push.detail
    assert "BEGIN" not in result.summary() and "AAAA" not in result.summary()  # no content


@pytest.mark.parametrize("platform", [MAC, LINUX])
def test_push_is_never_the_alert_channel(config_path, tmp_path, monkeypatch, platform):
    """Fable's case on #828: no Telegram, only the three APNs variables, a garbage key file
    and no registered device. Push reaches nobody, so the start is refused."""
    monkeypatch.setattr("sys.platform", platform)
    garbage = tmp_path / "AuthKey.p8"
    garbage.write_text("x", "utf-8")
    environ = _apns_only(config_path, garbage)
    result = _run(config_path, environ)
    assert result.cause is PreflightCause.ALERT_CHANNEL_MISSING and result.blocks_start
    failed = {c.name.value for c in result.checks if not c.passed}
    assert {"alert-channel", "alert-push"} <= failed
    # A good key and still no Telegram: push alone is never enough either.
    cfg = config_module.load(config_path).alerts
    environ[cfg.apns_key_path_variable] = str(_p8(tmp_path / "good.p8"))
    good = _run(config_path, environ)
    assert good.cause is PreflightCause.ALERT_CHANNEL_MISSING
    assert {c.name.value for c in good.checks if not c.passed} == {"alert-channel"}
    # Telegram is what lets the bot start; push then rides along.
    environ[cfg.bot_token_variable] = CANARY
    environ[cfg.chat_id_variable] = CANARY
    assert _run(config_path, environ).cause is None
