"""GET /alerts and PUT /alerts/routing: the notification settings (backlog T14)."""

from __future__ import annotations

import json

import pytest

from tickr.runtime.alert_routing import LOCKED
from tickr.runtime.alerts import AlertKind

from .schema_check import errors
from .test_remote import local_api, paired, remote, remote_api, remote_settings  # noqa: F401
from .test_server import api, doc, request, schema, settings  # noqa: F401 - fixtures

ROUTING = "/alerts/routing"


def _kind(document, name):
    return next(k for k in document["kinds"] if k["kind"] == name)


def _record(settings, checks):  # noqa: F811 - the fixture is passed in
    path = settings.alerts.preflight_record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1, "job": "preflight", "started_at": "2026-11-16T08:30:00+00:00",
        "finished_at": "2026-11-16T08:30:05+00:00", "ny_date": "2026-11-16",
        "outcome": "failed", "blocks_start": True, "cause": "alert-channel-missing",
        "checks": [{"name": name, "passed": passed, "cause": None} for name, passed in checks],
    }), "utf-8")


def test_everything_is_on_before_the_owner_changes_anything(api):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "GET", "/alerts")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("alerts-v1"), body) == []
    assert [k["kind"] for k in body["kinds"]] == [k.value for k in AlertKind]
    assert all(all(k["channels"].values()) for k in body["kinds"])
    assert {k["kind"] for k in body["kinds"] if k["locked"]} == {k.value for k in LOCKED}
    assert body["routing_cause"] is None
    assert [(c["channel"], c["configured"], c["cause"]) for c in body["channels"]] == [
        ("telegram", False, "no-preflight"), ("push", False, "not-built")]


def test_channels_come_from_the_latest_preflight_checks(api, settings):  # noqa: F811
    server, _ = api
    _record(settings, [("alert-channel", False)])
    body = doc(request(server, "GET", "/alerts")[1])
    assert [(c["channel"], c["configured"], c["cause"]) for c in body["channels"]] == [
        ("telegram", False, "not-configured"), ("push", False, "not-built")]
    _record(settings, [("alert-channel", True), ("alert-telegram", True)])
    body = doc(request(server, "GET", "/alerts")[1])
    assert [(c["channel"], c["configured"], c["cause"]) for c in body["channels"]] == [
        ("telegram", True, None), ("push", False, "not-built")]


def test_a_cell_is_switched_off_and_on_and_the_answer_is_the_new_settings(api, settings):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "PUT", ROUTING,
                            {"kind": "disk-low", "channel": "telegram", "on": False})
    assert resp.status == 200, payload
    assert errors(schema("alerts-v1"), doc(payload)) == []
    assert _kind(doc(payload), "disk-low")["channels"] == {
        "telegram": False, "push": True}
    assert _kind(doc(request(server, "GET", "/alerts")[1]), "disk-low")["channels"]["telegram"] \
        is False
    assert settings.alerts.routing.path.exists()
    resp, payload = request(server, "PUT", ROUTING,
                            {"kind": "disk-low", "channel": "telegram", "on": True})
    assert resp.status == 200 and _kind(doc(payload), "disk-low")["channels"]["telegram"]


@pytest.mark.parametrize("kind", ["unprotected-position", "unknown-order-status", "bot-down"])
def test_a_critical_kind_cannot_be_turned_off(api, settings, kind):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "PUT", ROUTING,
                            {"kind": kind, "channel": "telegram", "on": False})
    assert resp.status == 409
    assert doc(payload)["error"] == "conflict"
    assert doc(payload)["cause"].startswith("locked-kind:")
    assert not settings.alerts.routing.path.exists()


@pytest.mark.parametrize(("body", "cause"), [
    ({"kind": "no-such-kind", "channel": "telegram", "on": False}, "unknown-kind:"),
    ({"kind": "disk-low", "channel": "fax", "on": False}, "unknown-channel:"),
    ({"kind": "disk-low", "channel": "telegram", "on": "false"}, "body must be"),
    ({"kind": "disk-low", "channel": "telegram"}, "body must be"),
    ({"kind": "disk-low", "channel": "telegram", "on": False, "extra": 1}, "body must be"),
])
def test_a_malformed_write_is_a_bad_request(api, settings, body, cause):  # noqa: F811
    server, _ = api
    resp, payload = request(server, "PUT", ROUTING, body)
    assert resp.status == 400
    assert doc(payload)["cause"].startswith(cause)
    assert not settings.alerts.routing.path.exists()


def test_an_unreadable_routing_file_is_shown_and_never_overwritten(api, settings):  # noqa: F811
    server, _ = api
    path = settings.alerts.routing.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", "utf-8")
    body = doc(request(server, "GET", "/alerts")[1])
    assert body["routing_cause"].startswith("routing-file-unreadable")
    assert all(all(k["channels"].values()) for k in body["kinds"])
    resp, payload = request(server, "PUT", ROUTING,
                            {"kind": "disk-low", "channel": "telegram", "on": False})
    assert resp.status == 409 and path.read_text("utf-8") == "{broken"


def test_a_deeply_nested_routing_file_shows_its_cause_and_everything_on(api, settings):  # noqa: F811
    server, _ = api
    path = settings.alerts.routing.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[" * 200000, "utf-8")
    resp, payload = request(server, "GET", "/alerts")
    assert resp.status == 200
    body = doc(payload)
    assert body["routing_cause"] == "routing-file-unreadable: RecursionError"
    assert all(all(k["channels"].values()) for k in body["kinds"])


@pytest.mark.parametrize(("method", "path"), [
    ("POST", ROUTING), ("GET", ROUTING), ("PUT", "/alerts"), ("PUT", "/status")])
def test_other_methods_are_refused(api, method, path):  # noqa: F811
    server, _ = api
    resp, _ = request(server, method, path, {} if method != "GET" else None)
    assert resp.status == 405


def test_the_phone_needs_its_token_to_change_routing(remote_api, remote_settings):  # noqa: F811
    body = {"kind": "disk-low", "channel": "telegram", "on": False}
    resp, _ = remote(remote_api, "PUT", ROUTING, body=body)
    assert resp.status == 401
    assert not remote_settings.alerts.routing.path.exists()
    token = paired(remote_api, remote_settings)
    resp, payload = remote(remote_api, "PUT", ROUTING, token=token, body=body)
    assert resp.status == 200, payload
    assert _kind(doc(payload), "disk-low")["channels"]["telegram"] is False
