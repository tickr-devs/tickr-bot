"""A signed-in device's tokens (api/sessions.py) and their rows in the device list
(api/devices.py), without a server: rotation, reuse, expiry and the digests kept."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tickr import config
from tickr.api import devices, sessions
from tickr.api.server import settings_from_config
from tickr.api.sessions import Lifetimes, RefreshRefused
from tickr.config import ConfigError

from .test_server import ROOT

NOW = datetime(2026, 11, 27, 17, 0, tzinfo=UTC)
LIFE = Lifetimes(access_seconds=900, refresh_seconds=86400, session_seconds=3 * 86400)
BYTES = 32


def _signed_in(name="Phone"):
    device, issued = sessions.sign_in(name, NOW, LIFE, BYTES)
    return (device,), issued


def test_sign_in_keeps_digests_and_the_three_expiries():
    (device,), issued = _signed_in()
    assert device.sha256 == devices.digest(issued.access_token)
    assert device.session.refresh_sha256 == devices.digest(issued.refresh_token)
    assert issued.access_expires_at == NOW + timedelta(seconds=LIFE.access_seconds)
    assert issued.refresh_expires_at == NOW + timedelta(seconds=LIFE.refresh_seconds)
    assert device.session.expires_at == NOW + timedelta(seconds=LIFE.session_seconds)
    assert issued.refresh_token.startswith(device.session.family + sessions.SEPARATOR)
    stored = devices.render((device,))
    assert issued.access_token not in stored and issued.refresh_token not in stored


def test_the_device_list_round_trips_session_rows_and_paired_rows():
    listed, _ = _signed_in()
    paired = devices.Device("Mac", devices.digest("t"), NOW, None)
    both = (*listed, paired)
    assert devices.parse(devices.render(both)) == both


@pytest.mark.parametrize("damage", [
    lambda s: s.pop("expires_at"),
    lambda s: s.update(family="NOT-HEX"),
    lambda s: s.update(refresh_sha256="short"),
    lambda s: s.update(extra=1),
])
def test_a_damaged_session_row_is_refused(damage):
    listed, _ = _signed_in()
    document = json.loads(devices.render(listed))
    damage(document["devices"][0]["session"])
    with pytest.raises(devices.DeviceFileError):
        devices.parse(json.dumps(document))


def test_two_devices_sharing_a_family_are_refused():
    (device,), _ = _signed_in()
    twin = replace(device, name="Twin")
    with pytest.raises(devices.DeviceFileError, match="family"):
        devices.parse(devices.render((device, twin)))


def test_a_refresh_retires_the_presented_token_in_the_same_registry():
    listed, first = _signed_in()
    renewed, second = sessions.refresh(listed, first.refresh_token, NOW, LIFE, BYTES)
    (device,) = renewed
    assert device.session.refresh_sha256 == devices.digest(second.refresh_token)
    assert device.sha256 == devices.digest(second.access_token)
    assert device.session.family == listed[0].session.family
    assert device.session.expires_at == listed[0].session.expires_at
    with pytest.raises(RefreshRefused) as refused:
        sessions.refresh(renewed, first.refresh_token, NOW, LIFE, BYTES)
    assert refused.value.revoked == "Phone" and "used already" in refused.value.cause


@pytest.mark.parametrize("token", ["", "x", "abc.def", "0" * 32 + ".", "0" * 32 + ".secret"])
def test_a_token_of_no_device_revokes_nothing(token):
    listed, _ = _signed_in()
    with pytest.raises(RefreshRefused) as refused:
        sessions.refresh(listed, token, NOW, LIFE, BYTES)
    assert refused.value.revoked is None


def test_a_paired_device_has_no_refresh_token_and_its_token_never_expires():
    paired = devices.Device("Mac", devices.digest("t"), NOW, None)
    assert not sessions.access_expired(paired, NOW + timedelta(days=10_000))
    with pytest.raises(RefreshRefused):
        sessions.refresh((paired,), "0" * 32 + ".t", NOW, LIFE, BYTES)


def test_the_access_token_expires_on_the_bots_clock():
    (device,), _ = _signed_in()
    end = NOW + timedelta(seconds=LIFE.access_seconds)
    assert not sessions.access_expired(device, end - timedelta(microseconds=1))
    assert sessions.access_expired(device, end)


def test_an_expired_refresh_token_signs_the_device_out():
    listed, issued = _signed_in()
    later = NOW + timedelta(seconds=LIFE.refresh_seconds)
    with pytest.raises(RefreshRefused) as refused:
        sessions.refresh(listed, issued.refresh_token, later, LIFE, BYTES)
    assert refused.value.revoked == "Phone" and "expired" in refused.value.cause


def test_no_token_outlives_the_sign_in():
    listed, issued = _signed_in()
    near_end = NOW + timedelta(seconds=LIFE.session_seconds - 60)
    current = issued
    at = NOW
    while at + timedelta(seconds=LIFE.refresh_seconds) < near_end:
        at += timedelta(seconds=LIFE.refresh_seconds - 1)
        listed, current = sessions.refresh(listed, current.refresh_token, at, LIFE, BYTES)
    listed, current = sessions.refresh(listed, current.refresh_token, near_end, LIFE, BYTES)
    end = listed[0].session.expires_at
    assert current.access_expires_at == end and current.refresh_expires_at == end


@pytest.mark.parametrize("keys", [
    {"remote_access_token_seconds": 0},
    {"remote_access_token_seconds": 100, "remote_refresh_token_seconds": 99},
    {"remote_refresh_token_seconds": 10**9},
])
def test_lifetimes_out_of_order_refuse_the_config(tmp_path, keys):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    cfg = replace(cfg, api=replace(cfg.api, **keys))
    with pytest.raises(ConfigError, match="remote_session_max_seconds"):
        settings_from_config(cfg)
