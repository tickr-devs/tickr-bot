"""Owner alerts (spec section 11): a Telegram message to the owner's chat and a line in the
alerts feed file (`[alerts] feed_file`), which a Claude session waits on. Telegram is the one
channel that works on a server; the apps' APNs push (`ApnsNotifier`) is only ever an extra one.

`Alerter.alert(kind, text)` never raises and never retries. It returns an `AlertOutcome` that the
caller always writes to its journal, whether the notification was shown, suppressed as a repeat,
or could not be delivered. Repeats of the same kind and text inside the configured window are
suppressed so that a condition that persists does not flood the owner. The owner's routing
(`runtime/alert_routing.py`) may turn a kind off on some channels; critical kinds are never off,
and what was skipped is named in the outcome.

Alert text must never contain a secret value: callers pass variable NAMES, never contents.
The Telegram bot token and chat id are personal data: only the variable NAMES are configuration
(`[alerts] bot_token_variable`, `chat_id_variable`); the values are read from the
environment at the moment of one send and never stored, logged or put in an error text. The
channel is off while neither variable is set.
APNs push to the Tickr apps works the same way: `[alerts] apns_*_variable` NAME the key id, team
id and the path of the .p8 signing key; the key is read from that path only to sign a provider
token and is never logged. The apps' device tokens are personal data (`runtime/push_tokens.py`):
an error names a device by `short_hash` only.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from tickr.api.contract import PushTokenKind
from tickr.config import AlertsConfig
from tickr.runtime.push_tokens import (
    PushToken,
    PushTokenStore,
    StoreError,
    short_hash,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric import ec

# h2 and cryptography are imported inside the APNs code path only (signing, sending, the key
# check), so the bot, the watchdog and the API import this module on a machine without them
# and only a configured push needs them.

FEED = "feed"
TELEGRAM = "telegram"
PUSH = "push"
NOTIFIER = "notifier"  # name of a single channel given to Alerter


class AlertKind(StrEnum):
    """Every alert the owner can receive (spec section 11). Values are stable journal names."""

    UNPROTECTED_POSITION = "unprotected-position"
    STOP_REJECTED = "stop-rejected"
    # A closing order was refused; the stop is back and the operator must look.
    EXIT_REFUSED = "exit-refused"
    UNKNOWN_ORDER_STATUS = "unknown-order-status"
    FOREIGN_EXPOSURE = "foreign-exposure"
    BROKER_READS_FAILING = "broker-reads-failing"
    STREAM_LOST_WITH_POSITION = "stream-lost-with-position"
    DAILY_CEILING_REACHED = "daily-ceiling-reached"
    BOT_CRASHED = "bot-crashed"
    BOT_DID_NOT_START = "bot-did-not-start"
    # Added to the spec list by review of #13: SIGTERM forwarded, bot still running.
    BOT_DID_NOT_STOP = "bot-did-not-stop"
    PREFLIGHT_FAILED = "preflight-failed"
    TOKEN_NEEDS_CONFIRMATION = "token-needs-confirmation"
    # The observer queue overflowed: observers are switched off for the run (trading goes on).
    OBSERVERS_STOPPED = "observers-stopped"
    # The active strategy or its settings version (backlog S3) was refused at start: the bot
    # protects and reconciles, but opens no entries this run.
    STRATEGY_NOT_LOADED = "strategy-not-loaded"
    # Broker reads after the flatten time (plus grace) still show our position or order.
    NOT_FLAT_AFTER_FLATTEN = "not-flat-after-flatten"
    # Raised by the separate watchdog job (runtime/watchdog.py), which outlives a crashed bot:
    # broker reads after the flatten time show any position or any open order;
    WATCHDOG_NOT_FLAT = "watchdog-not-flat"
    # the watchdog could not tell (broker read, database or calendar failed);
    WATCHDOG_CANNOT_CHECK = "watchdog-cannot-check"
    # during the session the bot process is not running while the database shows an open trade.
    BOT_DOWN_WITH_OPEN_TRADE = "bot-down-with-open-trade"
    # at any hour the bot is not running (or hung) although its last status was not a clean stop;
    BOT_DOWN = "bot-down"
    # at any hour the bot's data disk has less free space than [runtime] disk_warn_free_mb;
    DISK_LOW = "disk-low"
    # near the flatten time the watchdog started the bot job (once a day) so it flattens;
    WATCHDOG_RESTARTED_BOT = "watchdog-restarted-bot"
    # it would have, but may not (stop marker present, restart record unreadable, day unknown);
    WATCHDOG_RESTART_REFUSED = "watchdog-restart-refused"
    # the restart failed: the command failed, or at a later check the bot is still down or the
    # broker is still not flat. The loudest watchdog alert: act by hand now.
    WATCHDOG_RESTART_FAILED = "watchdog-restart-failed"
    # A closing order is still not filled [orders] max_exit_seconds after it was sent.
    EXIT_NOT_FILLED = "exit-not-filled"
    # A close (session flatten or any other exit) was due outside the regular session: no
    # MARKET order was sent and the stop stays; the owner closes by hand (fence market-closed).
    FLATTEN_AFTER_CLOSE = "flatten-after-close"
    # A finished order with fills still has no recorded broker fees after
    # [orders] fees_unknown_alert_seconds: today's money is unknown, so entries are refused.
    FEES_UNKNOWN = "fees-unknown"
    # The account's cash activity proved an order's recorded fees wrong; they were replaced.
    FEES_CORRECTED = "fees-corrected"
    # Raised by the API at its start: it holds a credential variable its launchd job must not
    # give it (any, while [chart] webull_enabled is false; any but the active environment's app
    # key and secret while it is true): its plist is stale or hand-edited (runtime/launchd.py).
    API_CREDENTIALS_UNEXPECTED = "api-credentials-unexpected"
    # A change of config/bot.toml the running bot did not adopt: invalid or unreadable, looser
    # protection, or a setting outside live protection (runtime/liveprotection.py).
    PROTECTION_CHANGE_IGNORED = "protection-change-ignored"
    # Raised by the API when a client address used up its failed-authentication budget on the
    # remote port (device token or POST /pair), or every new address is refused because the
    # address table is full of recent failures (api/authlimit.py).
    REMOTE_AUTH_BLOCKED = "remote-auth-blocked"
    # Raised by the API for each accepted entry-stop "off" or start-bot: the device (by name),
    # the port and the time, so a stolen device token shows up at once.
    RISK_COMMAND_USED = "risk-command-used"
    # Raised by the API when a device signs in with tickr.bot, signs out, or is signed out
    # because a retired refresh token came back: whoever controls tickr.bot's signing key could
    # sign a device in, so every new device shows up at once.
    DEVICE_SESSION = "device-session"


class Delivery(StrEnum):
    SHOWN = "shown"
    SUPPRESSED_REPEAT = "suppressed-repeat"
    NO_NOTIFIER = "no-notifier"
    SENDING = "sending"  # background channels only; their outcomes follow
    FAILED = "failed"
    # The owner's routing turned this kind off on every channel this process has.
    ROUTED_OFF = "routed-off"


class NotifyError(Exception):
    """The notification could not be shown; the message says why."""


class NoRecipient(NotifyError):
    """The channel has nobody to send to and already said so in this process (the apps' push
    before any device registered). Nothing is sent; the alert gets no outcome for this channel,
    so the journal names the empty channel once, not on every alert."""


class Notifier(Protocol):
    """One alert delivery channel (`TelegramNotifier`, `FeedNotifier`), or several at once
    (`Channels`). `default_notifier` chooses them for the bot and the watchdog alike."""

    def notify(self, title: str, text: str) -> None:
        """Show one notification or raise NotifyError."""


# Telegram Bot API protocol constants: the method, its JSON fields and the HTTPS scheme.
TELEGRAM_SCHEME = "https"
TELEGRAM_METHOD = "sendMessage"
HTTP_OK = 200
Opener = Callable[[urllib.request.Request, float], Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx answer is refused, never followed: the bot token is in the request path."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return _OPENER.open(request, timeout=timeout)


def telegram_on(config: AlertsConfig, environ: Mapping[str, str]) -> bool:
    """Whether the owner set up Telegram: either variable is present (presence only). Neither
    present = the channel is off; one alone is a half setup that preflight refuses."""
    return any(environ.get(name) for name in (config.bot_token_variable, config.chat_id_variable)
               if name)


def telegram_missing(config: AlertsConfig, environ: Mapping[str, str]) -> list[str]:
    """NAMES of the Telegram variables not set (empty when it is off or complete). Presence
    only: the values are compared with "" and never stored, printed or returned."""
    if not telegram_on(config, environ):
        return []
    return [name for name in (config.bot_token_variable, config.chat_id_variable)
            if not environ.get(name)]


class TelegramNotifier:
    """One Telegram message to the owner's chat (HTTPS POST to the Bot API), so the owner is
    told away from the desk. One attempt per alert, never retried. Errors name the failure kind
    or the HTTP status only: the request address holds the bot token."""

    def __init__(self, config: AlertsConfig, environ: Mapping[str, str] = os.environ,
                 opener: Opener = _open) -> None:
        self._config = config
        self._environ = environ
        self._timeout = float(config.notification_timeout_seconds)
        self._opener = opener

    def notify(self, title: str, text: str) -> None:
        try:
            self._send(title, text)
        except NotifyError:
            raise
        except Exception as exc:  # noqa: BLE001 - a Telegram failure never breaks the caller
            # Only the type: the message or the traceback of an unexpected error may hold the
            # request address (with the token) or the chat id. `from None` drops the chain.
            raise NotifyError(f"telegram failed: {type(exc).__name__}") from None

    def _send(self, title: str, text: str) -> None:
        missing = telegram_missing(self._config, self._environ)
        if missing:
            raise NotifyError("telegram-missing: " + ", ".join(missing) + " not set")
        token = self._environ[self._config.bot_token_variable]
        body = json.dumps({"chat_id": self._environ[self._config.chat_id_variable],
                           "text": f"{title}\n{text}"}).encode()
        url = f"{TELEGRAM_SCHEME}://{self._config.telegram_host}/bot{token}/{TELEGRAM_METHOD}"
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener(request, self._timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            raise NotifyError(f"telegram answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NotifyError(f"telegram did not answer: {type(exc).__name__}") from None
        if status != HTTP_OK:
            raise NotifyError(f"telegram answered HTTP {status}")


# APNs protocol constants (Apple, "Sending notification requests to APNs" and "Establishing a
# token-based connection to APNs"): HTTPS port, HTTP/2 by ALPN, the request path, the headers and
# answers this sender reads, and the ES256 provider token (a JWT signed with P-256 and SHA-256,
# its signature the raw 32-byte r and s).
APNS_PORT = 443
APNS_ALPN = "h2"
APNS_PATH = "/3/device/"
APNS_PUSH_TYPE = "alert"
APNS_PRIORITY = "10"  # deliver at once (alerts are never deferred to save power)
APNS_SOUND = "default"
APNS_CRITICAL_LEVEL = "time-sensitive"  # breaks through Focus; iOS uses "active" until allowed
APNS_GONE = 410  # the device token is no longer active for the topic
APNS_BAD_DEVICE_TOKEN = "BadDeviceToken"
APNS_EXPIRED_TOKEN = "ExpiredProviderToken"
APNS_REASON = re.compile(r"^[A-Za-z]{1,64}$")  # Apple's reasons are one CamelCase word
JWT_ALGORITHM = "ES256"
ES256_COORDINATE_BYTES = 32
H2_READ_BYTES = 65535  # one socket read: the HTTP/2 default flow-control window


def apns_names(config: AlertsConfig) -> tuple[str, str, str]:
    return (config.apns_key_id_variable, config.apns_team_id_variable,
            config.apns_key_path_variable)


def apns_on(config: AlertsConfig, environ: Mapping[str, str]) -> bool:
    """Whether the owner set up APNs: any of its variables is present (presence only). None
    present = the push channel is off; some alone is a half setup that preflight refuses."""
    return any(environ.get(name) for name in apns_names(config) if name)


def apns_missing(config: AlertsConfig, environ: Mapping[str, str]) -> list[str]:
    """NAMES of the APNs variables not set (empty when it is off or complete). Presence only."""
    if not apns_on(config, environ):
        return []
    return [name for name in apns_names(config) if not environ.get(name)]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_provider_token(key: ec.EllipticCurvePrivateKey, key_id: str, team_id: str,
                        issued_at: int) -> str:
    """The APNs provider token: an ES256 JWT {alg, kid} . {iss, iat}."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    header = _b64url(json.dumps({"alg": JWT_ALGORITHM, "kid": key_id},
                                separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iss": team_id, "iat": issued_at},
                                separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = (r.to_bytes(ES256_COORDINATE_BYTES, "big")
                 + s.to_bytes(ES256_COORDINATE_BYTES, "big"))
    return f"{header}.{claims}.{_b64url(signature)}"


def load_key(path: str) -> ec.EllipticCurvePrivateKey:
    """The .p8 signing key at `path`. Errors name the failure kind only, never the content.
    Preflight calls it too, so a wrong key file refuses the start, not the first alert."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise NotifyError(f"apns-key-unreadable: {type(exc).__name__}") from None
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise NotifyError("apns-key-invalid: not a PEM private key") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != ec.SECP256R1.name:
        raise NotifyError("apns-key-invalid: not a P-256 key")
    return key


class ProviderToken:
    """The signed provider token, reused for `[alerts] apns_token_lifetime_seconds` (below
    Apple's one hour) and signed again after that, or when a variable changes. Thread-safe: the
    background sends share it."""

    def __init__(self, config: AlertsConfig, environ: Mapping[str, str],
                 clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._environ = environ
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[tuple[str, str, str], float, str] | None = None

    def get(self) -> str:
        """The token; NotifyError when the key cannot be read or is not a P-256 key."""
        names = apns_names(self._config)
        ids = (self._environ[names[0]], self._environ[names[1]], self._environ[names[2]])
        now = self._clock()
        with self._lock:
            if self._cached is not None:
                cached_ids, issued_at, token = self._cached
                if (cached_ids == ids
                        and now - issued_at < self._config.apns_token_lifetime_seconds):
                    return token
            issued = int(now)
            token = sign_provider_token(load_key(ids[2]), ids[0], ids[1], issued)
            self._cached = (ids, issued, token)
            return token

    def forget(self) -> None:
        """APNs said the token expired: the next send signs a new one."""
        with self._lock:
            self._cached = None


# One HTTP/2 POST: (host, path, headers, body, timeout seconds) -> (status, response body).
Transport = Callable[[str, str, list[tuple[str, str]], bytes, float], tuple[int, bytes]]
Connect = Callable[[str, float], Any]


def _tls_connect(host: str, timeout: float) -> ssl.SSLSocket:
    context = ssl.create_default_context()
    context.set_alpn_protocols([APNS_ALPN])
    raw = socket.create_connection((host, APNS_PORT), timeout=timeout)
    try:
        sock = context.wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise
    if sock.selected_alpn_protocol() != APNS_ALPN:
        sock.close()
        raise NotifyError("apns did not agree to HTTP/2")
    return sock


def h2_post(host: str, path: str, headers: list[tuple[str, str]], body: bytes,
            timeout: float, connect: Connect = _tls_connect) -> tuple[int, bytes]:
    """One POST on a new HTTP/2 connection, closed after the answer. Raises NotifyError (the
    stream or connection ended without an answer, or an answer without a numeric status),
    OSError or an h2 error."""
    import h2.config
    import h2.connection
    import h2.events
    sock = connect(host, timeout)
    try:
        conn = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
        conn.initiate_connection()
        stream = conn.get_next_available_stream_id()
        conn.send_headers(stream, [(":method", "POST"), (":scheme", "https"),
                                   (":authority", host), (":path", path), *headers,
                                   ("content-length", str(len(body)))])
        conn.send_data(stream, body, end_stream=True)
        sock.sendall(conn.data_to_send())
        status: int | None = None
        data = bytearray()
        while True:
            chunk = sock.recv(H2_READ_BYTES)
            if not chunk:
                raise NotifyError("apns closed the connection before answering")
            for event in conn.receive_data(chunk):
                if isinstance(event, h2.events.ResponseReceived) and event.stream_id == stream:
                    answered = dict(event.headers).get(":status")
                    if not (isinstance(answered, str) and answered.isdigit()):
                        raise NotifyError("apns answered without a numeric status")
                    status = int(answered)
                elif isinstance(event, h2.events.DataReceived) and event.stream_id == stream:
                    data += event.data
                    conn.acknowledge_received_data(event.flow_controlled_length, stream)
                elif isinstance(event, h2.events.StreamEnded) and event.stream_id == stream:
                    if status is None:
                        raise NotifyError("apns ended the stream without a status")
                    return status, bytes(data)
                elif isinstance(event, h2.events.StreamReset) and event.stream_id == stream:
                    raise NotifyError(f"apns reset the stream (HTTP/2 error {event.error_code})")
                elif isinstance(event, h2.events.ConnectionTerminated):
                    raise NotifyError(f"apns closed the connection (HTTP/2 error "
                                      f"{event.error_code})")
            pending = conn.data_to_send()
            if pending:
                sock.sendall(pending)
    finally:
        sock.close()


def apns_payload(kind: str | None, title: str, text: str, critical: bool) -> bytes:
    """The notification: alert title and body with the default sound, grouped by kind
    (`thread-id`, and `kind` for the app); a critical kind is time-sensitive."""
    aps: dict[str, Any] = {"alert": {"title": title, "body": text}, "sound": APNS_SOUND}
    body: dict[str, Any] = {"aps": aps}
    if kind is not None:
        aps["thread-id"] = kind
        body["kind"] = kind
    if critical:
        aps["interruption-level"] = APNS_CRITICAL_LEVEL
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _apns_reason(body: bytes) -> str:
    """Apple's reason word from an error answer, or "" (anything else is never echoed)."""
    try:
        reason = json.loads(body).get("reason")
    except (ValueError, AttributeError):
        return ""
    return reason if isinstance(reason, str) and APNS_REASON.match(reason) else ""


def _never_critical(_kind: str) -> bool:
    return False


class Recipients(NamedTuple):
    """`ApnsClient.devices`: the tokens to push to, and what went wrong on the way (never a
    token)."""

    tokens: list[PushToken]
    failures: tuple[str, ...]


class ApnsClient:
    """One APNs POST to one registered device of the Tickr apps (`push_tokens.PushTokenStore`),
    to the APNs host of its build's environment, and only for a bundle id in `[alerts]
    apns_topics`; shared by the alert push (`ApnsNotifier`) and the Live Activity publisher
    (`runtime/live_activity.py`). One attempt, never retried. A token APNs says is gone (410, or
    BadDeviceToken) is removed and the removal named in the error, which the caller journals.
    A token whose device is signed out (`PushToken.expires_at`) is never a recipient and is
    dropped (`devices`). Errors name the device by `short_hash`, the HTTP status and Apple's
    reason only: never a device token, the key or the provider token."""

    def __init__(self, config: AlertsConfig, store: PushTokenStore,
                 environ: Mapping[str, str] = os.environ, transport: Transport = h2_post,
                 clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._store = store
        self._environ = environ
        self._transport = transport
        self._timeout = float(config.notification_timeout_seconds)
        self._clock = clock
        self._provider = ProviderToken(config, environ, clock)

    def devices(self, kind: PushTokenKind) -> Recipients:
        """The live registered tokens of `kind`, in a stable order: the one place every APNs
        sender (alerts, Live Activity) takes its recipients from. Tokens whose device's sign-in
        has ended are left out and dropped from the store; a failed drop is named in
        `failures`, the tokens are left out all the same. NotifyError while APNs is not set up
        or the store cannot be read."""
        missing = apns_missing(self._config, self._environ)
        if missing or not apns_on(self._config, self._environ):
            raise NotifyError("apns-missing: " + ", ".join(missing or apns_names(self._config))
                              + " not set")
        now = datetime.fromtimestamp(self._clock(), UTC)
        try:
            tokens = self._store.read()
        except StoreError as exc:
            raise NotifyError(f"apns-store-unreadable: {exc}") from None
        failures = []
        if any(token.expired(now) for token in tokens.values()):
            # A signed-out device (it stopped refreshing): never pushed to, dropped or not.
            try:
                self._store.drop_expired(now)
            except (OSError, StoreError) as exc:
                failures.append(f"tokens of an ended sign-in not dropped: {type(exc).__name__}")
        live = [token for _key, token in sorted(tokens.items())
                if token.kind is kind and not token.expired(now)]
        return Recipients(live, tuple(failures))

    def send(self, device: PushToken, payload: bytes, *, push_type: str, priority: str,
             topic_suffix: str = "") -> str | None:
        """One push to one device on topic `<bundle id><topic_suffix>`; the error text, or None
        when APNs accepted it."""
        if device.bundle_id not in self._config.apns_topics:
            return "topic-refused: the bundle id is not in [alerts] apns_topics"
        host = self._config.apns_hosts.get(device.environment)
        if host is None:
            return "environment-unknown: not a key of [alerts] apns_hosts"
        try:
            jwt = self._provider.get()
        except NotifyError as exc:
            return str(exc)
        headers = [("authorization", f"bearer {jwt}"),
                   ("apns-topic", device.bundle_id + topic_suffix),
                   ("apns-push-type", push_type), ("apns-priority", priority),
                   ("content-type", "application/json")]
        try:
            status, body = self._transport(host, APNS_PATH + device.token,
                                           headers, payload, self._timeout)
        except NotifyError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - OSError, an h2 error: one device's failure
            # (h2 is imported only inside the transport.) Only the type, never the message.
            return f"apns did not answer: {type(exc).__name__}"
        if status == HTTP_OK:
            return None
        reason = _apns_reason(body)
        text = f"HTTP {status}" + (f" {reason}" if reason else "")
        if reason == APNS_EXPIRED_TOKEN:
            self._provider.forget()
        if status == APNS_GONE or reason == APNS_BAD_DEVICE_TOKEN:
            try:
                removed = self._store.remove_token(device.token)
            except (OSError, StoreError) as exc:
                return f"{text} (token not removed: {type(exc).__name__})"
            if removed:
                return f"{text} (token removed)"
        return text


class ApnsNotifier:
    """The alert push: one notification per device's `alert` token (`ApnsClient`). The Live
    Activity tokens are never pushed to here."""

    def __init__(self, config: AlertsConfig, store: PushTokenStore,
                 environ: Mapping[str, str] = os.environ, transport: Transport = h2_post,
                 critical: Callable[[str], bool] = _never_critical,
                 clock: Callable[[], float] = time.time) -> None:
        self._client = ApnsClient(config, store, environ, transport, clock)
        self._critical = critical
        # Whether "no device registered" was already reported by this process; set again
        # once a device is seen, so the journal names it once per empty spell, not per alert.
        self._reported_empty = False
        self._empty_lock = threading.Lock()

    def notify(self, title: str, text: str) -> None:
        self.notify_kind(None, title, text)

    def notify_kind(self, kind: str | None, title: str, text: str) -> None:
        try:
            self._push(kind, title, text)
        except NotifyError:
            raise
        except Exception as exc:  # noqa: BLE001 - a push failure never breaks the caller
            # Only the type: an unexpected error's message may hold a device or provider token.
            # `from None` drops the chain.
            raise NotifyError(f"apns failed: {type(exc).__name__}") from None

    def _push(self, kind: str | None, title: str, text: str) -> None:
        devices, dropped = self._client.devices(PushTokenKind.ALERT)
        with self._empty_lock:
            if not devices:
                if dropped:
                    raise NotifyError("apns-no-device: no app has a live push token; "
                                      + "; ".join(dropped))
                if self._reported_empty:
                    raise NoRecipient("apns-no-device: no app has registered a push token")
                self._reported_empty = True
                raise NotifyError("apns-no-device: no app has registered a push token")
            self._reported_empty = False
        payload = apns_payload(kind, title, text, kind is not None and self._critical(kind))
        failures = list(dropped)
        for device in devices:
            error = self._client.send(device, payload, push_type=APNS_PUSH_TYPE,
                                      priority=APNS_PRIORITY)
            if error is not None:
                failures.append(f"device {short_hash(device.token)}: {error}")
        if failures:
            raise NotifyError("apns: " + "; ".join(failures))


class FeedNotifier:
    """Appends one JSON line per alert ({at, title, text}) to the alerts feed file, so whatever
    waits on that file (a Claude session) learns of the alert at once. Every process
    that alerts appends to the same file; one short `write` in append mode per line. Raises
    NotifyError when the file cannot be written (a full disk, a missing folder)."""

    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self._path = path
        self._clock = clock or _utc_now

    def notify(self, title: str, text: str) -> None:
        line = json.dumps({"at": self._clock().isoformat(), "title": title, "text": text},
                          sort_keys=True) + "\n"
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            raise NotifyError(f"feed not written: {type(exc).__name__}") from exc


def feed_path(root: Path, data_dir: str, config: AlertsConfig) -> Path:
    """The alerts feed file: `[alerts] feed_file` under `[run] data_dir`."""
    return root / data_dir / config.feed_file


class Done(Protocol):
    """A background channel's result: its name, the error or None when shown, and `quiet`
    when it had nobody to send to and already said so (`NoRecipient`): no outcome follows."""

    def __call__(self, name: str, error: str | None, quiet: bool = False) -> None: ...


def _spawn_daemon(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, name="alert-channel", daemon=True)
    thread.start()
    return thread


class Channels:
    """Every alert through every channel; a failing channel never stops the others.

    `channels` run in the caller's thread. `background` channels each run once on a daemon
    thread, so the bot's loop never waits on the network; each
    reports its result through `done` exactly once. Never retried."""

    def __init__(self, channels: tuple[tuple[str, Notifier], ...],
                 background: tuple[tuple[str, Notifier], ...] = (),
                 spawn: Callable[[Callable[[], None]], threading.Thread] = _spawn_daemon,
                 ) -> None:
        self._channels = channels
        self.background = tuple(name for name, _ in background)
        self._background = background
        self._spawn = spawn
        self._threads: list[threading.Thread] = []

    def background_except(self, skip: frozenset[str]) -> tuple[str, ...]:
        """Names of the background channels a send that skips `skip` starts."""
        return tuple(name for name in self.background if name not in skip)

    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self._channels) + self.background

    def send(self, title: str, text: str, done: Done,
             skip: frozenset[str] = frozenset(),
             kind: str | None = None) -> list[tuple[str, str | None]]:
        """Start the background channels, run the others, all but those named in `skip`;
        (name, error or None) of the ones run in this thread. `kind` (an AlertKind value)
        reaches the channels that use it (the app push groups and ranks by it)."""
        self._threads = [t for t in self._threads if t.is_alive()]
        for name, channel in self._background:
            if name in skip:
                continue
            try:
                self._threads.append(self._spawn(_once(channel, name, kind, title, text, done)))
            except Exception as exc:  # noqa: BLE001 - a thread that cannot start
                # Reported like any failed send, so the caller never raises into the bot's
                # loop and never waits for an outcome that cannot come.
                done(name, f"not started: {type(exc).__name__}")
        results: list[tuple[str, str | None]] = []
        for name, channel in self._channels:
            if name in skip:
                continue
            try:
                _deliver(channel, kind, title, text)
            except NoRecipient:
                continue  # nobody to send to, already reported: no result
            except NotifyError as exc:
                results.append((name, str(exc)))
            else:
                results.append((name, None))
        return results

    def notify(self, title: str, text: str) -> None:
        failed = [f"{n}: {e}" for n, e in self.send(title, text, _ignore) if e is not None]
        if failed:
            raise NotifyError("; ".join(failed))

    def wait(self) -> None:
        """Until the background sends started so far have finished (each is bounded by its own
        timeout). For short-lived commands, which would otherwise exit first."""
        for thread in self._threads:
            thread.join()
        self._threads = []


def _ignore(_name: str, _error: str | None, _quiet: bool = False) -> None:
    return None


def _deliver(channel: Notifier, kind: str | None, title: str, text: str) -> None:
    if isinstance(channel, ApnsNotifier):
        channel.notify_kind(kind, title, text)
    else:
        channel.notify(title, text)


def _once(channel: Notifier, name: str, kind: str | None, title: str, text: str,
          done: Done) -> Callable[[], None]:
    def run() -> None:
        try:
            _deliver(channel, kind, title, text)
        except NoRecipient as exc:
            done(name, str(exc), quiet=True)
        except NotifyError as exc:
            done(name, str(exc))
        except Exception as exc:  # noqa: BLE001 - a background channel never raises upward
            done(name, f"{type(exc).__name__}")
        else:
            done(name, None)
    return run


def default_notifier(config: AlertsConfig, environ: Mapping[str, str] = os.environ,
                     feed: Path | None = None,
                     data_dir: Path | None = None) -> Notifier | None:
    """The channels for this machine: Telegram once one of its variables is set, the apps' push
    once an APNs variable is set (given the data folder that holds their device tokens) and,
    given its path, the alerts feed file, all in the background because the network can take
    seconds and the bot's loop must not wait for it. None when Telegram is not set up: the feed
    file alone is not a way to reach the owner (preflight refuses, `alert-channel-missing`). The
    same on every operating system.

    The push is only ever an extra channel: it reaches nobody until a phone registered, the
    token is still valid and notifications are allowed, none of which can be proved here. So
    without Telegram there is no notifier (None), push or not."""
    if not telegram_on(config, environ):
        return None
    background: list[tuple[str, Notifier]] = [(TELEGRAM, TelegramNotifier(config, environ))]
    if apns_on(config, environ) and data_dir is not None:
        # Imported here: alert_routing imports this module (its delivery names).
        from tickr.runtime.alert_routing import LOCKED
        locked = frozenset(kind.value for kind in LOCKED)
        store = PushTokenStore(data_dir / config.push_tokens_file)
        background.append((PUSH, ApnsNotifier(config, store, environ,
                                              critical=locked.__contains__)))
    if feed is not None:
        background.append((FEED, FeedNotifier(feed)))
    return Channels((), tuple(background))


@dataclass(frozen=True)
class AlertOutcome:
    """What happened to one alert. The caller journals `journal_body()`.

    `pending` names background channels still sending; each later yields its own outcome
    (from `Alerter.completed`) with `channel` set. `routed_off` names the channels the owner's
    routing skipped for this kind."""

    kind: AlertKind
    text: str
    at: datetime
    delivery: Delivery
    error: str | None
    pending: tuple[str, ...] = ()
    channel: str | None = None
    routed_off: tuple[str, ...] = ()

    def journal_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": self.kind.value,
            "text": self.text,
            "at": self.at.isoformat(),
            "delivery": self.delivery.value,
            "error": self.error,
        }
        if self.pending:
            body["pending"] = list(self.pending)
        if self.channel is not None:
            body["channel"] = self.channel
        if self.routed_off:
            body["routed_off"] = list(self.routed_off)
        return body


def _utc_now() -> datetime:
    return datetime.now(UTC)


# The channels (by their names in `Channels`) the owner's routing turned off for one kind;
# `alert_routing.Routing.skipped` reads the routing file on each call.
Routing = Callable[[AlertKind], frozenset[str]]


def route_all(_kind: AlertKind) -> frozenset[str]:
    return frozenset()


class Alerter:
    """Shows alerts with repeat suppression. One instance per process.

    An alert counts as shown (for repeat suppression) once any channel delivered it, so a
    channel that keeps failing does not repeat the others on every occurrence. While a
    background send of the same alert is still running it is a repeat too, so a condition
    raised on every loop pass starts one send, not one per pass. Background
    results arrive through `completed()`, which the owner of the journal calls regularly (the
    bot once per loop pass), and `flush()` before a short-lived process exits.

    `routing` names the channels to skip for a kind (the owner's routing; never a critical
    kind). An alert every channel of which is skipped is `routed-off`, never counted as shown."""

    def __init__(
        self,
        config: AlertsConfig,
        title: str,
        notifier: Notifier | None,
        clock: Callable[[], datetime] = _utc_now,
        routing: Routing = route_all,
    ) -> None:
        self._window = timedelta(seconds=float(config.dedupe_window_seconds))
        self._title = title
        self._channels = (notifier if isinstance(notifier, Channels) or notifier is None
                          else Channels(((NOTIFIER, notifier),)))
        self._clock = clock
        self._routing = routing
        self._last_shown: dict[tuple[AlertKind, str], datetime] = {}
        # Background sends of each alert not yet collected by completed().
        self._sending: dict[tuple[AlertKind, str], int] = {}
        self._done: queue.SimpleQueue[tuple[AlertKind, str, str, str | None, bool]] = \
            queue.SimpleQueue()

    def alert(self, kind: AlertKind, text: str) -> AlertOutcome:
        if not isinstance(kind, AlertKind):
            raise TypeError(f"kind must be an AlertKind, got {kind!r}")
        now = self._clock()
        key = (kind, text)
        last = self._last_shown.get(key)
        if (last is not None and now - last < self._window) or key in self._sending:
            return AlertOutcome(kind, text, now, Delivery.SUPPRESSED_REPEAT, None)
        if self._channels is None:
            return AlertOutcome(kind, text, now, Delivery.NO_NOTIFIER, None)

        def done(name: str, error: str | None, quiet: bool = False) -> None:  # background
            self._done.put((kind, text, name, error, quiet))

        try:
            skip = self._routing(kind)
        except Exception:  # noqa: BLE001 - an injected routing never drops an alert
            skip = frozenset()
        routed_off = tuple(name for name in self._channels.names() if name in skip)
        if routed_off and len(routed_off) == len(self._channels.names()):
            return AlertOutcome(kind, text, now, Delivery.ROUTED_OFF, None,
                                routed_off=routed_off)
        pending = self._channels.background_except(skip)
        if pending:
            self._sending[key] = len(pending)
        try:
            results = self._channels.send(f"{self._title}: {kind.value}", text, done, skip,
                                          kind=kind.value)
        except BaseException:
            # No outcome will follow for sends that never started: the alert must not stay
            # suppressed until the process restarts.
            self._sending.pop(key, None)
            raise
        failed = [error if name == NOTIFIER else f"{name}: {error}"
                  for name, error in results if error is not None]
        if len(failed) < len(results):
            self._last_shown[key] = now
        if failed:
            # The next occurrence tries again (never an automatic retry).
            return AlertOutcome(kind, text, now, Delivery.FAILED, "; ".join(failed), pending,
                                routed_off=routed_off)
        if not results:  # background channels only: their outcomes follow
            return AlertOutcome(kind, text, now, Delivery.SENDING, None, pending,
                                routed_off=routed_off)
        return AlertOutcome(kind, text, now, Delivery.SHOWN, None, pending,
                            routed_off=routed_off)

    def completed(self) -> list[AlertOutcome]:
        """Outcomes of background sends that finished since the last call."""
        outcomes = []
        while True:
            try:
                kind, text, name, error, quiet = self._done.get_nowait()
            except queue.Empty:
                return outcomes
            now = self._clock()
            left = self._sending.get((kind, text), 1) - 1
            if left > 0:
                self._sending[(kind, text)] = left
            else:
                self._sending.pop((kind, text), None)
            if quiet:
                continue  # nobody to send to, reported once already: no outcome, not shown
            if error is None:
                self._last_shown[(kind, text)] = now
            outcomes.append(AlertOutcome(kind, text, now,
                                         Delivery.SHOWN if error is None else Delivery.FAILED,
                                         None if error is None else f"{name}: {error}",
                                         channel=name))

    def flush(self) -> list[AlertOutcome]:
        """Wait for the background sends, then their outcomes (short-lived commands only)."""
        if self._channels is not None:
            self._channels.wait()
        return self.completed()


def token_pending_text(wait_seconds: int) -> str:
    """Text of the TOKEN_NEEDS_CONFIRMATION alert: the SDK now waits for the owner."""
    return f"approve the API token in the Webull app within {wait_seconds} s"
