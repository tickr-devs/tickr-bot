"""Failed-authentication limit of the remote port (api/authlimit.py): per client address, a
sliding window per budget, 429 before the credential is checked, bounded memory, fail closed."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from tickr import config
from tickr.api import authlimit
from tickr.api.authlimit import AuthLimiter, BadForwardedFor, client_key
from tickr.api.server import (
    PAIR_ROUTE_KIND,
    TOKEN_ROUTE_KIND,
    make_remote_server,
    make_server,
    pair,
    settings_from_config,
)

from .test_remote import NOW, PHONE, REMOTE, link_code, paired, redeem, remote, with_remote_hosts
from .test_server import ROOT, Clock, doc

PROXY = "127.0.0.1"  # the test client's TCP peer, as Caddy's and tailscale serve's
CLIENT = "203.0.113.5"  # documentation addresses (RFC 5737)
OTHER = "203.0.113.6"


class Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fail(limiter: AuthLimiter, key: str, times: int = 1) -> bool:
    started = False
    for _ in range(times):
        assert limiter.acquire(key) is None
        started = limiter.release(key, failed=True)
    return started


# ---------------------------------------------------------------- the limiter


def test_the_budget_blocks_until_the_oldest_failure_leaves_the_window():
    clock = Monotonic()
    limiter = AuthLimiter(3, 600, 10, clock)
    assert not fail(limiter, CLIENT, 2)
    clock.now += 100
    assert fail(limiter, CLIENT)  # the third failure starts the block
    refused = limiter.acquire(CLIENT)
    assert refused is not None and not refused.table_full and refused.retry_after == 500
    clock.now += 499
    assert limiter.acquire(CLIENT) is not None
    clock.now += 1  # the first two failures are 600 s old: two of three are free again
    assert limiter.acquire(CLIENT) is None


def test_a_success_resets_nothing():
    limiter = AuthLimiter(3, 600, 10, Monotonic())
    fail(limiter, CLIENT, 2)
    assert limiter.acquire(OTHER) is None
    limiter.release(OTHER, failed=False)
    assert limiter.acquire(CLIENT) is None
    limiter.release(CLIENT, failed=False)  # its own success neither
    assert fail(limiter, CLIENT)
    assert limiter.acquire(CLIENT) is not None


def test_addresses_have_their_own_budgets_and_a_success_holds_no_slot():
    limiter = AuthLimiter(1, 600, 10, Monotonic())
    fail(limiter, CLIENT)
    assert limiter.acquire(CLIENT) is not None
    assert limiter.acquire(OTHER) is None
    limiter.release(OTHER, failed=False)
    assert len(limiter) == 1


def test_checks_in_flight_count_against_the_budget():
    limiter = AuthLimiter(2, 600, 10, Monotonic())
    assert limiter.acquire(CLIENT) is None
    assert limiter.acquire(CLIENT) is None
    refused = limiter.acquire(CLIENT)  # a third parallel guess: refused before any check
    assert refused is not None and refused.retry_after == authlimit.MIN_RETRY_AFTER_SECONDS


def test_memory_is_bounded_and_the_oldest_aged_out_address_is_evicted():
    clock = Monotonic()
    limiter = AuthLimiter(2, 600, 3, clock)
    for i in range(3):
        fail(limiter, f"198.51.100.{i}")
        clock.now += 10
    clock.now += 600 - 25  # the first address's failure has left the window, the others not
    assert limiter.acquire(CLIENT) is None
    limiter.release(CLIENT, failed=True)
    assert len(limiter) == 3
    # Forgotten: it is now a new address, and the table holds only fresh failures.
    refused = limiter.acquire("198.51.100.0")
    assert refused is not None and refused.table_full


def test_a_table_full_of_fresh_failures_refuses_new_addresses():
    clock = Monotonic()
    limiter = AuthLimiter(5, 600, 2, clock)
    fail(limiter, "198.51.100.1")
    clock.now += 50
    fail(limiter, "198.51.100.1")  # its newest failure ages out at 650
    clock.now += 10
    fail(limiter, "198.51.100.2")
    refused = limiter.acquire(CLIENT)
    assert refused is not None and refused.table_full and refused.retry_after == 590
    assert len(limiter) == 2
    assert limiter.acquire("198.51.100.2") is None  # a tracked address keeps its budget
    limiter.release("198.51.100.2", failed=False)
    clock.now += 590
    assert limiter.acquire(CLIENT) is None


# ---------------------------------------------------------------- the client address


def test_the_trusted_proxy_names_the_client_by_its_last_entry():
    assert client_key(PROXY, [CLIENT], PROXY, 64) == CLIENT
    # Whatever the caller sent first, the entry the proxy added is the last one.
    assert client_key(PROXY, [f"{OTHER}, {CLIENT}"], PROXY, 64) == CLIENT
    assert client_key(PROXY, [OTHER, CLIENT], PROXY, 64) == CLIENT


def test_x_forwarded_for_from_anyone_else_is_ignored():
    assert client_key("192.0.2.9", [CLIENT], PROXY, 64) == "192.0.2.9"


def test_without_the_header_the_client_is_the_peer():
    assert client_key(PROXY, [], PROXY, 64) == PROXY


@pytest.mark.parametrize("value", ["", "unknown", f"{CLIENT}:443", "a, b", f"{CLIENT},"])
def test_a_proxy_header_not_ending_in_an_address_is_refused(value):
    with pytest.raises(BadForwardedFor):
        client_key(PROXY, [value], PROXY, 64)


def test_ipv6_counts_per_prefix_and_mapped_ipv4_as_ipv4():
    assert client_key(PROXY, ["2001:db8:1:2:aaaa::1"], PROXY, 64) == "2001:db8:1:2::/64"
    assert (client_key(PROXY, ["2001:db8:1:2:bbbb::7"], PROXY, 64)
            == client_key(PROXY, ["2001:db8:1:2:aaaa::1"], PROXY, 64))
    assert client_key(PROXY, [f"::ffff:{CLIENT}"], PROXY, 64) == CLIENT


# ---------------------------------------------------------------- the remote port


@pytest.fixture
def make_remote(tmp_path):
    started = []

    def build(**changes):
        cfg = with_remote_hosts(config.load(ROOT / "config" / "bot.toml", root=tmp_path), REMOTE)
        settings = replace(settings_from_config(cfg), port=0, remote_port=0, **changes)
        local = make_server(settings, Clock(NOW))
        server = make_remote_server(local)
        alerts: list[str] = []
        server.auth_alert = alerts.append
        for s in (local, server):
            threading.Thread(target=s.serve_forever, args=(0.01,), daemon=True).start()
            started.append(s)
        return server, settings, alerts

    yield build
    for s in started:
        s.shutdown()
        s.server_close()


def via_proxy(client):
    return {"X-Forwarded-For": client}


def test_bad_tokens_then_429_before_the_good_token_is_checked(make_remote, capsys):
    server, settings, alerts = make_remote()
    token = paired(server, settings)
    budget = settings.auth_failures
    for _ in range(budget):
        resp, _ = remote(server, "GET", "/status", token="wrong", headers=via_proxy(CLIENT))
        assert resp.status == 401
    resp, payload = remote(server, "GET", "/status", token=token, headers=via_proxy(CLIENT))
    assert resp.status == 429 and doc(payload)["error"] == "auth_rate_limited"
    assert int(resp.headers["Retry-After"]) == settings.auth_window_seconds
    assert token not in payload.decode()
    # Another address, and the same device from it, is not affected.
    assert remote(server, "GET", "/status", token=token, headers=via_proxy(OTHER))[0].status \
        != 429
    err = capsys.readouterr().err
    blocks = [line for line in err.splitlines() if "auth limit" in line]
    assert len(blocks) == 1 and CLIENT in blocks[0] and TOKEN_ROUTE_KIND in blocks[0]
    assert token not in err and "wrong" not in err
    assert len(alerts) == 1 and CLIENT in alerts[0] and token not in alerts[0]


def test_pair_has_its_own_stricter_budget(make_remote):
    server, settings, alerts = make_remote()
    token = paired(server, settings)
    assert settings.pair_failures < settings.auth_failures
    code = link_code(pair(settings, PHONE, now=NOW))
    for _ in range(settings.pair_failures):
        assert redeem(server, "wrong", headers=via_proxy(CLIENT))[0].status == 401
    resp, payload = redeem(server, code, headers=via_proxy(CLIENT))
    assert resp.status == 429 and doc(payload)["error"] == "auth_rate_limited"
    assert code not in payload.decode()
    assert settings.pairing_code_path.exists()  # the right code was never looked at
    # The token budget of that address is untouched.
    assert remote(server, "GET", "/status", token=token,
                  headers=via_proxy(CLIENT))[0].status != 429
    assert len(alerts) == 1 and PAIR_ROUTE_KIND in alerts[0] and code not in alerts[0]


def test_a_malformed_body_tests_no_code_and_counts_nothing(make_remote):
    server, settings, _ = make_remote()
    for _ in range(settings.pair_failures + 1):
        resp, _ = remote(server, "POST", "/pair", body={"nope": 1}, headers=via_proxy(CLIENT))
        assert resp.status == 400


def test_x_forwarded_for_counts_only_from_the_trusted_proxy(make_remote):
    server, settings, _ = make_remote(auth_trusted_proxy="192.0.2.1", auth_failures=2)
    for client in (CLIENT, OTHER):  # both counted under the peer, whatever they claim
        assert remote(server, "GET", "/status", token="wrong",
                      headers=via_proxy(client))[0].status == 401
    resp, _ = remote(server, "GET", "/status", token="wrong", headers=via_proxy("198.51.100.7"))
    assert resp.status == 429


def test_a_caller_cannot_choose_its_address_through_the_trusted_proxy(make_remote):
    server, settings, _ = make_remote(auth_failures=2)
    for _ in range(2):
        remote(server, "GET", "/status", token="wrong", headers=via_proxy(CLIENT))
    # A forged first entry: the proxy's own entry, the last, still names the caller.
    resp, _ = remote(server, "GET", "/status", token="wrong",
                     headers=via_proxy(f"{OTHER}, {CLIENT}"))
    assert resp.status == 429


def test_a_malformed_proxy_header_is_refused_and_counted_under_the_peer(make_remote):
    server, settings, _ = make_remote(auth_failures=2)
    for _ in range(2):
        resp, payload = remote(server, "GET", "/status", token="x", headers=via_proxy("bogus"))
        assert resp.status == 400 and doc(payload)["error"] == "bad_request"
    resp, _ = remote(server, "GET", "/status", token="wrong")  # no header: the peer itself
    assert resp.status == 429


def test_a_full_table_fails_closed_for_new_addresses(make_remote, capsys):
    server, settings, alerts = make_remote(auth_max_addresses=2)
    token = paired(server, settings)
    for client in ("198.51.100.1", "198.51.100.2"):
        remote(server, "GET", "/status", token="wrong", headers=via_proxy(client))
    resp, payload = remote(server, "GET", "/status", token=token, headers=via_proxy(CLIENT))
    assert resp.status == 429 and doc(payload)["error"] == "auth_rate_limited"
    assert remote(server, "GET", "/status", token=token,
                  headers=via_proxy(OTHER))[0].status == 429
    assert len(server.auth_limits[TOKEN_ROUTE_KIND]) == 2
    assert len(alerts) == 1  # once per spell, not once per refused request
    assert token not in capsys.readouterr().err


@pytest.mark.parametrize(("key", "value"), [
    ("remote_auth_trusted_proxy", "caddy"), ("remote_auth_failures", 0),
    ("remote_pair_window_seconds", 599), ("remote_auth_ipv6_prefix_length", 129),
])
def test_bad_limit_settings_refused_at_start(tmp_path, key, value):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    cfg = replace(cfg, api=replace(cfg.api, **{key: value}))
    with pytest.raises(config.ConfigError, match=key):
        settings_from_config(cfg)
