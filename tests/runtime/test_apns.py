"""APNs push to the Tickr apps (runtime/alerts.py ApnsNotifier) and its device token store.

Every key here is generated in the test; the network is a fake transport, or an in-process
HTTP/2 server on a socket pair for the wire framing."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import socket
import stat
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import h2.config
import h2.connection
import h2.events
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from tickr import config as config_module
from tickr.api.contract import PushPlatform, PushTokenKind
from tickr.runtime.alert_routing import LOCKED
from tickr.runtime.alerts import (
    APNS_PATH,
    PUSH,
    TELEGRAM,
    Alerter,
    AlertKind,
    ApnsNotifier,
    Channels,
    Delivery,
    NoRecipient,
    NotifyError,
    ProviderToken,
    apns_missing,
    apns_on,
    apns_payload,
    default_notifier,
    h2_post,
)
from tickr.runtime.push_tokens import PushToken, PushTokenStore, short_hash

from .test_alerts import CONFIG

BOT = Path(__file__).resolve().parents[2] / "config" / "bot.toml"
IOS, MACOS = PushPlatform.IOS, PushPlatform.MACOS
ALERT = PushTokenKind.ALERT

APNS = replace(CONFIG, apns_key_id_variable="TICKR_TEST_APNS_KEY_ID",
               apns_team_id_variable="TICKR_TEST_APNS_TEAM_ID",
               apns_key_path_variable="TICKR_TEST_APNS_KEY_PATH")
KEY_ID = "KEYID12345"
TEAM_ID = "TEAMID6789"
IOS_TOKEN = "a1" * 32
MAC_TOKEN = "b2" * 32
AT = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
NORMAL = AlertKind.DISK_LOW
CRITICAL = AlertKind.UNPROTECTED_POSITION


@pytest.fixture
def key(tmp_path):
    """A P-256 key made for this test (never a real one), written as Apple's .p8 is."""
    private = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "AuthKey_TEST.p8"
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    path.chmod(0o600)
    return private, path


@pytest.fixture
def environ(key):
    return {APNS.apns_key_id_variable: KEY_ID, APNS.apns_team_id_variable: TEAM_ID,
            APNS.apns_key_path_variable: str(key[1])}


@pytest.fixture
def store(tmp_path):
    store = PushTokenStore(tmp_path / "data" / APNS.push_tokens_file)
    store.register("phone", PushToken(ALERT, IOS_TOKEN, "development", "bot.tickr.ios", IOS,
                                      AT))
    store.register("local", PushToken(ALERT, MAC_TOKEN, "production", "bot.tickr.mac", MACOS,
                                      AT))
    return store


class Wire:
    """A fake HTTP/2 transport: records each request, answers from `answers` by device token."""

    def __init__(self, answers=None) -> None:
        self.calls: list[tuple[str, str, dict[str, str], dict]] = []
        self.answers = answers or {}

    def __call__(self, host, path, headers, body, timeout):
        self.calls.append((host, path, dict(headers), json.loads(body)))
        answer = self.answers.get(path.removeprefix(APNS_PATH), (200, b""))
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _critical(kind: str) -> bool:
    return kind in {k.value for k in LOCKED}


def notifier(store, environ, wire, clock=None, config=APNS):
    return ApnsNotifier(config, store, environ, wire, critical=_critical,
                        clock=clock or Clock())


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _jwt(headers: dict[str, str]) -> str:
    scheme, token = headers["authorization"].split(" ")
    assert scheme == "bearer"
    return token


# ---------------------------------------------------------------- provider token


def test_provider_token_is_an_es256_jwt_with_kid_iss_and_iat(key, environ):
    clock = Clock()
    token = ProviderToken(APNS, environ, clock).get()
    head, claims, signature = token.split(".")
    assert json.loads(_b64(head)) == {"alg": "ES256", "kid": KEY_ID}
    assert json.loads(_b64(claims)) == {"iss": TEAM_ID, "iat": int(clock.now)}
    raw = _b64(signature)
    assert len(raw) == 64  # raw r || s, as JWS ES256 requires (not DER)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key[0].public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_provider_token_is_reused_within_its_lifetime_and_signed_again_after(key, environ):
    clock = Clock()
    tokens = ProviderToken(APNS, environ, clock)
    first = tokens.get()
    clock.now += APNS.apns_token_lifetime_seconds - 1
    assert tokens.get() == first
    clock.now += 1
    second = tokens.get()
    assert second != first
    assert json.loads(_b64(second.split(".")[1]))["iat"] == int(clock.now)
    # A changed variable is a new key: signed again at once.
    environ[APNS.apns_key_id_variable] = "OTHERKEY99"
    assert json.loads(_b64(tokens.get().split(".")[0]))["kid"] == "OTHERKEY99"
    # APNs said it expired: forgotten, the next send signs again.
    third = tokens.get()
    tokens.forget()
    clock.now += 1
    assert tokens.get() != third


def test_the_lifetime_stays_below_apples_hour():
    assert 0 < config_module.load(BOT).alerts.apns_token_lifetime_seconds < 60 * 60


# ---------------------------------------------------------------- sending


def test_one_push_per_device_to_the_host_of_its_environment(store, environ):
    wire = Wire()
    notifier(store, environ, wire).notify_kind(NORMAL.value, "Tickr sandbox: disk-low", "12 MB")
    assert sorted((host, path) for host, path, _, _ in wire.calls) == [
        (APNS.apns_hosts["production"], APNS_PATH + MAC_TOKEN),
        (APNS.apns_hosts["development"], APNS_PATH + IOS_TOKEN)]
    for _, path, headers, _ in wire.calls:
        topic = "bot.tickr.ios" if path.endswith(IOS_TOKEN) else "bot.tickr.mac"
        assert headers["apns-topic"] == topic
        assert headers["apns-push-type"] == "alert" and headers["apns-priority"] == "10"
    # One provider token for both sends.
    assert len({_jwt(headers) for _, _, headers, _ in wire.calls}) == 1


def test_payload_of_a_normal_kind_and_of_a_locked_kind():
    assert NORMAL not in LOCKED and CRITICAL in LOCKED
    normal = json.loads(apns_payload(NORMAL.value, "T", "body", False))
    assert normal == {"aps": {"alert": {"title": "T", "body": "body"}, "sound": "default",
                              "thread-id": "disk-low"}, "kind": "disk-low"}
    locked = json.loads(apns_payload(CRITICAL.value, "T", "body", True))
    assert locked["aps"]["interruption-level"] == "time-sensitive"
    assert locked["aps"]["thread-id"] == locked["kind"] == "unprotected-position"


def test_the_alerter_hands_the_kind_to_the_push_and_locked_kinds_are_time_sensitive(
        store, environ):
    wire = Wire()
    channels = Channels((), ((PUSH, notifier(store, environ, wire)),),
                        spawn=lambda run: _Inline(run))
    alerter = Alerter(CONFIG, "Tickr", channels)
    alerter.alert(NORMAL, "12 MB left")
    alerter.alert(CRITICAL, "AAPL has no stop")
    outcomes = alerter.completed()
    assert [o.delivery for o in outcomes] == [Delivery.SHOWN, Delivery.SHOWN]
    payloads = [body for *_, body in wire.calls]
    normal = [p for p in payloads if p["kind"] == NORMAL.value]
    locked = [p for p in payloads if p["kind"] == CRITICAL.value]
    assert len(normal) == len(locked) == 2  # both devices
    assert all("interruption-level" not in p["aps"] for p in normal)
    assert all(p["aps"]["interruption-level"] == "time-sensitive" for p in locked)
    assert all(p["aps"]["alert"] == {"title": "Tickr: unprotected-position",
                                      "body": "AAPL has no stop"} for p in locked)


class _Inline:
    def __init__(self, run) -> None:
        run()

    def is_alive(self) -> bool:
        return False

    def join(self) -> None:
        return None


@pytest.mark.parametrize("answer", [(410, b'{"reason":"Unregistered","timestamp":1}'),
                                    (400, b'{"reason":"BadDeviceToken"}')])
def test_a_gone_token_is_removed_and_named_by_its_short_hash(store, environ, answer):
    wire = Wire({IOS_TOKEN: answer})
    with pytest.raises(NotifyError) as error:
        notifier(store, environ, wire).notify_kind(NORMAL.value, "T", "x")
    text = str(error.value)
    assert f"device {short_hash(IOS_TOKEN)}: HTTP {answer[0]}" in text
    assert "(token removed)" in text and IOS_TOKEN not in text
    assert [t.token for t in store.read().values()] == [MAC_TOKEN]  # the other device kept
    assert len(wire.calls) == 2  # one attempt each, never retried


def test_other_refusals_keep_the_token_and_a_send_is_never_retried(store, environ):
    wire = Wire({IOS_TOKEN: (403, b'{"reason":"InvalidProviderToken"}')})
    with pytest.raises(NotifyError, match="HTTP 403 InvalidProviderToken"):
        notifier(store, environ, wire).notify_kind(NORMAL.value, "T", "x")
    assert len(store.read()) == 2 and len(wire.calls) == 2


def test_an_expired_provider_token_is_signed_again_at_the_next_alert(store, environ):
    clock = Clock()
    wire = Wire({IOS_TOKEN: (403, b'{"reason":"ExpiredProviderToken"}')})
    push = notifier(store, environ, wire, clock)
    with pytest.raises(NotifyError):
        push.notify_kind(NORMAL.value, "T", "x")
    first = _jwt(wire.calls[0][2])
    clock.now += 1
    wire.answers = {}
    push.notify_kind(NORMAL.value, "T", "x")
    assert _jwt(wire.calls[-1][2]) != first


def test_failures_name_status_or_reason_only(store, environ, key):
    secret_reason = json.dumps({"reason": IOS_TOKEN}).encode()  # not one CamelCase word
    wire = Wire({IOS_TOKEN: (500, secret_reason),
                 MAC_TOKEN: OSError(f"connect to {MAC_TOKEN} failed")})
    push = notifier(store, environ, wire)
    with pytest.raises(NotifyError) as error:
        push.notify_kind(NORMAL.value, "T", "x")
    text = str(error.value)
    jwt = _jwt(wire.calls[0][2])
    pem = key[1].read_text()
    for leaked in (IOS_TOKEN, MAC_TOKEN, jwt, KEY_ID, TEAM_ID, str(key[1]), pem):
        assert leaked not in text
    assert "HTTP 500" in text and "apns did not answer: OSError" in text


def test_an_unreadable_or_wrong_key_fails_without_its_content(store, environ, key, tmp_path):
    environ[APNS.apns_key_path_variable] = str(tmp_path / "missing.p8")
    with pytest.raises(NotifyError, match="apns-key-unreadable: FileNotFoundError") as error:
        notifier(store, environ, Wire()).notify_kind(NORMAL.value, "T", "x")
    assert "missing.p8" not in str(error.value)
    rsa_path = tmp_path / "rsa.p8"
    rsa_path.write_bytes(rsa.generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    environ[APNS.apns_key_path_variable] = str(rsa_path)
    with pytest.raises(NotifyError, match="apns-key-invalid: not a P-256 key"):
        notifier(store, environ, Wire()).notify_kind(NORMAL.value, "T", "x")
    garbage = tmp_path / "garbage.p8"
    garbage.write_text("-----BEGIN PRIVATE KEY-----\nsecretbytes\n-----END PRIVATE KEY-----\n")
    environ[APNS.apns_key_path_variable] = str(garbage)
    with pytest.raises(NotifyError) as error:
        notifier(store, environ, Wire()).notify_kind(NORMAL.value, "T", "x")
    assert "secretbytes" not in str(error.value)


def test_the_sender_never_signs_for_a_topic_outside_the_configured_list(store, environ):
    wire = Wire()
    narrowed = replace(APNS, apns_topics=("bot.tickr.ios",))
    with pytest.raises(NotifyError, match="topic-refused") as error:
        notifier(store, environ, wire, config=narrowed).notify_kind(NORMAL.value, "T", "x")
    assert short_hash(MAC_TOKEN) in str(error.value)
    assert [path for _, path, _, _ in wire.calls] == [APNS_PATH + IOS_TOKEN]


def test_no_registered_device_and_an_unreadable_store_are_failures(tmp_path, environ):
    empty = PushTokenStore(tmp_path / "none.json")
    with pytest.raises(NotifyError, match="apns-no-device"):
        notifier(empty, environ, Wire()).notify_kind(NORMAL.value, "T", "x")
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"schema_version": 1, "devices": {"x": {"token": IOS_TOKEN}}}))
    with pytest.raises(NotifyError, match="apns-store-unreadable") as error:
        notifier(PushTokenStore(broken), environ, Wire()).notify_kind(NORMAL.value, "T", "x")
    assert IOS_TOKEN not in str(error.value)


def test_no_registered_device_is_journaled_once_per_empty_spell(tmp_path, environ, store):
    """Fable #828 note 4: with push on and no phone yet, the first alert records the failed
    push; later alerts record nothing for push (no outcome, never `shown`) until a device
    registers, and an empty store again is named again once."""
    empty = PushTokenStore(tmp_path / "data" / "empty.json")
    push = notifier(empty, environ, Wire())
    alerter = Alerter(CONFIG, "Tickr", Channels((), ((PUSH, push),),
                                                spawn=lambda run: _Inline(run)))
    alerter.alert(NORMAL, "first")
    first = alerter.completed()
    assert [(o.delivery, o.channel) for o in first] == [(Delivery.FAILED, PUSH)]
    assert "apns-no-device" in first[0].error
    alerter.alert(NORMAL, "second")
    alerter.alert(CRITICAL, "third")
    assert alerter.completed() == []  # nothing to journal for push, nothing counted as shown
    alerter.alert(NORMAL, "second")  # not suppressed as a repeat: it was never shown
    assert alerter.completed() == []
    # A device registers: pushes go out; it goes away: the empty store is named once more.
    empty.register("phone", PushToken(ALERT, IOS_TOKEN, "development", "bot.tickr.ios", IOS,
                                      AT))
    alerter.alert(NORMAL, "fourth")
    assert [o.delivery for o in alerter.completed()] == [Delivery.SHOWN]
    empty.remove("phone")
    alerter.alert(NORMAL, "fifth")
    assert [o.delivery for o in alerter.completed()] == [Delivery.FAILED]
    with pytest.raises(NoRecipient):
        push.notify_kind(NORMAL.value, "T", "sixth")


class _Boom(Exception):
    pass


def test_an_unexpected_error_never_leaves_notify_kind_as_anything_but_notify_error(
        store, environ):
    """Fable #828 note 2: the same last-resort wrapper as Telegram, so a direct caller is
    never broken by a push; only the type is named, the chain is dropped."""
    def critical(_kind: str) -> bool:
        raise _Boom(IOS_TOKEN)

    push = ApnsNotifier(APNS, store, environ, Wire(), critical=critical, clock=Clock())
    with pytest.raises(NotifyError) as error:
        push.notify_kind(CRITICAL.value, "T", "x")
    assert str(error.value) == "apns failed: _Boom"
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert IOS_TOKEN not in str(error.value)


@pytest.mark.parametrize("answer", [KeyError(":status"), ValueError("abc"), _Boom("x")])
def test_any_transport_error_is_one_devices_failure(store, environ, answer):
    wire = Wire({IOS_TOKEN: answer})
    with pytest.raises(NotifyError) as error:
        notifier(store, environ, wire).notify_kind(NORMAL.value, "T", "x")
    assert f"apns did not answer: {type(answer).__name__}" in str(error.value)
    assert len(wire.calls) == 2  # the other device was still pushed, once


def test_the_store_lock_is_held_across_processes(store):
    """Fable #828 note 1: the API and the bot are different processes. A writer holding the
    sibling lock file (as another process would) makes a removal wait, so a registration in
    between is never dropped."""
    fd = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    done = threading.Event()
    remover = threading.Thread(target=lambda: (store.remove_token(MAC_TOKEN), done.set()))
    remover.start()
    try:
        assert not done.wait(0.3)  # blocked on the other holder's lock
        # The other "process" registers while it holds the lock (read-modify-write by hand).
        other = PushTokenStore(store.path)
        tokens = other.read()
        tokens[("tablet", ALERT)] = PushToken(ALERT, "e5" * 32, "development",
                                              "bot.tickr.ios", IOS, AT)
        from tickr.runtime import push_tokens
        push_tokens._write_private(store.path, push_tokens._text(tokens))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    remover.join(5)
    assert done.is_set()
    assert {k: t.token for k, t in store.read().items()} == {
        ("phone", ALERT): IOS_TOKEN, ("tablet", ALERT): "e5" * 32}
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_alerts_imports_without_h2_or_cryptography(tmp_path):
    """Fable #828 note 3: the bot, the watchdog and the API import runtime/alerts.py; only
    the APNs code path needs h2 and cryptography."""
    import subprocess
    import sys
    blocker = tmp_path / "block"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('h2', 'cryptography'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "sys.meta_path.insert(0, _Block())\n", "utf-8")
    src = Path(__file__).resolve().parents[2] / "src"
    code = ("import tickr.runtime.alerts, tickr.runtime.preflight, tickr.runtime.watchdog\n"
            "import sys\n"
            "assert not any(m.split('.')[0] in ('h2', 'cryptography') for m in sys.modules)\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env={"PYTHONPATH": f"{blocker}{os.pathsep}{src}",
                                 "PATH": os.environ.get("PATH", "")},
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr[-2000:]


# ---------------------------------------------------------------- setup


def test_on_and_missing_check_presence_only(environ):
    assert apns_on(APNS, {}) is False and apns_missing(APNS, {}) == []
    half = {APNS.apns_key_id_variable: KEY_ID}
    assert apns_on(APNS, half) is True
    assert apns_missing(APNS, half) == [APNS.apns_team_id_variable,
                                        APNS.apns_key_path_variable]
    assert apns_missing(APNS, environ) == []


def test_default_notifier_adds_push_once_set_up_and_given_the_data_folder(tmp_path, environ):
    assert default_notifier(APNS, "linux", {}, data_dir=tmp_path) is None
    both = replace(APNS, bot_token_variable="TICKR_TEST_TG_TOKEN",
                   chat_id_variable="TICKR_TEST_TG_CHAT")
    telegram = {**environ, both.bot_token_variable: "set", both.chat_id_variable: "set"}
    channels = default_notifier(both, "linux", telegram, data_dir=tmp_path)
    assert isinstance(channels, Channels) and channels.background == (TELEGRAM, PUSH)
    mac = default_notifier(APNS, "darwin", environ, data_dir=tmp_path)
    assert PUSH in mac.background
    no_folder = default_notifier(APNS, "darwin", environ)
    assert PUSH not in no_folder.background


def test_push_is_never_the_only_channel(tmp_path, environ):
    """Push reaches nobody until a phone registered: with no other owner channel there is no
    notifier at all, so the watchdog and preflight refuse instead of starting deaf."""
    assert default_notifier(APNS, "linux", environ, data_dir=tmp_path) is None
    feed = tmp_path / "alerts.jsonl"
    assert default_notifier(APNS, "linux", environ, feed=feed, data_dir=tmp_path) is None


def test_store_file_is_private_and_replaces_per_device(store):
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.register("phone", PushToken(ALERT, MAC_TOKEN, "production", "bot.tickr.ios", IOS, AT))
    # The same device token under another key is dropped: one device is pushed once.
    assert list(store.read()) == [("phone", ALERT)]
    # Another kind is stored beside it and replaced on its own.
    start = PushToken(PushTokenKind.ACTIVITY_START, "c3" * 32, "production", "bot.tickr.ios",
                      IOS, AT)
    store.register("phone", start)
    store.register("phone", replace(start, token="d4" * 32))
    assert {k: t.token for k, t in store.read().items()} == {
        ("phone", ALERT): MAC_TOKEN, ("phone", PushTokenKind.ACTIVITY_START): "d4" * 32}
    assert store.remove("phone") is True and store.remove("phone") is False
    assert list(store.read()) == [("phone", PushTokenKind.ACTIVITY_START)]
    assert store.remove("phone", PushTokenKind.ACTIVITY_START) is True
    assert store.read() == {}


def test_live_activity_tokens_are_stored_but_never_sent_alerts(store, environ):
    for kind, token in ((PushTokenKind.ACTIVITY_START, "c3" * 32),
                        (PushTokenKind.ACTIVITY_UPDATE, "d4" * 32)):
        store.register("phone", PushToken(kind, token, "development", "bot.tickr.ios", IOS, AT))
    wire = Wire()
    notifier(store, environ, wire).notify_kind(NORMAL.value, "T", "x")
    assert sorted(path for _, path, _, _ in wire.calls) == [APNS_PATH + IOS_TOKEN,
                                                            APNS_PATH + MAC_TOKEN]


# ---------------------------------------------------------------- HTTP/2 framing


def _serve_once(sock: socket.socket, status: int | str, body: bytes, seen: dict) -> None:
    """A minimal HTTP/2 server: reads one request, answers `status` with `body`."""
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False,
                                                                 header_encoding="utf-8"))
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    data = bytearray()
    while True:
        chunk = sock.recv(65535)
        if not chunk:
            return
        for event in conn.receive_data(chunk):
            if isinstance(event, h2.events.RequestReceived):
                seen["headers"] = dict(event.headers)
            elif isinstance(event, h2.events.DataReceived):
                data += event.data
                conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded):
                seen["body"] = bytes(data)
                conn.send_headers(event.stream_id, [(":status", str(status))],
                                  end_stream=not body)
                if body:
                    conn.send_data(event.stream_id, body, end_stream=True)
                sock.sendall(conn.data_to_send())
                return
        sock.sendall(conn.data_to_send())


@pytest.mark.parametrize(("status", "body"), [(200, b""), (410, b'{"reason":"Unregistered"}')])
def test_h2_post_speaks_http2(status, body):
    client, server = socket.socketpair()
    seen: dict = {}
    thread = threading.Thread(target=_serve_once, args=(server, status, body, seen))
    thread.start()
    try:
        answer = h2_post("apns.test", APNS_PATH + IOS_TOKEN, [("apns-topic", "bot.tickr.ios")],
                         b'{"aps":{}}', 5.0, connect=lambda _host, _timeout: client)
    finally:
        thread.join(5)
        server.close()
    assert answer == (status, body)
    assert seen["headers"][":method"] == "POST"
    assert seen["headers"][":path"] == APNS_PATH + IOS_TOKEN
    assert seen["headers"][":authority"] == "apns.test"
    assert seen["headers"]["apns-topic"] == "bot.tickr.ios"
    assert seen["body"] == b'{"aps":{}}'


def test_h2_post_refuses_an_answer_without_a_numeric_status():
    client, server = socket.socketpair()
    thread = threading.Thread(target=_serve_once, args=(server, "abc", b"", {}))
    thread.start()
    try:
        with pytest.raises(NotifyError, match="without a numeric status"):
            h2_post("apns.test", APNS_PATH + IOS_TOKEN, [], b"{}", 5.0,
                    connect=lambda _host, _timeout: client)
    finally:
        thread.join(5)
        server.close()


def test_h2_post_refuses_a_connection_closed_before_the_answer():
    client, server = socket.socketpair()
    server.close()
    with pytest.raises((NotifyError, OSError)):
        h2_post("apns.test", APNS_PATH + IOS_TOKEN, [], b"{}", 5.0,
                connect=lambda _host, _timeout: client)
