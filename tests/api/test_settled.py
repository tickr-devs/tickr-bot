"""Past days' answers built once and kept (api/settled.py): hits, misses, invalidation, today."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tickr import config
from tickr.api import contract, settled
from tickr.api.history import History
from tickr.api.server import make_remote_server, settings_from_config
from tickr.observers.log import DecisionLog
from tickr.strategy.base import Role

from .test_decisions import ACTIVE, DAY, _record, _serve, _write
from .test_server import request
from .test_skipped import _capture, _enter, api, cfg  # noqa: F401 - fixtures

NEXT = DAY + timedelta(days=1)


def _records(cfg):  # noqa: F811
    log = DecisionLog.from_config(cfg)
    _write(log, _record(), _record(minute=1, symbol="EFGH"),
           _enter(), _enter(ACTIVE, Role.ACTIVE, minute=6))
    _capture(cfg)
    return log


def _past(api):  # noqa: F811
    """Today is the day after DAY: DAY is settled."""
    api.clock.now = api.clock.now + timedelta(days=1)


def _body(api, route, day=DAY):  # noqa: F811
    resp, payload = request(api, "GET", f"{route}?date={day.isoformat()}")
    assert resp.status == 200, payload
    return payload


def _uncached(api, route, log, day=DAY, today=NEXT):  # noqa: F811
    document, _ = api.day_document(route, day, today, lambda role: log.records(day, role=role))
    return contract.dumps(document).encode("utf-8")


def _day_folder(api, day=DAY):  # noqa: F811
    return api.settings.settled_dir / day.isoformat()


def test_a_past_day_answers_as_built_and_keeps_every_route_on_disk(api, cfg):  # noqa: F811
    log = _records(cfg)
    _past(api)
    for route in settled.ROUTES:
        assert _body(api, route) == _uncached(api, route, log), route
    names = sorted(p.name.split(settled.KEY_SEPARATOR)[0] for p in _day_folder(api).iterdir())
    assert names == sorted(settled.route_name(r) for r in settled.ROUTES)


def test_a_kept_answer_is_served_without_building(api, cfg, monkeypatch):  # noqa: F811
    _records(cfg)
    _past(api)
    first = {route: _body(api, route) for route in settled.ROUTES}

    def refuse(*args, **kwargs):
        raise AssertionError("built again")

    monkeypatch.setattr(api, "day_document", refuse)
    assert {route: _body(api, route) for route in settled.ROUTES} == first
    # A new API process (a restart) reads them from disk.
    fresh = _serve(cfg)
    try:
        fresh.clock.now = api.clock.now
        monkeypatch.setattr(fresh, "day_document", refuse)
        assert {route: _body(fresh, route) for route in settled.ROUTES} == first
    finally:
        fresh.shutdown()
        fresh.server_close()


def test_a_changed_input_file_builds_the_day_again(api, cfg):  # noqa: F811
    log = _records(cfg)
    _past(api)
    before = _body(api, "/watching")
    _write(log, _record(minute=9, symbol="WXYZ"))  # a late line in the day's decision file
    after = _body(api, "/watching")
    assert after != before and b"WXYZ" in after
    assert after == _uncached(api, "/watching", log)
    # Only the newest key's file is kept for the route.
    assert len([p for p in _day_folder(api).iterdir() if p.name.startswith("watching-")]) == 1
    # A capture file of the day changing (here archived-like: rewritten) is a new key too.
    bars = _capture(cfg, tail="")
    stamp = bars.stat()
    os.utime(bars, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1))
    key = api.settled_key(DAY, NEXT)
    assert api.settled.get(DAY, key, "/skipped") is None
    assert _body(api, "/skipped") == _uncached(api, "/skipped", log)


def test_another_code_or_config_is_another_key(api, cfg):  # noqa: F811
    _records(cfg)
    key = api.settled_key(DAY, NEXT)
    assert key == api.settled_key(DAY, NEXT)
    api.code_key = "another release"
    assert api.settled_key(DAY, NEXT) != key
    other = replace(api.settings, config_digest="another config")
    api.code_key = settled.code_key()
    api.settings = other
    assert api.settled_key(DAY, NEXT) != key


def test_today_is_never_kept(api, cfg):  # noqa: F811
    log = _records(cfg)
    assert api.settled_key(DAY, DAY) is None
    assert api.settled_key(NEXT, DAY) is None
    for route in settled.ROUTES:
        assert _body(api, route) == _uncached(api, route, log, today=DAY), route
    assert not api.settings.settled_dir.exists()


def test_an_answer_a_transient_failure_decided_is_not_kept(api, cfg):  # noqa: F811
    log = _records(cfg)
    _past(api)
    paths = api.observer_results.paths
    api.observer_results.paths = replace(paths, calendar=paths.calendar.parent / "missing")
    failed = _body(api, "/observers")
    assert b"session calendar unreadable" in failed
    kept = {p.name.split(settled.KEY_SEPARATOR)[0] for p in _day_folder(api).iterdir()}
    assert "observers" not in kept and "watching" in kept
    api.observer_results.paths = paths  # the calendar is back, no input file changed
    again = _body(api, "/observers")
    assert b"session calendar unreadable" not in again
    assert again == _uncached(api, "/observers", log)


def test_a_refused_route_is_answered_and_not_kept_while_the_others_are(api, cfg):  # noqa: F811
    log = _records(cfg)
    _past(api)
    with open(log.path(DAY), "a", encoding="utf-8") as handle:
        handle.write('{"role": "active", broken\n')  # malformed, matched by the active filter
    resp, _ = request(api, "GET", f"/watching?date={DAY.isoformat()}")
    assert resp.status == 503
    assert not _day_folder(api).exists() or not any(
        p.name.startswith("watching-") for p in _day_folder(api).iterdir())


def test_parallel_reads_of_one_day_build_each_route_once(api, cfg, monkeypatch):  # noqa: F811
    _records(cfg)
    _past(api)
    built: list[str] = []
    original = api.day_document

    def counted(path, day, today, records):
        built.append(path)
        return original(path, day, today, records)

    monkeypatch.setattr(api, "day_document", counted)
    bodies: dict[str, bytes] = {}
    # As the apps read a pick: every route at once (called directly: six connections at once
    # would exceed the test server's listen backlog).
    threads = [threading.Thread(
        target=lambda r=route: bodies.__setitem__(r, api.settled_body(r, DAY, NEXT)))
        for route in settled.ROUTES]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(bodies) == set(settled.ROUTES)
    assert sorted(built) == sorted(settled.ROUTES)  # one build of the day serves every request


def test_the_phone_listener_shares_the_kept_answers(api, cfg):  # noqa: F811
    api.settings = replace(api.settings, remote_port=0)
    remote = make_remote_server(api)
    try:
        assert remote.settled is api.settled and remote.captured_bars is api.captured_bars
    finally:
        remote.server_close()


def test_warming_the_latest_past_day_builds_and_keeps_it(api, cfg):  # noqa: F811
    _records(cfg)
    _past(api)
    api._warm_past_day(DAY, NEXT)
    key = api.settled_key(DAY, NEXT)
    assert all(api.settled.get(DAY, key, route) is not None for route in settled.ROUTES)
    assert list(_day_folder(api).glob(f"{settled.BARS_FOLDER}-*/{settled.SYMBOLS_FILE}"))


# ---------------------------------------------------------------- a past day's bars index


def _plain(api):  # noqa: F811
    """The same history without the index: what GET /days/<date> answered before it."""
    return History(api.settings.history, api.settings.after)


def test_a_past_days_trades_and_bars_read_through_the_index_are_unchanged(api, cfg):  # noqa: F811
    _records(cfg)
    _past(api)
    assert contract.dumps(api.history.day(DAY, NEXT)) == contract.dumps(_plain(api).day(DAY, NEXT))
    assert api.history.bars("ABCD", DAY, NEXT) == _plain(api).bars("ABCD", DAY, NEXT)
    assert api.history.bars("ABCD", DAY, NEXT).bars  # the fixture's bars, not an empty index
    assert api.history.bars("NONE", DAY, NEXT).bars == ()
    resp, _ = request(api, "GET", f"/days/{DAY.isoformat()}")
    assert resp.status == 200


def test_the_index_is_built_once_and_again_for_another_bars_file(api, cfg, monkeypatch):  # noqa: F811
    _records(cfg)
    builds = []
    original = History._bars_v1_on

    def counted(self, day, today):
        builds.append(day)
        return original(self, day, today)

    monkeypatch.setattr(History, "_bars_v1_on", counted)
    api.history.day(DAY, NEXT)
    api.history.day(DAY, NEXT)
    api.history.bars("ABCD", DAY, NEXT)
    assert builds == [DAY]
    path = _capture(cfg, symbol="WXYZ")  # the bars file rewritten: another stamp
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1))
    assert api.history.day(DAY, NEXT).bar_symbols == _plain(api).day(DAY, NEXT).bar_symbols
    assert builds == [DAY, DAY]
    assert len(list(_day_folder(api).glob(f"{settled.BARS_FOLDER}-*"))) == 1


def test_todays_bars_never_go_through_the_index(api, cfg, monkeypatch):  # noqa: F811
    _records(cfg)
    monkeypatch.setattr(History, "_bars_v1_on", lambda *a: pytest.fail("indexed today"))
    assert api.history.day(DAY, DAY).bar_symbols == ("ABCD",)
    assert not api.settings.settled_dir.exists()


# ---------------------------------------------------------------- the store


def test_memory_keeps_the_most_recently_read_days(tmp_path):
    store = settled.SettledAnswers(tmp_path, memory_days=2, disk_days=5)
    days = [date(2026, 11, d) for d in (10, 11, 12)]
    for day in days:
        store.put(day, "k", {"/flow": day.isoformat().encode()})
    assert list(store._memory) == days[1:]
    # The dropped day still comes from disk, and is remembered again.
    assert store.get(days[0], "k", "/flow") == b"2026-11-10"
    assert list(store._memory) == [days[2], days[0]]


def test_disk_keeps_the_newest_days_and_one_key_per_route(tmp_path):
    store = settled.SettledAnswers(tmp_path, memory_days=1, disk_days=2)
    (tmp_path / "notes").mkdir()  # not a day folder: never deleted
    for d in (10, 12, 11):
        store.put(date(2026, 11, d), "old", {"/flow": b"1"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-11-11", "2026-11-12", "notes"]
    store.put(date(2026, 11, 12), "new", {"/flow": b"2"})
    assert [p.name for p in (tmp_path / "2026-11-12").iterdir()] == ["flow-new.json"]
    assert store.get(date(2026, 11, 12), "old", "/flow") is None
    assert store.get(date(2026, 11, 12), "new", "/flow") == b"2"


@pytest.mark.parametrize("key", ["settled_memory_days", "settled_disk_days"])
def test_counts_must_be_positive(cfg, key):  # noqa: F811
    path = cfg.root / "config" / "bot.toml"
    text = path.read_text("utf-8")
    line = next(x for x in text.splitlines() if x.startswith(f"{key} = "))
    path.write_text(text.replace(line, f"{key} = 0"), "utf-8")
    with pytest.raises(config.ConfigError, match=key):
        settings_from_config(config.load(path))


def test_the_cache_folder_must_be_a_plain_subfolder(cfg):  # noqa: F811
    path = cfg.root / "config" / "bot.toml"
    text = path.read_text("utf-8")
    path.write_text(text.replace('settled_cache_dir = "api-settled"',
                                 'settled_cache_dir = "../elsewhere"'), "utf-8")
    with pytest.raises(config.ConfigError, match="settled_cache_dir"):
        settings_from_config(config.load(path))
