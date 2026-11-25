"""`tickr poll-revisions` (model/revpoll.py): revisions appended with receipt times, a failed poll
or a restart leaves a typed gap that `write_l1bz` refuses, a restart resumes from the stored
state. No network (a stand-in for urlopen), no real key."""

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
from tickr.model import revisions as rv
from tickr.model import revpoll
from tickr.model.errors import ModelRefused

ROOT = Path(__file__).resolve().parents[2]
KEY = "api-canary-7"
T0 = datetime.datetime(2026, 11, 25, 15, 0, 0, tzinfo=datetime.UTC)  # 10:00 New York
DAY = "2026-11-25"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    monkeypatch.setenv(c.massive.api_key_variable, KEY)
    return dataclasses.replace(c, massive=dataclasses.replace(c.massive,
                                                              revision_poll_enabled=True))


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
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        self.t += datetime.timedelta(seconds=1)
        return self.t


def page(rows):
    return json.dumps({"results": rows}).encode()


def rep(i, lu, **kw):
    return {"benzinga_id": i, "ticker": "AAA", "date": DAY, "time": "08:30:00",
            "last_updated": lu, "actual_eps": 0.3, "estimated_eps": 0.1, **kw}


def art(i, published, **kw):
    return {"benzinga_id": i, "published": published, "tickers": ["AAA"], "title": "kept", **kw}


def wire_for(cfg, earnings, news):
    return Wire({cfg.massive.benzinga_earnings_path: list(earnings),
                 cfg.massive.benzinga_news_path: list(news)})


def run(cfg, wire, clock, polls):
    return revpoll.run(cfg, opener=wire, now=clock, polls=polls, out=lambda s: None,
                       wait=lambda s: False)


def stores(cfg):
    folder = cfg.root / cfg.run.data_dir / cfg.massive.revision_store_dir
    return folder, rv.RevisionStore(folder, rv.EARNINGS), rv.RevisionStore(folder, rv.NEWS)


def test_disabled_by_default_in_the_shipped_config(tmp_path):
    c = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    assert c.massive.revision_poll_enabled is False
    with pytest.raises(ModelRefused) as exc:
        revpoll.Poller(c)
    assert exc.value.cause == "revision_poll_disabled"


def test_every_changed_record_is_appended_with_receipt_and_previous_poll(cfg):
    e1, e2 = rep("e1", "2026-11-25T15:01:00Z", actual_eps=None), rep("e1", "2026-11-25T15:03:00Z")
    a1 = art("a1", "2026-11-25T14:00:00Z", last_updated="2026-11-25T14:00:00Z")
    wire = wire_for(cfg, [page([e1]), page([e1, rep("e2", "2026-11-25T15:04:00Z")]), page([e2])],
                    [page([a1]), page([a1]), page([a1])])
    assert run(cfg, wire, Clock(), 3) == 0
    folder, er, news = stores(cfg)
    assert [v.record["actual_eps"] for v in er.by_id["e1"]] == [None, 0.3]
    assert [v.baseline for v in er.by_id["e1"]] == [True, False]  # the first poll times nothing
    assert er.by_id["e2"][0].baseline is False
    v = er.by_id["e1"][1]
    assert v.previous_poll_ns is not None and v.previous_poll_ns < v.received_ns
    assert len(news.by_id["a1"]) == 1  # the same record again is not stored again
    assert KEY not in "".join(p.read_text() for p in folder.rglob("*.jsonl"))
    # the full record is kept, whatever the probe keeps
    assert news.by_id["a1"][0].record["title"] == "kept"


def test_earnings_poll_by_last_updated_and_news_by_published_window(cfg):
    m = cfg.massive
    wire = wire_for(cfg, [page([]), page([])], [page([]), page([])])
    run(cfg, wire, Clock(), 2)
    earn1, news1, earn2, news2 = (urllib.parse.urlsplit(r.full_url) for r in wire.requests)
    folder, _, _ = stores(cfg)
    events = rv.CoverageLog(folder, rv.EARNINGS).events
    sent1 = datetime.datetime.fromisoformat(events[1]["sent_utc"])
    nsent = [datetime.datetime.fromisoformat(e["sent_utc"])
             for e in rv.CoverageLog(folder, rv.NEWS).events[1:]]
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    lookback = datetime.timedelta(seconds=m.news_probe_news_lookback_seconds)
    assert earn1.path == m.benzinga_earnings_path and news1.path == m.benzinga_news_path
    assert dict(urllib.parse.parse_qsl(earn1.query)) == {
        "last_updated.gte": (sent1 - lookback).strftime(stamp), "sort": "last_updated.asc",
        "limit": str(m.page_limit)}
    assert dict(urllib.parse.parse_qsl(earn2.query))["last_updated.gte"] == (
        sent1 - datetime.timedelta(seconds=m.revision_poll_overlap_seconds)).strftime(stamp)
    for q, sent in ((news1.query, nsent[0]), (news2.query, nsent[1])):  # the window, every poll
        assert dict(urllib.parse.parse_qsl(q)) == {
            "published.gte": (sent - lookback).strftime(stamp), "sort": "published.asc",
            "limit": str(m.page_limit)}
    assert "last_updated" not in news2.query
    for r in wire.requests:
        assert r.get_header("Authorization") == "Bearer " + KEY and KEY not in r.full_url


def test_news_is_never_listed_by_last_updated():
    with pytest.raises(ModelRefused) as exc:
        revpoll.listing_query(rv.NEWS, T0, "last_updated")
    assert exc.value.cause == "news_last_updated_unsupported"


def test_a_server_that_ignores_the_bound_fails_the_poll_and_nothing_is_kept(cfg):
    old = art("old", "2026-11-20T10:00:00Z", last_updated="2026-11-20T10:00:00Z")
    fine = art("fine", "2026-11-25T14:00:00Z", last_updated="2026-11-25T14:00:00Z")
    stale = rep("e0", "2026-11-01T00:00:00Z")
    wire = wire_for(cfg, [page([stale]), page([rep("e1", "2026-11-25T15:01:00Z")])],
                    [page([fine, old]), page([fine])])
    run(cfg, wire, Clock(), 2)
    folder, er, news = stores(cfg)
    assert "old" not in news.by_id and "fine" in news.by_id  # poll 1 failed whole; poll 2 kept
    assert "e0" not in er.by_id and "e1" in er.by_id
    for kind in (rv.NEWS, rv.EARNINGS):
        log = rv.CoverageLog(folder, kind)
        assert [e["event"] for e in log.events] == ["start", "failed", "ok"]
        assert log.events[1]["cause"] == "bound_ignored"
        assert [g.cause for g in log.coverage().gaps] == []  # no ok before the failure
    assert "before" in rv.CoverageLog(folder, rv.NEWS).events[1]["detail"]


def test_an_error_poll_leaves_a_typed_gap_and_write_l1bz_refuses_that_range(cfg, tmp_path):
    ok = page([rep("e1", "2026-11-25T15:01:00Z")])
    # earnings answers: ok, 500, ok, ok; news is quiet throughout
    wire = wire_for(cfg, [ok, 500, ok, ok], [page([])] * 4)
    run(cfg, wire, Clock(), 4)
    folder, er, _ = stores(cfg)
    log = rv.CoverageLog(folder, rv.EARNINGS)
    assert [e["event"] for e in log.events] == ["start", "ok", "failed", "ok", "ok"]
    failed = log.events[2]
    assert failed["cause"] == "http_status" and failed["detail"].startswith("500 ")
    cov = log.coverage()
    assert [(g.cause) for g in cov.gaps] == ["poll_failed:http_status"]
    gap = cov.gaps[0]
    assert gap.end_ns is not None and gap.start_ns < gap.end_ns
    cutoff = datetime.datetime(2026, 11, 25, 12, 0, 0, tzinfo=rv.NEW_YORK)
    snap = er.as_of(cutoff)
    with pytest.raises(ModelRefused) as exc:
        rv.write_l1bz(snap, rv.EARNINGS, tmp_path / "out", DAY, DAY, coverage=cov)
    assert exc.value.cause == "revision_range_not_covered"
    assert "gap:poll_failed:http_status" in exc.value.detail
    assert not (tmp_path / "out").exists()
    # the news polls never failed: its coverage has no gap (the range still starts before the
    # first poll of the run, so it is refused for that, with its own cause)
    news_cov = rv.CoverageLog(folder, rv.NEWS).coverage()
    assert news_cov.gaps == []


def test_a_range_inside_an_uninterrupted_run_is_written(cfg, tmp_path):
    wire = wire_for(cfg, [page([rep("e1", "2026-11-25T15:01:00Z")])] * 3, [page([])] * 3)
    folder, _, _ = stores(cfg)
    run(cfg, wire, Clock(), 3)
    _, er, _ = stores(cfg)
    cov = rv.CoverageLog(folder, rv.EARNINGS).coverage()
    t = cov.spans[0]
    a, b = rv._stamp(t[0]), rv._stamp(t[1])
    assert cov.refusal(t[0], t[1]) is None
    assert cov.refusal(t[0] - 1, t[1]) == "before_first_poll"
    assert cov.refusal(t[0], t[1] + 1) == "after_last_poll"
    assert a < b
    # a whole New York day is not covered by a run of seconds
    with pytest.raises(ModelRefused) as exc:
        rv.write_l1bz(er.as_of(datetime.datetime(2026, 11, 25, 12, tzinfo=rv.NEW_YORK)),
                      rv.EARNINGS, tmp_path / "o", DAY, DAY, coverage=cov)
    assert exc.value.cause == "revision_range_not_covered"


def test_a_row_without_an_id_is_stored_around_and_logged_as_a_gap(cfg):
    wire = wire_for(cfg, [page([rep("e1", "2026-11-25T15:01:00Z")]),
                          page([{"ticker": "AAA"}, rep("e1", "2026-11-25T15:02:00Z")]),
                          page([])], [page([])] * 3)
    run(cfg, wire, Clock(), 3)
    folder, er, _ = stores(cfg)
    assert len(er.by_id["e1"]) == 2  # the readable row is kept
    events = rv.CoverageLog(folder, rv.EARNINGS).events
    assert [e["event"] for e in events] == ["start", "ok", "failed", "ok"]
    assert events[2]["cause"] == "row_unreadable"
    assert [g.cause for g in rv.CoverageLog(folder, rv.EARNINGS).coverage().gaps] == [
        "poll_failed:row_unreadable"]


def test_a_restart_resumes_from_the_stored_state_and_records_a_gap(cfg):
    e1 = rep("e1", "2026-11-25T15:01:00Z", actual_eps=None)
    wire = wire_for(cfg, [page([e1]), page([e1])], [page([]), page([])])
    clock = Clock()
    run(cfg, wire, clock, 2)
    # a new process: nothing in memory, the stores and logs are read back
    e2 = rep("e1", "2026-11-25T15:20:00Z")
    wire2 = wire_for(cfg, [page([e1, e2])], [page([])])
    clock.t += datetime.timedelta(minutes=30)
    run(cfg, wire2, clock, 1)
    folder, er, _ = stores(cfg)
    assert [v.record["actual_eps"] for v in er.by_id["e1"]] == [None, 0.3]
    assert er.by_id["e1"][1].baseline is False  # resumed: not a baseline
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(wire2.requests[0].full_url).query))
    last_ok = rv.CoverageLog(folder, rv.EARNINGS).events[2]["sent_utc"]
    want = (datetime.datetime.fromisoformat(last_ok)
            - datetime.timedelta(seconds=cfg.massive.revision_poll_overlap_seconds))
    assert q["last_updated.gte"] == want.strftime("%Y-%m-%dT%H:%M:%SZ")
    cov = rv.CoverageLog(folder, rv.EARNINGS).coverage()
    assert [g.cause for g in cov.gaps] == ["restart"]
    assert er.by_id["e1"][1].previous_poll_ns == cov.gaps[0].start_ns


def test_a_refused_key_is_logged_and_stops_the_run(cfg):
    wire = wire_for(cfg, [401], [page([])])
    with pytest.raises(ModelRefused) as exc:
        run(cfg, wire, Clock(), 1)
    assert exc.value.cause == "http_status"
    folder, _, _ = stores(cfg)
    assert [e["event"] for e in rv.CoverageLog(folder, rv.EARNINGS).events] == ["start", "failed"]


def test_an_unreadable_coverage_log_refuses_the_start(cfg):
    folder, _, _ = stores(cfg)
    (folder / "coverage").mkdir(parents=True)
    (folder / "coverage" / "earnings.jsonl").write_text("{broken\n")
    with pytest.raises(ModelRefused) as exc:
        revpoll.Poller(cfg)
    assert exc.value.cause == "revision_store_unreadable"
