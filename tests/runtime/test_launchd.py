from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

from aperture import config as config_module
from aperture.runtime import launchd

PATHS = launchd.Paths(repo=Path("/Users/o/Aperture"),
                      python=Path("/Users/o/Aperture/.venv/bin/python"),
                      config=Path("/Users/o/Aperture/config/bot.toml"),
                      env_file=Path("/Users/o/.config/aperture/env & more"),
                      log_dir=Path("/Users/o/Library/Logs/Aperture"))


def _render(config_path: Path, repo: Path, out: Path) -> dict[str, dict]:
    cfg = config_module.load(config_path)
    written = launchd.render_all(repo / "ops" / "launchd", out,
                                 launchd.values(cfg.runtime, PATHS))
    return {p.name: plistlib.loads(p.read_bytes()) for p in written}


def test_templates_render_to_valid_plists(config_path, repo, tmp_path):
    jobs = _render(config_path, repo, tmp_path / "out")
    assert set(jobs) == {"local.aperture.preflight.plist", "local.aperture.bot.plist",
                         "local.aperture.api.plist", "local.aperture.watchdog.plist",
                         "local.aperture.test-alert.plist"}
    for job in jobs.values():
        assert job["WorkingDirectory"] == str(PATHS.repo)


def test_bot_is_never_relaunched_and_gets_exit_time(config_path, repo, tmp_path):
    cfg = config_module.load(config_path)
    bot = _render(config_path, repo, tmp_path / "out")["local.aperture.bot.plist"]
    assert bot["KeepAlive"] is False and bot["RunAtLoad"] is False
    assert bot["ExitTimeOut"] == cfg.runtime.bot_exit_timeout_seconds
    hour, minute = map(int, cfg.runtime.bot_start_local.split(":"))
    assert bot["StartCalendarInterval"] == [
        {"Weekday": d, "Hour": hour, "Minute": minute} for d in cfg.runtime.launch_weekdays]
    script = bot["ProgramArguments"][2]
    assert "run-bot" in script and bot["ProgramArguments"][3] == str(PATHS.env_file)


def test_api_always_on_and_preflight_scheduled(config_path, repo, tmp_path):
    jobs = _render(config_path, repo, tmp_path / "out")
    api = jobs["local.aperture.api.plist"]
    assert api["KeepAlive"] is True and api["RunAtLoad"] is True
    assert "ENV_FILE" not in str(api) and str(PATHS.env_file) not in str(api)
    preflight = jobs["local.aperture.preflight.plist"]
    assert preflight["KeepAlive"] is False
    assert len(preflight["StartCalendarInterval"]) == len(
        config_module.load(config_path).runtime.launch_weekdays)


def test_templates_contain_no_numbers_or_paths_of_their_own(repo):
    for template in (repo / "ops" / "launchd").glob("*.plist.template"):
        body = re.sub(r"<!--.*?-->", "", template.read_text("utf-8"), flags=re.S)
        body = body.split("<plist", 1)[1]
        assert not re.search(r"<integer>\d", body), template.name
        assert "/Users/" not in body


def test_unknown_placeholder_refused():
    with pytest.raises(config_module.ConfigError, match="NOPE"):
        launchd.render_text("{{NOPE}}", {"REPO": "x"})


def test_relative_path_refused(config_path):
    cfg = config_module.load(config_path)
    bad = launchd.Paths(**{**vars(PATHS), "log_dir": Path("logs")})
    with pytest.raises(config_module.ConfigError, match="absolute"):
        launchd.values(cfg.runtime, bad)


@pytest.mark.parametrize("key,value,match", [
    ("preflight_start_local", '"8:30"', "HH:MM"),
    ("bot_start_local", '"24:00"', "time of day"),
    ("launch_weekdays", "[1, 1]", "distinct"),
    ("launch_weekdays", "[8]", "distinct"),
    ("launch_weekdays", "[]", "distinct"),
    ("bot_exit_timeout_seconds", "0", "positive"),
    ("stop_alert_after_seconds", "120", "below bot_exit_timeout_seconds"),
    ("stop_alert_after_seconds", "0", "below bot_exit_timeout_seconds"),
    ("supervisor_poll_seconds", "0", "positive"),
    ("min_free_disk_mb", "0", "positive"),
])
def test_bad_runtime_values_refused(config_path, key, value, match):
    text = re.sub(rf"(?m)^{key} = .*$", f"{key} = {value}", config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    cfg = config_module.load(config_path)
    with pytest.raises(config_module.ConfigError, match=match):
        launchd.schedule(cfg.runtime)


@pytest.mark.parametrize("drain,match", [
    ("90", "shutdown_deadline_seconds"),    # equal to stop_alert_after_seconds
    ("100", "shutdown_deadline_seconds"),   # above it
    ("0", "shutdown_deadline_seconds"),
])
def test_engine_drain_must_end_before_supervisor_alert(config_path, drain, match):
    text = re.sub(r"(?m)^shutdown_deadline_seconds = .*$", f"shutdown_deadline_seconds = {drain}",
                  config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    cfg = config_module.load(config_path)
    with pytest.raises(config_module.ConfigError, match=match):
        launchd.check_timings(cfg)


def test_repository_deadlines_are_ordered(config_path):
    cfg = config_module.load(config_path)
    launchd.check_timings(cfg)
    assert (0 < cfg.orders.shutdown_deadline_seconds < cfg.runtime.stop_alert_after_seconds
            < cfg.runtime.bot_exit_timeout_seconds)


def test_flatten_grace_must_cover_a_reconcile_pass(config_path):
    text = re.sub(r"(?m)^flatten_grace_seconds = .*$", "flatten_grace_seconds = 10",
                  config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    cfg = config_module.load(config_path)
    with pytest.raises(config_module.ConfigError, match="flatten_grace_seconds"):
        launchd.check_timings(cfg)


def test_watchdog_job_scheduled_and_both_long_jobs_keep_the_mac_awake(config_path, repo,
                                                                      tmp_path):
    cfg = config_module.load(config_path)
    jobs = _render(config_path, repo, tmp_path / "out")
    dog = jobs["local.aperture.watchdog.plist"]
    assert dog["KeepAlive"] is False and dog["RunAtLoad"] is False
    hour, minute = map(int, cfg.runtime.watchdog_start_local.split(":"))
    assert dog["StartCalendarInterval"] == [
        {"Weekday": d, "Hour": hour, "Minute": minute} for d in cfg.runtime.launch_weekdays]
    assert dog["ProgramArguments"][2].endswith('--config "$2" watchdog')
    assert '--config "$2" run-bot' in jobs["local.aperture.bot.plist"]["ProgramArguments"][2]
    for name in ("local.aperture.bot.plist", "local.aperture.watchdog.plist"):
        script = jobs[name]["ProgramArguments"][2]
        # A side process waiting on the job's own pid; the job's process is still exec'd, so
        # launchd's SIGTERM reaches the supervisor directly.
        assert '/usr/bin/caffeinate -i -s -w $$ >/dev/null 2>&1 & exec "$1"' in script
    assert "caffeinate" not in str(jobs["local.aperture.preflight.plist"])
    test = jobs["local.aperture.test-alert.plist"]  # runs only on a manual kickstart
    assert test["RunAtLoad"] is False and test["KeepAlive"] is False
    assert "StartCalendarInterval" not in test
    assert test["ProgramArguments"][:2] == dog["ProgramArguments"][:2]  # same program chain


@pytest.mark.parametrize("key,value", [
    ("watchdog_after_flatten_seconds", "120"),   # not above flatten_grace_seconds
    ("watchdog_after_flatten_seconds", "260"),   # + before_close reaches the close
    ("watchdog_before_close_seconds", "0"),
    ("watchdog_status_stale_seconds", "10"),     # not above reconcile_interval_seconds
    ("watchdog_status_stale_seconds", "3"),      # not above detail_poll_seconds
])
def test_watchdog_checks_must_fit_between_day_end_check_and_close(config_path, key, value):
    text = re.sub(rf"(?m)^{key} = .*$", f"{key} = {value}", config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    with pytest.raises(config_module.ConfigError, match="watchdog_"):
        launchd.check_timings(config_module.load(config_path))


def test_bad_watchdog_start_refused(config_path):
    text = re.sub(r"(?m)^watchdog_start_local = .*$", 'watchdog_start_local = "9:05"',
                  config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    with pytest.raises(config_module.ConfigError, match="watchdog_start_local"):
        launchd.schedule(config_module.load(config_path).runtime)
