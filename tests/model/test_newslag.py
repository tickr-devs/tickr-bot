"""`tickr news-lag` (model/newslag.py): figures from synthetic revision-store rows, the share above
the pinned bz_lag_s, coverage gaps, the once-a-day alert. No network, no real key."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest

from tickr import config
from tickr.model import newslag
from tickr.model import revisions as rv
from tickr.runtime.alerts import Alerter, AlertKind

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.datetime(2026, 11, 25, 16, 0, 0, tzinfo=datetime.UTC)
LAG = 60
WINDOW = 3600.0


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def fill_news(folder, delays, *, start=NOW - datetime.timedelta(minutes=30), baseline=False):
    """One news id per delay (s): published `delay` before it was received."""
    store = rv.RevisionStore(folder, rv.NEWS)
    for n, d in enumerate(delays):
        received = start + datetime.timedelta(seconds=n)
        pub = received - datetime.timedelta(seconds=d)
        store.append([{"benzinga_id": f"n{start.timestamp()}-{n}", "published": stamp(pub)}],
                     received, baseline=baseline)


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "store"


def test_median_worst_and_share_above_the_lag(folder):
    fill_news(folder, [10, 20, 30, 90, 600])
    got = newslag.measure(folder, rv.NEWS, NOW, WINDOW, LAG)
    assert got["count"] == 5
    assert got["median"] == 30 and got["worst"] == 600
    assert got["share_above"] == pytest.approx(2 / 5)
    assert got["bz_lag_s"] == LAG and got["gaps"] == 0
    assert not newslag.breached(got)


def test_earnings_uses_last_updated_of_the_first_version(folder):
    store = rv.RevisionStore(folder, rv.EARNINGS)
    for n, d in enumerate([5, 200]):
        received = NOW - datetime.timedelta(minutes=10, seconds=-n)
        store.append([{"benzinga_id": f"e{n}", "ticker": "AAA", "date": "2026-11-25",
                       "time": "07:00:00",
                       "last_updated": stamp(received - datetime.timedelta(seconds=d))}],
                     received)
    got = newslag.measure(folder, rv.EARNINGS, NOW, WINDOW, LAG)
    assert (got["count"], got["worst"], got["share_above"]) == (2, 200, 0.5)


def test_window_and_baseline_leave_ids_out(folder):
    fill_news(folder, [999], start=NOW - datetime.timedelta(hours=3))  # before the window
    fill_news(folder, [999], start=NOW - datetime.timedelta(minutes=5), baseline=True)
    fill_news(folder, [7])
    got = newslag.measure(folder, rv.NEWS, NOW, WINDOW, LAG)
    assert (got["count"], got["worst"]) == (1, 7)


def test_empty_store_has_no_figures(folder):
    got = newslag.measure(folder, rv.NEWS, NOW, WINDOW, LAG)
    assert got["count"] == 0 and got["median"] is None and not newslag.breached(got)


def test_gaps_in_the_window_are_counted(folder):
    log = rv.CoverageLog(folder, rv.NEWS)

    def ok(at):
        log.add({"event": "ok", "sent_utc": stamp(at), "received_utc": stamp(at)})

    ok(NOW - datetime.timedelta(hours=5))
    log.add({"event": "failed", "sent_utc": stamp(NOW - datetime.timedelta(hours=4)),
             "cause": "http_status", "detail": "500"})
    ok(NOW - datetime.timedelta(hours=4, minutes=-1))  # gap wholly before the window
    log.add({"event": "start", "at_utc": stamp(NOW - datetime.timedelta(minutes=30))})
    ok(NOW - datetime.timedelta(minutes=29))  # restart gap inside the window
    log.add({"event": "failed", "sent_utc": stamp(NOW - datetime.timedelta(minutes=1)),
             "cause": "http_status", "detail": "500"})  # open gap, still unfilled
    assert newslag.measure(folder, rv.NEWS, NOW, WINDOW, LAG)["gaps"] == 2


class Recorder:
    def __init__(self):
        self.shown = []

    def notify(self, title, text):
        self.shown.append((title, text))


@pytest.fixture
def cfg(tmp_path):
    c = config.load(ROOT / "config" / "bot.toml", root=tmp_path)
    return dataclasses.replace(c, massive=dataclasses.replace(c.massive, revision_store_dir="rs"))


def test_alert_fires_once_a_day_per_kind(cfg, tmp_path):
    folder = newslag.store_folder(cfg)
    fill_news(folder, [100, 200, 300])
    rec = Recorder()
    alerter = Alerter(cfg.alerts, "Tickr x", rec)
    rep = newslag.report(folder, NOW, WINDOW, LAG)
    assert newslag.raise_alerts(rep, folder, NOW, alerter) == ["news"]
    kind, text = rec.shown[0]
    assert kind == f"Tickr x: {AlertKind.NEWS_LAG_HIGH.value}"
    assert "median 200 s" in text and "worst 300 s" in text and "bz_lag_s 60 s" in text
    later = NOW + datetime.timedelta(hours=2)
    assert newslag.raise_alerts(newslag.report(folder, later, 10 * WINDOW, LAG), folder, later,
                                Alerter(cfg.alerts, "Tickr x", rec)) == []  # same day, new process
    assert len(rec.shown) == 1
    tomorrow = NOW + datetime.timedelta(days=1)
    assert newslag.raise_alerts(newslag.report(folder, tomorrow, 2 * 86400.0, LAG), folder,
                                tomorrow, Alerter(cfg.alerts, "Tickr x", rec)) == ["news"]


def test_no_alert_below_the_lag_or_when_nothing_was_delivered(cfg):
    folder = newslag.store_folder(cfg)
    fill_news(folder, [1, 2, 3])
    rec = Recorder()
    rep = newslag.report(folder, NOW, WINDOW, LAG)
    assert newslag.raise_alerts(rep, folder, NOW, Alerter(cfg.alerts, "t", rec)) == []
    fill_news(folder, [500, 600, 700, 800, 900], start=NOW - datetime.timedelta(minutes=10))
    rep = newslag.report(folder, NOW, WINDOW, LAG)
    # No notifier: nothing delivered, so the day is not used up.
    assert newslag.raise_alerts(rep, folder, NOW, Alerter(cfg.alerts, "t", None)) == []
    assert newslag.raise_alerts(rep, folder, NOW, Alerter(cfg.alerts, "t", rec)) == ["news"]


def test_main_prints_figures_and_uses_the_pinned_lag(cfg, capsys):
    fill_news(newslag.store_folder(cfg), [10, 20])
    lines = []
    assert newslag.main(cfg, window_s=WINDOW, send_alert=False, now=lambda: NOW,
                        out=lines.append) == 0
    assert f"bz_lag_s (pinned training setting): {rv.pinned_lag_s()} s" in lines[0]
    assert any(line.startswith("[news] ids 2, median 10 s, worst 20 s") for line in lines)
    assert newslag.main(cfg, window_s=0, send_alert=False, now=lambda: NOW) == 3
    assert "refused" in capsys.readouterr().err
