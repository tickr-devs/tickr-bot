"""GET /quotes: the bot's quotes-v1 file; a missing or bad file is 503."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.server import make_server, settings_from_config

from .samples import T0
from .schema_check import errors
from .test_server import ROOT, Clock, doc, request, schema

QUOTES = contract.QuotesV1(T0, (contract.LiveQuote("ABCD", Decimal("12.50"), Decimal("12.52"),
                                                    T0, T0 + timedelta(milliseconds=200)),))


@pytest.fixture
def served(tmp_path):
    cfg = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    settings = replace(settings_from_config(cfg), port=0)
    server = make_server(settings, Clock(T0 + timedelta(seconds=1)))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_quotes_are_served_as_the_bot_wrote_them(served):
    contract.write_quotes(served.settings.quotes_path, QUOTES)
    resp, payload = request(served, "GET", "/quotes")
    assert resp.status == 200
    body = doc(payload)
    assert errors(schema("quotes-response-v1"), body) == []
    assert body["quotes"] == contract.to_jsonable(QUOTES)
    assert body["served_at"] == "2026-11-16T15:00:01.123Z"


def test_missing_quotes_file_is_unavailable(served):
    resp, payload = request(served, "GET", "/quotes")
    assert resp.status == 503
    assert doc(payload)["cause_codes"] == [{"code": "no_quotes_file", "params": {}}]


def test_invalid_quotes_file_is_unavailable_with_a_cause(served):
    path = served.settings.quotes_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", "utf-8")
    resp, payload = request(served, "GET", "/quotes")
    assert resp.status == 503
    assert doc(payload)["cause_codes"][0]["code"] == "quotes_file_invalid"


def test_settings_tell_clients_how_often_to_read_quotes_and_how_old_one_may_be(served):
    resp, payload = request(served, "GET", "/settings")
    body = doc(payload)
    assert body["quotes_poll_seconds"] == "1"
    assert body["live_quote_max_age_seconds"] == "60"
