from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tickr import config as config_module
from tickr.runtime import launchd

PATHS = launchd.Paths(release=Path("/Users/o/Tickr-releases/live"),
                      config=Path("config/bot.toml"),
                      env_file=Path("/Users/o/.config/tickr/env & more"),
                      log_dir=Path("/Users/o/Library/Logs/Tickr"))


def _render(config_path: Path, repo: Path, out: Path) -> dict[str, dict]:
    cfg = config_module.load(config_path)
    written = launchd.render_all(repo / "ops" / "launchd", out,
                                 launchd.values(cfg.runtime, PATHS,
                                                launchd.api_credential_variables(cfg)))
    return {p.name: plistlib.loads(p.read_bytes()) for p in written}


def test_templates_render_to_valid_plists(config_path, repo, tmp_path):
    jobs = _render(config_path, repo, tmp_path / "out")
    assert set(jobs) == {"local.tickr.preflight.plist", "local.tickr.bot.plist",
                         "local.tickr.api.plist", "local.tickr.watchdog.plist",
                         "local.tickr.test-alert.plist", "local.tickr.nightly-fetch.plist"}
    for job in jobs.values():
        assert job["WorkingDirectory"] == str(PATHS.release)
        # Every job resolves the release link once at its start and runs the interpreter and
        # config inside the folder it resolved to: a later switch never changes its code.
        args = job["ProgramArguments"]
        assert args[:2] == ["/bin/sh", "-c"]
        script = args[2]
        first = 1 if 'cd -P "$1"' in script else 0  # $0 is the env file when one is sourced
        assert f'cd -P "${first}" || exit;' in script
        assert args[3 + first:6 + first] == [str(PATHS.release), ".venv/bin/python",
                                             "config/bot.toml"]
        assert f'exec "$PWD/${first + 1}"' in script.replace("exec nice", "exec")
        assert f'--config "$PWD/${first + 2}"' in script


def test_bot_is_never_relaunched_and_gets_exit_time(config_path, repo, tmp_path):
    cfg = config_module.load(config_path)
    bot = _render(config_path, repo, tmp_path / "out")["local.tickr.bot.plist"]
    assert bot["KeepAlive"] is False and bot["RunAtLoad"] is False
    assert bot["ExitTimeOut"] == cfg.runtime.bot_exit_timeout_seconds
    hour, minute = map(int, cfg.runtime.bot_start_local.split(":"))
    assert bot["StartCalendarInterval"] == [
        {"Weekday": d, "Hour": hour, "Minute": minute} for d in cfg.runtime.launch_weekdays]
    script = bot["ProgramArguments"][2]
    assert "run-bot" in script and bot["ProgramArguments"][3] == str(PATHS.env_file)


def test_api_always_on_and_preflight_scheduled(config_path, repo, tmp_path):
    jobs = _render(config_path, repo, tmp_path / "out")
    api = jobs["local.tickr.api.plist"]
    assert api["KeepAlive"] is True and api["RunAtLoad"] is True
    # The env file is never a job argument of the API: at most the two chart variables are
    # read from it inside the shell command (test_api_job_environment_* below).
    assert str(PATHS.env_file) not in api["ProgramArguments"][3:]
    preflight = jobs["local.tickr.preflight.plist"]
    assert preflight["KeepAlive"] is False
    assert len(preflight["StartCalendarInterval"]) == len(
        config_module.load(config_path).runtime.launch_weekdays)


def test_nightly_fetch_runs_after_the_close_with_its_folders(config_path, repo, tmp_path):
    cfg = config_module.load(config_path)
    job = _render(config_path, repo, tmp_path / "out")["local.tickr.nightly-fetch.plist"]
    assert job["KeepAlive"] is False and job["RunAtLoad"] is False
    hour, minute = map(int, cfg.runtime.nightly_fetch_start_local.split(":"))
    assert job["StartCalendarInterval"] == [
        {"Weekday": d, "Hour": hour, "Minute": minute} for d in cfg.runtime.launch_weekdays]
    assert "tickr.backtest" in job["ProgramArguments"][2]
    assert job["ProgramArguments"][-2:] == [cfg.runtime.nightly_fetch_out,
                                            cfg.runtime.nightly_fetch_seed]


def test_lab_must_replay_the_nightly_folder(config_path):
    from dataclasses import replace

    cfg = config_module.load(config_path)
    launchd.check_lab_folder(cfg)
    other = replace(cfg, api=replace(cfg.api, lab_replay_data_dir="backtest"))
    with pytest.raises(config_module.ConfigError, match="lab_replay_data_dir"):
        launchd.check_lab_folder(other)
    launchd.check_timings(other)  # the bot's preflight never refuses over a Lab setting


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
    for name in ("log_dir", "release"):
        bad = launchd.Paths(**{**vars(PATHS), name: Path("logs")})
        with pytest.raises(config_module.ConfigError, match="absolute"):
            launchd.values(cfg.runtime, bad, ())


@pytest.mark.parametrize("config", ["/Users/o/Tickr/config/bot.toml", "../config/bot.toml"])
def test_config_outside_the_release_refused(config_path, config):
    cfg = config_module.load(config_path)
    bad = launchd.Paths(**{**vars(PATHS), "config": Path(config)})
    with pytest.raises(config_module.ConfigError, match="relative to the release"):
        launchd.values(cfg.runtime, bad, ())


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


@pytest.mark.parametrize("value", ["600", "-1"])
def test_close_order_cutoff_must_leave_the_flatten_its_close(config_path, value):
    text = re.sub(r"(?m)^close_order_cutoff_seconds = .*$",
                  f"close_order_cutoff_seconds = {value}", config_path.read_text("utf-8"))
    config_path.write_text(text, "utf-8")
    cfg = config_module.load(config_path)
    with pytest.raises(config_module.ConfigError, match="close_order_cutoff_seconds"):
        launchd.check_timings(cfg)


def test_watchdog_job_scheduled_and_both_long_jobs_keep_the_mac_awake(config_path, repo,
                                                                      tmp_path):
    cfg = config_module.load(config_path)
    jobs = _render(config_path, repo, tmp_path / "out")
    dog = jobs["local.tickr.watchdog.plist"]
    assert dog["KeepAlive"] is False and dog["RunAtLoad"] is False
    hour, minute = map(int, cfg.runtime.watchdog_start_local.split(":"))
    assert dog["StartCalendarInterval"] == [
        {"Weekday": d, "Hour": hour, "Minute": minute} for d in cfg.runtime.launch_weekdays]
    assert dog["ProgramArguments"][2].endswith('--config "$PWD/$3" watchdog')
    assert '--config "$PWD/$3" run-bot' in jobs["local.tickr.bot.plist"]["ProgramArguments"][2]
    for name in ("local.tickr.bot.plist", "local.tickr.watchdog.plist"):
        script = jobs[name]["ProgramArguments"][2]
        # A side process waiting on the job's own pid; the job's process is still exec'd, so
        # launchd's SIGTERM reaches the supervisor directly.
        assert '/usr/bin/caffeinate -i -s -w $$ >/dev/null 2>&1 & exec "$PWD/$2"' in script
    assert "caffeinate" not in str(jobs["local.tickr.preflight.plist"])
    test = jobs["local.tickr.test-alert.plist"]  # runs only on a manual kickstart
    assert test["RunAtLoad"] is False and test["KeepAlive"] is False
    assert "StartCalendarInterval" not in test
    assert test["ProgramArguments"][:2] == dog["ProgramArguments"][:2]  # same program chain


@pytest.mark.parametrize("key,value", [
    ("watchdog_after_flatten_seconds", "120"),   # not above flatten_grace_seconds
    ("watchdog_after_flatten_seconds", "550"),   # + before_close reaches the close
    ("watchdog_before_close_seconds", "0"),
    ("watchdog_status_stale_seconds", "10"),     # not above reconcile_interval_seconds
    ("watchdog_status_stale_seconds", "3"),      # not above detail_poll_seconds
    ("watchdog_restart_before_flatten_seconds", "0"),
    ("watchdog_restart_before_flatten_seconds", "600"),  # reaches the last entry time
    ("watchdog_restart_command", "[]"),
    ("watchdog_restart_timeout_seconds", "0"),
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


# --- what the API job's environment holds (charts on: the two chart variables, else none) -------

# Names of a fake env file that are never the active environment's app key or secret: every
# other credential the config names, plus unrelated lines.
_OTHER_LINES = ("TICKR_REMOTE_TOKEN", "SOME_OTHER_SECRET", "PLAIN_SETTING")
_FAKE_PYTHON = '''#!{python}
import json
import os

names = sorted(os.environ)
with open(os.path.join(os.getcwd(), "seen.json"), "w", encoding="utf-8") as out:
    json.dump({{"names": names, "values": {{n: os.environ.get(n) for n in {wanted!r}}}}}, out)
'''


def _with_charts(cfg: config_module.Config, enabled: bool) -> config_module.Config:
    from dataclasses import replace

    return replace(cfg, chart=replace(cfg.chart, webull_enabled=enabled))


def _fake_env_file(folder: Path, cfg: config_module.Config) -> tuple[Path, set[str]]:
    """An env file with a dummy value for every credential the config names (every
    environment), unrelated lines, an exported line and output of its own. No real secret."""
    names = set(config_module.credential_variables(cfg)) | set(_OTHER_LINES)
    lines = ["echo 'env file noise on stdout'", "echo 'env file noise on stderr' >&2"]
    for index, name in enumerate(sorted(names)):
        prefix = "export " if index % 2 else ""
        lines.append(f"{prefix}{name}='dummy-{name.lower()}'")
    env_file = folder / "env 'quoted' & more"  # a path the shell must quote
    env_file.write_text("\n".join(lines) + "\n", "utf-8")
    env_file.chmod(0o600)
    return env_file, names


def _run_api_job(cfg: config_module.Config, repo: Path, tmp_path: Path) -> dict:
    """Render the plists for `cfg`, then run the API job's command exactly as launchd would,
    from a minimal environment, with a fake interpreter that records which variable NAMES it
    was started with (and the values of the two chart variables only, all dummies)."""
    release = tmp_path / "release"
    (release / ".venv" / "bin").mkdir(parents=True)
    env_file, _ = _fake_env_file(tmp_path, cfg)
    env = cfg.environment
    wanted = (env.app_key_variable, env.app_secret_variable)
    python = release / ".venv" / "bin" / "python"
    python.write_text(_FAKE_PYTHON.format(python=sys.executable, wanted=wanted), "utf-8")
    python.chmod(0o755)
    paths = launchd.Paths(release=release, config=Path("config/bot.toml"), env_file=env_file,
                          log_dir=tmp_path / "logs")
    out = tmp_path / "out"
    launchd.render_all(repo / "ops" / "launchd", out,
                       launchd.values(cfg.runtime, paths, launchd.api_credential_variables(cfg)))
    job = plistlib.loads((out / "local.tickr.api.plist").read_bytes())
    launch_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}  # as launchd starts a job
    done = subprocess.run(job["ProgramArguments"], env=launch_env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    # The env file's own output never reaches the job's log.
    assert "noise" not in done.stdout + done.stderr
    return json.loads((release / "seen.json").read_text("utf-8"))


def test_api_job_environment_with_charts_on_holds_exactly_the_key_and_secret(config_path, repo,
                                                                            tmp_path):
    cfg = _with_charts(config_module.load(config_path), True)
    env = cfg.environment
    on = _run_api_job(cfg, repo, tmp_path / "on")
    off = _run_api_job(_with_charts(cfg, False), repo, tmp_path / "off")
    _, file_names = _fake_env_file(tmp_path, cfg)
    wanted = {env.app_key_variable, env.app_secret_variable}
    assert set(on["names"]) & file_names == wanted
    assert set(on["names"]) - set(off["names"]) == wanted
    assert env.account_variable not in on["names"]
    assert on["values"] == {name: f"dummy-{name.lower()}" for name in wanted}


def test_api_job_environment_with_charts_off_holds_no_env_file_variable(config_path, repo,
                                                                       tmp_path):
    cfg = _with_charts(config_module.load(config_path), False)
    seen = _run_api_job(cfg, repo, tmp_path)
    _, file_names = _fake_env_file(tmp_path, cfg)
    assert not set(seen["names"]) & file_names
    assert launchd.api_credential_variables(cfg) == ()


def test_api_job_with_charts_off_never_reads_the_env_file(config_path, repo, tmp_path):
    cfg = _with_charts(config_module.load(config_path), False)
    written = launchd.render_all(repo / "ops" / "launchd", tmp_path,
                                 launchd.values(cfg.runtime, PATHS, ()))
    api = next(p for p in written if p.name == "local.tickr.api.plist")
    assert str(PATHS.env_file).split()[0] not in api.read_text("utf-8")


def test_api_credential_variables_are_the_active_key_and_secret_only(config_path):
    cfg = _with_charts(config_module.load(config_path), True)
    env = cfg.environment
    names = launchd.api_credential_variables(cfg)
    assert names == (env.app_key_variable, env.app_secret_variable)
    others = config_module.credential_variables(cfg) - set(names)
    assert env.account_variable in others and not others & set(names)


@pytest.mark.parametrize("key,value,match", [
    ("app_key_variable", "KEY; touch /tmp/x", "shell variable name"),
    ("app_secret_variable", "$(id)", "shell variable name"),
    ("app_secret_variable", "", "shell variable name"),
    ("app_key_variable", "ACCOUNT_SAME", "three different names"),
])
def test_api_credential_variables_refuse_unsafe_or_shared_names(config_path, key, value, match):
    from dataclasses import replace

    cfg = _with_charts(config_module.load(config_path), True)
    env = cfg.environment
    changed = {key: value}
    if value == "ACCOUNT_SAME":
        changed = {key: env.account_variable}
    bad = replace(cfg, environments={**cfg.environments,
                                     cfg.environment_name: replace(env, **changed)})
    with pytest.raises(config_module.ConfigError, match=match):
        launchd.api_credential_variables(bad)


def test_bot_job_sources_its_own_env_file(config_path, repo, tmp_path):
    """POST /commands/start-bot kickstarts this job without any credential variable
    (api/botstart.py): safe, because the job never takes the caller's environment; its own
    shell sources the env file before anything else."""
    bot = _render(config_path, repo, tmp_path / "out")["local.tickr.bot.plist"]
    args = bot["ProgramArguments"]
    assert args[2].startswith('set -a; . "$0"; set +a;') and args[3] == str(PATHS.env_file)
    cfg = config_module.load(config_path)
    assert list(cfg.api.start_bot_command[:2]) == ["/bin/launchctl", "kickstart"]
    assert cfg.api.start_bot_command[-1].endswith(f"/{bot['Label']}")
