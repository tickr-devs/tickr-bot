"""Replay parity harness: on a synthetic day whose reference table the training's own code built
from the same inputs, the replay reports zero difference (marker variant) and only the
last-quote difference without the marker; a perturbed input gives a located difference; sealed
days are refused before any file is read."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys

import model_bars as bars_maker
import model_inputs as inputs
import model_shared
import model_ticks as tick_maker
import numpy as np
import pandas as pd
import pytest

from tickr import config
from tickr.model import headroom, parity
from tickr.model.errors import ModelRefused
from tickr.model.features import ticks

DAY = "2024-03-06"  # a day of the open archive with 20 calendar sessions before it
SYMBOLS = bars_maker.TICKERS
NEWS_MINUTES = [320, 338, 339, 340, 352, 360, 371, 384, 386, 400]
TICK_SYMBOLS, TRADES, QUOTES = 8, 400, 700  # a small synthetic tick day: events per symbol
QUIET = "T001"  # no quote update 11:55..12:01 New York: its 11:54 update is the latest at 12:00
NO_BARS = "ZZZZ"  # ticks but no bars: never a table row, absent from the replay's events
MINUTES = ["04:30", "09:45", "12:00", "15:30"]  # minutes with table rows on this day
TICK_GROUPS = {"tk", "tk2"}
EARLY = "T002"  # has a trade and a quote before 04:00
HALTED = "T004"  # stops quoting in the late regular session (its final quote has a row)
COVERED = {"T001", EARLY, HALTED}  # the tickers of the synthetic audit copy


@pytest.fixture(autouse=True)
def one_thread(monkeypatch):
    """One converter thread on both sides: the comparison is then bit-exact. The run's disk
    and memory readers say there is room (the machine's own are read in test_headroom.py), so
    these tests do not depend on the machine."""
    monkeypatch.setenv("TICK_THREADS", "1")
    monkeypatch.setattr(headroom, "free_bytes", lambda path: ROOMY)
    monkeypatch.setattr(headroom, "available_memory", lambda: ROOMY)


ROOMY = 10**15  # bytes: far above any reserve


def _room(work, **limits):
    """The harness's guard on `work` with config/bot.toml's [parity], `limits` replaced."""
    base = headroom.Limits.from_config(config.load(parity.DEFAULT_CONFIG).parity)
    return headroom.Headroom(work, dataclasses.replace(base, **limits))


def _tick_names(frame):
    names = {f"S{i:04d}": f"T{i:03d}" for i in range(TICK_SYMBOLS - 3)}
    names[f"S{TICK_SYMBOLS - 3:04d}"] = NO_BARS
    return frame.assign(ticker=frame.ticker.map(lambda t: names.get(t, t)))


def _training_table(root, given, tick_file):
    """The training's way: its history and whole-day table from the bar folder (wm2_l1f.day2 on
    files via l1labels.bar_load), its whole-day tick file, l1feat.assemble and l1feat.write."""
    code = inputs.research()
    labels, w = code.labels, code.w
    need = labels.needed(labels.calendar(), labels.bar_names(str(given.bars))[0], DAY)
    body = (given.reference / "splits.jsonl").read_bytes()
    sp = labels.Splits(labels.splits_parse(body), str(given.reference / "splits.jsonl"),
                       labels.md5b(body))
    saved = w.load
    w.load = labels.bar_load
    try:
        (prev, mprev, hist), _ = labels.history(str(given.bars), None, need, w.day2, sp)
        t = w.day2(str(given.bars / f"{DAY}.csv.gz"), prev, None, hist, mprev, want=True)[0]
    finally:
        w.load = saved
    t = t.reset_index(drop=True)
    pc = np.array([prev.get(s, np.nan) for s in t.sym.values], np.float64)
    work = root / "work"
    work.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        o, rec = code.feat.assemble(t, pc, DAY, str(tick_file), str(given.reference),
                                    str(given.benzinga), str(given.fl_facts), code.settings,
                                    str(work), w, code.ref, code.tk)
        code.feat.write(o, rec, str(root / "table"), DAY)
    return root / "table" / f"{DAY}.parquet"


@pytest.fixture(scope="module")
def day(tmp_path_factory):
    return model_shared.once(tmp_path_factory, "parity-day", _day)


def _day(root):
    labels = inputs.research().labels
    cal = labels.calendar()
    i = cal.index(DAY)
    folder = root / "bars"
    folder.mkdir()
    closes: dict[str, float] = {}
    for k, d in enumerate(cal[i - labels.HIST:i + 1]):
        frame = bars_maker.day_bars(d, k, closes, d == DAY)
        frame.to_csv(folder / f"{d}.csv.gz", index=False, compression="gzip")
    trades, quotes = tick_maker.day_events(DAY, TICK_SYMBOLS, TRADES, QUOTES)
    trades, quotes = _tick_names(trades), _tick_names(quotes)
    gap = ((quotes.ticker == QUIET)
           & (quotes.sip_timestamp >= tick_maker.stamp(DAY, "11:55:00"))
           & (quotes.sip_timestamp < tick_maker.stamp(DAY, "12:01:00")))
    quotes = quotes[~gap].reset_index(drop=True)
    # a symbol with events before 04:00 (the day file spans midnight: pre-04:00 carry)
    early = stamp_of("03:59:30")
    top = int(max(trades.sequence_number.max(), quotes.sequence_number.max()))
    pre_t = trades[trades.ticker == EARLY].head(1).assign(sip_timestamp=early,
                                                          sequence_number=top + 1)
    pre_q = quotes[quotes.ticker == EARLY].head(1).assign(sip_timestamp=early - 10**9,
                                                          sequence_number=top + 2)
    trades = pd.concat([pre_t, trades], ignore_index=True)
    quotes = pd.concat([pre_q, quotes], ignore_index=True)
    # a symbol that stops quoting and trading after a minute with a bar in 15:31 .. 15:45
    # (halted): its final quote of the day lies in a minute with a row
    q4 = quotes[quotes.ticker == HALTED].sip_timestamp
    halt = next(m for m in range(table_minute("15:31"), table_minute("15:46"))
                if (frame[frame.ticker == HALTED].window_start == start_of(m)).any()
                and ((q4 >= start_of(m)) & (q4 < start_of(m + 1))).any())
    stop = start_of(halt + 1)
    quotes = quotes[(quotes.ticker != HALTED) | (quotes.sip_timestamp < stop)]
    trades = trades[(trades.ticker != HALTED) | (trades.sip_timestamp < stop)]
    quotes, trades = quotes.reset_index(drop=True), trades.reset_index(drop=True)
    tf, qf = root / "trades.csv.gz", root / "quotes.csv.gz"
    ticks.write_flat_file(trades, tf, tick_maker.TRADE_FILE_COLUMNS)
    ticks.write_flat_file(quotes, qf, tick_maker.QUOTE_FILE_COLUMNS)
    whole = root / "tk-whole.parquet"  # the training's run: the converter's own local mode
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TICK_THREADS", "1")
        subprocess.run([sys.executable, str(ticks.PINNED_DIR / ticks.CONVERTER_FILE), "local",
                        str(tf), str(qf), str(whole)], check=True, capture_output=True)
    given = parity.ReplayInputs(
        bars=folder, types=None, trades=tf, quotes=qf,
        reference=inputs.reference(root / "ref", DAY, SYMBOLS, NEWS_MINUTES),
        benzinga=inputs.benzinga(root / "bz", DAY, SYMBOLS, NEWS_MINUTES, "2023-01-02"),
        fl_facts=inputs.fl_facts(root / "fl.csv", DAY, SYMBOLS), table=root / "none")
    table = _training_table(root, given, whole)
    given = parity.ReplayInputs(**{**given.__dict__, "table": table})
    return given, trades, quotes, whole, halt


def stamp_of(hms):
    return tick_maker.stamp(DAY, hms)


def table_minute(hm):
    return parity.table_minutes([hm])[0]


def start_of(minute):
    return parity._ns_end(DAY, minute - 1)


def _args(given, out, *extra):
    return ["--day", DAY, "--minutes", *MINUTES, "--bars", str(given.bars), "--types", "-",
            "--trades", str(given.trades), "--quotes", str(given.quotes), "--reference",
            str(given.reference), "--benzinga", str(given.benzinga), "--fl",
            str(given.fl_facts), "--table", str(given.table), "--out", str(out), *extra]


def test_same_inputs_give_zero_difference_with_the_marker(day, tmp_path, capsys):
    given = day[0]
    assert parity.main(_args(given, tmp_path, "--mode", "both", "--rtol", "1e-6")) == 0
    report = json.loads((tmp_path / f"parity-{DAY}.json").read_text())
    assert (tmp_path / f"parity-{DAY}.txt").read_text() == capsys.readouterr().out
    assert report["minutes"] == MINUTES and set(report["variants"]) == {"plain", "marker"}
    for hm, r in report["variants"]["marker"].items():
        assert r["rows_compared"] == r["rows_reference"] == r["rows_replay"] > 0, hm
        assert r["columns"] == {} and r["symbols"] == {}, hm
        assert r["bar_groups_exact"] and r["exact"] and r["keys_exact"], hm
    assert report["exceptions"] > 0  # all in the plain variant
    cells = pd.read_csv(tmp_path / f"parity-{DAY}-exceptions.csv")
    assert len(cells) == report["exceptions"] and set(cells.variant) == {"plain"}
    assert set(cells["class"]) <= set(parity.CLASSES)
    assert report["versus_plain"]["marker"]["12:00"]["value"] > 0
    # without the marker: the bars are still exact; only the tick groups differ (the latest
    # quote of each stock weighs 0 until its next update)
    differ = set()
    for hm, r in report["variants"]["plain"].items():
        assert r["bar_groups_exact"] and not r["only_in_reference"], hm
        assert {c["group"] for c in r["columns"].values()} <= TICK_GROUPS, hm
        differ |= set(r["columns"])
    assert differ
    assert report["variants"]["plain"]["12:00"]["symbols"].get(QUIET)
    assert parity.main(_args(given, tmp_path)) == 2  # never written over


def test_a_perturbed_trade_gives_a_located_difference(day, tmp_path):
    given, trades = day[0], day[1]
    sym = "T003"
    lo, hi = tick_maker.stamp(DAY, "09:59:00"), tick_maker.stamp(DAY, "10:00:00")
    hit = ((trades.ticker == sym) & (trades.sip_timestamp >= lo) & (trades.sip_timestamp < hi)
           & (trades.conditions == "") & (trades.correction == 0))
    assert hit.any()
    changed = trades.copy()
    changed.loc[hit, "price"] = (changed.loc[hit, "price"] * 1.05).round(4)
    tf = tmp_path / "trades.csv.gz"
    ticks.write_flat_file(changed, tf, tick_maker.TRADE_FILE_COLUMNS)
    work = tmp_path / "work"
    work.mkdir()
    report = parity.replay(DAY, ["10:00"], parity.ReplayInputs(**{**given.__dict__,
                                                                 "trades": tf}),
                           variants=("marker",), work=work)
    r = report["variants"]["marker"]["10:00"]
    assert r["bar_groups_exact"] and r["columns"]
    assert {c["group"] for c in r["columns"].values()} <= TICK_GROUPS
    assert max(r["symbols"], key=r["symbols"].get) == sym
    assert all(c["max_abs"] != 0 or c["nan_only_reference"] + c["nan_only_replay"]
               for c in r["columns"].values())


def _whole_day(given):
    t = ticks.tick_rows(given.trades, given.quotes, DAY, 24 * 60 - 1,
                        groups=ticks.default_groups())
    return t[t.sym != NO_BARS]


def test_the_marker_makes_the_last_quote_minute_the_whole_day_value(day, tmp_path, monkeypatch):
    given = day[0]
    whole = _whole_day(given).reset_index(drop=True)
    minute = parity.table_minutes(["12:00"])[0]
    through = minute + 240
    symbols = set(whole.sym)
    parity.extract_events(given, symbols, parity._ns_end(DAY, minute), tmp_path)
    room = _room(tmp_path)
    want = whole[whole.m <= through].reset_index(drop=True)
    stale = (want.sym == QUIET) & (want.m == 11 * 60 + 54)
    assert stale.sum() == 1
    marked = parity.minute_ticks(room, DAY, minute, marker=True)
    pd.testing.assert_frame_equal(marked, want, check_exact=True)
    plain = parity.minute_ticks(room, DAY, minute, marker=False)
    cols = ["qspr", "qbs", "qas", "qimb", "qbad", "qbd", "qad"]
    a, b = plain.loc[stale, cols].to_numpy(), want.loc[stale, cols].to_numpy()
    assert not np.array_equal(a, b, equal_nan=True)
    # only the marker's stamp matters: other prices and sizes give the same rows up to m
    monkeypatch.setattr(parity, "MARKER_VALUES", {**parity.MARKER_VALUES, "bid_price": "'0'",
                                                  "ask_price": "'0'", "bid_size": "'0'",
                                                  "ask_size": "'0'", "conditions": "'1'"})
    pd.testing.assert_frame_equal(parity.minute_ticks(room, DAY, minute, marker=True), want,
                                  check_exact=True)


@pytest.mark.parametrize(("when", "cause"), [
    ("2026-07-15", "sealed_day"), ("2026-11-20", "sealed_day"),
    ("2026-08-20", "history_in_sealed_window"), ("2026-09-14", "history_in_sealed_window"),
    ("2024-03-09", "not_a_trading_day"), (DAY, "bar_file_missing")])
def test_sealed_and_unbuildable_days_are_refused_before_any_read(tmp_path, when, cause):
    given = parity.ReplayInputs(*(tmp_path / "missing" for _ in range(8)))
    given = parity.ReplayInputs(**{**given.__dict__, "bars": tmp_path})
    with pytest.raises(ModelRefused) as exc:
        parity.replay(when, MINUTES, given, work=tmp_path)
    assert exc.value.cause == "replay_day_refused" and f"REFUSED: {cause}" in exc.value.detail


def test_a_table_of_another_day_and_bad_minutes_are_refused(day, tmp_path):
    given = day[0]
    import duckdb

    other = tmp_path / "other.parquet"
    duckdb.sql(f"COPY (SELECT * REPLACE ('2024-03-05' AS date) FROM read_parquet("
               f"'{given.table}')) TO '{other}' (FORMAT parquet)")
    with pytest.raises(ModelRefused) as exc:
        parity.reference_table(other, DAY)
    assert exc.value.cause == "replay_table_wrong_day"
    for bad in (["20:00"], ["9:31"], ["09:31", "09:31"]):
        with pytest.raises(ModelRefused) as exc:
            parity.table_minutes(bad)
        assert exc.value.cause == "replay_minute_invalid"


def _audit(frame, path, columns):
    """The research bucket's audit copy: the covered tickers, every column as text, a missing
    value as the empty text."""
    part = frame[frame.ticker.isin(COVERED)][columns]
    text = pd.DataFrame({c: [("" if pd.isna(v) else str(v)) for v in part[c]] for c in columns})
    _write(text, path)
    return path


def _write(frame, path):
    import duckdb

    con = duckdb.connect()
    con.register("f", frame)
    con.execute(f"COPY (SELECT * FROM f) TO '{path}' (FORMAT parquet)")
    con.close()


def _layout(day, root):
    """A local copy of the research layout (module docstring of parity) from the fixture."""
    given, trades, quotes, whole, _ = day
    year = DAY[:4]
    for sub in (f"tickraw-audit/{year}", f"tk61/{year}", f"l1/feat/7d0ba403/tables/{year}"):
        (root / sub).mkdir(parents=True)
    (root / "minute_aggs").symlink_to(given.bars)
    (root / f"l1/feat/7d0ba403/tables/{year}/{DAY}.parquet").symlink_to(given.table)
    (root / f"tk61/{year}/{DAY}.parquet").symlink_to(whole)
    _audit(trades, root / f"tickraw-audit/{year}/{DAY}-trades.parquet",
           tick_maker.TRADE_FILE_COLUMNS)
    _audit(quotes, root / f"tickraw-audit/{year}/{DAY}-quotes.parquet",
           tick_maker.QUOTE_FILE_COLUMNS)
    return root


def test_the_open_day_layout_with_the_audit_copy_default_minutes_and_final_quotes(day,
                                                                                  tmp_path):
    """codex-108 points 1, 2 and 9 on a synthetic open day: the research layout, the audit
    copy of three tickers (tick groups compared only for them, tk61 rows for the others), the
    default minutes, the exact threshold with its classes, and the final-quote cases."""
    given, halt = day[0], day[4]
    root = _layout(day, tmp_path / "copy")
    out = tmp_path / "out"
    args = ["--day", DAY, "--folder", str(root), "--types", "-", "--reference",
            str(given.reference), "--benzinga", str(given.benzinga), "--fl",
            str(given.fl_facts), "--out", str(out), "--mode", "both"]
    assert parity.main(args) == 0
    report = json.loads((out / f"parity-{DAY}.json").read_text())
    assert report["tick_form"] == "audit" and report["covered_tickers"] == len(COVERED)
    assert report["tick_rows_from_tk61_symbols"] > 0
    why = report["chosen_because"]
    assert set(MINUTES) <= set(report["minutes"])
    assert f"final quote {HALTED}" in why[parity.hhmm(halt)]
    assert any(f"pre-04:00 carry {EARLY}" in r for r in why.values())
    assert any("regular-session peak" in r for r in why.values())
    marker, plain = report["variants"]["marker"], report["variants"]["plain"]
    for hm, r in marker.items():
        assert r["bar_groups_exact"] and r["keys_exact"], hm
        # uncovered rows: tick groups not compared (a minute may have no row at all)
        assert r["tick_rows_not_compared"] > 0 or r["rows_compared"] == 0, hm
        if parity.table_minutes([hm])[0] < halt:  # before any final quote: exact
            assert r["exact"], hm
            assert report["ticks"]["marker"][hm]["exact"], hm
    # the halted symbol's final quote: weight 0 in the archive, its minute-end weight with the
    # marker; the plain prefix gives it weight 0 as the archive does
    case = {c["sym"]: c for c in report["final_quote"]["marker"]["cases"]
            if c["minute"] == parity.hhmm(halt)}[HALTED]
    assert case["value_changed"] and case["stopped_before_close"]
    flat = {c["sym"]: c for c in report["final_quote"]["plain"]["cases"]
            if c["minute"] == parity.hhmm(halt)}[HALTED]
    assert not flat["value_changed"] and not flat["missing_changed"]
    assert report["final_quote"]["marker"]["summary"]["value_changed"] >= 1
    assert any(not r["exact"] for r in plain.values())
    assert any(c["value"] for c in report["versus_plain"]["marker"].values())


def test_the_tick_table_alone_against_tk61(day, tmp_path):
    root = _layout(day, tmp_path / "copy")
    work = tmp_path / "work"
    work.mkdir()
    found = parity.layout(root, DAY)
    given = parity.ReplayInputs(found["bars"], None, found["trades"], found["quotes"],
                                *(tmp_path / "none" for _ in range(4)), tk61=found["tk61"],
                                tick_form="audit")
    report = parity.replay(DAY, ["09:45", "12:00"], given, variants=("plain", "marker"),
                           work=work, ticks_only=True)
    assert report["variants"] == {}
    for hm in ("09:45", "12:00"):
        assert report["ticks"]["marker"][hm]["exact"], hm
        assert report["ticks"]["marker"][hm]["rows_compared"] > 0
    plain = report["ticks"]["plain"]["12:00"]
    assert set(plain["columns"]) <= {"qspr", "qbs", "qas", "qimb", "qbad", "qbd", "qad"}
    assert plain["columns"] and plain["classes"]["value"] + plain["classes"]["missing"] > 0


def test_audit_values_v11_cannot_cast_are_refused(day, tmp_path):
    root = _layout(day, tmp_path / "copy")
    found = parity.layout(root, DAY)
    import duckdb

    bad = duckdb.sql(f"SELECT * FROM read_parquet('{found['quotes']}')").df()
    bad.loc[bad.index[0], "conditions"] = "[12]"
    path = tmp_path / "bad-quotes.parquet"
    _write(bad, path)
    given = parity.ReplayInputs(found["bars"], None, found["trades"], path,
                                *(tmp_path / "none" for _ in range(4)), tk61=found["tk61"],
                                tick_form="audit")
    with pytest.raises(ModelRefused) as exc:
        parity.check_audit(given)
    assert exc.value.cause == "tick_value_invalid" and "quotes.conditions" in exc.value.detail
    with pytest.raises(ModelRefused) as exc:  # the audit form never runs without tk61
        parity.replay(DAY, ["09:45"], parity.ReplayInputs(**{**given.__dict__, "tk61": None}),
                      work=tmp_path)
    assert exc.value.cause == "replay_tk61_missing"


def test_the_open_day_2026_11_17_passes_the_date_rule(tmp_path):
    labels = inputs.research().labels
    cal = labels.calendar()
    i = cal.index("2026-11-17")
    for d in cal[i - labels.HIST:i + 1]:
        (tmp_path / f"{d}.csv.gz").write_bytes(b"")
    got = parity.day_files("2026-11-17", tmp_path)
    assert got[-1] == "2026-11-17" and len(got) == labels.HIST + 1


# --- disk and memory guard (tickr.model.headroom) -------------------------------------------


def _extracted(day, work, hm):
    given = day[0]
    minute = table_minute(hm)
    parity.extract_events(given, set(_whole_day(given).sym), parity._ns_end(DAY, minute), work)
    return minute


def test_the_prefix_goes_compressed_and_its_folder_is_removed(day, tmp_path, monkeypatch):
    """The converter reads the prefix as zstd csv (bytes of the old uncompressed csv measured
    beside it), in a conversion folder under the work folder that is gone after the minute;
    the rows are the same as from the uncompressed prefix."""
    import duckdb

    minute = _extracted(day, tmp_path, "15:30")
    room = _room(tmp_path)
    seen = []
    real = ticks.tick_rows

    def spy(t, q, *args, **kw):
        seen.append({"inputs": [str(t), str(q)], "bytes": os.path.getsize(t) + os.path.getsize(q),
                     "tmp_dir": kw.get("tmp_dir"), "memory": kw.get("memory")})
        return real(t, q, *args, **kw)

    monkeypatch.setattr(ticks, "tick_rows", spy)
    got = parity.minute_ticks(room, DAY, minute, marker=False)
    assert len(seen) == 1 and all(p.endswith(".csv.zst") for p in seen[0]["inputs"])
    assert seen[0]["tmp_dir"].parent == tmp_path and seen[0]["memory"] == room.duckdb_memory()
    assert not seen[0]["tmp_dir"].exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("conv-")]
    pick = f"CAST(sip_timestamp AS BIGINT) < {parity._ns_end(DAY, minute)}"
    w = ticks.converter()
    plain = 0
    for kind, cols, name in (("trades", w.TCOLS, "t.csv"), ("quotes", w.QCOLS, "q.csv")):
        duckdb.execute(f"COPY (SELECT {', '.join(cols)} FROM read_parquet('{tmp_path}/"
                       f"{kind}.parquet') WHERE {pick}) TO '{tmp_path / name}' (HEADER)")
        plain += (tmp_path / name).stat().st_size
    assert seen[0]["bytes"] * 2 < plain  # 104,823 against 318,360 bytes on this day
    rows, per_row = room.measured["prefix"]
    assert rows > 0 and per_row > 0
    monkeypatch.setattr(ticks, "tick_rows", real)
    pd.testing.assert_frame_equal(got, real(tmp_path / "t.csv", tmp_path / "q.csv", DAY,
                                            minute + 240, groups=ticks.default_groups()),
                                  check_exact=True)


def test_the_guard_refuses_before_writing(day, tmp_path, monkeypatch):
    """With the bytes per row measured on the run, a conversion that would leave less than the
    disk reserve is refused before anything is written; so is one below the memory reserve."""
    minute = _extracted(day, tmp_path, "12:00")
    free = {"bytes": ROOMY}
    memory = {"bytes": ROOMY}
    room = _room(tmp_path, disk_reserve=1000)
    room.free, room.memory = (lambda path: free["bytes"]), (lambda: memory["bytes"])
    parity.minute_ticks(room, DAY, minute - 1, marker=True)
    rows, per_row = room.measured["prefix"]
    before = sorted(p.name for p in tmp_path.iterdir())
    calls = []
    monkeypatch.setattr(ticks, "tick_rows", lambda *a, **k: calls.append(a))
    free["bytes"] = 1000 + rows * per_row  # the next minute has more rows
    with pytest.raises(ModelRefused) as exc:
        parity.minute_ticks(room, DAY, minute, marker=True)
    assert exc.value.cause == "disk_space_insufficient"
    assert "bytes measured" in exc.value.detail and "reserve 1000" in exc.value.detail
    free["bytes"], memory["bytes"] = ROOMY, room.limits.memory_reserve - 1
    with pytest.raises(ModelRefused) as exc:
        parity.minute_ticks(room, DAY, minute, marker=True)
    assert exc.value.cause == "memory_insufficient"
    assert calls == [] and sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_large_first_conversion_is_measured_by_a_probe(day, tmp_path, monkeypatch):
    minute = _extracted(day, tmp_path, "12:00")
    room = _room(tmp_path, probe_rows=10)
    calls = []
    real = ticks.tick_rows

    def spy(*args, **kw):
        calls.append(args)
        return real(*args, **kw)

    monkeypatch.setattr(ticks, "tick_rows", spy)
    got = parity.minute_ticks(room, DAY, minute, marker="marker")
    assert len(calls) == 2  # the probe on the first 10 events of each file, then the minute
    assert room.measured["prefix"][0] > 20
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("conv-")]
    pd.testing.assert_frame_equal(got, parity.minute_ticks(_room(tmp_path), DAY, minute,
                                                           marker="marker"), check_exact=True)


def test_a_refusal_stops_the_run_cleanly_with_a_report(day, tmp_path, monkeypatch, capsys):
    """The first minute is its own measurement; the next one would go below the reserve: the
    run stops there, the report holds the minute done, its resources and the refusal, and
    the work folder is gone."""
    given = day[0]
    monkeypatch.setattr(headroom, "free_bytes", lambda path: headroom.GB + 1)
    work = tmp_path / "scratch"
    out = tmp_path / "out"
    assert parity.main(_args(given, out, "--mode", "plain", "--disk-reserve-gb", "1",
                             "--work-dir", str(work))) == 3
    report = json.loads((out / f"parity-{DAY}.json").read_text())
    assert report["stopped"]["cause"] == "disk_space_insufficient"
    assert report["stopped"]["minute"] == MINUTES[1] and report["minutes_done"] == MINUTES[:1]
    assert list(report["variants"]["plain"]) == MINUTES[:1] and not report["complete"]
    used = report["resources"][MINUTES[0]]
    assert used["peak_rss_bytes"] > 0 and used["free_disk_bytes"] == headroom.GB + 1
    assert report["stopped"]["limits"]["disk_reserve"] == headroom.GB
    assert "STOPPED at 09:45: disk_space_insufficient" in capsys.readouterr().out
    assert work.is_dir() and list(work.iterdir()) == []


def test_flags_come_from_the_declaration_not_from_the_day():
    """codex-109 point 1: a column whose values happen to be 0 or 1 is not a flag; a declared
    flag is one whatever its values."""
    kinds = parity.column_kinds(["ret1", "inl", "fl_has", "kr_spr5", "sess"])
    assert kinds == {"ret1": "value", "inl": "flag", "fl_has": "flag", "kr_spr5": "rank",
                     "sess": "categorical"}
