"""HTTP tests against a real server on an ephemeral loopback port."""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.contract import CommandKind
from tickr.api.server import ApiSettings, make_server, settings_from_config

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


def test_refusal_logs_port_and_host(api, capsys):
    server, _ = api
    resp, _ = request(server, "GET", "/status", host="127.0.0.1:8767")
    assert resp.status == 403
    line = next(x for x in capsys.readouterr().err.splitlines() if "GET /status" in x)
    assert f"403 - port={server.server_port} host='127.0.0.1:8767'" in line


def test_answered_request_logs_port_not_host(api, capsys):
    server, _ = api
    resp, _ = request(server, "GET", "/status")
    assert resp.status == 200
    line = next(x for x in capsys.readouterr().err.splitlines() if "GET /status" in x)
    assert f"port={server.server_port}" in line
    assert "host=" not in line


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
    # The API's own start, so the apps read /settings again after a restart.
    assert body["api_started_at"] == contract.to_jsonable(server.started_at)
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
    ("/commands/flatten", {"symbol": "ABCD"}, CommandKind.FLATTEN),
    ("/commands/stop-bot", {}, CommandKind.STOP_BOT),
    ("/commands/stop-bot", {"mode": "close_and_stop"}, CommandKind.STOP_BOT),
    ("/commands/stop-bot", {"mode": "leave_open"}, CommandKind.STOP_BOT),
    ("/commands/emergency", {}, CommandKind.EMERGENCY),
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
    assert item.command.symbol == body.get("symbol")
    assert answer["command"]["symbol"] == body.get("symbol")
    assert item.command.mode == body.get("mode")
    assert answer["command"]["mode"] == body.get("mode")
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
    ("/commands/flatten", {"symbol": None}),  # never read as "everything"
    ("/commands/flatten", {"symbol": "abcd"}),
    ("/commands/flatten", {"symbol": ""}),
    ("/commands/flatten", {"symbol": 5}),
    ("/commands/flatten", {"symbol": "ABCD", "x": 1}),
    ("/commands/stop-bot", {"now": True}),
    ("/commands/stop-bot", {"symbol": "ABCD"}),
    ("/commands/stop-bot", {"mode": None}),  # never read as "no mode"
    ("/commands/stop-bot", {"mode": "close"}),
    ("/commands/stop-bot", {"mode": "CLOSE_AND_STOP"}),
    ("/commands/stop-bot", {"mode": "leave_open", "x": 1}),
    ("/commands/flatten", {"mode": "leave_open"}),
    ("/commands/emergency", {"mode": "close_and_stop"}),
    ("/commands/emergency", {"symbol": "ABCD"}),
    ("/commands/cancel-own-orders", {"symbol": "ABCD"}),
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


def test_settings_endpoint_shows_the_loaded_config(api, settings):
    server, _ = api
    resp, payload = request(server, "GET", "/settings")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("settings-v1"), body) == []
    cfg = config.load(ROOT / "config" / "bot.toml")
    assert body["risk"]["max_daily_loss_usd"] == str(cfg.risk.max_daily_loss_usd)
    assert body["risk"]["max_open_positions"] == cfg.risk.max_open_positions
    assert body["risk"]["min_daily_room_usd"] == str(cfg.risk.min_daily_room_usd)
    assert body["session"]["flatten_minutes_before_close"] == (
        cfg.session.flatten_minutes_before_close)
    assert body["active_strategy"] == cfg.strategy.active
    assert request(server, "POST", "/settings", {})[0].status == 405


def test_schema_endpoint(api):
    server, _ = api
    body = doc(request(server, "GET", "/schema")[1])
    assert set(body) == set(contract.SCHEMA_TYPES)
    assert body["status-v1"] == contract.json_schema(contract.StatusV1)


# ---------------------------------------------------------------- Idempotency-Key


def keyed(server, path, body, key):
    return request(server, "POST", path, body, headers={"Idempotency-Key": key})


@pytest.mark.parametrize("key", ["", "x" * 129, "a b", "a/b", "a,b", "ключ"])
def test_bad_idempotency_key_refused(api, settings, key):
    server, _ = api
    headers = {"Idempotency-Key": key.encode("utf-8").decode("latin-1")}
    resp, payload = request(server, "POST", "/commands/flatten", {}, headers=headers)
    assert resp.status == 400
    assert doc(payload)["error"] == "bad_request"
    assert contract.count_inbox(settings.inbox) == 0


def test_longest_allowed_key_accepted(api, settings):
    server, _ = api
    key = "aZ09._:-" * 16
    assert len(key) == 128
    assert keyed(server, "/commands/flatten", {}, key)[0].status == 202


def test_repeated_key_returns_the_same_answer_and_queues_nothing(api, settings):
    server, clock = api
    first, body = keyed(server, "/commands/emergency", {}, "phone-1:a")
    assert first.status == 202
    clock.now += timedelta(seconds=5)
    again, repeat = keyed(server, "/commands/emergency", {}, "phone-1:a")
    assert again.status == 202 and repeat == body
    assert errors(schema("command-accepted-v1"), doc(repeat)) == []
    assert contract.count_inbox(settings.inbox) == 1
    # The bot consumes the command: a repeat still answers the first command, queues nothing.
    (item,) = contract.read_commands(settings.inbox, clock.now, Decimal("60"))
    contract.retire_command(item, settings.inbox.parent / contract.PROCESSED)
    resp, later = keyed(server, "/commands/emergency", {}, "phone-1:a")
    assert resp.status == 202 and later == body
    assert contract.count_inbox(settings.inbox) == 0
    # Another key is another command.
    assert keyed(server, "/commands/emergency", {}, "phone-1:b")[0].status == 202
    assert contract.count_inbox(settings.inbox) == 1


def test_repeated_key_with_the_same_stop_mode_queues_nothing(api, settings):
    server, _ = api
    first, body = keyed(server, "/commands/stop-bot", {"mode": "close_and_stop"}, "k-mode")
    again, repeat = keyed(server, "/commands/stop-bot", {"mode": "close_and_stop"}, "k-mode")
    assert (first.status, again.status) == (202, 202) and repeat == body
    assert contract.count_inbox(settings.inbox) == 1


def test_keys_are_case_sensitive(api, settings):
    server, _ = api
    a = doc(keyed(server, "/commands/stop-bot", {}, "Key")[1])
    b = doc(keyed(server, "/commands/stop-bot", {}, "key")[1])
    assert a["command"]["command_id"] != b["command"]["command_id"]
    assert contract.count_inbox(settings.inbox) == 2


@pytest.mark.parametrize("first, second", [
    (("/commands/flatten", {}), ("/commands/emergency", {})),
    (("/commands/flatten", {}), ("/commands/flatten", {"symbol": "ABCD"})),
    (("/commands/flatten", {"symbol": "ABCD"}), ("/commands/flatten", {"symbol": "WXYZ"})),
    (("/commands/entry-stop", {"state": "on"}), ("/commands/entry-stop", {"state": "off"})),
    (("/commands/stop-bot", {}), ("/commands/stop-bot", {"mode": "leave_open"})),
    (("/commands/stop-bot", {"mode": "close_and_stop"}),
     ("/commands/stop-bot", {"mode": "leave_open"})),
])
def test_same_key_for_another_command_is_a_conflict(api, settings, first, second):
    server, _ = api
    assert keyed(server, *first, "k1")[0].status == 202
    resp, payload = keyed(server, *second, "k1")
    assert resp.status == 409
    assert doc(payload)["error"] == "conflict"
    assert errors(schema("error-v1"), doc(payload)) == []
    assert contract.count_inbox(settings.inbox) == 1


def test_key_is_forgotten_after_its_lifetime(api, settings):
    server, clock = api
    first = doc(keyed(server, "/commands/flatten", {}, "old")[1])
    assert len(list(settings.idempotency.glob("*.json"))) == 1
    clock.now += timedelta(seconds=settings.idempotency_ttl_seconds + 1)
    again = doc(keyed(server, "/commands/flatten", {}, "old")[1])
    assert again["command"]["command_id"] != first["command"]["command_id"]
    assert keyed(server, "/commands/flatten", {}, "new")[0].status == 202
    clock.now += timedelta(seconds=settings.idempotency_ttl_seconds + 1)
    assert keyed(server, "/commands/flatten", {}, "newest")[0].status == 202
    assert len(list(settings.idempotency.glob("*.json"))) == 1  # expired files pruned


def test_repeat_is_answered_even_when_the_inbox_is_full(api, settings):
    server, _ = api
    _, body = keyed(server, "/commands/flatten", {}, "first")
    for _ in range(settings.max_pending_commands - 1):
        assert request(server, "POST", "/commands/flatten", {})[0].status == 202
    assert request(server, "POST", "/commands/flatten", {})[0].status == 429
    resp, repeat = keyed(server, "/commands/flatten", {}, "first")
    assert resp.status == 202 and repeat == body


def test_unreadable_key_record_queues_nothing(api, settings):
    server, _ = api
    assert keyed(server, "/commands/flatten", {}, "k")[0].status == 202
    (record,) = settings.idempotency.glob("*.json")
    record.write_text("{", "utf-8")
    resp, payload = keyed(server, "/commands/flatten", {}, "k")
    assert resp.status == 500 and doc(payload)["error"] == "internal"
    assert contract.count_inbox(settings.inbox) == 1


def receipt(server, key):
    return request(server, "GET", "/commands/receipt", headers={"Idempotency-Key": key})


def test_receipt_answers_what_the_key_queued_and_writes_nothing(api, settings):
    """A client whose POST answer was lost reads the key's receipt instead of sending again."""
    server, clock = api
    resp, payload = receipt(server, "phone:lost")
    assert resp.status == 200
    assert doc(payload) == {"queued": False, "accepted": None, "api_version": 1}
    assert errors(schema("command-receipt-v1"), doc(payload)) == []
    _, body = keyed(server, "/commands/flatten", {"symbol": "ABCD"}, "phone:lost")
    clock.now += timedelta(seconds=5)
    resp, payload = receipt(server, "phone:lost")
    assert resp.status == 200 and doc(payload)["queued"] is True
    assert doc(payload)["accepted"] == doc(body)
    assert errors(schema("command-receipt-v1"), doc(payload)) == []
    # Reading it queued nothing and stored nothing: one command, one key.
    assert contract.count_inbox(settings.inbox) == 1
    assert len(list(settings.idempotency.glob("*.json"))) == 1
    assert doc(receipt(server, "phone:other")[1])["queued"] is False


def test_receipt_after_the_bot_consumed_the_command(api, settings):
    server, clock = api
    _, body = keyed(server, "/commands/emergency", {}, "k")
    (item,) = contract.read_commands(settings.inbox, clock.now, Decimal("60"))
    contract.retire_command(item, settings.inbox.parent / contract.PROCESSED)
    assert doc(receipt(server, "k")[1])["accepted"] == doc(body)


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "a b"}])
def test_receipt_needs_a_well_formed_key(api, headers):
    server, _ = api
    resp, payload = request(server, "GET", "/commands/receipt", headers=headers)
    assert resp.status == 400 and doc(payload)["error"] == "bad_request"


def test_unreadable_key_record_is_no_receipt(api, settings):
    server, _ = api
    keyed(server, "/commands/flatten", {}, "k")
    (record,) = settings.idempotency.glob("*.json")
    record.write_text("{", "utf-8")
    resp, payload = receipt(server, "k")
    assert resp.status == 500 and doc(payload)["error"] == "internal"


def test_key_lifetime_must_outlast_the_command_age(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    bad = replace(cfg, api=replace(cfg.api, idempotency_key_ttl_seconds=int(
        cfg.api.command_max_age_seconds)))
    with pytest.raises(config.ConfigError, match="idempotency_key_ttl_seconds"):
        settings_from_config(bad)


# ---------------------------------------------------------------- slow clients


def _short_timeout_api(settings):
    server = make_server(replace(settings, request_timeout_seconds=Decimal("0.2")),
                         Clock(T0 + timedelta(seconds=2)))
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server


def test_settings_refuse_non_positive_request_timeout(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    for value in (Decimal(0), Decimal(-1)):
        bad = replace(cfg, api=replace(cfg.api, request_timeout_seconds=value))
        with pytest.raises(config.ConfigError, match="request_timeout_seconds"):
            settings_from_config(bad)


def test_idle_connection_is_closed_after_the_request_timeout(settings):
    """A client that connects and sends nothing must not hold a server thread forever."""
    server = _short_timeout_api(settings)
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            assert sock.recv(1) == b""  # the server closed it; the 5 s guard never fires
    finally:
        server.shutdown()
        server.server_close()


def test_body_shorter_than_its_length_is_dropped_and_queues_nothing(settings):
    server = _short_timeout_api(settings)
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            sock.sendall(f"POST /commands/flatten HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}"
                         "\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n{"
                         .encode())
            received = b""
            while chunk := sock.recv(4096):
                received += chunk
        assert b" 202 " not in received
        assert contract.count_inbox(settings.inbox) == 0
        # The server still answers others.
        assert request(server, "GET", "/status")[0].status == 200
    finally:
        server.shutdown()
        server.server_close()
