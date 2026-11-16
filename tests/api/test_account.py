"""GET /account: the bot's account-v1 file with its age; a missing or bad file is 503."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.server import make_server, settings_from_config

from .samples import T0, account
from .schema_check import errors
from .test_server import ROOT, Clock, doc, request, schema


@pytest.fixture
def served(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    settings = replace(settings_from_config(cfg), port=0)
    clock = Clock(T0 + timedelta(seconds=4))
    server = make_server(settings, clock)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server, clock
    server.shutdown()
    server.server_close()


def test_fresh_account_is_served_with_its_age(served):
    server, _ = served
    contract.write_account(server.settings.account_path, account())
    resp, payload = request(server, "GET", "/account")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("account-response-v1"), body) == []
    assert (body["age_seconds"], body["stale"], body["api_version"]) == ("4.000", False, 1)
    assert body["account"] == contract.to_jsonable(account())


def test_old_account_is_stale(served):
    server, clock = served
    contract.write_account(server.settings.account_path, account())
    clock.now = T0 + timedelta(seconds=float(server.settings.account_stale_seconds) + 1)
    resp, payload = request(server, "GET", "/account")
    body = doc(payload)
    assert resp.status == 200 and body["stale"] is True
    assert Decimal(body["age_seconds"]) > server.settings.account_stale_seconds


def test_missing_account_file_is_unavailable(served):
    server, _ = served
    resp, payload = request(server, "GET", "/account")
    assert resp.status == 503
    assert doc(payload) == {"error": "unavailable",
                            "cause": "the bot has not written an account file",
                            "api_version": 1,
                            "cause_codes": [{"code": "no_account_file", "params": {}}]}


@pytest.mark.parametrize("text", ["{", '{"read_at": "2026-11-16T15:00:00.123Z"}'])
def test_invalid_account_file_is_unavailable_with_a_cause(served, text):
    server, _ = served
    path = server.settings.account_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    resp, payload = request(server, "GET", "/account")
    assert resp.status == 503
    assert doc(payload)["cause"].startswith("account file is invalid")


def test_account_from_the_future_is_unavailable(served):
    server, _ = served
    contract.write_account(server.settings.account_path, account(T0 + timedelta(minutes=1)))
    resp, payload = request(server, "GET", "/account")
    assert resp.status == 503 and "future" in doc(payload)["cause"]


def test_account_takes_no_query_and_only_get(served):
    server, _ = served
    contract.write_account(server.settings.account_path, account())
    assert request(server, "GET", "/account?x=1")[0].status == 400
    assert request(server, "POST", "/account", {})[0].status == 405
