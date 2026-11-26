"""risk-command-used: one alert per accepted entry-stop "off" or start-bot, naming the device
(never its token), the port and the time. A refused command raises none."""

from __future__ import annotations

import pytest

from tickr import config
from tickr.runtime.alerts import Alerter

from .test_remote import (  # noqa: F401 - fixtures
    PHONE,
    local_api,
    paired,
    remote,
    remote_api,
    remote_settings,
)
from .test_server import ROOT, request
from .test_start_bot import Runner


class Recorder:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def notify(self, title: str, text: str) -> None:
        self.shown.append((title, text))


@pytest.fixture
def wired(local_api, remote_api):  # noqa: F811
    recorder = Recorder()
    cfg = config.load(ROOT / "config" / "bot.toml")
    local_api.alerter = remote_api.alerter = Alerter(cfg.alerts, "Tickr", recorder)
    local_api.start_runner = remote_api.start_runner = Runner()
    return recorder


def _only(recorder):
    ((title, text),) = recorder.shown
    assert title.endswith("risk-command-used")
    return text


def test_remote_entry_stop_off_names_the_device(wired, remote_api, remote_settings):  # noqa: F811
    token = paired(remote_api, remote_settings)
    resp, _ = remote(remote_api, "POST", "/commands/entry-stop", token=token,
                     body={"state": "off"})
    assert resp.status == 202
    text = _only(wired)
    assert f"'{PHONE}'" in text and "remote port" in text and "entry-stop off" in text
    assert token not in text and "sha256" not in text.lower()


def test_remote_start_bot_names_the_device(wired, remote_api, remote_settings):  # noqa: F811
    token = paired(remote_api, remote_settings)
    resp, _ = remote(remote_api, "POST", "/commands/start-bot", token=token, body={})
    assert resp.status == 202
    text = _only(wired)
    assert f"'{PHONE}'" in text and "start-bot" in text and token not in text


def test_local_port_names_the_device_local(wired, local_api):  # noqa: F811
    resp, _ = request(local_api, "POST", "/commands/entry-stop", {"state": "off"})
    assert resp.status == 202
    text = _only(wired)
    assert "'local'" in text and "local port" in text
    wired.shown.clear()
    resp, _ = request(local_api, "POST", "/commands/start-bot", {})
    assert resp.status == 202
    text = _only(wired)
    assert "start-bot" in text and "'local'" in text


def test_entry_stop_on_raises_no_alert(wired, local_api):  # noqa: F811
    resp, _ = request(local_api, "POST", "/commands/entry-stop", {"state": "on"})
    assert resp.status == 202 and wired.shown == []


def test_refused_commands_raise_no_alert(wired, remote_api, remote_settings):  # noqa: F811
    token = paired(remote_api, remote_settings)
    resp, _ = remote(remote_api, "POST", "/commands/entry-stop", body={"state": "off"})
    assert resp.status == 401
    resp, _ = remote(remote_api, "POST", "/commands/start-bot", token="wrong", body={})
    assert resp.status == 401
    resp, _ = remote(remote_api, "POST", "/commands/entry-stop", token=token,
                     body={"state": "maybe"})
    assert resp.status == 400
    resp, _ = remote(remote_api, "POST", "/commands/start-bot", token=token, body={"x": 1})
    assert resp.status == 400
    assert wired.shown == []
