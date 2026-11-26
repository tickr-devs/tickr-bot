"""`tickr probe-news-delay` (model/newsprobe.py): receipt times of Benzinga news and earnings;
no network (a stand-in for urlopen), no real key."""

from __future__ import annotations

import dataclasses
import datetime
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from tickr import config
from tickr.cli import main as cli_main
from tickr.model import newsprobe
from tickr.model.errors import ModelRefused

ROOT = Path(__file__).resolve().parents[2]
KEY = "api-canary-7"
T0 = datetime.datetime(2026, 11, 25, 15, 0, 0, tzinfo=datetime.UTC)  # 10:00 New York


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    monkeypatch.setenv(c.massive.api_key_variable, KEY)
    return c


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Wire:
    """urlopen stand-in: path -> list of answers, one taken per request (bytes, or a status)."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        path = urllib.parse.urlsplit(request.full_url).path
        answer = self.answers[path].pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "x", {}, None)
        return Response(answer)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t += datetime.timedelta(seconds=1)
        return self.t


def page(rows):
    return json.dumps({"results": rows}).encode()


def art(i, published, **kw):
    return {"benzinga_id": i, "published": published, "tickers": ["AAA"], "channels": ["news"],
            "title": "never kept", "body": "never kept", **kw}


def rep(i, **kw):
    return {"benzinga_id": i, "ticker": "AAA", "date": "2026-11-25", "time": "08:30:00",
            "last_updated": "2026-11-25T13:31:00Z", "actual_eps": 0.3, "estimated_eps": 0.1, **kw}


def records(cfg):
    folder = cfg.root / cfg.run.data_dir / cfg.massive.news_probe_dir
    return [json.loads(line) for line in (folder / "2026-11-25.jsonl").read_text().splitlines()]


def test_new_and_revised_records_carry_receipt_and_previous_poll(cfg):
    m = cfg.massive
    a1 = art(1, "2026-11-25T14:00:00Z")
    wire = Wire({
        m.benzinga_news_path: [page([a1]), page([a1, art(2, "2026-11-25T15:00:01.5Z")]),
                               page([art(1, "2026-11-25T14:00:00Z", last_updated="x"),
                                     art(2, "2026-11-25T15:00:01.5Z")])],
        m.benzinga_earnings_path: [page([rep(9, actual_eps=None)]), page([rep(9)]),
                                   page([rep(9)])],
        m.massive_news_path: [page([])] * 3,
    })
    assert newsprobe.run(cfg, opener=wire, now=Clock(), polls=3, out=lambda s: None) == 0
    got = records(cfg)
    items = [r for r in got if r["event"] != "poll"]
    assert [(r["source"], r["event"], r["id"]) for r in items] == [
        ("benzinga_news", "baseline", "1"), ("benzinga_earnings", "baseline", "9"),
        ("benzinga_news", "new", "2"), ("benzinga_earnings", "revised", "9"),
        ("benzinga_news", "revised", "1")]
    new = items[2]
    assert new["published"] == "2026-11-25T15:00:01.5Z" and new["tickers"] == ["AAA"]
    assert "title" not in new and "body" not in new and "last_updated" not in new
    assert new["previous_poll_sent_utc"] < new["poll_sent_utc"] < new["received_utc"]
    assert items[0]["previous_poll_sent_utc"] is None
    assert items[3]["actual_eps"] == 0.3
    polls = [r for r in got if r["event"] == "poll"]
    assert len(polls) == 9 and polls[3]["new"] == 1 and polls[3]["rows"] == 2
    assert KEY not in json.dumps(got)


def test_queries_are_the_training_endpoints_with_the_key_in_the_header_only(cfg):
    m = cfg.massive
    wire = Wire({m.benzinga_news_path: [page([])], m.benzinga_earnings_path: [page([])],
                 m.massive_news_path: [page([])]})
    newsprobe.run(cfg, opener=wire, now=Clock(), polls=1, out=lambda s: None)
    news, earn, general = (urllib.parse.urlsplit(r.full_url) for r in wire.requests)
    assert news.hostname == m.api_host == earn.hostname == general.hostname
    q = dict(urllib.parse.parse_qsl(news.query))
    sent = T0 + datetime.timedelta(seconds=2)
    since = sent - datetime.timedelta(seconds=m.news_probe_news_lookback_seconds)
    assert q == {"published.gte": since.isoformat(timespec="seconds"), "sort": "published.asc",
                 "limit": str(m.page_limit)}
    q = dict(urllib.parse.parse_qsl(earn.query))
    first = datetime.date(2026, 11, 25) - datetime.timedelta(
        days=m.news_probe_earnings_lookback_days)
    assert q == {"date.gte": first.isoformat(), "date.lte": "2026-11-25", "sort": "date.asc",
                 "limit": str(m.page_limit)}
    sent = T0 + datetime.timedelta(seconds=6)  # third request of the round
    since = sent - datetime.timedelta(seconds=m.news_probe_news_lookback_seconds)
    assert general.path == "/v2/reference/news"
    assert dict(urllib.parse.parse_qsl(general.query)) == {
        "published_utc.gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "order": "asc",
        "sort": "published_utc", "limit": str(m.page_limit)}
    for r in wire.requests:
        assert r.get_header("Authorization") == "Bearer " + KEY
        assert KEY not in r.full_url


def test_a_failed_poll_is_recorded_and_the_next_one_times_from_the_last_success(cfg):
    m = cfg.massive
    a1 = art(1, "2026-11-25T14:00:00Z")
    wire = Wire({m.benzinga_news_path: [page([a1]), 500, page([a1, art(2, "2026-11-25T15:00Z")])],
                 m.benzinga_earnings_path: [page([]), page([]), page([])],
                 m.massive_news_path: [page([])] * 3})
    newsprobe.run(cfg, opener=wire, now=Clock(), polls=3, out=lambda s: None)
    got = records(cfg)
    failed = [r for r in got if r["event"] == "poll_failed"]
    assert [(r["source"], r["cause"]) for r in failed] == [("benzinga_news", "http_status")]
    new = next(r for r in got if r["event"] == "new")
    first_poll = next(r for r in got if r["event"] == "poll" and r["source"] == "benzinga_news")
    assert new["previous_poll_sent_utc"] == first_poll["poll_sent_utc"]


def test_a_row_without_id_is_counted_never_guessed(cfg):
    m = cfg.massive
    wire = Wire({m.benzinga_news_path: [page([{"published": "2026-11-25T14:00:00Z"},
                                              art(1, "2026-11-25T14:00:00Z")])],
                 m.benzinga_earnings_path: [page([])], m.massive_news_path: [page([])]})
    newsprobe.run(cfg, opener=wire, now=Clock(), polls=1, out=lambda s: None)
    got = records(cfg)
    assert [r["event"] for r in got if r["source"] == "benzinga_news"] == [
        "row_unreadable", "baseline", "poll"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_stops_the_probe(cfg, status, capsys):
    wire = Wire({cfg.massive.benzinga_news_path: [status]})
    assert newsprobe.main(cfg, opener=wire, now=Clock(), polls=1, out=lambda s: None) == 3
    assert "refused: http_status" in capsys.readouterr().err


def test_a_missing_key_is_named_and_nothing_is_sent(cfg, monkeypatch, capsys):
    monkeypatch.delenv(cfg.massive.api_key_variable)
    wire = Wire({})
    assert newsprobe.main(cfg, opener=wire, now=Clock(), polls=1, out=lambda s: None) == 3
    assert wire.requests == []
    assert f"credential_missing: {cfg.massive.api_key_variable}" in capsys.readouterr().err


def test_bad_probe_settings_are_refused(cfg):
    for change in ({"news_probe_poll_seconds": 0}, {"news_probe_dir": "../x"},
                   {"news_probe_news_lookback_seconds": 0}):
        bad = dataclasses.replace(cfg, massive=dataclasses.replace(cfg.massive, **change))
        with pytest.raises(ModelRefused) as exc:
            newsprobe.Probe(bad)
        assert exc.value.cause == "probe_config"


def test_the_cli_command_runs_the_probe(monkeypatch):
    seen = []
    monkeypatch.setattr(newsprobe, "main", lambda c: seen.append(c.massive.api_host) or 0)
    assert cli_main(["probe-news-delay"]) == 0
    assert seen == ["api.massive.com"]


def general(i, published, publisher="GlobeNewswire Inc.", **kw):
    return {"id": i, "published_utc": published, "tickers": ["AAA"], "title": "never kept",
            "publisher": {"name": publisher, "homepage_url": "never kept"}, **kw}


def test_general_news_keeps_the_training_fields_and_every_publisher(cfg):
    m = cfg.massive
    g1 = general("a", "2026-11-25T14:00:00Z")
    wire = Wire({m.benzinga_news_path: [page([])] * 3, m.benzinga_earnings_path: [page([])] * 3,
                 m.massive_news_path: [
                     page([g1]),
                     page([g1, general("b", "2026-11-25T15:00:00Z", "Zacks")]),
                     page([general("a", "2026-11-25T14:00:00Z", homepage="x"),
                           general("b", "2026-11-25T15:00:00Z", "Zacks", tickers=["BBB"])])]})
    newsprobe.run(cfg, opener=wire, now=Clock(), polls=3, out=lambda s: None)
    items = [r for r in records(cfg) if r["source"] == "massive_news" and r["event"] != "poll"]
    assert [(r["event"], r["id"]) for r in items] == [("baseline", "a"), ("new", "b"),
                                                     ("revised", "b")]
    assert items[1] == {"event": "new", "source": "massive_news", "id": "b",
                        "published_utc": "2026-11-25T15:00:00Z", "tickers": ["AAA"],
                        "publisher": {"name": "Zacks"},
                        "poll_sent_utc": items[1]["poll_sent_utc"],
                        "received_utc": items[1]["received_utc"],
                        "previous_poll_sent_utc": items[1]["previous_poll_sent_utc"]}
    assert items[1]["previous_poll_sent_utc"] < items[1]["received_utc"]
