"""The API's start check: a credential variable its launchd job must not give it is alerted
(api-credentials-unexpected), names only, and the API still serves."""

from __future__ import annotations

from dataclasses import replace

from tickr import config
from tickr.api.__main__ import check_credentials, unexpected_credentials
from tickr.runtime.alerts import AlertKind

from .test_server import ROOT


def _cfg(charts: bool) -> config.Config:
    cfg = config.load(ROOT / "config" / "bot.toml")
    return replace(cfg, chart=replace(cfg.chart, webull_enabled=charts))


class Recorder:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def notify(self, title: str, text: str) -> None:
        self.shown.append((title, text))


def _check(cfg: config.Config, environ: dict[str, str]) -> Recorder:
    recorder = Recorder()
    thread = check_credentials(cfg, environ, lambda _cfg: recorder)
    if thread is not None:
        thread.join(10)
    return recorder


def test_charts_on_with_exactly_the_key_and_secret_is_quiet(capsys):
    cfg = _cfg(True)
    env = cfg.environment
    environ = {env.app_key_variable: "dummy", env.app_secret_variable: "dummy", "PATH": "/bin"}
    assert unexpected_credentials(cfg, environ) == ()
    assert _check(cfg, environ).shown == [] and capsys.readouterr().err == ""


def test_charts_off_with_the_key_alerts_with_names_only(capsys):
    cfg = _cfg(False)
    env = cfg.environment
    environ = {env.app_key_variable: "dummy-value-1", env.app_secret_variable: "dummy-value-2"}
    recorder = _check(cfg, environ)
    expected = tuple(sorted((env.app_key_variable, env.app_secret_variable)))
    assert unexpected_credentials(cfg, environ) == expected
    ((title, text),) = recorder.shown
    assert title.endswith(AlertKind.API_CREDENTIALS_UNEXPECTED.value)
    assert all(name in text for name in expected) and "dummy-value" not in text
    err = capsys.readouterr().err
    assert AlertKind.API_CREDENTIALS_UNEXPECTED.value in err and "dummy-value" not in err


def test_charts_on_with_the_account_or_another_credential_alerts():
    cfg = _cfg(True)
    env = cfg.environment
    other = sorted(config.credential_variables(cfg)
                   - {env.app_key_variable, env.app_secret_variable})
    assert env.account_variable in other
    environ = {name: "dummy" for name in other}
    environ[env.app_key_variable] = "dummy"
    assert unexpected_credentials(cfg, environ) == tuple(other)
    assert len(_check(cfg, environ).shown) == 1


def test_an_empty_variable_is_not_held():
    cfg = _cfg(False)
    assert unexpected_credentials(cfg, {cfg.environment.account_variable: ""}) == ()
