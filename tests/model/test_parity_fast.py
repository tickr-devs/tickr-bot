"""The replay parity harness's incremental engine (`--engine incremental`): one walk over the
day gives the full-prefix engine's tick tables and features exactly, for every variant; the
results journal resumes after a stop and rebuilds the report; a difference between the engines
is reported as its own class. Synthetic days only (test_parity.py's day)."""
# ruff: noqa: F811 - the tests take test_parity.py's fixture `day`, imported above

from __future__ import annotations

import json

import model_shared
import numpy as np
import pandas as pd
import pytest
from test_parity import DAY, MINUTES, ROOMY, _args, _room, day  # noqa: F401 - fixture `day`

from tickr import config
from tickr.model import headroom, parity, parity_walk
from tickr.model.errors import ModelRefused
from tickr.model.features import full, ticks

# Several minutes: early (pre-04:00 carry), the open minute by minute, midday (a stale latest
# quote), late; spans of many minutes and of one.
WALK_MINUTES = ["04:00", "04:30", "09:30", "09:31", "09:45", "11:59", "12:00", "15:59", "19:59"]


@pytest.fixture(autouse=True)
def one_thread(monkeypatch):
    monkeypatch.setenv("TICK_THREADS", "1")
    monkeypatch.setattr(headroom, "free_bytes", lambda path: ROOMY)
    monkeypatch.setattr(headroom, "available_memory", lambda: ROOMY)


def test_the_walk_gives_the_full_prefix_tick_table_of_every_variant(day, tmp_path):
    given, trades, quotes = day[0], day[1], day[2]
    symbols = set(trades.ticker) | set(quotes.ticker)
    minutes = parity.table_minutes(WALK_MINUTES)
    parity.extract_events(given, symbols, parity._ns_end(DAY, minutes[-1]), tmp_path)
    room = _room(tmp_path)
    walk = parity_walk.TickWalk(room, DAY, plain=True, groups=ticks.default_groups())
    try:
        for m in minutes:
            through = m + 240
            walk.advance(through)
            for variant in parity.VARIANTS:
                want = parity.minute_ticks(room, DAY, m, variant != "plain" and variant)
                got = walk.table(variant, through)
                pd.testing.assert_frame_equal(got, want, check_exact=True,
                                              obj=f"{variant} {parity.hhmm(m)}")
    finally:
        walk.close()
    assert walk.calls > len(minutes)
    assert {"minute", "span", "prefix"} <= set(room.measured)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("conv-")]
    with pytest.raises(ModelRefused) as exc:  # a table only where the walk stands
        walk.table("plain", minutes[0] + 240)
    assert exc.value.cause == "replay_engine_order"


# The engine comparisons run on three of the base minutes: 09:45 (the open's first lists) is
# left out for time, the walk test checks its tick tables of every variant and 09:30 / 09:31
ENGINE_MINUTES = ["04:30", "12:00", "15:30"]


def _run(given, out, *extra, minutes=MINUTES):
    args = _args(given, out, "--mode", "all", *extra)
    k = args.index("--minutes")
    args[k + 1:k + 1 + len(MINUTES)] = minutes
    assert parity.main(args) == 0
    return json.loads((out / f"parity-{DAY}.json").read_text())


def _results(report):
    """What a run found, without its clocks or engine settings."""
    drop = {"seconds", "engine", "verify_full_prefix", "engine_check", "resources"}
    return {k: v for k, v in report.items() if k not in drop}


@pytest.fixture(scope="module")
def incremental(day, tmp_path_factory):
    """One incremental run of the base minutes, read (never changed) by the resume and guard
    tests: its folder and report."""
    def build(out):
        with pytest.MonkeyPatch.context() as mp:  # the autouse one_thread is function scoped
            mp.setenv("TICK_THREADS", "1")
            mp.setattr(headroom, "free_bytes", lambda path: ROOMY)
            mp.setattr(headroom, "available_memory", lambda: ROOMY)
            return out, _run(day[0], out, "--engine", "incremental")

    return model_shared.once(tmp_path_factory, "parity-incremental", build)


def test_the_incremental_engine_equals_full_prefix_on_all_191_columns(day, tmp_path):
    given = day[0]
    ref = _run(given, tmp_path / "ref", minutes=ENGINE_MINUTES)
    got = _run(given, tmp_path / "inc", "--engine", "incremental", "--verify-full-prefix",
               str(len(ENGINE_MINUTES)), "--workers", "2", minutes=ENGINE_MINUTES)
    check = got["engine_check"]
    assert check["minutes"] == ENGINE_MINUTES and check["differing_minutes"] == []
    for hm in ENGINE_MINUTES:
        e = check["per_minute"][hm]
        assert e["bars"]["exact"] and e["bars"]["rows_incremental"] > 0, hm
        assert set(e["variants"]) == set(parity.VARIANTS), hm
        assert all(v["ticks"]["exact"] and "features" not in v for v in e["variants"].values())
    # the same cells against the archive in both engines, value by value: with the marker every
    # minute is exact (all 191 columns equal the archive), without it the differing cells carry
    # the replay's values, which are then equal too
    assert _results(got) == _results(ref)
    assert all(r["exact"] for r in got["variants"]["marker"].values())
    assert any(not r["exact"] for r in got["variants"]["plain"].values())
    cells = [pd.read_csv(p / f"parity-{DAY}-exceptions.csv") for p in (tmp_path / "ref",
                                                                       tmp_path / "inc")]
    pd.testing.assert_frame_equal(*cells)
    assert len(cells[0]) == got["exceptions"] > 0


def test_resume_skips_finished_minutes_and_rebuilds_the_same_report(day, incremental, tmp_path,
                                                                    monkeypatch):
    given = day[0]
    first, whole = incremental
    lines = (first / f"parity-{DAY}.jsonl").read_text().splitlines(keepends=True)
    assert len(lines) == 1 + len(MINUTES)
    # a stop after two minutes, in the middle of writing the third line, the third minute's
    # cells already written
    out = tmp_path / "stopped"
    out.mkdir()
    (out / f"parity-{DAY}.jsonl").write_text("".join(lines[:3]) + lines[3][:40])
    cells = pd.read_csv(first / f"parity-{DAY}-exceptions.csv")
    cells[cells.minute.isin(MINUTES[:3])].to_csv(out / f"parity-{DAY}-exceptions.csv",
                                                 index=False)
    assert parity.main(_args(given, out, "--mode", "all", "--engine", "incremental")) == 2
    run = []
    real = parity._prepare
    monkeypatch.setattr(parity, "_prepare", lambda *a: run.append(a[1]) or real(*a))
    again = _run(given, out, "--engine", "incremental", "--resume")
    assert [parity.hhmm(m) for m in run] == MINUTES[2:]
    assert _results(again) == _results(whole)
    pd.testing.assert_frame_equal(pd.read_csv(out / f"parity-{DAY}-exceptions.csv"), cells)
    # the report again from the journal alone
    js = (out / f"parity-{DAY}.json").read_text()
    for f in (out / f"parity-{DAY}.json", out / f"parity-{DAY}.txt"):
        f.unlink()
    assert parity.main(["--day", DAY, "--out", str(out), "--rebuild-report"]) == 0
    assert (out / f"parity-{DAY}.json").read_text() == js
    # another run's settings never continue this journal
    for f in (out / f"parity-{DAY}.json", out / f"parity-{DAY}.txt"):
        f.unlink()
    (tmp_path / "w").mkdir()
    with pytest.raises(ModelRefused) as exc:
        parity.replay(DAY, MINUTES, given, variants=("marker",), work=tmp_path / "w",
                      engine="incremental", journal=parity.Journal(out, DAY, resume=True))
    assert exc.value.cause == "replay_resume_mismatch" and "variants_run" in exc.value.detail


def test_a_difference_between_the_engines_is_reported(day, tmp_path, monkeypatch):
    given = day[0]
    target = parity.table_minutes(["12:00"])[0] + 240
    real = parity_walk.TickWalk._convert

    def changed(self, *a, **k):
        rows = real(self, *a, **k)
        hit = (rows.m == target) & rows.qspr.notna()
        if hit.any():  # one cell of one stock-minute: in every table from that minute on
            rows = rows.copy()
            rows.loc[rows.index[hit][0], "qspr"] += 1.0
        return rows

    monkeypatch.setattr(parity_walk.TickWalk, "_convert", changed)
    out = tmp_path / "out"
    report = _run(given, out, "--engine", "incremental", "--verify-full-prefix",
                  str(len(ENGINE_MINUTES)), minutes=ENGINE_MINUTES)
    check = report["engine_check"]
    assert check["differing_minutes"] == ["12:00", "15:30"]
    e = check["per_minute"]["12:00"]
    assert e["bars"]["exact"]
    for variant, x in e["variants"].items():
        assert not x["ticks"]["exact"] and x["ticks"]["cells"] == 1, variant
        assert x["ticks"]["columns"] == {"qspr": 1}
        assert "features" in x, variant  # the feature cells it moves, compared too
    assert not e["variants"]["marker"]["features"]["exact"]
    cells = pd.read_csv(out / f"parity-{DAY}-exceptions.csv")
    engine = cells[cells["class"] == "engine"]
    assert set(engine.minute) == {"12:00", "15:30"} and set(engine.table) == {"engine"}
    assert {"engine-ticks", "engine-features"} <= set(engine.group)
    text = (out / f"parity-{DAY}.txt").read_text()
    assert "ENGINE DIFFERENCE at 12:00 15:30" in text


def test_the_walk_stops_at_the_guard_and_a_resume_finishes_the_day(day, incremental, tmp_path,
                                                                   monkeypatch, capsys):
    """The incremental engine's conversions go through the same guard: below the disk reserve
    the run stops with a `stop` line in the journal and a report of the minutes done (exit 3);
    with room again, --resume redoes the stopped minute and gives the whole run's results."""
    given, whole = day[0], incremental[1]
    out = tmp_path / "out"
    real = parity_walk.TickWalk._convert
    stop_at = parity.table_minutes([MINUTES[2]])[0] + 240

    def tight(self, carry, t, q, first, through, marker):
        if through >= stop_at:  # from here on the disk is full but for the reserve
            self.room.free = lambda path: self.room.limits.disk_reserve
        return real(self, carry, t, q, first, through, marker)

    monkeypatch.setattr(parity_walk.TickWalk, "_convert", tight)
    args = _args(given, out, "--mode", "all", "--engine", "incremental")
    assert parity.main(args) == 3
    report = json.loads((out / f"parity-{DAY}.json").read_text())
    assert report["stopped"]["cause"] == "disk_space_insufficient"
    assert report["stopped"]["minute"] == MINUTES[2] and report["minutes_done"] == MINUTES[:2]
    stopped = report["stopped"]
    assert stopped["resources"]["free_disk_bytes"] == stopped["limits"]["disk_reserve"]
    assert all(report["resources"][hm]["peak_rss_bytes"] > 0 for hm in MINUTES[:2])
    assert "STOPPED at 12:00: disk_space_insufficient" in capsys.readouterr().out
    kinds = [json.loads(x)["kind"] for x in (out / f"parity-{DAY}.jsonl").read_text().splitlines()]
    assert kinds == ["header", "minute", "minute", "stop"]
    monkeypatch.setattr(parity_walk.TickWalk, "_convert", real)
    for f in (out / f"parity-{DAY}.json", out / f"parity-{DAY}.txt"):
        f.unlink()
    again = _run(given, out, "--engine", "incremental", "--resume")
    assert again["stopped"] is None and again["complete"]
    assert _results(again) == _results(whole)


def test_an_event_file_out_of_order_is_refused(tmp_path):
    """The walk streams the extracted file in file order (extraction sorted it); a step back
    in (stamp, sequence) refuses instead of converting events out of order."""
    import duckdb

    cols = ticks.trade_columns()
    rows = pd.DataFrame({c: ["1"] * 3 for c in cols})
    rows["ticker"] = "AAA"
    rows["sip_timestamp"] = ["30", "20", "40"]
    rows["sequence_number"] = ["1", "2", "3"]
    path = tmp_path / "trades.parquet"
    con = duckdb.connect()
    con.register("r", rows)
    con.execute(f"COPY (SELECT * FROM r) TO '{path}' (FORMAT parquet)")
    con.close()
    events = parity_walk._Events(path, cols, _room(tmp_path))
    with pytest.raises(ModelRefused) as refused:
        events.take(100)
    assert refused.value.cause == "tick_file_unordered"
    events.close()


def test_no_table_equals_an_empty_table_but_never_a_table_with_rows():
    """An engine without a table (None) has no rows: it equals the other engine's empty table
    exactly (columns and types included), and differs from one with rows."""
    keys = ["sym", "m"]
    empty = pd.DataFrame({"sym": pd.Series([], dtype=object), "m": pd.Series([], dtype="int64"),
                          "x": pd.Series([], dtype="float64")})
    assert parity.exact_diff(None, empty, keys, "bars", [])["exact"]
    assert parity.exact_diff(empty, None, keys, "bars", [])["exact"]
    rows = pd.DataFrame({"sym": ["A"], "m": [1], "x": [1.0]})
    assert not parity.exact_diff(None, rows, keys, "bars", [])["exact"]
    assert not parity.exact_diff(rows, None, keys, "bars", [])["exact"]


def test_a_span_end_marker_equals_closing_every_minute_of_the_span(day, tmp_path):
    """codex-109 point 5: catching up over a span with one marker at its end gives, for every
    minute of the span and the later windows, the rows that closing each minute on its own
    gives. The synthetic day holds silent minutes, a halted symbol and a symbol's final real
    quote, all inside the stretches walked one minute at a time here."""
    given, trades, quotes, halt = day[0], day[1], day[2], day[4]
    symbols = set(trades.ticker) | set(quotes.ticker)
    last = parity.table_minutes(["15:59"])[0]
    # the one-minute closes walk the stretches that hold those cases (the opening, QUIET's silent
    # 11:55 to 12:01 and the halted symbol's final quote, one minute either side); between
    # stretches, and from there to 15:59, the first walk also catches up with one marker, a span
    # the second walk's longer span contains. Each advance runs the pinned converter, so walking
    # the whole session one minute at a time would take minutes
    open_, noon = (parity.table_minutes(s) for s in (["09:30", "09:36"], ["11:54", "12:01"]))
    at = parity.table_minutes(["09:45"])[0]
    stretches = [open_, (at, at), noon, (halt - 1, halt + 1), (last, last)]
    every = [m for lo, hi in stretches for m in range(lo, hi + 1)]
    assert every == sorted(set(every))
    assert set(parity.table_minutes(["11:55", "12:01"])) <= set(every)
    stops = parity.table_minutes(["09:30", "09:45", "11:59", "15:59"])
    assert set(stops) <= set(every)
    one, two = tmp_path / "one", tmp_path / "two"
    for work in (one, two):
        work.mkdir()
        parity.extract_events(given, symbols, parity._ns_end(DAY, last), work)
    walks = [parity_walk.TickWalk(_room(w), DAY, plain=False, groups=ticks.default_groups())
             for w in (one, two)]
    try:
        for m in every:
            walks[0].advance(m + 240)
            if m in stops:
                walks[1].advance(m + 240)
                pd.testing.assert_frame_equal(walks[1].table("marker", m + 240),
                                              walks[0].table("marker", m + 240),
                                              check_exact=True, obj=parity.hhmm(m))
    finally:
        for w in walks:
            w.close()
    assert walks[0].calls > walks[1].calls


# --- codex-110: native precision, rank recompute, engine diagnostics, no outcomes ---------

NATIVE_MINUTES = ["09:45", "12:00"]  # minutes with kr_* exceptions in the plain variant


def _native_run(given, out, *extra):
    args = _args(given, out, "--mode", "plain", *extra)
    k = args.index("--minutes")
    args[k + 1:k + 1 + len(MINUTES)] = NATIVE_MINUTES
    assert parity.main(args) == 0
    report = json.loads((out / f"parity-{DAY}.json").read_text())
    cells = pd.read_csv(out / f"parity-{DAY}-exceptions.csv", keep_default_na=False)
    return report, cells


def _number(text):
    return float(text) if text != "" else None


def test_exceptions_carry_native_values_and_kr_ranks_are_reproduced(day, tmp_path):
    """codex-110 points 1 and 2: every tk / tk2 exception carries our native float64 value
    (whose float32 cast is the delivered value) and, with tk61, the training's native value;
    the kr_* ranks recomputed from native values over the whole cohort leave no original
    mismatch and change no matching rank, and the training's own native ranks are its table."""
    given, whole = day[0], day[3]
    report, cells = _native_run(given, tmp_path / "with", "--tk61", str(whole))
    assert report["reference_native"] == "tk61" and report["native_groups"] == ["tk", "tk2"]
    feats = cells[(cells.table == "features") & cells.group.isin(["tk", "tk2"])]
    assert len(feats) and set(feats["class"]) >= {"rank", "value"}
    for _, c in feats.iterrows():
        ours, theirs = _number(c.replay_native), _number(c.reference_native)
        assert ours is not None and theirs is not None, (c.sym, c.column)
        delivered = _number(c.replay)
        assert np.float32(ours) == np.float32(delivered) or (np.isnan(ours)
                                                             and np.isnan(delivered))
        reference = _number(c.reference)
        assert np.float32(theirs) == np.float32(reference) or (np.isnan(theirs)
                                                                and np.isnan(reference))
        if not (np.isnan(ours) or np.isnan(theirs)):
            assert int(c.native_ulp64) == parity._ulps64([ours], [theirs])[0]
    assert (cells[cells.table == "ticks"].replay_native == "").all()  # v11's own float64
    ranks = report["ranks"]
    kr = {(c.minute, c.column) for _, c in feats.iterrows() if c.column.startswith("kr_")}
    assert kr and {(hm, col) for hm, per in ranks.items() for col in per["plain"]} == kr
    for per in ranks.values():
        for r in per["plain"].values():
            assert r["cohort_equal"] and r["substituted"] and r["training_rank_mismatches"] == 0
            assert r["original_mismatches"] == r["resolved"] > 0
            assert r["remaining"] == [] and r["changed"] == [] and r["hidden_native"] == []
    text = (tmp_path / "with" / f"parity-{DAY}.txt").read_text()
    assert "ranks plain 12:00 kr_spr5 (native k_spr5, whole cohort)" in text

    # without tk61: our native values still, the training's missing, and the ranks say so
    report, cells = _native_run(given, tmp_path / "without")
    feats = cells[(cells.table == "features") & cells.group.isin(["tk", "tk2"])]
    assert (feats.replay_native != "").all() and (feats.reference_native == "").all()
    for per in report["ranks"].values():
        for r in per["plain"].values():
            assert any("--tk61" in x for x in r["missing"]) and "remaining" not in r


def _ranked(values, m=7):
    import numpy as np

    return pd.Series(values).groupby(np.full(len(values), m)).rank(pct=True).to_numpy(
        np.float32)


def test_rank_recompute_reports_remaining_and_changed_ranks():
    """A native difference below float32 moves a rank: substituting the training's native
    value of the one symbol whose float32 differs (C) resolves C, and the hidden native
    difference of A changes ranks the float32 values say are equal; both sets are listed."""
    syms = ["A", "B", "C", "D"]
    theirs = [1.0, 1.0 + 1e-12, 3.0, 4.0]  # the training's native k_spr5
    ours = [1.0 + 2e-12, 1.0 + 1e-12, 5.0, 4.0]  # A differs below float32, C above
    assert np.float32(ours[0]) == np.float32(theirs[0])
    table = _ranked(theirs)
    want = pd.DataFrame({"sym": syms, "m": 7, "kr_spr5": table})
    native = pd.DataFrame({"k_spr5": ours}, index=pd.Index(syms, name="sym"))
    ref_native = pd.DataFrame({"k_spr5": theirs}, index=pd.Index(syms, name="sym"))
    cells = [{"sym": "C", "column": "k_spr5", "class": "value"},
             {"sym": "C", "column": "kr_spr5", "class": "rank"}]
    # (b): only C's rank was delivered wrong; the recompute changes A and B, which matched
    got = pd.DataFrame({"sym": syms, "m": 7, "kr_spr5": [table[0], table[1], 1.0, table[3]]})
    r = parity.rank_recompute(got, native, want, ref_native, cells)["kr_spr5"]
    assert r["substituted"] == ["C"] and r["original_mismatches"] == 1 and r["resolved"] == 1
    assert r["remaining"] == [] and [x["sym"] for x in r["changed"]] == ["A", "B"]
    assert r["hidden_native"] == ["A"] and r["training_rank_mismatches"] == 0
    # (a): our own delivered ranks (A and B swapped as well): they remain after substitution
    got = got.assign(kr_spr5=_ranked(ours))
    r = parity.rank_recompute(got, native, want, ref_native, cells)["kr_spr5"]
    assert [x["sym"] for x in r["remaining"]] == ["A", "B"] and r["changed"] == []
    assert r["remaining"][0] == {"sym": "A", "replay": 0.5, "reference": 0.25,
                                 "recomputed": 0.5}
    # nothing approximated without a native side
    r = parity.rank_recompute(got, native.iloc[:, :0], want, None, cells)["kr_spr5"]
    assert len(r["missing"]) == 2 and "remaining" not in r


WDC = ("1046.190217", "1046.1902169999998")  # the 2026-11-17 engine-check cell (claude-115 R4)


def _tick(sym, minute, value):
    return pd.DataFrame({"sym": [sym], "m": [minute], "tburst": [float(value)]})


def test_the_declared_one_ulp_cell_is_a_listed_diagnostic_never_a_bound():
    """codex-110 point 2: the WDC tburst reproducer (one float64 ULP, 0 float32 ULP) is the
    class `diagnostic`, listed with its name, symbol, row minute, column and ULP; the same
    difference anywhere else, or a larger one there, stays an engine difference."""
    entries = config.load(parity.DEFAULT_CONFIG).parity.engine_diagnostics
    declared = parity.engine_diagnostics(entries, "2026-11-17")
    assert declared == {("WDC", "19:16", "tburst"): ("wdc-tburst", 1)}
    assert parity.engine_diagnostics(entries, DAY) == {}
    row = 19 * 60 + 16
    cells = []
    r = parity.exact_diff(_tick("WDC", row, WDC[0]), _tick("WDC", row, WDC[1]), ["sym", "m"],
                          "engine-ticks", cells, declared)
    assert r["exact"] and r["cells"] == 0
    assert r["diagnostic"] == [{"name": "wdc-tburst", "sym": "WDC", "row_minute": "19:16",
                                "column": "tburst", "ulp64": 1, "incremental": float(WDC[0]),
                                "full_prefix": float(WDC[1])}]
    assert [c["class"] for c in cells] == ["diagnostic"] and cells[0]["ulp"] == 0
    two = float(np.nextafter(np.nextafter(float(WDC[0]), 0), 0))
    for mine, ref in ((_tick("WDC", row, WDC[0]), _tick("WDC", row, two)),
                      (_tick("WDC", row + 1, WDC[0]), _tick("WDC", row + 1, WDC[1])),
                      (_tick("WDCX", row, WDC[0]), _tick("WDCX", row, WDC[1]))):
        cells = []
        r = parity.exact_diff(mine, ref, ["sym", "m"], "engine-ticks", cells, declared)
        assert not r["exact"] and r["diagnostic"] == [] and r["cells"] == 1
        assert [c["class"] for c in cells] == ["engine"]
    for bad in ("wdc 2026-11-17 WDC 19:16 tburst", "w 2026-13-17 WDC 19:16 tburst 1",
                "w 2026-11-17 WDC 19:16 tburst 0", "w 2026-11-17 WDC 7:16 tburst 1"):
        with pytest.raises(ModelRefused) as exc:
            parity.engine_diagnostics([bad], "2026-11-17")
        assert exc.value.cause == "parity_diagnostic_invalid"


def test_zero_row_schema_and_diagnostics_are_their_own_report_classes():
    """codex-110 point 2: no row on either side, with no table on one side or another empty
    schema, is the class zero_row_schema (exact, listed apart); rows still always count. The
    report and its text list both classes apart from the differing minutes."""
    keys = ["sym"]
    empty = pd.DataFrame({"sym": pd.Series([], dtype=object), "x": pd.Series([], dtype="f8")})
    other = empty.astype({"x": "f4"})
    r = parity.exact_diff(None, empty, keys, "engine-bars", [])
    assert r["exact"] and r["zero_row_schema"] == {
        "incremental": "no table", "full_prefix": "empty table", "columns_equal": True,
        "dtypes_equal": True}
    r = parity.exact_diff(empty, other, keys, "engine-bars", [])
    assert r["exact"] and r["zero_row_schema"]["dtypes_equal"] is False
    assert parity.exact_diff(empty, empty, keys, "engine-bars", [])["zero_row_schema"] is None
    rows = pd.DataFrame({"sym": ["A"], "x": [1.0]})
    r = parity.exact_diff(None, rows, keys, "engine-bars", [])
    assert not r["exact"] and r["zero_row_schema"] is None
    row = 19 * 60 + 16
    declared = {("WDC", "19:16", "tburst"): ("wdc-tburst", 1)}
    ticks_part = parity.exact_diff(_tick("WDC", row, WDC[0]), _tick("WDC", row, WDC[1]),
                                   ["sym", "m"], "engine-ticks", [], declared)
    engine = {"bars": parity.exact_diff(None, empty, keys, "engine-bars", []),
              "variants": {"marker": {"ticks": ticks_part, "features": {
                  "exact": True, "classes": {}, "columns": [],
                  "native_cells_delivered_equal": 0, "native_columns": []}}}}
    header = {"kind": "header", "day": "2026-11-17", "minutes": ["19:59"],
              "variants_run": ["marker"], "ticks_only": True}
    record = {"kind": "minute", "minute": "19:59", "m": 959, "engine": engine, "ticks": {},
              "versus_plain": {}, "cells": 1, "seconds": {}}
    report = parity.build_report(header, [record])
    check = report["engine_check"]
    assert check["differing_minutes"] == []
    assert [(z["minute"], z["table"]) for z in check["zero_row_schema"]] == [("19:59", "bars")]
    assert [(d["minute"], d["variant"], d["name"]) for d in check["diagnostic"]] == [
        ("19:59", "marker", "wdc-tburst")]
    # the delivered features moved by the declared cell must still be exact
    engine["variants"]["marker"]["features"]["exact"] = False
    assert parity.build_report(header, [record])["engine_check"]["differing_minutes"] == [
        "19:59"]


def test_the_harness_reads_no_label_return_or_outcome(day, tmp_path):
    """codex-110 point 3: of the training's table only date, sym and the 191 features are
    read (a label column in the file is never read); of the pinned l1labels.py only the day
    rules, the minute parser, the splits and types readers and two constants; parity_walk and
    headroom import no training code."""
    import ast

    import duckdb

    given = day[0]
    labelled = tmp_path / "labelled.parquet"
    duckdb.execute(f"COPY (SELECT *, 0.5 AS h10, 1.0 AS u30, 2.0 AS ret FROM read_parquet("
                   f"'{given.table}')) TO '{labelled}' (FORMAT parquet)")
    want = ["date", "sym", *dict.fromkeys(full.full_features())]
    assert list(parity.reference_table(labelled, DAY).columns) == want
    table = parity.ReferenceTable(labelled, DAY)
    assert list(table.minute(parity.table_minutes(["12:00"])[0]).columns) == want
    source = (parity.Path(parity.__file__)).read_text("utf-8")
    used = {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "labels"}
    used |= {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)
             and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "research"
             and n.attr != "feat"}
    assert used <= {"day_ok", "bar_names", "needed", "calendar", "cut_minute", "splits_parse",
                    "read_types", "md5b", "raw", "TYPES_MD5", "HARD_LAST", "labels"}, used
    feat = {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "feat"}
    assert feat <= {"cut_inputs", "ident"}, feat
    for module in (parity_walk, headroom):
        text = parity.Path(module.__file__).read_text("utf-8")
        assert "research(" not in text and "l1labels" not in text and "l1feat" not in text
