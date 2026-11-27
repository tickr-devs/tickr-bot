"""Signing a device in at the remote port with a tickr.bot grant (POST /session), renewing its
tokens (POST /session/refresh) and signing it out (DELETE /session), against grants signed
offline with a test key (tests/fixtures/signin/)."""

from __future__ import annotations

import json
import shutil
import stat
from dataclasses import replace
from datetime import timedelta

import pytest

from tickr import config
from tickr.api import devices
from tickr.api.server import make_remote_server, make_server, settings_from_config
from tickr.config import ConfigError
from tickr.runtime.alerts import Alerter

from .test_remote import REMOTE, _serve, remote, with_remote_hosts
from .test_risk_alert import Recorder
from .test_server import ROOT, Clock, doc
from .test_signin import FIXTURES, GRANTS, ISSUED, RULES, _key

NAME = "Valentyn iPhone"


@pytest.fixture
def settings(tmp_path):
    cfg = with_remote_hosts(config.load(ROOT / "config" / "bot.toml", root=tmp_path), REMOTE)
    return replace(settings_from_config(cfg), port=0, remote_port=0, signin_rules=RULES,
                   signin_key=_key())


@pytest.fixture
def alerts():
    return Recorder()


@pytest.fixture
def serve(alerts):
    started = []

    def start(settings, now=ISSUED + timedelta(seconds=1), clock=None):
        local = _serve(make_server(settings, clock or Clock(now)))
        started.append(local)
        server = _serve(make_remote_server(local))
        started.append(server)
        cfg = config.load(ROOT / "config" / "bot.toml")
        local.alerter = server.alerter = Alerter(cfg.alerts, "Tickr", alerts)
        return server

    yield start
    for server in reversed(started):
        server.shutdown()
        server.server_close()


def sign_in(server, case="valid", name=NAME):
    return remote(server, "POST", "/session", body={"grant": GRANTS["tokens"][case], "name": name})


def refresh(server, token):
    return remote(server, "POST", "/session/refresh", body={"refresh_token": token})


def kinds(alerts):
    return [title.rsplit(" ", 1)[-1] for title, _ in alerts.shown]


def test_a_grant_buys_a_token_pair_that_reads_the_bot(settings, serve, alerts):
    server = serve(settings)
    resp, payload = sign_in(server)
    assert resp.status == 200, payload
    answer = doc(payload)
    lifetimes = settings.session_lifetimes
    assert answer["access_expires_in"] == lifetimes.access_seconds
    assert answer["refresh_expires_in"] == lifetimes.refresh_seconds
    token = answer["access_token"]
    assert remote(server, "GET", "/status", token=token)[0].status != 401
    assert remote(server, "GET", "/status", token=answer["refresh_token"])[0].status == 401
    listed = devices.parse(settings.devices_path.read_text("utf-8"))
    assert [d.name for d in listed] == [NAME] and listed[0].session is not None
    stored = settings.devices_path.read_text("utf-8")
    assert token not in stored and answer["refresh_token"] not in stored
    assert stat.S_IMODE(settings.devices_path.stat().st_mode) == 0o600
    ((title, text),) = alerts.shown
    assert title.endswith("device-session") and f"'{NAME}' signed in" in text
    assert token not in text and GRANTS["tokens"]["valid"] not in text


def test_a_grant_buys_one_token_only_across_a_restart(settings, serve):
    assert sign_in(serve(settings))[0].status == 200
    resp, payload = sign_in(serve(settings))  # a new server process reads the used-grant file
    assert resp.status == 401 and "used already" in doc(payload)["cause"]
    used = settings.signin_used_path.read_text("ascii")
    assert "grant-1" not in used and stat.S_IMODE(settings.signin_used_path.stat().st_mode) == 0o600


def test_signing_in_again_replaces_that_devices_token(settings, serve):
    server = serve(settings)
    first = doc(sign_in(server)[1])["access_token"]
    second = doc(sign_in(server, "valid_mac_list_audience")[1])["access_token"]
    assert remote(server, "GET", "/status", token=first)[0].status == 401
    assert remote(server, "GET", "/status", token=second)[0].status != 401


@pytest.mark.parametrize("case", ["other_key", "tampered", "other_account", "long_life"])
def test_a_refused_grant_adds_nothing_and_never_echoes_the_grant(settings, serve, case):
    resp, payload = sign_in(serve(settings), case)
    assert resp.status == 401
    assert GRANTS["tokens"][case] not in payload.decode("utf-8")
    assert not settings.devices_path.exists() and not settings.signin_used_path.exists()


def test_an_expired_grant_is_refused(settings, serve):
    resp, payload = sign_in(serve(settings, now=ISSUED + timedelta(seconds=60)))
    assert resp.status == 401 and "expired" in doc(payload)["cause"]


@pytest.mark.parametrize("body", [
    {"grant": "x"}, {"grant": "x", "name": "a/b"}, {"grant": "", "name": NAME},
    {"grant": "x", "name": NAME, "y": 1},
])
def test_a_malformed_body_is_a_bad_request(settings, serve, body):
    assert remote(serve(settings), "POST", "/session", body=body)[0].status == 400


@pytest.mark.parametrize("name", ["local", "simulator"])
def test_the_bots_own_device_names_are_refused(settings, serve, name):
    resp, payload = sign_in(serve(settings), name=name)
    assert resp.status == 400 and "reserved" in doc(payload)["cause"]
    assert not settings.devices_path.exists() and not settings.signin_used_path.exists()


def test_sign_in_is_off_without_accounts(settings, serve):
    resp, payload = sign_in(serve(replace(settings, signin_rules=None, signin_key=None)))
    assert resp.status == 401 and "off" in doc(payload)["cause"]


def test_failed_sign_ins_count_against_the_pair_budget(settings, serve):
    server = serve(replace(settings, pair_failures=2))
    assert sign_in(server, "other_key")[0].status == 401
    assert sign_in(server, "other_key")[0].status == 401
    assert sign_in(server)[0].status == 429


def test_signing_out_revokes_only_that_device(settings, serve, alerts):
    server = serve(settings)
    token = doc(sign_in(server)[1])["access_token"]
    other = doc(sign_in(server, "valid_mac_list_audience", name="Mac")[1])["access_token"]
    resp, _ = remote(server, "DELETE", "/session", token=token)
    assert resp.status == 204
    assert remote(server, "GET", "/status", token=token)[0].status == 401
    assert remote(server, "GET", "/status", token=other)[0].status != 401
    assert kinds(alerts) == ["device-session"] * 3
    assert f"'{NAME}' signed out" in alerts.shown[-1][1]


def test_signing_out_ends_the_refresh_token_too(settings, serve):
    server = serve(settings)
    answer = doc(sign_in(server)[1])
    assert remote(server, "DELETE", "/session", token=answer["access_token"])[0].status == 204
    assert refresh(server, answer["refresh_token"])[0].status == 401


def test_a_refresh_rotates_both_tokens(settings, serve):
    clock = Clock(ISSUED + timedelta(seconds=1))
    server = serve(settings, clock=clock)
    first = doc(sign_in(server)[1])
    clock.now += timedelta(seconds=settings.session_lifetimes.access_seconds)
    resp, payload = remote(server, "GET", "/status", token=first["access_token"])
    assert resp.status == 401 and "POST /session/refresh" in doc(payload)["cause"]
    resp, payload = refresh(server, first["refresh_token"])
    assert resp.status == 200, payload
    second = doc(payload)
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["refresh_token"].split(".")[0] == first["refresh_token"].split(".")[0]
    assert remote(server, "GET", "/status", token=second["access_token"])[0].status != 401
    stored = settings.devices_path.read_text("utf-8")
    assert second["refresh_token"] not in stored and second["access_token"] not in stored


def test_a_retired_refresh_token_signs_the_device_out(settings, serve, alerts):
    server = serve(settings)
    other = doc(sign_in(server, "valid_mac_list_audience", name="Mac")[1])
    first = doc(sign_in(server)[1])
    second = doc(refresh(server, first["refresh_token"])[1])
    resp, payload = refresh(server, first["refresh_token"])
    assert resp.status == 401 and "used already" in doc(payload)["cause"]
    assert first["refresh_token"] not in payload.decode("utf-8")
    assert remote(server, "GET", "/status", token=second["access_token"])[0].status == 401
    assert refresh(server, second["refresh_token"])[0].status == 401
    assert remote(server, "GET", "/status", token=other["access_token"])[0].status != 401
    names = [d.name for d in devices.parse(settings.devices_path.read_text("utf-8"))]
    assert names == ["Mac"]
    title, text = alerts.shown[-1]
    assert title.endswith("device-session") and f"'{NAME}' signed out" in text
    assert first["refresh_token"] not in text


def test_a_refresh_after_the_refresh_lifetime_is_refused(settings, serve, alerts):
    clock = Clock(ISSUED + timedelta(seconds=1))
    server = serve(settings, clock=clock)
    first = doc(sign_in(server)[1])
    clock.now += timedelta(seconds=settings.session_lifetimes.refresh_seconds)
    resp, payload = refresh(server, first["refresh_token"])
    assert resp.status == 401 and "expired" in doc(payload)["cause"]
    assert not devices.parse(settings.devices_path.read_text("utf-8"))
    assert "signed out" in alerts.shown[-1][1]


def test_no_refresh_outlives_the_sign_in(settings, serve):
    lifetimes = settings.session_lifetimes
    clock = Clock(ISSUED + timedelta(seconds=1))
    server = serve(settings, clock=clock)
    token = doc(sign_in(server)[1])["refresh_token"]
    end = clock.now + timedelta(seconds=lifetimes.session_seconds)
    step = timedelta(seconds=lifetimes.refresh_seconds - 1)
    while clock.now + step < end:
        clock.now += step
        resp, payload = refresh(server, token)
        assert resp.status == 200, payload
        answer = doc(payload)
        left = (end - clock.now).total_seconds()
        assert answer["refresh_expires_in"] <= left and answer["access_expires_in"] <= left
        token = answer["refresh_token"]
    clock.now = end
    assert refresh(server, token)[0].status == 401


@pytest.mark.parametrize("token", [
    "nodot", "short.secret", "0123456789abcdef" * 2 + ".", "0123456789abcdef" * 2 + ".x",
])
def test_an_unknown_refresh_token_is_refused_and_changes_nothing(settings, serve, alerts, token):
    server = serve(settings)
    sign_in(server)
    before = settings.devices_path.read_text("utf-8")
    resp, payload = refresh(server, token)
    assert resp.status == 401 and token not in payload.decode("utf-8")
    assert settings.devices_path.read_text("utf-8") == before
    assert kinds(alerts) == ["device-session"]


@pytest.mark.parametrize("body", [{}, {"refresh_token": ""}, {"refresh_token": 1},
                                  {"refresh_token": "x", "y": 1}])
def test_a_malformed_refresh_body_is_a_bad_request(settings, serve, body):
    assert remote(serve(settings), "POST", "/session/refresh", body=body)[0].status == 400


def test_failed_refreshes_count_against_the_pair_budget(settings, serve):
    server = serve(replace(settings, pair_failures=2))
    token = doc(sign_in(server)[1])["refresh_token"]
    assert refresh(server, "nodot")[0].status == 401
    assert refresh(server, "nodot")[0].status == 401
    assert refresh(server, token)[0].status == 429


def test_signing_out_needs_a_token(settings, serve):
    assert remote(serve(settings), "DELETE", "/session")[0].status == 401


def test_the_local_port_has_no_session_route(settings, serve):
    local = _serve(make_server(settings, Clock(ISSUED)))
    try:
        from .test_server import request
        assert request(local, "DELETE", "/session")[0].status == 405
    finally:
        local.shutdown()
        local.server_close()


def test_config_with_accounts_pins_the_key_file(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    cfg = replace(cfg, api=replace(cfg.api, remote_signin_accounts=("1",)))
    with pytest.raises(ConfigError, match="remote_signin_key_file"):
        settings_from_config(cfg)
    target = tmp_path / cfg.api.remote_signin_key_file
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "provider.pem", target)
    built = settings_from_config(cfg)
    assert built.signin_key == _key() and built.signin_rules.accounts == frozenset({"1"})
    json.dumps(built.signin_rules.issuer)  # a plain string from bot.toml
