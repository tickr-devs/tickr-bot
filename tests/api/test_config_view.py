"""GET /config: the bot's configuration file, read-only, with its validation."""

from __future__ import annotations

import re
import shutil
import threading
import tomllib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import config
from tickr.api import contract
from tickr.api.configview import sections_of
from tickr.api.server import make_server, settings_from_config

from .samples import T0
from .schema_check import errors
from .test_server import Clock, doc, request, schema

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def served(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    path = tmp_path / "config" / "bot.toml"
    path.write_bytes((ROOT / "config" / "bot.toml").read_bytes())
    cfg = config.load(path)
    server = make_server(replace(settings_from_config(cfg, path), port=0),
                         Clock(T0 + timedelta(seconds=2)))
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield server, path
    server.shutdown()
    server.server_close()


def test_shows_every_key_of_the_shipped_file_with_its_comment(served):
    server, path = served
    resp, payload = request(server, "GET", "/config")
    body = doc(payload)
    assert resp.status == 200 and errors(schema("config-v1"), body) == []
    assert body["file"] == "config/bot.toml"
    assert body["valid"] is True and body["error"] is None
    assert body["changed_since_start"] is False
    parsed = tomllib.loads(path.read_text("utf-8"), parse_float=Decimal)
    shown = {s["name"]: {k["key"]: k for k in s["keys"]} for s in body["sections"]}

    inline = {"route_limits"}  # the shipped file's only inline tables

    def tables(prefix: str, table: dict) -> dict[str, dict]:
        out = {prefix: {k: v for k, v in table.items()
                        if not isinstance(v, dict) or k in inline}}
        for k, v in table.items():
            if isinstance(v, dict) and k not in inline:
                out |= tables(f"{prefix}.{k}" if prefix else k, v)
        return {name: keys for name, keys in out.items() if keys}

    expected = tables("", parsed)
    assert list(shown) == list(expected)  # every table, in file order
    for name, keys in expected.items():
        assert list(shown[name]) == list(keys), name
        for key, value in keys.items():  # every value reads back as TOML gives it
            assert tomllib.loads(f"v = {shown[name][key]['value']}", parse_float=Decimal)["v"] \
                == value, (name, key)
    assert shown["run"]["environment"]["value"] == '"sandbox"'
    assert shown["run"]["environment"]["comment"].startswith("Active environment;")
    assert all(k["comment"] for s in body["sections"] for k in s["keys"])  # rule 6


def test_an_edited_file_that_no_longer_loads_is_shown_with_its_error(served):
    server, path = served
    text = path.read_text("utf-8").replace("maintain_keep_sessions = 20",
                                           "maintain_keep_sessions = 0")
    path.write_text(text, "utf-8")
    body = doc(request(server, "GET", "/config")[1])
    assert body["changed_since_start"] is True and body["valid"] is False
    assert "maintain_keep_sessions" in body["error"]
    run = next(s for s in body["sections"] if s["name"] == "run")
    assert {"key": "maintain_keep_sessions", "value": "0",
            "comment": next(k for k in run["keys"]
                            if k["key"] == "maintain_keep_sessions")["comment"]} in run["keys"]


def test_broken_toml_still_shows_lines_and_the_parse_error(served):
    server, path = served
    path.write_text(path.read_text("utf-8") + "\nbroken = [\n", "utf-8")
    resp, payload = request(server, "GET", "/config")
    body = doc(payload)
    assert resp.status == 200 and body["valid"] is False and body["sections"] == []
    assert body["error"].startswith("config/bot.toml: ")
    assert str(path.parent) not in body["error"]  # the full path never reaches the phone


def test_a_non_finite_number_is_a_config_error_not_a_crash(served):
    server, path = served
    text, count = re.subn(r"(?m)^max_daily_loss_usd = \S+", "max_daily_loss_usd = nan",
                          path.read_text("utf-8"))
    assert count == 1
    path.write_text(text, "utf-8")
    resp, payload = request(server, "GET", "/config")
    body = doc(payload)
    assert resp.status == 200 and body["valid"] is False
    assert "max_daily_loss_usd" in body["error"] and "finite" in body["error"]


def test_without_a_config_path_it_is_unavailable(served):
    server, _ = served
    server.settings = replace(server.settings, config_file=None)
    resp, payload = request(server, "GET", "/config")
    assert resp.status == 503 and doc(payload)["error"] == "unavailable"


def test_post_is_not_allowed(served):
    server, _ = served
    assert request(server, "POST", "/config", {})[0].status == 405


def test_values_come_from_toml_and_comments_from_the_lines():
    text = ('# file header\n\ntop = 1\n[a]\n# first line\n# second line\nx = 1.50\n'
            'y = "a # not a comment"  # trailing\n# multi\nlong = [\n  1,\n  2,\n]\n'
            's = """\nfoo = 1\n"""\n"q.k" = true\n\n# orphan\n\n[ b . c ]\nz = {u = 2}\n'
            '[b.c.d]\nw = 3\n')
    assert sections_of(text) == (
        contract.ConfigSectionV1("", (contract.ConfigKeyV1("top", "1", ""),)),
        contract.ConfigSectionV1("a", (
            contract.ConfigKeyV1("x", "1.50", "first line second line"),
            contract.ConfigKeyV1("y", '"a # not a comment"', "trailing"),
            contract.ConfigKeyV1("long", "[1, 2]", "multi"),
            contract.ConfigKeyV1("s", '"foo = 1\\n"', ""),
            contract.ConfigKeyV1("q.k", "true", ""))),
        contract.ConfigSectionV1("b.c", (contract.ConfigKeyV1("z", '{"u" = 2}', ""),)),
        contract.ConfigSectionV1("b.c.d", (contract.ConfigKeyV1("w", "3", ""),)),
    )
