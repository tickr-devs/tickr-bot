"""POST and DELETE /push-token: the apps register their APNs device token, one per device."""

from __future__ import annotations

import hashlib
import json
import stat

import pytest

from tickr.runtime.push_tokens import LOCAL, parse_registration

from .schema_check import errors
from .test_remote import local_api, paired, remote, remote_api, remote_settings  # noqa: F401
from .test_server import api, doc, request, schema, settings  # noqa: F401 - fixtures

ROUTE = "/push-token"
TOKEN = "a1" * 32  # a 32-byte device token in hex, as APNs hands it to the app
OTHER = "b2" * 32


def body(token=TOKEN, environment="development", bundle_id="bot.tickr.ios", platform="ios"):
    return {"token": token, "environment": environment, "bundle_id": bundle_id,
            "platform": platform}


def stored(settings, kind="alert"):  # noqa: F811 - the fixture is passed in
    """The stored tokens of `kind`, by device key."""
    devices = json.loads(settings.alerts.push_tokens.path.read_text("utf-8"))["devices"]
    return {device: kinds[kind] for device, kinds in devices.items() if kind in kinds}


def test_register_replace_and_delete_on_the_local_port(api, settings):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "POST", ROUTE, body())
    assert resp.status == 200, payload
    answer = doc(payload)
    assert errors(schema("push-token-v1"), answer) == []
    assert (answer["registered"], answer["environment"], answer["bundle_id"],
            answer["platform"]) == (True, "development", "bot.tickr.ios", "ios")
    assert TOKEN not in payload.decode()  # never echoed
    path = settings.alerts.push_tokens.path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stored(settings)[LOCAL]["token"] == TOKEN
    # Registering again replaces this device's token: still one entry.
    resp, _ = request(server, "POST", ROUTE, body(OTHER.upper(), "production", "bot.tickr.mac",
                                                  "macos"))
    assert resp.status == 200
    assert list(stored(settings)) == [LOCAL]
    assert stored(settings)[LOCAL] | {"registered_at": None} == {
        "token": OTHER, "environment": "production", "bundle_id": "bot.tickr.mac",
        "platform": "macos", "registered_at": None}
    resp, payload = request(server, "DELETE", ROUTE)
    assert resp.status == 200
    assert errors(schema("push-token-v1"), doc(payload)) == []
    assert doc(payload)["registered"] is False and doc(payload)["environment"] is None
    assert stored(settings) == {}
    resp, _ = request(server, "DELETE", ROUTE)  # nothing left: still fine
    assert resp.status == 200


@pytest.mark.parametrize(("change", "names"), [
    ({"token": "not-hex"}, "token"),
    ({"token": "abc"}, "token"),  # odd number of hex digits
    ({"token": ""}, "token"),
    ({"token": 7}, "token"),
    ({"environment": "staging"}, "environment"),
    ({"environment": "Production"}, "environment"),
    ({"bundle_id": "com.example.other"}, "bundle_id"),
    ({"platform": "watchos"}, "platform"),
    ({"extra": True}, "body"),
    ({"kind": "background"}, "kind"),
    ({"kind": None}, "kind"),
])
def test_a_bad_registration_is_400_and_stores_nothing(api, settings, change, names):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "POST", ROUTE, body() | change)
    assert resp.status == 400
    assert doc(payload)["error"] == "bad_request"
    assert names in doc(payload)["cause"]
    assert not settings.alerts.push_tokens.path.exists()


def test_each_kind_is_kept_and_replaced_on_its_own(api, settings):  # noqa: F811
    server, _ = api
    assert request(server, "POST", ROUTE, body())[0].status == 200  # kind left out: alert
    resp, payload = request(server, "POST", ROUTE, body(OTHER) | {"kind": "activity_start"})
    assert resp.status == 200
    assert errors(schema("push-token-v1"), doc(payload)) == []
    assert doc(payload)["kind"] == "activity_start"
    request(server, "POST", ROUTE, body("c3" * 32) | {"kind": "activity_update"})
    request(server, "POST", ROUTE, body("d4" * 32) | {"kind": "activity_start"})
    assert stored(settings)[LOCAL]["token"] == TOKEN  # the alert token untouched
    assert stored(settings, "activity_start")[LOCAL]["token"] == "d4" * 32
    resp, payload = request(server, "DELETE", ROUTE + "?kind=activity_start")
    assert resp.status == 200 and doc(payload) | {"api_version": 1} == {
        "registered": False, "kind": "activity_start", "environment": None, "bundle_id": None,
        "platform": None, "registered_at": None, "api_version": 1}
    assert stored(settings, "activity_start") == {}
    assert stored(settings)[LOCAL]["token"] == TOKEN
    assert stored(settings, "activity_update")[LOCAL]["token"] == "c3" * 32
    resp, payload = request(server, "DELETE", ROUTE)  # no kind: alert
    assert resp.status == 200 and doc(payload)["kind"] == "alert"
    assert stored(settings) == {} and stored(settings, "activity_update")


@pytest.mark.parametrize("query", ["?kind=background", "?kind=", "?kind=alert&kind=alert",
                                   "?other=1"])
def test_delete_with_an_unknown_kind_is_400(api, settings, query):  # noqa: F811
    server, _ = api
    request(server, "POST", ROUTE, body())
    resp, payload = request(server, "DELETE", ROUTE + query)
    assert resp.status == 400 and doc(payload)["error"] == "bad_request"
    assert stored(settings)[LOCAL]["token"] == TOKEN


def test_get_is_405(api):  # noqa: F811
    server, _ = api
    resp, _ = request(server, "GET", ROUTE)
    assert resp.status == 405
    assert resp.getheader("Allow") == "POST, DELETE"


def test_an_unreadable_store_is_503_and_kept(api, settings):  # noqa: F811
    server, _ = api
    path = settings.alerts.push_tokens.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", "utf-8")
    resp, payload = request(server, "POST", ROUTE, body())
    assert resp.status == 503
    assert path.read_text("utf-8") == "{"
    assert "{" not in doc(payload)["cause"].split(";")[0].replace(path.name, "")


def test_each_paired_device_has_its_own_token_and_the_token_is_never_logged(
        local_api, remote_api, remote_settings, capfd):  # noqa: F811
    phone = paired(remote_api, remote_settings)
    resp, payload = remote(remote_api, "POST", ROUTE, token=phone, body=body())
    assert resp.status == 200, payload
    # The Mac's own app on the local port is another device.
    resp, _ = request(local_api, "POST", ROUTE, body(OTHER, platform="macos",
                                                     bundle_id="bot.tickr.mac"))
    assert resp.status == 200
    key = hashlib.sha256(phone.encode("utf-8")).hexdigest()
    devices = stored(remote_settings)
    assert devices[key]["token"] == TOKEN and devices[LOCAL]["token"] == OTHER
    # Without the paired token nothing is stored or removed.
    resp, _ = remote(remote_api, "POST", ROUTE, body=body(OTHER))
    assert resp.status == 401
    resp, _ = remote(remote_api, "DELETE", ROUTE)
    assert resp.status == 401
    resp, _ = remote(remote_api, "DELETE", ROUTE, token=phone)
    assert resp.status == 200
    assert list(stored(remote_settings)) == [LOCAL]
    out, err = capfd.readouterr()
    assert TOKEN not in out + err and phone not in out + err


def test_the_same_device_token_under_a_new_pairing_is_kept_once(api, settings):  # noqa: F811
    store = settings.alerts.push_tokens
    server, _ = api
    request(server, "POST", ROUTE, body())
    # The phone, paired again, registers the same device token under its new bearer digest.
    token = parse_registration(body(), ("bot.tickr.ios",), {"development"}, server.clock())
    store.register("f" * 64, token)
    assert list(stored(settings)) == ["f" * 64]
