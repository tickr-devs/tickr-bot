"""HTTP tests against a real server on an ephemeral loopback port."""

from __future__ import annotations

import http.client
import json
import re
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aperture import config
from aperture.api import contract
from aperture.api.contract import CommandKind
from aperture.api.server import ApiSettings, make_server, settings_from_config

from .samples import T0, status
from .schema_check import errors

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def settings(tmp_path) -> ApiSettings:
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    return replace(settings_from_config(cfg), port=0)


@pytest.fixture
def api(settings):
    clock = Clock(T0 + timedelta(seconds=2))
    server = make_server(settings, clock)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server, clock
    server.shutdown()
    server.server_close()


def request(server, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    sent = {"Host": host or f"127.0.0.1:{server.server_port}"}
    if body is not None:
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for name, value in sent.items():
        if value is not None:
            conn.putheader(name, value)
    if data is not None and "Content-Length" not in sent:
        conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp, payload


def doc(payload: bytes):
    return json.loads(payload)


def schema(name: str):
    return contract.json_schema(contract.SCHEMA_TYPES[name])


# ---------------------------------------------------------------- config


def test_settings_refuse_non_loopback_host(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    for host in ("0.0.0.0", "192.168.1.5", "localhost"):
        bad = replace(cfg, api=replace(cfg.api, host=host))
        with pytest.raises(config.ConfigError, match="loopback"):
            settings_from_config(bad)


def test_settings_paths_come_from_config(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    s = settings_from_config(cfg)
    assert s.status_path == tmp_path / cfg.run.data_dir / cfg.api.status_file
    assert s.inbox == tmp_path / cfg.run.data_dir / cfg.api.commands_dir / contract.INBOX


# ---------------------------------------------------------------- guard


@pytest.mark.parametrize("host", ["evil.example:8766", "127.0.0.1", "127.0.0.1:1", None])
def test_foreign_host_refused(api, host):
    server, _ = api
    resp, payload = request(server, "GET", "/status",
                            host=host if host else "", headers={"Host": host})
    assert resp.status == 403
    assert doc(payload)["error"] == "forbidden_host"


def test_localhost_name_allowed(api):
    server, _ = api
    resp, _ = request(server, "GET", "/status", host=f"localhost:{server.server_port}")
    assert resp.status == 200


def test_foreign_origin_refused_even_for_commands(api, settings):
    server, _ = api
    resp, payload = request(server, "POST", "/commands/flatten", {},
                            headers={"Origin": "http://evil.example"})
    assert resp.status == 403
    assert doc(payload)["error"] == "forbidden_origin"
    assert contract.count_inbox(settings.inbox) == 0


def test_same_origin_allowed(api):
    server, _ = api
    origin = f"http://127.0.0.1:{server.server_port}"
    resp, _ = request(server, "GET", "/status", headers={"Origin": origin})
    assert resp.status == 200
    assert resp.getheader("Access-Control-Allow-Origin") is None


# ---------------------------------------------------------------- status


def test_status_unavailable_before_bot_writes(api):
    server, _ = api
    resp, payload = request(server, "GET", "/status")
    body = doc(payload)
    assert resp.status == 200
    assert body["status"] is None and body["age_seconds"] is None
    assert body["unavailable_cause"] == "the bot has not written a status file"
    assert body["summary"].startswith("Bot status unavailable")
    assert errors(schema("status-response-v1"), body) == []


def test_status_served_with_age(api, settings):
    server, _ = api
    contract.write_status(settings.status_path, status())
    resp, payload = request(server, "GET", "/status")
    body = doc(payload)
    assert resp.status == 200
    assert resp.getheader("Cache-Control") == "no-store"
    assert body["age_seconds"] == "2.000"
    assert body["stale"] is False
    assert body["summary"] == status().entry_block.text
    assert body["poll_seconds"] == format(settings.poll_seconds, "f")
    assert contract.from_jsonable(contract.StatusResponseV1, body).status == status()
    assert errors(schema("status-response-v1"), body) == []


def test_status_stale(api, settings):
    server, clock = api
    contract.write_status(settings.status_path, status())
    clock.now = T0 + timedelta(seconds=int(settings.status_stale_seconds) + 1)
    body = doc(request(server, "GET", "/status")[1])
    assert body["stale"] is True
    assert "may be down" in body["summary"]


def test_status_from_the_future_is_refused(api, settings):
    server, _ = api
    contract.write_status(settings.status_path, status(written_at=T0 + timedelta(minutes=1)))
    body = doc(request(server, "GET", "/status")[1])
    assert body["status"] is None
    assert "future" in body["unavailable_cause"]


def test_invalid_status_file_is_reported_not_guessed(api, settings):
    server, _ = api
    settings.status_path.parent.mkdir(parents=True, exist_ok=True)
    settings.status_path.write_text('{"schema_version": 1}', "utf-8")
    body = doc(request(server, "GET", "/status")[1])
    assert body["status"] is None
    assert body["unavailable_cause"].startswith("status file is invalid")


# ---------------------------------------------------------------- commands


@pytest.mark.parametrize("path, body, kind", [
    ("/commands/entry-stop", {"state": "on"}, CommandKind.ENTRY_STOP_ON),
    ("/commands/entry-stop", {"state": "off"}, CommandKind.ENTRY_STOP_OFF),
    ("/commands/cancel-own-orders", {}, CommandKind.CANCEL_OWN_ORDERS),
    ("/commands/flatten", {}, CommandKind.FLATTEN),
])
def test_command_is_queued_for_the_bot(api, settings, path, body, kind):
    server, clock = api
    contract.write_status(settings.status_path, status())
    resp, payload = request(server, "POST", path, body)
    answer = doc(payload)
    assert resp.status == 202
    assert errors(schema("command-accepted-v1"), answer) == []
    assert answer["command"]["kind"] == kind.value
    assert answer["warning"] is None
    (item,) = contract.read_commands(settings.inbox, clock.now, Decimal("60"))
    assert item.command.kind == kind
    assert item.command.command_id == answer["command"]["command_id"]
    assert item.command.source == "api"
    pending = doc(request(server, "GET", "/commands/pending")[1])
    assert errors(schema("pending-commands-v1"), pending) == []
    assert [c["command_id"] for c in pending["commands"]] == [answer["command"]["command_id"]]


def test_command_warns_when_bot_looks_down(api, settings):
    server, _ = api
    answer = doc(request(server, "POST", "/commands/flatten", {})[1])
    assert "does not look alive" in answer["warning"]
    assert contract.count_inbox(settings.inbox) == 1


@pytest.mark.parametrize("path, body", [
    ("/commands/entry-stop", {}),
    ("/commands/entry-stop", {"state": "ON"}),
    ("/commands/entry-stop", {"state": "on", "x": 1}),
    ("/commands/flatten", {"now": True}),
    ("/commands/flatten", []),
    ("/commands/flatten", b"{"),
    ("/commands/flatten", b'{"a":1,"a":2}'),
])
def test_bad_command_body_refused(api, settings, path, body):
    server, _ = api
    resp, payload = request(server, "POST", path, body)
    assert resp.status == 400
    assert doc(payload)["error"] == "bad_request"
    assert contract.count_inbox(settings.inbox) == 0


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_command_requires_json_content_type(api, settings, content_type):
    server, _ = api
    resp, payload = request(server, "POST", "/commands/flatten", {},
                            headers={"Content-Type": content_type})
    assert resp.status == 415
    assert contract.count_inbox(settings.inbox) == 0


def test_command_body_too_large(api, settings):
    server, _ = api
    big = b'{"state":"' + b"x" * settings.max_request_body_bytes + b'"}'
    resp, payload = request(server, "POST", "/commands/entry-stop", big)
    assert resp.status == 413
    assert contract.count_inbox(settings.inbox) == 0


def test_too_many_pending_commands(api, settings):
    server, _ = api
    for _ in range(settings.max_pending_commands):
        assert request(server, "POST", "/commands/flatten", {})[0].status == 202
    resp, payload = request(server, "POST", "/commands/flatten", {})
    assert resp.status == 429
    assert doc(payload)["error"] == "too_many_pending"
    assert contract.count_inbox(settings.inbox) == settings.max_pending_commands


# ---------------------------------------------------------------- routing and page


@pytest.mark.parametrize("method, path, code", [
    ("GET", "/nope", 404),
    ("POST", "/nope", 404),
    ("GET", "/commands/flatten", 405),
    ("POST", "/status", 405),
    ("DELETE", "/status", 405),
    ("OPTIONS", "/commands/flatten", 405),
    ("PUT", "/commands/flatten", 405),
])
def test_routing(api, method, path, code):
    server, _ = api
    resp, payload = request(server, method, path, {} if method != "GET" else None)
    assert resp.status == code
    assert errors(schema("error-v1"), doc(payload)) == []


def test_status_page(api):
    server, _ = api
    resp, payload = request(server, "GET", "/")
    page = payload.decode("utf-8")
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/html")
    assert "default-src 'none'" in resp.getheader("Content-Security-Policy")
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert 'fetch("/status"' in page
    assert ".innerHTML" not in page  # data is inserted with textContent only
    assert not re.search(r"<script[^>]+src=", page)  # no external scripts


def test_schema_endpoint(api):
    server, _ = api
    body = doc(request(server, "GET", "/schema")[1])
    assert set(body) == set(contract.SCHEMA_TYPES)
    assert body["status-v1"] == contract.json_schema(contract.StatusV1)
