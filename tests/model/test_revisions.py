"""Benzinga revisions as received and the snapshot as of a cutoff (Codex codex-108 point 7):
revision order, late revisions ignored, missing fields refused per item, a replay recovers the
record originally seen, and the snapshot files are read by the pinned training reader."""

from __future__ import annotations

import datetime
import gzip
import json

import model_inputs as inputs
import pytest

from tickr.model import revisions as rv
from tickr.model.errors import ModelRefused
from tickr.model.features import full

UTC = datetime.UTC
DAY = "2025-08-18"  # a day of the open archive (summer time: New York = UTC - 4)


def at(hhmmss: str, day: str = DAY) -> datetime.datetime:
    """A New York time of `day` as an aware datetime."""
    h, m, s = (int(x) for x in hhmmss.split(":"))
    d = datetime.date.fromisoformat(day)
    return datetime.datetime(d.year, d.month, d.day, h, m, s, tzinfo=rv.NEW_YORK)


def utc(hhmmss: str, day: str = DAY) -> str:
    return at(hhmmss, day).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def report(i: str, *, lu: str, actual: float | None = 0.3, estimate: float | None = 0.1,
           tk: str = "IG", date: str = DAY, time: str = "08:30:00", **more: object) -> dict:
    r = {"benzinga_id": i, "ticker": tk, "date": date, "time": time, "last_updated": lu,
         "actual_eps": actual, "estimated_eps": estimate, "currency": "USD"}
    r.update(more)
    return r


def store_with(tmp_path, versions):
    """A store fed (receipt New York time, rows) in order."""
    s = rv.RevisionStore(tmp_path / "store", rv.EARNINGS)
    for when, rows in versions:
        s.append(rows, at(when))
    return s


def test_the_latest_eligible_revision_received_by_the_cutoff_is_chosen(tmp_path):
    s = store_with(tmp_path, [
        ("07:00:00", [report("e1", lu=utc("06:59:00"), actual=None)]),  # scheduled, estimate only
        ("08:31:10", [report("e1", lu=utc("08:31:00"), actual=0.25)]),  # first actual
        ("08:45:05", [report("e1", lu=utc("08:45:00"), actual=0.3)]),  # corrected actual
    ])
    assert [v.record["actual_eps"] for v in s.by_id["e1"]] == [None, 0.25, 0.3]
    assert s.as_of(at("08:29:59")).refused == {"e1": "report_after_cutoff"}
    snap = s.as_of(at("08:31:05"))  # report time passed, the actual not yet received
    assert snap.records["e1"]["actual_eps"] is None and snap.placed_only == {"e1"}
    snap = s.as_of(at("08:31:10"))
    assert snap.records["e1"]["actual_eps"] == 0.25 and not snap.placed_only
    assert s.as_of(at("09:00:00")).records["e1"]["actual_eps"] == 0.3


def test_a_revision_received_after_the_cutoff_is_ignored(tmp_path):
    s = store_with(tmp_path, [
        ("08:31:10", [report("e1", lu=utc("08:31:00"), actual=0.25)]),
        ("08:50:00", [report("e1", lu=utc("08:40:00"), actual=0.3)]),  # updated 08:40, came 08:50
    ])
    assert s.as_of(at("08:45:00")).records["e1"]["actual_eps"] == 0.25
    assert s.as_of(at("08:50:00")).records["e1"]["actual_eps"] == 0.3


def test_a_revision_updated_after_the_cutoff_is_not_used_and_fractions_round_up(tmp_path):
    s = store_with(tmp_path, [("08:40:00", [report("e1", lu=utc("08:31:00"), actual=0.25)])])
    s.append([report("e1", lu=at("08:41:00").astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.000000001Z"), actual=0.3)], at("08:40:00"))  # vendor clock ahead
    assert s.as_of(at("08:41:00")).records["e1"]["actual_eps"] == 0.25  # 08:41:00.000000001
    assert s.as_of(at("08:41:01")).records["e1"]["actual_eps"] == 0.3


def test_a_missing_field_is_refused_per_item_never_filled(tmp_path):
    s = store_with(tmp_path, [("09:00:00", [
        report("e1", lu=utc("08:31:00")),
        {k: v for k, v in report("e2", lu=utc("08:31:00"), tk="NK").items() if k != "time"},
        {k: v for k, v in report("e3", lu=utc("08:31:00"), tk="ZZ").items()
         if k != "last_updated"},
        report("e4", lu="2025-08-18 12:31:00", tk="QQ"),
        report("e5", lu=utc("08:31:00"), tk="RR", estimate=None),
    ])])
    snap = s.as_of(at("09:00:00"))
    assert snap.refused == {"e2": "field_missing:time", "e4": "stamp_unreadable:last_updated"}
    assert set(snap.records) == {"e1", "e3", "e5"} and snap.placed_only == {"e3", "e5"}
    assert snap.records["e5"]["estimated_eps"] is None  # er_surp stays missing in l1bz


def test_a_later_report_time_is_never_replaced_by_an_earlier_version(tmp_path):
    s = store_with(tmp_path, [
        ("07:00:00", [report("e1", lu=utc("06:59:00"), actual=None)]),
        ("08:00:00", [report("e1", lu=utc("07:59:00"), actual=None, date="2025-08-19")]),
    ])
    assert s.as_of(at("09:00:00")).refused == {"e1": "report_after_cutoff"}


def test_a_rescheduling_correction_supersedes_the_old_schedule(tmp_path):
    """Codex 109 point 8: the current version is chosen before the report-time check, so a
    report moved to a later day is not placed from its older (past, complete) version."""
    s = store_with(tmp_path, [
        ("08:31:10", [report("e1", lu=utc("08:31:00"), actual=0.25)]),
        ("08:40:05", [report("e1", lu=utc("08:40:00"), actual=None, date="2025-08-19")]),
        ("08:31:10", [report("e2", lu=utc("06:00:00"), actual=None, tk="NK",
                             date="2025-08-19")]),
        ("08:50:05", [report("e2", lu=utc("08:50:00"), actual=0.4, tk="NK")]),  # moved earlier
    ])
    assert s.as_of(at("08:35:00")).records["e1"]["actual_eps"] == 0.25
    snap = s.as_of(at("08:45:00"))
    assert snap.refused == {"e1": "report_after_cutoff", "e2": "report_after_cutoff"}
    snap = s.as_of(at("08:51:00"))
    assert "e1" in snap.refused and snap.records["e2"]["actual_eps"] == 0.4


def test_a_stale_redelivery_is_kept_but_never_wins(tmp_path):
    old = report("e1", lu=utc("08:31:00"), actual=0.25)
    new = report("e1", lu=utc("08:45:00"), actual=0.3)
    s = rv.RevisionStore(tmp_path / "store", rv.EARNINGS)
    assert s.append([old], at("08:31:10"))["saved"] == 1
    assert s.append([dict(old)], at("08:31:20"))["unchanged"] == 1  # a retry: not stored
    assert s.append([new], at("08:45:05"))["saved"] == 1
    got = s.append([dict(old)], at("08:50:00"))  # the old payload arrives again
    assert got["saved"] == 1 and got["repeated"] == 1
    s.append([dict(old, currency="usd")], at("08:55:00"))  # an older update, reworded
    lines = (tmp_path / "store" / "earnings" / f"{DAY}.jsonl").read_text().splitlines()
    assert len(lines) == 4  # every observation kept
    for store in (s, rv.RevisionStore(tmp_path / "store", rv.EARNINGS)):
        assert store.as_of(at("09:00:00")).records["e1"] == new
        assert store.as_of(at("08:40:00")).records["e1"] == old
    by_id = rv.load_versions(sorted(s.folder.glob("*.jsonl")), rv.EARNINGS)
    assert [v.record["actual_eps"] for v in by_id["e1"]] == [0.25, 0.3, 0.25]


def test_different_versions_at_the_same_update_time_are_a_tie(tmp_path):
    s = store_with(tmp_path, [
        ("08:31:10", [report("e1", lu=utc("08:31:00"), actual=0.25)]),
        ("08:31:20", [report("e1", lu=utc("08:31:00"), actual=0.26)]),
        ("08:31:30", [report("e2", lu=utc("08:31:00"), tk="NK")]),
    ])
    snap = s.as_of(at("08:40:00"))
    assert snap.refused == {"e1": "version_tie"} and set(snap.records) == {"e2"}
    s.append([report("e1", lu=utc("08:41:00"), actual=0.27)], at("08:41:05"))
    assert s.as_of(at("08:42:00")).records["e1"]["actual_eps"] == 0.27  # a later update orders
    # without update stamps the receipt orders; two records in one receipt cannot be ordered
    t = rv.RevisionStore(tmp_path / "t", rv.EARNINGS)
    t.append([report("e3", lu="", actual=0.1), report("e3", lu="", actual=0.2)], at("08:31:10"))
    assert t.as_of(at("08:40:00")).refused == {"e3": "version_tie"}


def test_a_past_report_without_eps_or_update_time_keeps_its_timing(tmp_path):
    no_lu = {k: v for k, v in report("e1", lu="", actual=0.25).items() if k != "last_updated"}
    s = store_with(tmp_path, [
        ("08:31:10", [no_lu,
                      report("e2", lu=utc("08:31:00"), tk="NK", actual=None, estimate=None)]),
        ("08:31:10", [report("e3", lu=utc("08:31:00"), tk="QQ", actual=0.5)]),
        ("08:40:05", [report("e3", lu=utc("08:40:00"), tk="QQ", actual=None)]),  # EPS dropped
        ("08:45:05", [dict(no_lu, actual_eps=0.3)]),  # no stamp: the receipt orders
    ])
    snap = s.as_of(at("08:42:00"))
    assert set(snap.records) == {"e1", "e2", "e3"} and not snap.refused
    assert snap.placed_only == {"e1", "e2", "e3"}  # er_surp needs EPS and a usable stamp
    assert snap.records["e3"]["actual_eps"] is None  # never filled from the older version
    assert s.as_of(at("08:46:00")).records["e1"]["actual_eps"] == 0.3
    code = full.live_context(inputs.market_day(DAY)).code
    import pandas as pd

    folder = tmp_path / "l1bz"
    rv.write_l1bz(snap, rv.EARNINGS, folder, "2025-08-01", DAY)
    t = pd.DataFrame({"sym": ["IG", "NK", "QQ"], "m": [281, 281, 281]})  # mark 08:42
    code.bz.Earnings(str(folder), code.labels.calendar(), DAY, code.settings).add(t)
    assert list(t.er_since) == [0, 0, 0] and t.er_surp.isna().all()


def test_a_replay_recovers_the_record_originally_seen(tmp_path):
    first = report("e1", lu=utc("08:31:00"), actual=0.25, note={"b": 1, "a": [2]})
    s = store_with(tmp_path, [("08:31:10", [first]), ("08:31:20", [dict(first)]),
                              ("08:45:05", [report("e1", lu=utc("08:45:00"))])])
    assert len(s.by_id["e1"]) == 2  # an unchanged record is not stored again
    again = rv.RevisionStore(tmp_path / "store", rv.EARNINGS)  # a fresh process reads the files
    assert again.as_of(at("08:40:00")).records["e1"] == first
    assert list(again.as_of(at("08:40:00")).records["e1"]) == list(first)  # key order kept
    assert again.as_of(at("09:00:00")).records["e1"]["actual_eps"] == 0.3
    lines = (tmp_path / "store" / "earnings" / f"{DAY}.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["record"] == first


def test_a_torn_line_refuses_the_store(tmp_path):
    s = store_with(tmp_path, [("08:31:10", [report("e1", lu=utc("08:31:00"))])])
    path = s.folder / f"{DAY}.jsonl"
    path.write_text(path.read_text() + '{"kind": "earn')
    with pytest.raises(ModelRefused) as exc:
        rv.RevisionStore(tmp_path / "store", rv.EARNINGS)
    assert exc.value.cause == "revision_store_unreadable"


def test_rows_without_an_id_are_counted_not_stored(tmp_path):
    s = rv.RevisionStore(tmp_path / "store", rv.NEWS)
    got = s.append([{"published": utc("08:00:00")}, "x", {"benzinga_id": ""}], at("08:01:00"))
    assert got == {"saved": 0, "unchanged": 0, "repeated": 0, "unreadable": 3} and not s.by_id


def test_vendor_stamps_are_read_as_the_training_reads_them():
    bz = full.live_context(inputs.market_day(DAY)).code.bz
    for text in ("2025-08-18T12:40:00Z", "2025-08-18T12:40:00.000000001Z",
                 "2025-08-18T12:39:59.999Z", "2025-08-18T08:40:00-04:00",
                 "2025-08-18T08:40:00.5-0400", "2024-12-31T23:59:59.1Z"):
        assert rv.vendor_s(text) == bz.utc_s(text), text
    for bad in ("2025-08-18T12:40:00", "2025-02-30T10:00:00Z", None, 20250818):
        assert rv.vendor_s(bad) is None
    assert rv.report_s({"date": DAY, "time": "08:30:00"}) == bz.ny_s(DAY, 8, 30, 0)


def test_the_snapshot_files_are_read_by_the_pinned_l1bz(tmp_path):
    assert full.full_digests()["l1bz.py"] == full.FULL_PINS["l1bz.py"]
    code = full.live_context(inputs.market_day(DAY)).code
    bz, cal, settings = code.bz, code.labels.calendar(), code.settings
    er = rv.RevisionStore(tmp_path / "store", rv.EARNINGS)
    old = report("e0", lu="2025-05-16T12:31:00Z", date="2025-05-16", actual=0.10,
                 estimate=0.004)
    er.append([old], at("08:00:00", "2025-05-16"))
    er.append([report("e1", lu=utc("08:31:00"), actual=-0.03, estimate=-0.02)], at("08:31:30"))
    er.append([report("e1", lu=utc("08:44:00"), actual=-0.05, estimate=-0.02)], at("08:44:30"))
    er.append([report("e2", lu=utc("06:00:00"), date="2025-08-19")], at("06:00:01"))  # future
    news = rv.RevisionStore(tmp_path / "store", rv.NEWS)
    art = {"benzinga_id": "a1", "published": utc("08:30:00"), "tickers": ["IG"],
           "channels": ["Earnings"], "title": "kept as received", "last_updated": utc("08:30:00")}
    news.append([art], at("08:30:20"))
    news.append([dict(art, channels=["Earnings", "Movers"], last_updated=utc("08:50:00"))],
                at("08:50:05"))
    import pandas as pd

    def features_at(cutoff: str, minute: int) -> pd.DataFrame:
        """The bz columns of IG's row whose mark is `cutoff`, from the snapshot at it."""
        folder = tmp_path / cutoff.replace(":", "")
        rv.write_l1bz(er.as_of(at(cutoff)), rv.EARNINGS, folder, "2025-05-01", DAY)
        rv.write_l1bz(news.as_of(at(cutoff)), rv.NEWS, folder, "2025-07-01", DAY)
        t = pd.DataFrame({"sym": ["IG"], "m": [minute]})
        bz.Earnings(str(folder), cal, DAY, settings).add(t)
        bz.News(str(folder), cal, DAY, settings).add(t)
        return t.iloc[0]

    floor = settings["er_surp_floor"]
    a = features_at("08:40:00", 279)  # the mark of row minute 279 (08:39)
    assert a.er_since == 0 and a.er_surp == pytest.approx((-0.03 + 0.02) / max(0.02, floor))
    assert a.bz_n60 == 1 and a.bz_c_earn == 1 and a.bz_c_mov == 0
    b = features_at("08:45:00", 284)
    assert b.er_surp == pytest.approx((-0.05 + 0.02) / max(0.02, floor))
    assert b.bz_c_mov == 0  # the 08:50 revision of the article was not received by 08:45
    c = features_at("08:51:00", 290)
    assert c.bz_c_mov == 1 and c.bz_n60 == 1
    snap = er.as_of(at("08:45:00"))
    assert snap.refused == {"e2": "report_after_cutoff"}
    path = rv.write_l1bz(snap, rv.EARNINGS, tmp_path / "one", "2025-05-01", DAY)
    with gzip.open(path, "rt") as f:
        assert [json.loads(x)["benzinga_id"] for x in f] == ["e0", "e1"]
    with pytest.raises(ModelRefused) as exc:
        rv.write_l1bz(snap, rv.EARNINGS, tmp_path / "one", "2025-05-01", "2025-08-19")
    assert exc.value.cause == "revision_file_range"


def test_the_report_gives_delays_and_first_asof_final_differences(tmp_path):
    er = rv.RevisionStore(tmp_path / "store", rv.EARNINGS)
    er.append([report("e0", lu=utc("07:00:00"), tk="OLD")], at("07:00:00"), baseline=True)
    er.append([report("e1", lu=utc("06:00:00"), actual=None)], at("06:00:30"),
              previous_poll=at("06:00:10"))
    er.append([report("e1", lu=utc("08:31:00"), actual=0.25)], at("08:31:20"),
              previous_poll=at("08:31:05"))
    er.append([report("e1", lu=utc("08:40:00"), actual=0.3)], at("08:40:30"),
              previous_poll=at("08:40:15"))
    logs = sorted((tmp_path / "store").rglob("*.jsonl"))
    rep = rv.report(logs, [], [0.5], 60)
    e = rep["earnings"]
    assert e["ids"] == 2 and e["versions"] == 4 and e["revised_ids"] == 1
    assert e["delay_values"]["receipt - last_updated"] == [20.0, 30.0, 30.0]
    assert e["delay_values"]["receipt - last_updated (poll before)"] == [5.0, 10.0, 15.0]
    assert e["delay_values"]["receipt - report time (first with actual and estimate)"] == [80.0]
    assert e["delays"]["receipt - last_updated"] == {"n": 3, "min": 20.0, "max": 30.0,
                                                     "q0.5": 30.0}
    d = e["differences"]
    # final = last received (08:40 update); the training knows it at 08:40:00, we had it 08:40:30
    assert d["ids"] == 1 and d["baseline_ids"] == 1 and d["as_of_absent"] == 0
    assert d["fields"]["actual_eps"] == {"first_vs_final": 1, "asof_vs_final": 1,
                                         "first_vs_asof": 1}
    assert d["fields"]["ticker"] == {"first_vs_final": 0, "asof_vs_final": 0,
                                     "first_vs_asof": 0}
    archive = tmp_path / "archive"
    archive.mkdir()
    with gzip.open(archive / f"earnings-2025-08-01_{DAY}.jsonl.gz", "wt") as f:
        f.write(json.dumps(report("e1", lu=utc("09:00:00"), actual=0.31)) + "\n")
    d = rv.report(logs, sorted(archive.iterdir()), [0.5], 60)["earnings"]["differences"]
    assert d["ids"] == 1 and d["fields"]["actual_eps"]["asof_vs_final"] == 1
    assert d["fields"]["last_updated"]["asof_vs_final"] == 1


def test_the_report_reads_the_probe_records(tmp_path):
    lines = [
        {"event": "baseline", "source": "benzinga_news", "id": "a0", "received_utc":
         "2025-08-18T12:00:00.000+00:00", "previous_poll_sent_utc": None,
         "published": utc("07:00:00")},
        {"event": "new", "source": "benzinga_news", "id": "a1", "poll_sent_utc": "x",
         "received_utc": "2025-08-18T12:31:30.500+00:00",
         "previous_poll_sent_utc": "2025-08-18T12:31:15.000+00:00",
         "published": utc("08:30:00"), "tickers": ["IG"]},
        {"event": "new", "source": "massive_news", "id": "m1", "received_utc":
         "2025-08-18T12:31:30.500+00:00", "published_utc": utc("08:30:00")},
        {"event": "poll", "source": "benzinga_news", "rows": 2},
    ]
    log = tmp_path / f"{DAY}.jsonl"
    log.write_text("".join(json.dumps(x) + "\n" for x in lines))
    n = rv.report([log], [], [0.5], 60)["news"]
    assert n["ids"] == 2
    assert n["delay_values"]["receipt - published (first receipt)"] == [90.5]
    assert n["first_receipt_after_bz_lag_s"] == 1
    assert n["differences"]["as_of_absent"] == 1  # known to the training at 08:31, here 08:31:30


def test_the_report_command_prints_and_writes(tmp_path, capsys):
    s = store_with(tmp_path, [("08:31:10", [report("e1", lu=utc("08:31:00"))])])
    out = tmp_path / "rep.json"
    assert rv.main(["report", *map(str, sorted(s.folder.glob("*.jsonl"))),
                    "--config", "config/bot.toml", "--out", str(out)]) == 0
    assert "[earnings] ids 1" in capsys.readouterr().out
    assert json.loads(out.read_text())["bz_lag_s"] == 60
