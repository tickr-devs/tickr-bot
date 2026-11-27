"""The tokens of a device signed in with tickr.bot (POST /session): a short-lived access token
and a rotating refresh token, as the owner asked (2026-11-27: "a token that expires, refreshes").

Sign-in gives the device both. The access token is its bearer token on every request until
`access_expires_at`. Before then (or after) the app trades the refresh token for a new pair at
POST /session/refresh: the presented refresh token is retired in the same write that records the
new one, so at most one refresh token per device is ever live. A refresh token is
`<family>.<secret>`: the family is a public id shared by every refresh token of one sign-in, so
a retired token is told from an unknown one without keeping a digest of every token ever
issued. A retired token presented again means a copy exists (or the device lost the answer to
its own refresh): the whole sign-in is revoked and the owner alerted. Nothing here outlives the
sign-in's own end (`expires_at`); after it the device signs in again.

Only digests are stored (api/devices.py); the tokens leave this module once, in `Issued`, for
the one answer that carries them. Callers hold the pairing locks around the read-modify-write.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from tickr.api.devices import FAMILY, Device, Session, digest

FAMILY_BYTES = 16  # a family id: 16 random bytes, 32 hex characters (devices.FAMILY)
SEPARATOR = "."  # between family and secret; token_urlsafe never writes one


@dataclass(frozen=True)
class Lifetimes:
    """How long each token lives, from `[api]` in config/bot.toml."""

    access_seconds: int
    refresh_seconds: int
    session_seconds: int


@dataclass(frozen=True)
class Issued:
    """A new token pair, for the one answer that carries it; never stored or logged."""

    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


class RefreshRefused(Exception):
    """A refresh token that buys nothing, with the one cause the app is told. `revoked` names
    the device whose sign-in it ended (a retired token presented again, or an expired sign-in),
    or is None when the token belongs to no device."""

    def __init__(self, cause: str, revoked: str | None = None):
        super().__init__(cause)
        self.cause = cause
        self.revoked = revoked


def sign_in(name: str, now: datetime, lifetimes: Lifetimes,
            token_bytes: int) -> tuple[Device, Issued]:
    """A new signed-in device `name` with its first token pair."""
    family = secrets.token_hex(FAMILY_BYTES)
    expires_at = now + timedelta(seconds=lifetimes.session_seconds)
    issued, session = _pair(family, now, expires_at, lifetimes, token_bytes)
    return Device(name, digest(issued.access_token), now, None, session), issued


def refresh(devices: tuple[Device, ...], presented: str, now: datetime, lifetimes: Lifetimes,
            token_bytes: int) -> tuple[tuple[Device, ...], Issued]:
    """The registry with the presented refresh token's device given a new pair, and that pair.
    Raises RefreshRefused; when it names a revoked device, the caller removes it."""
    family, sep, secret = presented.partition(SEPARATOR)
    if not sep or not FAMILY.match(family) or not secret:
        raise RefreshRefused("not a refresh token")
    device = next((d for d in devices if d.session is not None and d.session.family == family),
                  None)
    if device is None or device.session is None:
        raise RefreshRefused("this refresh token belongs to no signed-in device; sign in again")
    session = device.session
    if not hmac.compare_digest(digest(presented), session.refresh_sha256):
        raise RefreshRefused("this refresh token was used already, so a copy of it may exist: "
                             "the device is signed out; sign in again", device.name)
    if now >= session.refresh_expires_at or now >= session.expires_at:
        raise RefreshRefused("the sign-in has expired; sign in again", device.name)
    issued, renewed = _pair(family, now, session.expires_at, lifetimes, token_bytes)
    fresh = replace(device, sha256=digest(issued.access_token), session=renewed)
    return tuple(fresh if d.name == device.name else d for d in devices), issued


def access_expired(device: Device, now: datetime) -> bool:
    """Whether a signed-in device's access token has run out (a paired device's never does)."""
    return device.session is not None and now >= device.session.access_expires_at


def _pair(family: str, now: datetime, expires_at: datetime, lifetimes: Lifetimes,
          token_bytes: int) -> tuple[Issued, Session]:
    access = secrets.token_urlsafe(token_bytes)
    refresh_token = f"{family}{SEPARATOR}{secrets.token_urlsafe(token_bytes)}"
    access_at = min(now + timedelta(seconds=lifetimes.access_seconds), expires_at)
    refresh_at = min(now + timedelta(seconds=lifetimes.refresh_seconds), expires_at)
    issued = Issued(access, refresh_token, access_at, refresh_at)
    return issued, Session(family, digest(refresh_token), access_at, refresh_at, expires_at)
