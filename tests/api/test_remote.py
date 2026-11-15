"""Remote access through an HTTPS proxy on the Mac (`tailscale serve`) to the remote port:
bearer token always required there, whatever the Host header says."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from tickr import config
from tickr.api import __main__ as api_main
from tickr.api import contract
from tickr.api.server import make_remote_server, make_server, pair, settings_from_config

from .samples import T0
from .schema_check import errors
from .test_server import ROOT, Clock, doc, request, schema

REMOTE = "mac.example-tailnet.ts.net"
NOW = T0 + timedelta(seconds=2)  # the servers' clock in these tests


@pytest.fixture
def remote_settings(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    cfg = replace(cfg, api=replace(cfg.api, remote_hosts=(REMOTE,)))
    return replace(settings_from_config(cfg), port=0, remote_port=0)


def _serve(server):
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server


@pytest.fixture
def local_api(remote_settings):
    server = _serve(make_server(remote_settings, Clock(T0 + timedelta(seconds=2))))
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def remote_api(local_api):
    server = _serve(make_remote_server(local_api))
    yield server
    server.shutdown()
    server.server_close()


def remote(server, method, path, token=None, body=None, headers=None):
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return request(server, method, path, body, headers=sent, host=REMOTE)


def link_code(link_file):
    """The one-time pairing code in the link file, read as the phone would."""
    return parse_qs(urlparse(link_file.read_text().strip()).query)["code"][0]


def redeem(server, code, **kwargs):
    return remote(server, "POST", "/pair", body={"code": code}, **kwargs)


def paired(server, settings):
    """Pair as the owner and the phone would: `pair`, then redeem the link's code. Returns the
    token."""
    resp, payload = redeem(server, link_code(pair(settings, now=NOW)))
    assert resp.status == 200, payload
    return doc(payload)["token"]


def test_shipped_config_has_remote_access_off(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    assert cfg.api.remote_hosts == ()


@pytest.mark.parametrize("name", ["Mac.ts.net", "https://mac.ts.net", "localhost", "mac", "a..b",
                                  "127.0.0.1", "mac.ts.net/x"])
def test_bad_remote_host_refused_at_startup(tmp_path, name):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    bad = replace(cfg, api=replace(cfg.api, remote_hosts=(name,)))
    with pytest.raises(config.ConfigError, match="remote_hosts"):
        settings_from_config(bad)


def test_remote_host_with_port_allowed(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    ok = replace(cfg, api=replace(cfg.api, remote_hosts=("mac.ts.net:8443",)))
    assert settings_from_config(ok).remote_hosts == ("mac.ts.net:8443",)


def test_remote_refused_before_pairing(remote_api):
    resp, payload = remote(remote_api, "GET", "/status", token="anything")
    assert resp.status == 401
    assert doc(payload)["error"] == "unauthorized"
    assert "pair" in doc(payload)["cause"]
    assert resp.getheader("WWW-Authenticate") == "Bearer"
    assert errors(schema("error-v1"), doc(payload)) == []


def test_pairing_stores_only_the_hashes(remote_api, remote_settings):
    link_file = pair(remote_settings, now=NOW)
    code = link_code(link_file)
    waiting = json.loads(remote_settings.pairing_code_path.read_text("ascii"))
    assert waiting["sha256"] == hashlib.sha256(code.encode()).hexdigest()
    assert code not in remote_settings.pairing_code_path.read_text("ascii")
    assert stat.S_IMODE(remote_settings.pairing_code_path.stat().st_mode) == 0o600
    assert len(code) >= remote_settings.token_bytes
    resp, payload = redeem(remote_api, code)
    assert resp.status == 200
    assert errors(schema("paired-v1"), doc(payload)) == []
    token = doc(payload)["token"]
    assert token != code and len(token) >= remote_settings.token_bytes
    stored = remote_settings.token_hash_path.read_text("ascii").strip()
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert stat.S_IMODE(remote_settings.token_hash_path.stat().st_mode) == 0o600
    assert resp.getheader("Cache-Control") == "no-store"


def test_pairing_needs_a_remote_host(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    with pytest.raises(config.ConfigError, match="remote_hosts is empty"):
        pair(settings_from_config(cfg))


def test_paired_token_reads_status_and_queues_commands(remote_api, remote_settings):
    token = paired(remote_api, remote_settings)
    resp, _ = remote(remote_api, "GET", "/status", token=token)
    assert resp.status == 200
    resp, payload = remote(remote_api, "POST", "/commands/entry-stop", token=token,
                           body={"state": "on"})
    assert resp.status == 202
    assert doc(payload)["command"]["kind"] == "entry-stop-on"
    assert contract.count_inbox(remote_settings.inbox) == 1


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc", "bearer {token}",
                                    "Bearer {token}x", "Bearer  {token}"])
def test_wrong_or_missing_token_refused_and_nothing_queued(remote_api, remote_settings, header):
    token = paired(remote_api, remote_settings)
    headers = {} if header is None else {"Authorization": header.format(token=token)}
    resp, payload = remote(remote_api, "POST", "/commands/flatten", body={}, headers=headers)
    assert resp.status == 401
    assert doc(payload)["error"] == "unauthorized"
    assert token not in payload.decode()
    assert contract.count_inbox(remote_settings.inbox) == 0


def test_repairing_revokes_the_old_token(remote_api, remote_settings):
    old = paired(remote_api, remote_settings)
    new = paired(remote_api, remote_settings)
    assert remote(remote_api, "GET", "/status", token=old)[0].status == 401
    assert remote(remote_api, "GET", "/status", token=new)[0].status == 200


def test_remote_origin_must_match(remote_api, remote_settings):
    token = paired(remote_api, remote_settings)
    ok = remote(remote_api, "GET", "/status", token=token, headers={"Origin": f"https://{REMOTE}"})
    assert ok[0].status == 200
    resp, payload = remote(remote_api, "POST", "/commands/flatten", token=token, body={},
                           headers={"Origin": f"http://{REMOTE}"})
    assert resp.status == 403
    assert doc(payload)["error"] == "forbidden_origin"
    assert contract.count_inbox(remote_settings.inbox) == 0


def test_local_port_needs_no_token(local_api, remote_api):
    resp, _ = request(local_api, "GET", "/status")
    assert resp.status == 200


@pytest.mark.parametrize("path,body", [("/status", None), ("/commands/flatten", {})])
def test_remote_port_with_a_local_host_header_is_refused(remote_api, remote_settings, path, body):
    """A caller cannot skip the token by sending a loopback Host through the proxy."""
    pair(remote_settings, now=NOW)
    for host in (f"127.0.0.1:{remote_api.server_port}", "localhost", "127.0.0.1"):
        resp, payload = request(remote_api, "POST" if body is not None else "GET", path, body,
                                host=host)
        assert resp.status == 403, host
        assert doc(payload)["error"] == "forbidden_host"
    assert contract.count_inbox(remote_settings.inbox) == 0


def test_remote_name_on_the_local_port_is_refused(local_api, remote_api, remote_settings):
    token = paired(remote_api, remote_settings)
    resp, _ = request(local_api, "GET", "/status", host=REMOTE,
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 403


def test_remote_port_must_differ(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    bad = replace(cfg, api=replace(cfg.api, remote_port=cfg.api.port))
    with pytest.raises(config.ConfigError, match="remote_port"):
        settings_from_config(bad)


def test_unreadable_hash_file_refuses_with_a_cause(remote_api, remote_settings):
    remote_settings.token_hash_path.parent.mkdir(parents=True, exist_ok=True)
    remote_settings.token_hash_path.write_bytes(b"\xff\xfe")
    resp, payload = remote(remote_api, "GET", "/status", token="x")
    assert resp.status == 503
    assert "unreadable" in doc(payload)["cause"]


def test_pairing_link_file_is_private_and_removed_when_redeemed(remote_api, remote_settings):
    link_file = pair(remote_settings, now=NOW)
    assert stat.S_IMODE(link_file.stat().st_mode) == 0o600
    code = link_code(link_file)
    resp, payload = redeem(remote_api, "wrong")
    assert resp.status == 401 and code not in payload.decode()
    assert link_file.exists() and remote_settings.pairing_code_path.exists()
    assert redeem(remote_api, code)[0].status == 200
    assert not link_file.exists() and not remote_settings.pairing_code_path.exists()


def test_a_pairing_code_buys_one_token_only(remote_api, remote_settings):
    """A copy of the link left anywhere (Files, iCloud Drive, a clipboard) is worthless once
    the phone has paired: the code is burnt and is never itself a token."""
    code = link_code(pair(remote_settings, now=NOW))
    token = doc(redeem(remote_api, code)[1])["token"]
    resp, payload = redeem(remote_api, code)
    assert resp.status == 401 and doc(payload)["error"] == "unauthorized"
    assert remote(remote_api, "GET", "/status", token=code)[0].status == 401
    assert remote(remote_api, "GET", "/status", token=token)[0].status == 200


def test_an_expired_pairing_code_is_refused_and_burnt(local_api, remote_settings):
    code = link_code(pair(remote_settings, now=T0 + timedelta(seconds=2)))
    late = T0 + timedelta(seconds=2 + remote_settings.pairing_code_ttl_seconds)
    server = _serve(make_remote_server(local_api))
    server.clock = Clock(late)
    try:
        resp, payload = redeem(server, code)
        assert resp.status == 401 and "expired" in doc(payload)["cause"]
        assert not remote_settings.pairing_code_path.exists()
        assert not remote_settings.pairing_path.exists()
        assert not remote_settings.token_hash_path.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_pair_revokes_the_current_token_at_once(remote_api, remote_settings):
    token = paired(remote_api, remote_settings)
    pair(remote_settings, now=NOW)
    assert remote(remote_api, "GET", "/status", token=token)[0].status == 401


@pytest.mark.parametrize("body", [{}, {"code": ""}, {"code": 1}, {"code": "x", "token": "y"}])
def test_pair_body_must_be_exactly_a_code(remote_api, remote_settings, body):
    pair(remote_settings, now=NOW)
    resp, payload = remote(remote_api, "POST", "/pair", body=body)
    assert resp.status == 400
    assert remote_settings.pairing_code_path.exists()


def test_redeeming_needs_the_remote_host_and_origin(remote_api, remote_settings):
    code = link_code(pair(remote_settings, now=NOW))
    resp, _ = request(remote_api, "POST", "/pair", {"code": code},
                      host=f"127.0.0.1:{remote_api.server_port}")
    assert resp.status == 403
    resp, _ = redeem(remote_api, code, headers={"Origin": "https://evil.example"})
    assert resp.status == 403
    assert remote_settings.pairing_code_path.exists()


def test_the_local_port_has_no_pair_route(local_api, remote_settings):
    code = link_code(pair(remote_settings, now=NOW))
    resp, _ = request(local_api, "POST", "/pair", {"code": code})
    assert resp.status == 404
    assert remote_settings.pairing_code_path.exists()


def test_redeem_without_a_waiting_pairing(remote_api):
    resp, payload = redeem(remote_api, "anything")
    assert resp.status == 401 and "pair" in doc(payload)["cause"]


@pytest.mark.parametrize("value", [1, 15])
def test_token_bytes_below_128_bits_refused(tmp_path, value):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    bad = replace(cfg, api=replace(cfg.api, remote_token_bytes=value))
    with pytest.raises(config.ConfigError, match="remote_token_bytes"):
        settings_from_config(bad)


def test_pairing_replaces_a_leftover_temp_file_with_wide_permissions(remote_settings):
    target = remote_settings.pairing_code_path
    leftover = target.with_name(target.name + ".tmp")
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("old")
    leftover.chmod(0o644)
    pair(remote_settings, now=NOW)
    assert stat.S_IMODE(remote_settings.pairing_code_path.stat().st_mode) == 0o600


def test_pair_command_never_prints_the_code(tmp_path, capsys, monkeypatch):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8").replace(
        "remote_hosts = []", f'remote_hosts = ["{REMOTE}"]', 1)
    path = tmp_path / "config" / "bot.toml"
    path.parent.mkdir()
    path.write_text(text, "utf-8")
    for name in ("calendar",):
        (tmp_path / "config" / name).symlink_to(ROOT / "config" / name)
    assert api_main.main(["--config", str(path), "pair"]) == 0
    captured = capsys.readouterr()
    cfg = config.load(path)
    data = cfg.root / cfg.run.data_dir
    link = (data / cfg.api.remote_pairing_file).read_text().strip()
    query = parse_qs(urlparse(link).query)
    assert link.startswith("tickr://pair?")
    assert query["url"] == [f"https://{REMOTE}"] and set(query) == {"url", "code"}
    code = query["code"][0]
    assert code not in captured.out + captured.err
    assert str(data / cfg.api.remote_pairing_file) in captured.out
    stored = json.loads((data / cfg.api.remote_pairing_code_file).read_text())["sha256"]
    assert stored == hashlib.sha256(code.encode()).hexdigest()


def test_idempotency_key_does_not_replace_the_token(remote_api, remote_settings):
    token = paired(remote_api, remote_settings)
    headers = {"Idempotency-Key": "phone:1"}
    resp, _ = remote(remote_api, "POST", "/commands/emergency", body={}, headers=headers)
    assert resp.status == 401
    assert not remote_settings.idempotency.exists()
    resp, first = remote(remote_api, "POST", "/commands/emergency", token=token, body={},
                         headers=headers)
    assert resp.status == 202
    resp, again = remote(remote_api, "POST", "/commands/emergency", token=token, body={},
                         headers=headers)
    assert resp.status == 202 and again == first
    assert contract.count_inbox(remote_settings.inbox) == 1


def test_idle_connection_on_the_remote_port_is_closed_before_any_token(remote_settings):
    """Anyone on the tailnet can open a connection before presenting a token: an idle one is
    dropped after [api] request_timeout_seconds instead of holding a thread forever."""
    import socket

    local = make_server(replace(remote_settings, request_timeout_seconds=Decimal("0.2")))
    server = _serve(make_remote_server(local))
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            assert sock.recv(1) == b""
    finally:
        server.shutdown()
        server.server_close()
        local.server_close()



def _hold_pairing_lock(settings):
    lock = settings.pairing_code_path.with_name(settings.pairing_code_path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_redeem_waits_for_the_pairing_lock(remote_api, remote_settings):
    """`pair` runs in another process: POST /pair waits while it holds the shared file lock, so
    the two never interleave."""
    code = link_code(pair(remote_settings, now=NOW))
    fd = _hold_pairing_lock(remote_settings)
    result = {}
    worker = threading.Thread(target=lambda: result.update(answer=redeem(remote_api, code)))
    worker.start()
    worker.join(0.3)
    assert worker.is_alive() and not result
    os.close(fd)
    worker.join(5)
    assert result["answer"][0].status == 200


def test_pair_waits_for_the_pairing_lock(remote_settings):
    remote_settings.pairing_code_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _hold_pairing_lock(remote_settings)
    worker = threading.Thread(target=pair, args=(remote_settings,), kwargs={"now": NOW})
    worker.start()
    worker.join(0.3)
    assert worker.is_alive() and not remote_settings.pairing_code_path.exists()
    os.close(fd)
    worker.join(5)
    assert remote_settings.pairing_code_path.exists()
