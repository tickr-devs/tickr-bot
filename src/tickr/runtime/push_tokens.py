"""The apps' APNs tokens: one per paired device and token kind, registered at POST /push-token.

The kind (`PushTokenKind`) says what the token is for: `alert` (the device token the alert
sender pushes to), `activity_start` (a Live Activity push-to-start token) or `activity_update`
(one Live Activity's update token). Only `alert` tokens are pushed to today; the activity kinds
are stored for the planned Live Activity sender.

The store is one JSON file under data_dir (`[alerts] push_tokens_file`, 0600, written atomically):
`{"schema_version": 2, "devices": {"<device name>": {"<kind>": {"token", "environment",
"bundle_id", "platform", "registered_at"}}}}`. The API is its writer; the push sender reads it on
each alert and removes a token APNs says is gone (410 or BadDeviceToken).

A device key is the name of the device that registered the token, as the API authorized it: the
paired or signed-in device's name in `api/devices.py`, `LOCAL` for the Mac's own app on the local
port (no token there), or the Simulator's reserved name. The name stays the same while a signed-in
device's access tokens rotate at each refresh, so its registration survives a refresh; signing the
device out or revoking it drops its tokens (`remove_device`). Schema 1 keyed devices by the SHA-256
of their bearer token, which changes at every refresh; such a file is refused (`OldSchema`), never
mapped to names by guessing: delete it and each app registers again at its next launch. A device
token is personal data: it is never logged or put in an error; `short_hash` names one in a journal
line instead.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tickr.api import contract
from tickr.api.contract import PushPlatform as Platform
from tickr.api.contract import PushTokenKind as Kind

SCHEMA_VERSION = 2
# The schema that keyed devices by their bearer token's SHA-256; refused, never migrated.
DIGEST_KEYED_SCHEMA = 1
LOCAL = "local"  # the device key of the Mac's own app: the API's LOCAL_DEVICE name
PRIVATE_MODE = 0o600  # owner read and write only (POSIX mode bits)
# An APNs device token: hex, an even number of digits. Apple documents no fixed length (32 bytes
# today), so any non-empty whole-byte hex up to the HTTP/2 path limit Apple allows is taken.
DEVICE_TOKEN = re.compile(r"^(?:[0-9a-fA-F]{2}){1,100}$")
SHORT_HASH_DIGITS = 8  # hex digits of a token's SHA-256 shown in a journal line
_FIELDS = frozenset({"token", "environment", "bundle_id", "platform"})
_ENTRY = frozenset({"token", "environment", "bundle_id", "platform", "registered_at"})
Key = tuple[str, Kind]  # (device key, token kind): one token each


class StoreError(Exception):
    """The store file cannot be read as written."""


class OldSchema(StoreError):
    """A schema-1 file, keyed by bearer-token digests that no longer name a device."""


class BadRegistration(ValueError):
    """A POST /push-token body that is refused; the message names the field, never its value."""


@dataclass(frozen=True)
class PushToken:
    kind: Kind
    token: str  # lowercase hex; personal data, never logged
    environment: str  # the build's aps-environment, a key of [alerts] apns_hosts
    bundle_id: str  # the APNs topic, one of [alerts] apns_topics
    platform: Platform
    registered_at: datetime


def short_hash(token: str) -> str:
    """A name for a device token in a journal line or error: never the token itself."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:SHORT_HASH_DIGITS]


def parse_registration(body: dict[str, Any], topics: tuple[str, ...],
                       environments: Collection[str], now: datetime) -> PushToken:
    """The token a POST /push-token body registers, or BadRegistration naming the field.
    `kind` is optional and `alert` when left out."""
    if not _FIELDS <= set(body) <= _FIELDS | {"kind"}:
        raise BadRegistration('body must be exactly {"token", "environment", "bundle_id", '
                              '"platform"} and optionally "kind"')
    kind = parse_kind(body.get("kind", Kind.ALERT.value))
    token, environment = body["token"], body["environment"]
    bundle_id, platform = body["bundle_id"], body["platform"]
    if not isinstance(token, str) or not DEVICE_TOKEN.match(token):
        raise BadRegistration("token must be the device token in hex")
    if not isinstance(environment, str) or environment not in environments:
        raise BadRegistration("environment is not an APNs environment ([alerts] apns_hosts: "
                              + ", ".join(sorted(environments)) + ")")
    if not isinstance(bundle_id, str) or bundle_id not in topics:
        raise BadRegistration("bundle_id is not one of the apps' bundle ids ([alerts] "
                              "apns_topics)")
    if not isinstance(platform, str) or platform not in set(Platform):
        raise BadRegistration('platform must be "ios" or "macos"')
    return PushToken(kind, token.lower(), environment, bundle_id, Platform(platform), now)


def parse_kind(value: Any) -> Kind:
    """A token kind from a body or query, or BadRegistration."""
    if not isinstance(value, str) or value not in set(Kind):
        raise BadRegistration("kind must be one of " + ", ".join(k.value for k in Kind))
    return Kind(value)


def _parse(text: str) -> dict[Key, PushToken]:
    body = json.loads(text)
    if not isinstance(body, dict) or set(body) != {"schema_version", "devices"}:
        raise ValueError("keys must be exactly schema_version and devices")
    if body["schema_version"] == DIGEST_KEYED_SCHEMA:
        raise OldSchema("schema 1")
    if body["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema_version {body['schema_version']!r}")
    if not isinstance(body["devices"], dict):
        raise ValueError("devices must be an object")
    out = {}
    for device, kinds in body["devices"].items():
        if not isinstance(kinds, dict):
            raise ValueError("a device's tokens must be an object")
        for kind_name, entry in kinds.items():
            kind = Kind(kind_name)  # ValueError for an unknown kind
            if not isinstance(entry, dict) or set(entry) != _ENTRY:
                raise ValueError("a token entry is out of shape")
            if not isinstance(entry["token"], str) or not DEVICE_TOKEN.match(entry["token"]):
                raise ValueError("a device token is not hex")
            if not (isinstance(entry["bundle_id"], str)
                    and isinstance(entry["environment"], str)):
                raise ValueError("a bundle_id or environment is not text")
            out[(device, kind)] = PushToken(kind, entry["token"], entry["environment"],
                                            entry["bundle_id"], Platform(entry["platform"]),
                                            contract.parse_time(entry["registered_at"]))
    return out


def _text(tokens: dict[Key, PushToken]) -> str:
    devices: dict[str, dict[str, Any]] = {}
    for (device, kind), t in sorted(tokens.items()):
        devices.setdefault(device, {})[kind.value] = {
            "token": t.token, "environment": t.environment, "bundle_id": t.bundle_id,
            "platform": t.platform.value, "registered_at": contract.format_time(t.registered_at)}
    return json.dumps({"schema_version": SCHEMA_VERSION, "devices": devices},
                      indent=2, sort_keys=True) + "\n"


def _write_private(path: Path, text: str) -> None:
    """Atomically, readable by the owner only (0600) from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), PRIVATE_MODE)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class PushTokenStore:
    """Read-modify-write under a lock held across processes: the API (register, delete) and
    the bot or watchdog (removing a token APNs refused) are different processes writing the
    same file. Each read-modify-write holds this process's lock and an exclusive `flock` on a
    sibling lock file (the store itself is replaced atomically, so it cannot carry the lock).
    An entry is removed only while it still holds that same token."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, PRIVATE_MODE)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)  # released when fd is closed
                yield
            finally:
                os.close(fd)

    def read(self) -> dict[Key, PushToken]:
        """Every (device, kind)'s token; empty when the file is missing; StoreError when
        unreadable."""
        try:
            text = self.path.read_text("utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreError(f"{self.path.name} unreadable: {type(exc).__name__}") from None
        try:
            return _parse(text)
        except OldSchema:
            raise OldSchema(f"{self.path.name} is schema 1, keyed by bearer-token digests that "
                            "change at every refresh; delete it, each app registers again at "
                            "its next launch") from None
        except (ValueError, TypeError, KeyError, contract.ContractError) as exc:
            # The message never holds file content: a ValueError above names a field only.
            raise StoreError(f"{self.path.name} out of shape: {type(exc).__name__}") from None

    def register(self, device: str, token: PushToken) -> None:
        """Store `token` for `device` and its kind, replacing only that kind's earlier one. The
        same token of that kind under another device name (the device signed in again under
        a new name) is dropped: one device is pushed once."""
        with self._locked():
            tokens = {k: t for k, t in self.read().items()
                      if not (t.kind is token.kind and t.token == token.token)}
            tokens[(device, token.kind)] = token
            _write_private(self.path, _text(tokens))

    def remove(self, device: str, kind: Kind = Kind.ALERT) -> bool:
        """Drop `device`'s token of `kind`; whether there was one."""
        with self._locked():
            tokens = self.read()
            if tokens.pop((device, kind), None) is None:
                return False
            _write_private(self.path, _text(tokens))
            return True

    def remove_device(self, device: str) -> int:
        """Drop every token of `device` (signed out or revoked); how many there were."""
        with self._locked():
            tokens = self.read()
            kept = {k: t for k, t in tokens.items() if k[0] != device}
            if len(kept) < len(tokens):
                _write_private(self.path, _text(kept))
            return len(tokens) - len(kept)

    def remove_token(self, token: str) -> bool:
        """Drop every entry still holding `token` (APNs said it is gone); whether any was."""
        with self._locked():
            devices = self.read()
            kept = {k: t for k, t in devices.items() if t.token != token}
            if len(kept) == len(devices):
                return False
            _write_private(self.path, _text(kept))
            return True
