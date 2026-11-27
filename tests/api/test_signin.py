"""The sign-in grant check (api/signin.py) against tokens signed once, offline, with a test RSA
key (tests/fixtures/signin/): the provider's public key, grants that pass and one grant per
refusal."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tickr.api import signin
from tickr.api.signin import KeyFileError, Rules, SignInRefused

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "signin"
GRANTS = json.loads((FIXTURES / "grants.json").read_text("utf-8"))
ISSUED = datetime.fromtimestamp(GRANTS["issued_at"], UTC)
# The audience the fixture grants were signed for: the bot's first address. Production reads
# remote_signin_audience from bot.toml; the check is the same for any audience.
RULES = Rules(issuer="https://tickr.bot", audience="https://bot.tickr.bot", scope="bot",
              clients=frozenset({"tickr-ios", "tickr-mac"}), accounts=frozenset({"1"}),
              max_lifetime_seconds=60, clock_skew_seconds=5)


def _key(name="provider.pem"):
    return signin.load_key((FIXTURES / name).read_text("ascii"))


def _check(case, now=ISSUED + timedelta(seconds=1)):
    return signin.check(GRANTS["tokens"][case], _key(), RULES, now)


def test_a_valid_grant_names_its_account_app_and_id():
    grant = _check("valid")
    assert (grant.account, grant.client, grant.jti) == ("1", "tickr-ios", "grant-1")
    assert grant.expires_at == ISSUED + timedelta(seconds=60)


def test_an_audience_list_that_holds_the_bot_passes():
    assert _check("valid_mac_list_audience").client == "tickr-mac"


@pytest.mark.parametrize(("case", "cause"), [
    ("other_key", "signature"),
    ("tampered", "signature"),
    ("alg_none", "RS256"),
    ("other_issuer", "issuer"),
    ("other_audience", "audience"),
    ("id_token", "access token"),
    ("no_scope", "scope"),
    ("long_life", "longer"),
    ("other_account", "account"),
    ("other_client", "app"),
    ("no_jti", "no id"),
    ("bool_times", "times"),
])
def test_each_wrong_grant_is_refused_with_its_one_cause(case, cause):
    with pytest.raises(SignInRefused) as refused:
        _check(case)
    assert cause in refused.value.cause
    assert GRANTS["tokens"][case] not in refused.value.cause


def test_a_grant_issued_beyond_the_clock_skew_is_refused():
    _check("valid", now=ISSUED - timedelta(seconds=5))
    with pytest.raises(SignInRefused, match="future"):
        _check("valid", now=ISSUED - timedelta(seconds=6))


def test_a_grant_is_refused_from_its_expiry_on():
    _check("valid", now=ISSUED + timedelta(seconds=59))
    with pytest.raises(SignInRefused, match="expired"):
        _check("valid", now=ISSUED + timedelta(seconds=60))


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "a.b.c", "x y.z.w", "e30.e30."])
def test_what_is_not_a_signed_token_is_refused(token):
    with pytest.raises(SignInRefused):
        signin.check(token, _key(), RULES, ISSUED)


@pytest.mark.parametrize("times", [(10**18, 10**18 + 30), (-(10**18), -(10**18) + 30)])
def test_times_out_of_range_are_a_refusal_not_a_crash(monkeypatch, times):
    """Only a correctly signed grant reaches the times, so the signature check is stood in for."""
    monkeypatch.setattr(signin, "_verified", lambda message, signature, key: True)
    header, payload, signature = GRANTS["tokens"]["valid"].split(".")
    claims = json.loads(signin._bytes(payload))
    claims["iat"], claims["exp"] = times
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(SignInRefused, match="out of range"):
        signin.check(f"{header}.{encoded}.{signature}", _key(), RULES, ISSUED)


def test_a_trailing_newline_is_not_a_signed_token():
    with pytest.raises(SignInRefused, match="not a signed token"):
        signin.check(GRANTS["tokens"]["valid"] + "\n", _key(), RULES, ISSUED + timedelta(seconds=1))


def test_a_signature_one_bit_off_is_refused():
    header, payload, signature = GRANTS["tokens"]["valid"].split(".")
    flipped = ("A" if signature[-2] != "A" else "B").join([signature[:-2], signature[-1:]])
    with pytest.raises(SignInRefused, match="signature"):
        signin.check(f"{header}.{payload}.{flipped}", _key(), RULES, ISSUED)


@pytest.mark.parametrize("name", ["short-key.pem", "pkcs1-key.pem"])
def test_only_a_spki_rsa_key_of_2048_bits_or_more_is_pinned(name):
    with pytest.raises(KeyFileError):
        _key(name)


def test_a_used_grant_is_kept_as_a_digest_only():
    digest = signin.jti_digest("grant-1")
    assert len(digest) == 64 and "grant-1" not in digest


@pytest.mark.parametrize("app", ["ios", "mac"])
def test_a_grant_tickr_bot_issued_passes(app):
    """Access tokens allauth.idp.oidc issued on the web branch's test server (web thread,
    2026-11-27; issuer "https://testserver" there, "https://tickr.bot" in production)."""
    recorded = json.loads((FIXTURES / "allauth-grants.json").read_text("utf-8"))[app]
    claims = recorded["claims"]
    rules = replace(RULES, issuer="https://testserver", audience="https://api.tickr.bot")
    now = datetime.fromtimestamp(claims["iat"], UTC)
    grant = signin.check(recorded["access_token"], _key("allauth.pem"), rules, now)
    assert (grant.account, grant.client, grant.jti) == (claims["sub"], claims["client_id"],
                                                        claims["jti"])


def test_the_shipped_provider_key_is_tickr_bots_grant_key():
    """config/signin/tickr-bot-provider.pem is the key tickr.bot serves at /api/app/grant-key.pem:
    RSA 3072, and its RFC 7638 thumbprint is the `kid` of tickr.bot's grants."""
    root = Path(__file__).resolve().parents[2]
    key = signin.load_key((root / "config/signin/tickr-bot-provider.pem").read_text("utf-8"))

    def b64(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = json.dumps({"e": b64(key.exponent), "kty": "RSA", "n": b64(key.modulus)},
                     separators=(",", ":"), sort_keys=True)
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(jwk.encode()).digest()).rstrip(b"=")
    assert key.modulus.bit_length() == 3072
    assert thumbprint.decode() == "vX45fH22Yuqamw3FA7sRPhhDDuEQlFoVaghMGr_QaEc"
