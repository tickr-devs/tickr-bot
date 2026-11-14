"""SdkTransport against the real SDK with the network replaced at requests.Session.send.

Skipped when the optional `webull` dependency group is not installed.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("webull")

import requests  # noqa: E402  (installed with the SDK)

from aperture.broker.webull import (  # noqa: E402
    AdapterConfigError,
    Route,
    SdkTransport,
    TransportFailure,
    WebullSettings,
)

HOST = "api.sandbox.webull.com"
ACCOUNT = "CANARY-ACCOUNT-7731"
SECRET = "CANARY-SECRET-5512"
KEY = "CANARY-KEY-9083"


def settings(tmp_path, host=HOST, allowed=frozenset({HOST})) -> WebullSettings:
    return WebullSettings(
        trading_host=host,
        http_port=443,
        allowed_hosts=allowed,
        region_id="us",
        connect_timeout_seconds=5,
        read_timeout_seconds=10,
        token_dir=str(tmp_path / "token"),
        token_check_duration_seconds=1,
        token_check_interval_seconds=1,
        app_key_env="T_APP_KEY",
        app_secret_env="T_APP_SECRET",
        account_id_env="T_ACCOUNT",
    )


class Wire:
    """Replaces the network. `routes` maps path -> list of (status, body) answered in order."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[requests.PreparedRequest] = []

    def send(self, session, prepared, **kwargs):
        self.calls.append(prepared)
        path = urlsplit(prepared.url).path
        answer = self.routes[path].pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
        response.url = prepared.url
        return response

    def hosts(self):
        return {urlsplit(c.url).hostname for c in self.calls}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("T_APP_KEY", KEY)
    monkeypatch.setenv("T_APP_SECRET", SECRET)
    monkeypatch.setenv("T_ACCOUNT", ACCOUNT)
    monkeypatch.chdir(tmp_path)  # the SDK's default log file would land here
    return tmp_path


def install(monkeypatch, routes) -> Wire:
    wire = Wire({"/openapi/config": [(200, {"token_check_enabled": False})], **routes})
    monkeypatch.setattr(requests.Session, "send", lambda s, p, **kw: wire.send(s, p, **kw))
    return wire


def test_host_not_on_allowlist_is_refused_before_any_request(env, monkeypatch):
    wire = install(monkeypatch, {})
    with pytest.raises(AdapterConfigError):
        SdkTransport(settings(env, host="api.webull.com"))
    assert wire.calls == []


def test_missing_credential_names_the_variable_only(env, monkeypatch):
    monkeypatch.delenv("T_ACCOUNT")
    with pytest.raises(AdapterConfigError, match="T_ACCOUNT"):
        SdkTransport(settings(env))


def test_requests_go_only_to_the_configured_host(env, monkeypatch):
    wire = install(monkeypatch, {
        "/trading/orders/get": [(200, {"client_order_id": "ap-1", "price": 1.25})],
        "/trading/orders/place": [(200, {"client_order_id": "ap-1"})],
    })
    transport = SdkTransport(settings(env))
    detail = transport.send(Route.ORDER_DETAIL, {"client_order_id": "ap-1"})
    assert detail.status == 200 and detail.body["price"] == Decimal("1.25")
    transport.send(Route.PLACE, {"new_orders": [{"client_order_id": "ap-1"}]})
    assert wire.hosts() == {HOST}
    get, post = wire.calls[-2:]
    assert parse_qs(urlsplit(get.url).query)["account_id"] == [ACCOUNT]
    assert json.loads(post.body)["account_id"] == ACCOUNT


def test_no_sdk_retries_on_get_or_post(env, monkeypatch):
    wire = install(monkeypatch, {
        "/trading/orders/get": [(500, {"error_code": "INTERNAL_ERROR"})] * 4,
        "/trading/orders/place": [requests.ConnectionError("boom")] * 4,
    })
    transport = SdkTransport(settings(env))
    resp = transport.send(Route.ORDER_DETAIL, {"client_order_id": "ap-1"})
    assert (resp.status, resp.error_code) == (500, "INTERNAL_ERROR")
    with pytest.raises(TransportFailure):
        transport.send(Route.PLACE, {"new_orders": []})
    paths = [urlsplit(c.url).path for c in wire.calls]
    assert paths.count("/trading/orders/get") == 1
    assert paths.count("/trading/orders/place") == 1


def test_error_message_never_carries_the_account_id(env, monkeypatch):
    install(monkeypatch, {
        "/trading/orders/cancel": [(417, {"error_code": "OPENAPI_PARAM_ERR",
                                          "message": f"account {ACCOUNT} order not present"})],
    })
    resp = SdkTransport(settings(env)).send(Route.CANCEL, {"client_order_id": "ap-1"})
    assert resp.status == 417 and ACCOUNT not in resp.message


def test_sdk_writes_no_logs_stdout_or_files(env, monkeypatch, capsys, caplog):
    monkeypatch.setenv("WEBULL_API_DEBUG", "sdk")  # SDK's own header dump switch
    install(monkeypatch, {
        "/trading/orders/get": [(417, {"error_code": "OPENAPI_PARAM_ERR",
                                       "message": "Order not present"})],
    })
    caplog.set_level(logging.DEBUG)
    transport = SdkTransport(settings(env))
    transport.send(Route.ORDER_DETAIL, {"client_order_id": "ap-1"})
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text
    for canary in (ACCOUNT, SECRET, KEY):
        assert canary not in text
    assert not [r for r in caplog.records if r.name.startswith("webull")]
    assert not list(env.glob("*.log"))


def test_market_data_requests_never_carry_the_account_id(env, monkeypatch):
    wire = install(monkeypatch, {
        "/market-data/stocks/snapshots/list": [(200, [])],
        "/market-data/stocks/bars/list": [(200, {"result": []})],
    })
    transport = SdkTransport(settings(env))
    transport.send(Route.SNAPSHOT, {"symbols": "AAA", "category": "US_STOCK"})
    transport.send(Route.BARS, {"symbols": ["AAA"], "category": "US_STOCK", "timespan": "M1",
                                "count": "390"})
    get, post = wire.calls[-2:]
    query = parse_qs(urlsplit(get.url).query)
    assert "account_id" not in query and query["symbols"] == ["AAA"]
    assert "account_id" not in json.loads(post.body)
    assert json.loads(post.body)["symbols"] == ["AAA"]
    assert ACCOUNT not in get.url and ACCOUNT not in str(post.body)


TOKEN = "CANARY-TOKEN-4417"
CREATE, CHECK = "/auth/tokens/create", "/auth/tokens/check"


def token_answer(status: str) -> tuple[int, dict]:
    return 200, {"token": TOKEN, "expires": 1893456000000, "status": status}


def install_token(monkeypatch, create, checks) -> Wire:
    """Token checking enabled (Production); `create` answers the create call, `checks` the
    polls of the SDK's wait loop, in order."""
    wire = install(monkeypatch, {CREATE: [token_answer(create)],
                                 CHECK: [token_answer(s) for s in checks]})
    wire.routes["/openapi/config"] = [(200, {"token_check_enabled": True})]
    return wire


@pytest.fixture
def sdk_clock(monkeypatch):
    """The SDK's token wait loop runs on a fake clock: no real sleeping in tests."""
    from webull.core.http.initializer.token import token_manager

    class Clock:
        now = 0.0

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    monkeypatch.setattr(token_manager, "time", Clock())


def test_pending_token_calls_the_hook_once_before_waiting(env, monkeypatch, sdk_clock, capsys):
    wire = install_token(monkeypatch, "PENDING", ["PENDING", "NORMAL"])
    seen_at: list[int] = []
    SdkTransport(settings(env), lambda: seen_at.append(len(wire.calls)))
    paths = [urlsplit(c.url).path for c in wire.calls]
    assert paths.count(CHECK) == 2
    assert seen_at == [paths.index(CREATE) + 1]  # right after create, before the first check
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_normal_or_disabled_token_does_not_call_the_hook(env, monkeypatch, sdk_clock):
    calls: list[str] = []
    install_token(monkeypatch, "NORMAL", [])
    SdkTransport(settings(env), lambda: calls.append("x"))
    install(monkeypatch, {})  # token checking disabled (Sandbox)
    SdkTransport(settings(env), lambda: calls.append("x"))
    assert calls == []


@pytest.mark.parametrize("state", ["INVALID", "EXPIRED"])
def test_invalid_or_expired_token_is_a_typed_refusal(env, monkeypatch, sdk_clock, state):
    install_token(monkeypatch, "PENDING", [state])
    with pytest.raises(AdapterConfigError, match="INVALID/EXPIRED") as refused:
        SdkTransport(settings(env), lambda: None)
    assert TOKEN not in str(refused.value)


def test_token_not_confirmed_in_time_is_a_typed_refusal(env, monkeypatch, sdk_clock):
    calls: list[str] = []
    install_token(monkeypatch, "PENDING", ["PENDING"] * 3)
    with pytest.raises(AdapterConfigError, match="not confirmed in the Webull app in time"):
        SdkTransport(settings(env), lambda: calls.append("x"))
    assert calls == ["x"]


def test_a_failing_hook_never_stops_token_creation(env, monkeypatch, sdk_clock, capsys):
    install_token(monkeypatch, "PENDING", ["PENDING", "NORMAL"])

    def hook():
        raise OSError("notifier down")

    SdkTransport(settings(env), hook)  # no exception: the token still becomes usable
    assert "token-pending alert failed (OSError)" in capsys.readouterr().err
