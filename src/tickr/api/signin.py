"""A device's sign-in at the remote port: the tickr.bot account grant the apps bring to
POST /session, checked here before the api server gives the device its own bearer token.

The grant is the 60-second JWT access token tickr.bot's OpenID provider (allauth.idp.oidc)
signs with RS256 for the audience `[api] remote_signin_audience`. The bot never asks tickr.bot
anything: it pins the provider's public key (`[api] remote_signin_key_file`, a PEM file that is
not a secret) and checks every claim itself. The standard library has no RSA, so the signature is
checked here (RFC 8017 RSASSA-PKCS1-v1_5 with SHA-256): the expected encoding of the digest is
built and compared whole, never parsed out of the signature, so no lenient parse can accept a
forged one.

Each refusal is a `SignInRefused` with one cause that names a claim, never the grant itself, so
nothing secret reaches a response or a log line.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ALGORITHM = "RS256"  # the only signature the grant may carry (JWA, RFC 7518)
TOKEN_USE = "access"  # allauth's claim on an access token (an id token says "id")
# DER of the DigestInfo prefix of a SHA-256 digest (RFC 8017, section 9.2, note 1).
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
RSA_ENCRYPTION = bytes.fromhex("2a864886f70d010101")  # OID 1.2.840.113549.1.1.1, its DER body
MIN_MODULUS_BITS = 2048  # NIST SP 800-131A: shorter RSA keys are not accepted
MIN_EXPONENT = 3  # the smallest odd public exponent RSA allows
FRAME_BYTES = 3  # the 0x00 0x01 before the 0xFF run and the 0x00 after it (RFC 8017, 9.2)
MIN_PADDING = 8  # RFC 8017, section 9.2 step 3: at least 8 bytes of 0xFF
DER_HEADER = 2  # a DER element's tag byte and first length byte
PEM = re.compile(r"-----BEGIN PUBLIC KEY-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END PUBLIC KEY-----")
B64URL = re.compile(r"^[A-Za-z0-9_-]*$")  # base64url without padding (RFC 7515, section 2)
# DER tags (X.690): SEQUENCE, INTEGER, BIT STRING, OBJECT IDENTIFIER, NULL.
SEQUENCE, INTEGER, BIT_STRING, OID, NULL = 0x30, 0x02, 0x03, 0x06, 0x05
LONG_LENGTH = 0x80  # a DER length byte with this bit set counts the length bytes that follow
JWS_PARTS = 3  # header, payload, signature


class SignInRefused(Exception):
    """The grant is not one this bot accepts; `cause` says which check failed."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class KeyFileError(Exception):
    """The pinned key file is not one RSA public key of at least MIN_MODULUS_BITS."""


@dataclass(frozen=True)
class PublicKey:
    modulus: int
    exponent: int

    @property
    def size(self) -> int:
        """The modulus length in bytes: a signature is exactly this long."""
        return (self.modulus.bit_length() + 7) // 8


@dataclass(frozen=True)
class Rules:
    """What a grant must say, from `[api]` in config/bot.toml."""
    issuer: str
    audience: str
    scope: str
    clients: frozenset[str]
    accounts: frozenset[str]
    max_lifetime_seconds: int  # exp - iat may not exceed this (the provider's token lifetime)
    clock_skew_seconds: int  # how far the provider's clock may run ahead of the bot's (iat check)


@dataclass(frozen=True)
class Grant:
    account: str  # `sub`
    client: str  # `client_id`: which app
    jti: str
    expires_at: datetime


def load_key(text: str) -> PublicKey:
    """The RSA public key of a PEM `PUBLIC KEY` (SubjectPublicKeyInfo) file."""
    found = PEM.fullmatch(text.strip())
    if found is None:
        raise KeyFileError("not one PEM PUBLIC KEY block")
    try:
        der = base64.b64decode("".join(found.group(1).split()), validate=True)
        info, rest = _element(der, SEQUENCE)
        algorithm, info = _element(info, SEQUENCE)
        oid, parameters = _element(algorithm, OID)
        null, extra = _element(parameters, NULL)
        bits, end = _element(info, BIT_STRING)
        if rest or end or extra or null or oid != RSA_ENCRYPTION or not bits or bits[0] != 0:
            raise ValueError("not an RSA SubjectPublicKeyInfo")
        numbers, tail = _element(bits[1:], SEQUENCE)
        modulus, numbers = _element(numbers, INTEGER)
        exponent, numbers = _element(numbers, INTEGER)
        if tail or numbers:
            raise ValueError("trailing bytes in the RSA key")
    except (ValueError, binascii.Error) as exc:
        raise KeyFileError(f"unreadable public key ({type(exc).__name__})") from exc
    key = PublicKey(int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big"))
    if key.modulus.bit_length() < MIN_MODULUS_BITS or key.exponent < MIN_EXPONENT \
            or key.exponent % 2 == 0:
        raise KeyFileError(f"not an RSA key of at least {MIN_MODULUS_BITS} bits")
    return key


def check(token: str, key: PublicKey, rules: Rules, now: datetime) -> Grant:
    """The grant `token` carries, or SignInRefused with the first check that failed."""
    parts = token.split(".")
    if len(parts) != JWS_PARTS or not all(p and B64URL.match(p) for p in parts):
        raise SignInRefused("the grant is not a signed token")
    header, payload = _json(parts[0], "header"), _json(parts[1], "claims")
    if header.get("alg") != ALGORITHM or "crit" in header:
        raise SignInRefused(f"the grant is not signed with {ALGORITHM}")
    signed = f"{parts[0]}.{parts[1]}".encode("ascii")
    if not _verified(signed, _bytes(parts[2]), key):
        raise SignInRefused("the grant's signature is not tickr.bot's")
    # Claims are read only once the signature holds: nothing unsigned decides anything.
    if payload.get("iss") != rules.issuer:
        raise SignInRefused("the grant is from another issuer")
    audience = payload.get("aud")
    if audience != rules.audience and not (isinstance(audience, list)
                                           and rules.audience in audience):
        raise SignInRefused("the grant is for another audience")
    if payload.get("token_use") != TOKEN_USE:
        raise SignInRefused("the grant is not an access token")
    scope = payload.get("scope")
    if not isinstance(scope, str) or rules.scope not in scope.split():
        raise SignInRefused(f"the grant lacks the {rules.scope!r} scope")
    issued, expires = payload.get("iat"), payload.get("exp")
    if not _whole(issued) or not _whole(expires) or expires <= issued:
        raise SignInRefused("the grant's times are missing")
    if expires - issued > rules.max_lifetime_seconds:
        raise SignInRefused("the grant lives longer than tickr.bot's tokens do")
    if datetime.fromtimestamp(issued, UTC) > now + timedelta(seconds=rules.clock_skew_seconds):
        raise SignInRefused("the grant is issued in the future; check the bot's clock")
    expires_at = datetime.fromtimestamp(expires, UTC)
    if now >= expires_at:
        raise SignInRefused("the grant has expired; sign in again")
    client, account, jti = payload.get("client_id"), payload.get("sub"), payload.get("jti")
    if not isinstance(client, str) or client not in rules.clients:
        raise SignInRefused("the grant is for another app")
    if not isinstance(account, str) or account not in rules.accounts:
        raise SignInRefused("this tickr.bot account may not reach the bot")
    if not isinstance(jti, str) or not jti:
        raise SignInRefused("the grant has no id")
    return Grant(account, client, jti, expires_at)


def jti_digest(jti: str) -> str:
    """What the server keeps of a used grant's id, so a grant is redeemed once."""
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _verified(message: bytes, signature: bytes, key: PublicKey) -> bool:
    """RSASSA-PKCS1-v1_5 verification with SHA-256 (RFC 8017, section 8.2.2)."""
    k = key.size
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.modulus:
        return False
    encoded = pow(s, key.exponent, key.modulus).to_bytes(k, "big")
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = k - len(digest_info) - FRAME_BYTES
    if padding < MIN_PADDING:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)



def _element(data: bytes, tag: int) -> tuple[bytes, bytes]:
    """The body of the DER element at the start of `data` (which must have `tag`) and the bytes
    after it. Definite lengths only, in their shortest form, as DER requires."""
    if len(data) < DER_HEADER or data[0] != tag:
        raise ValueError(f"expected DER tag {tag:#04x}")
    first = data[1]
    start = DER_HEADER
    if first < LONG_LENGTH:
        length = first
    else:
        count = first & ~LONG_LENGTH & 0xFF
        if count == 0 or count > len(data) - start or data[start] == 0:
            raise ValueError("bad DER length")
        length = int.from_bytes(data[start:start + count], "big")
        start += count
        if length < LONG_LENGTH:
            raise ValueError("DER length not in its shortest form")
    end = start + length
    if end > len(data):
        raise ValueError("DER element runs past its end")
    return data[start:end], data[end:]


def _bytes(part: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (ValueError, binascii.Error) as exc:
        raise SignInRefused("the grant is not a signed token") from exc


def _json(part: str, what: str) -> dict:
    try:
        value = json.loads(_bytes(part))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SignInRefused(f"the grant's {what} is not JSON") from exc
    if not isinstance(value, dict):
        raise SignInRefused(f"the grant's {what} is not an object")
    return value


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
