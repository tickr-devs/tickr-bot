"""The paired devices of the remote port: one bearer token each, kept as SHA-256 only.

The registry is one JSON file under data_dir (`[api] remote_devices_file`, 0600):
`{"devices": [{"name", "sha256", "created_at", "last_used_at"}]}`. A device name is unique;
pairing a name again replaces that device's token when the new code is redeemed. A token is
never stored, logged or printed: only its hex digest is.

A device signed in with tickr.bot (POST /session) also has a `session`: its `sha256` is then a
short-lived access token's, and the session holds the family id of its refresh tokens, the
current refresh token's digest and the three expiries (access, refresh, the whole session). A
paired device (`pair`) has no session and its token does not expire.

The file a single phone token used to live in (`[api] remote_token_hash_file`) is migrated once
into the registry as the device "default", created when that file was last written, so a phone
paired before several devices existed keeps working. The callers (api/server.py) hold the
pairing lock around every read-modify-write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from tickr.api import contract
from tickr.api.contract import ContractError

LEGACY_NAME = "default"  # the device name a migrated single token gets
LOCAL = "local"  # the device name of a request on the local port (the Mac's own app, no token)
SIMULATOR = "simulator"  # the iOS Simulator's token on the remote port's loopback address
# Names neither a sign-in nor `pair` may take: they name the bot's own access in the journal and
# key its push tokens, and DELETE /session or `revoke` cannot end them.
RESERVED = frozenset({LOCAL, SIMULATOR})
# A device name: letters, digits, space, dot, dash, underscore; starts and ends with a letter
# or digit, so it reads the same in a list and on the command line.
NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9 ._-]*[A-Za-z0-9])?$")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")  # a SHA-256 hex digest
FAMILY = re.compile(r"^[0-9a-f]{32}$")  # a refresh-token family id: 16 random bytes, hex
SESSION_FIELDS = {"family", "refresh_sha256", "access_expires_at", "refresh_expires_at",
                  "expires_at"}


class DeviceFileError(Exception):
    """The registry (or the legacy single-token file) cannot be read as written."""


class ReservedName(ValueError):
    """A device name in `RESERVED`, refused for a sign-in or a pairing."""

    def __init__(self, name: str) -> None:
        super().__init__(f"device name {name!r} is reserved for the bot's own access; use "
                         "another name")
        self.name = name


@dataclass(frozen=True)
class Session:
    """A signed-in device's tokens (api/sessions.py): digests and expiries only."""

    family: str  # public id shared by every refresh token of this sign-in
    refresh_sha256: str  # digest of the one live refresh token; every earlier one is retired
    access_expires_at: datetime
    refresh_expires_at: datetime
    expires_at: datetime  # the sign-in's own end: no refresh carries it past this


@dataclass(frozen=True)
class Device:
    name: str
    sha256: str  # hex digest of the device's bearer token, never the token
    created_at: datetime
    last_used_at: datetime | None  # last authenticated request, to the minute; None = never
    session: Session | None = None  # set for a device signed in with tickr.bot


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def valid_name(name: str) -> bool:
    return NAME.match(name) is not None


def refuse_reserved(name: str) -> None:
    """ReservedName when `name` is one of the bot's own (`RESERVED`)."""
    if name in RESERVED:
        raise ReservedName(name)


def match(devices: tuple[Device, ...], token: str) -> Device | None:
    """The device whose token this is, or None. Every stored digest is compared in constant
    time and none is skipped, so the answer's timing depends neither on the token nor on which
    device (if any) it belongs to."""
    presented = digest(token)
    found = None
    for device in devices:
        if hmac.compare_digest(presented, device.sha256):
            found = device
    return found


def used(devices: tuple[Device, ...], name: str, now: datetime) -> tuple[Device, ...] | None:
    """The registry with `name` marked used at `now`, kept to the minute so a busy phone does
    not rewrite the file on every request; None when nothing changes."""
    minute = now.astimezone(UTC).replace(second=0, microsecond=0)
    changed = False
    out = []
    for device in devices:
        if device.name == name and device.last_used_at != minute:
            device = replace(device, last_used_at=minute)
            changed = True
        out.append(device)
    return tuple(out) if changed else None


def with_device(devices: tuple[Device, ...], device: Device) -> tuple[Device, ...]:
    """Add `device`, replacing one of the same name (a re-paired device), keeping the order."""
    kept = tuple(d for d in devices if d.name != device.name)
    if len(kept) == len(devices):
        return (*devices, device)
    return tuple(device if d.name == device.name else d for d in devices)


def without(devices: tuple[Device, ...], name: str) -> tuple[Device, ...] | None:
    """The registry without `name`; None when no device has that name."""
    kept = tuple(d for d in devices if d.name != name)
    return None if len(kept) == len(devices) else kept


def render(devices: tuple[Device, ...]) -> str:
    return json.dumps({"devices": [_row(d) for d in devices]}, indent=1) + "\n"


def _row(d: Device) -> dict[str, object]:
    row: dict[str, object] = {
        "name": d.name,
        "sha256": d.sha256,
        "created_at": contract.format_time(d.created_at),
        "last_used_at": None if d.last_used_at is None else contract.format_time(d.last_used_at),
    }
    if d.session is not None:
        s = d.session
        row["session"] = {
            "family": s.family,
            "refresh_sha256": s.refresh_sha256,
            "access_expires_at": contract.format_time(s.access_expires_at),
            "refresh_expires_at": contract.format_time(s.refresh_expires_at),
            "expires_at": contract.format_time(s.expires_at),
        }
    return row


def _session(raw: object) -> Session:
    if not isinstance(raw, dict) or set(raw) != SESSION_FIELDS:
        raise ValueError("session fields")
    family, refresh = raw["family"], raw["refresh_sha256"]
    if not isinstance(family, str) or not FAMILY.match(family):
        raise ValueError("family")
    if not isinstance(refresh, str) or not HEX_DIGEST.match(refresh):
        raise ValueError("refresh_sha256")
    return Session(family, refresh, contract.parse_time(raw["access_expires_at"]),
                   contract.parse_time(raw["refresh_expires_at"]),
                   contract.parse_time(raw["expires_at"]))


def parse(text: str) -> tuple[Device, ...]:
    """The registry file's devices; any other shape raises DeviceFileError (never guessed)."""
    try:
        document = json.loads(text)
        if not isinstance(document, dict) or set(document) != {"devices"} \
                or not isinstance(document["devices"], list):
            raise ValueError("devices")
        devices = []
        for row in document["devices"]:
            base = {"name", "sha256", "created_at", "last_used_at"}
            if not isinstance(row, dict) or set(row) not in (base, base | {"session"}):
                raise ValueError("device fields")
            name, sha, used_at = row["name"], row["sha256"], row["last_used_at"]
            if not isinstance(name, str) or not valid_name(name):
                raise ValueError("name")
            if not isinstance(sha, str) or not HEX_DIGEST.match(sha):
                raise ValueError("sha256")
            devices.append(Device(name, sha, contract.parse_time(row["created_at"]),
                                  None if used_at is None else contract.parse_time(used_at),
                                  _session(row["session"]) if "session" in row else None))
    except (ValueError, TypeError, ContractError) as exc:
        raise DeviceFileError(f"not a device list ({type(exc).__name__})") from exc
    if len({d.name for d in devices}) != len(devices):
        raise DeviceFileError("a device name appears twice")
    families = [d.session.family for d in devices if d.session is not None]
    if len(set(families)) != len(families):
        raise DeviceFileError("a refresh-token family appears twice")
    return tuple(devices)


def legacy_device(legacy_path: Path) -> Device:
    """The single phone token's hash file as the device "default", created when the file was
    last written (that is when the phone was paired), never used as far as we know."""
    try:
        sha = legacy_path.read_text("ascii").strip()
        written = datetime.fromtimestamp(legacy_path.stat().st_mtime, UTC)
    except (OSError, UnicodeDecodeError) as exc:
        raise DeviceFileError(f"the single-token hash file is unreadable "
                              f"({type(exc).__name__})") from exc
    if not HEX_DIGEST.match(sha):
        raise DeviceFileError("the single-token hash file holds no SHA-256 digest")
    return Device(LEGACY_NAME, sha, written, None)
