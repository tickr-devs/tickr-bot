"""All 191 features: the pinned feature builder in the live date context, run on one minute's
snapshot, equals the training's whole-day `l1feat.assemble` on the same training-format inputs;
the context is private to one day, inference only, and refuses inputs from after the minute."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import datetime
import gzip
import io
import json
import shutil
import subprocess
import sys

import model_bars as synthetic
import model_inputs as inputs
import numpy as np
import pandas as pd
import pytest

from tickr.model import features
from tickr.model.errors import ModelRefused
from tickr.model.features import full

HISTORY_DAYS = 6  # the table code needs at least 5 earlier days (MINH) for the hist group
DAY = "2024-03-06"  # a day of the open archive: no date rule of the training is touched
LIVE_DAY = "2026-12-02"  # after the research archive's last day (2026-11-19)
OTHER_LIVE_DAY = "2026-11-25"
LIVE_MINUTE = 400  # 10:40 New York
LIVE_FIRST = "2026-08-15"  # a Benzinga file may not span the sealed window SEAL (it refuses)
SYMBOLS = ["SPY", "QQQ"] + [f"T{i:03d}" for i in range(12)]
TICK_MINUTES = range(300, 420)  # table minutes with tick rows (09:00 .. 10:59)
NEWS_MINUTES = [320, 338, 339, 340, 352, 360, 371, 384, 386, 400, 410]
CUTS = [339, 340, 360, 385]  # 09:39 .. 10:25, around the news and with ticks after each
# What an inference-only context must not reach: label simulation and label/return builders,
# comparisons with label files, writers of the training's files and its command-line modes.
NO_LIVE_PATH = {
    "labels": ("core", "run", "derive", "build", "_build", "frame", "load_day", "publish",
               "published", "verify", "report", "select_report", "select1", "rank30",
               "prefix", "keydiff", "calcheck", "coverage", "test", "history", "table",
               "tick_keys", "target_ok", "needed", "load_wm2"),
    "feat": ("keys_equal", "day_", "prefix", "table_of", "write", "refprep", "modules",
             "load_wm2f", "test"),
    "fl": ("facts", "closes", "test"),
    "bz": ("test",),
    "tk2": ("test",),
}


CUTOFF_LAG = datetime.timedelta(seconds=5)  # the receipt cutoff after the minute's mark


def _rows(ctx, bars, minute, state, snap, root, late=None, late_bars=None, cutoff=None):
    """`ctx.minute_rows` on the capture of `snap` and the bars with their receipts (see
    `model_inputs.capture`), cut off CUTOFF_LAG after the minute's mark unless `cutoff`."""
    held = inputs.capture(snap, ctx.day, root, late)
    return ctx.minute_rows(inputs.received_bars(bars, late_bars), minute, state, None, held,
                           cutoff or ctx.mark(minute) + CUTOFF_LAG)


def _inputs(root, day, first):
    root.mkdir()
    return full.FullInputs(
        tick_file=inputs.tick_file(root / "tk.parquet", SYMBOLS, TICK_MINUTES),
        reference=inputs.reference(root / "ref", day, SYMBOLS, NEWS_MINUTES),
        benzinga=inputs.benzinga(root / "bz", day, SYMBOLS, NEWS_MINUTES, first),
        fl_facts=inputs.fl_facts(root / "fl.csv", day, SYMBOLS))


def _day(last):
    days = synthetic.market(HISTORY_DAYS + 1, last=last)
    today, bars = days[-1]
    return today, bars, features.history(days[:-1], {}, today, None)


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    today, bars, state = _day(DAY)
    given = _inputs(tmp_path_factory.mktemp("in") / "x", today, "2023-01-02")
    return today, bars, state, _announce(given, today)


@pytest.fixture(scope="module")
def whole_day(setup, tmp_path_factory):
    """The training's way: research imports, the whole day's table and uncut inputs."""
    today, bars, state, given = setup
    code = inputs.research()
    table = features._day2(features.bars_frame(bars, today), today, state, None, want=True)[0]
    table = table.reset_index(drop=True)
    prev = np.array([state.prev.get(s, np.nan) for s in table.sym.values], np.float64)
    work = tmp_path_factory.mktemp("work")
    with contextlib.redirect_stdout(io.StringIO()):
        out, rec = code.feat.assemble(table, prev, today, str(given.tick_file),
                                      str(given.reference), str(given.benzinga),
                                      str(given.fl_facts), code.settings, str(work), code.w,
                                      code.ref, code.tk)
    return out, rec


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    today, bars, state = _day(LIVE_DAY)
    root = tmp_path_factory.mktemp("live")
    given = _inputs(root / "x", today, LIVE_FIRST)
    snap = inputs.asof(today, LIVE_MINUTE, given, root / "cut")
    return today, synthetic.cut(bars, today, LIVE_MINUTE), state, given, snap


def test_pinned_builder_files_are_the_training_bytes():
    assert full.full_digests() == full.FULL_PINS


def test_the_191_features_in_the_training_order():
    groups = full.full_groups()
    assert {g: len(v) for g, v in groups.items()} == {
        "bars": 41, "bz": 13, "company": 15, "cross": 4, "fl": 4, "hist": 8, "lists": 3,
        "market": 9, "tk": 73, "tk2": 21}
    names = full.full_features()
    assert len(names) == len(set(names)) == 191
    assert names[:3] == ["m", "sess", "c"] and names[41] == "bz_n60" and names[-1] == "k2_limb"
    assert set(features.live_features()) <= set(names)
    assert features.LIVE_GROUPS == ("bars", "lists", "market", "cross", "hist")


def test_the_whole_day_run_has_values_in_every_group(whole_day):
    out, rec = whole_day
    assert rec["feature_count"] == 191
    for name in ("bz_n1d", "er_since", "er_next", "so_log", "nw_3d", "sx_n", "fl_has",
                 "k_tn5", "kr_tn5", "k2_open", "k_bigw5"):
        assert out[name].notna().any(), name


@pytest.mark.parametrize("minute", CUTS)
def test_a_minute_in_the_live_context_equals_the_training_s_whole_day(setup, whole_day, minute,
                                                                     tmp_path):
    today, bars, state, given = setup
    out, _ = whole_day
    ctx = full.live_context(inputs.market_day(today))
    snap = inputs.asof(today, minute, given, tmp_path / "cut")
    got = _rows(ctx, synthetic.cut(bars, today, minute), minute, state, snap, tmp_path / "c")
    cols = ["sym", *full.full_features()]
    want = out.loc[out.m == minute, cols].sort_values("sym").reset_index(drop=True)
    got = got.sort_values("sym").reset_index(drop=True)
    assert len(want) > 0
    assert list(got.columns) == cols
    assert list(got.sym) == list(want.sym)
    for c in cols[1:]:
        assert got[c].dtype == want[c].dtype, c
        a, b = got[c].to_numpy(np.float64), want[c].to_numpy(np.float64)
        assert np.array_equal(a, b, equal_nan=True), c


def test_a_live_day_has_news_values(live, tmp_path):
    today, bars, state, _, snap = live
    got = _rows(full.live_context(inputs.market_day(today)), bars, LIVE_MINUTE, state, snap,
                tmp_path / "c")
    assert len(got) > 0
    assert got["bz_n1d"].notna().all() and got["nw_n"].notna().all()


def test_the_research_import_keeps_the_archive_end_and_the_context_is_private(live):
    code = full.live_context(inputs.market_day(OTHER_LIVE_DAY)).code
    research = inputs.research()
    assert research.labels.HARD_LAST == "2026-11-19"
    assert code.labels.HARD_LAST == OTHER_LIVE_DAY
    assert code.labels.SEAL == research.labels.SEAL == ("2026-07-01", "2026-08-14")
    assert code.labels is not research.labels and code.feat is not research.feat
    for module in (code.feat, code.bz, code.fl):
        assert module.L is code.labels
    assert research.feat.L is research.labels and sys.modules["l1bz"].L is research.labels
    held = {id(m) for m in sys.modules.values()}
    for module in (code.labels, code.feat, code.bz, code.fl, code.tk2, code.ref, code.tk):
        assert id(module) not in held and module.__name__.startswith(full.LIVE_MODULE_PREFIX)
    assert research.labels.sealed("2026-11-20") and not code.labels.sealed(OTHER_LIVE_DAY)
    assert code.labels.sealed("2026-11-30") and code.labels.sealed("2026-07-15")


def test_two_days_in_one_process_do_not_leak():
    first = full.live_context(inputs.market_day(OTHER_LIVE_DAY))
    second = full.live_context(inputs.market_day(LIVE_DAY))
    assert first.day == OTHER_LIVE_DAY and second.day == LIVE_DAY
    assert first.code.labels.HARD_LAST == OTHER_LIVE_DAY
    assert second.code.labels.HARD_LAST == LIVE_DAY
    for name in ("labels", "feat", "bz", "fl", "tk2", "ref", "tk"):
        assert getattr(first.code, name) is not getattr(second.code, name), name
    assert first.code.bz.L is first.code.labels and second.code.bz.L is second.code.labels
    assert first.code.labels.sealed("2026-11-30") and not second.code.labels.sealed("2026-11-30")
    assert full.live_context(inputs.market_day(LIVE_DAY)) is second


def test_a_fresh_process_never_imports_the_builder_under_its_research_names():
    script = (
        "import sys\n"
        "sys.path.insert(0, 'tests/model')\n"
        "import model_inputs\n"
        "from tickr.model.features import full\n"
        f"ctx = full.live_context(model_inputs.market_day('{OTHER_LIVE_DAY}'))\n"
        f"assert ctx.code.labels.HARD_LAST == '{OTHER_LIVE_DAY}'\n"
        "print(sorted(n for n in (*full.BUILDER, *full.TABLE) if n in sys.modules))\n"
    )
    root = inputs.CALENDAR_DIR.parents[1]
    done = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True,
                          text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "[]"


def test_the_date_rule_is_declared_and_checked():
    pinned = (features.PINNED_DIR / "l1labels.py").read_text("utf-8")
    assert pinned.count("HARD_LAST = '2026-11-19'") == 1  # the pinned bytes are untouched
    dated = full.dated_text(pinned, LIVE_DAY)
    changed = [(a, b) for a, b in zip(pinned.splitlines(), dated.splitlines(), strict=True)
               if a != b]
    assert len(changed) == 1
    assert changed[0][1] == changed[0][0].replace("'2026-11-19'", f"'{LIVE_DAY}'")
    assert "SEAL = ('2026-07-01', '2026-08-14')" in changed[0][1]
    for text in (pinned.replace("HARD_LAST = '2026-11-19'", "HARD_LAST = '2026-11-20'"),
                 pinned + "\nHARD_LAST = '2026-11-19'\n",
                 pinned.replace("SEAL = ('2026-07-01', '2026-08-14')",
                                "SEAL = ('2026-07-01', '2026-08-13')")):
        with pytest.raises(ModelRefused) as exc:
            full.dated_text(text, LIVE_DAY)
        assert exc.value.cause == "table_code_replacement"


def test_labels_and_returns_are_unreachable_from_the_context():
    code = full.live_context(inputs.market_day(LIVE_DAY)).code
    for holder, names in NO_LIVE_PATH.items():
        module = getattr(code, holder)
        for name in names:
            with pytest.raises(ModelRefused) as exc:
                getattr(module, name)()
            assert exc.value.cause == "outside_live_context", (holder, name)
            assert exc.value.detail.endswith(f".{name}")
    importer = code.feat.__builtins__["__import__"]
    for name in ("wm2_l1f", "wm2_l1", "wm", "wide", "refshard2b"):
        with pytest.raises(ModelRefused) as exc:
            importer(name)
        assert exc.value.cause == "outside_live_context"
    assert importer("l1labels") is code.labels


def test_only_the_bot_calendar_builds_a_context():
    for day in (LIVE_DAY, datetime.date(2026, 12, 2), datetime.datetime(2026, 12, 2, 10)):
        with pytest.raises(ModelRefused) as exc:
            full.live_context(day)
        assert exc.value.cause == "live_day_not_from_calendar"
    ctx = full.live_context(inputs.market_day(LIVE_DAY))
    with pytest.raises(ModelRefused) as exc:
        full.LiveContext(object(), LIVE_DAY, ctx.code, ctx.end_minute)
    assert exc.value.cause == "live_context_outside_bot"


def test_no_pinned_file_can_reach_the_bot():
    for path in features.PINNED_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                assert all(a.name.partition(".")[0] != "tickr" for a in node.names), path.name
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").partition(".")[0] != "tickr", path.name
        assert "live_context" not in path.read_text("utf-8"), path.name


@pytest.mark.parametrize(("day", "cause"), [("2026-07-15", "sealed_day"),
                                            ("2029-01-02", "unknown_trading_day")])
def test_a_sealed_or_uncovered_day_has_no_context(day, cause):
    with pytest.raises(ModelRefused) as exc:
        full.live_context(inputs.market_day(day))
    assert exc.value.cause == cause


def _gz_rows(path, rows):
    with gzip.open(path, "wt") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


def _append_line(path, row):
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def _after(day, days):
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=days)).isoformat()


def _add_gz_row(path, row):
    rows = [json.loads(x) for x in gzip.open(path, "rt")]
    path.unlink()
    _gz_rows(path, [*rows, row])


def _announce(given, day):
    """What the training keeps at every minute although it is dated after it: an announced
    split, later shares and short-interest records, a later news month, Benzinga files that
    start after the day (a scheduled report, an article), a revision of an earlier report
    updated after the mark, an fl fact known after the day."""
    ref = given.reference
    _append_line(ref / "splits.jsonl", {"execution_date": _after(day, 10), "ticker": SYMBOLS[2],
                                        "split_from": 1, "split_to": 3})
    _append_line(ref / "shares-outstanding-asof-a.jsonl",
                 {"ticker": SYMBOLS[3], "date": _after(day, 5),
                  "share_class_shares_outstanding": 7e6, "list_date": "2010-01-04",
                  "sic_code": "7372"})
    _append_line(ref / "short-interest-a.jsonl", {"settlement_date": _after(day, 5),
                                                  "ticker": SYMBOLS[3], "short_interest": 5e4,
                                                  "days_to_cover": 2.0})
    later = _after(day, 40)
    _append_line(ref / f"news-{later[:7]}.jsonl",
                 {"id": "later", "published_utc": inputs._ny_utc(later, 300),
                  "publisher": {"name": inputs.PUBLISHER}, "tickers": [SYMBOLS[4]]})
    manifest = json.loads((ref / "manifest.json").read_text())
    manifest["files"] = {p.name: inputs._ident(p) for p in ref.iterdir()
                         if p.name != "manifest.json"}
    (ref / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    first, last = _after(day, 1), _after(day, 60)
    _gz_rows(given.benzinga / f"earnings-{first}_{last}.jsonl.gz",
             [{"benzinga_id": "scheduled", "ticker": SYMBOLS[4], "date": _after(day, 20),
               "time": "16:05:00", "last_updated": inputs._ny_utc(day, 100),
               "actual_eps": None, "estimated_eps": 0.2}])
    _gz_rows(given.benzinga / f"news-{first}_{last}.jsonl.gz",
             [{"benzinga_id": "later", "published": inputs._ny_utc(_after(day, 2), 300),
               "tickers": [SYMBOLS[4]], "channels": ["movers"]}])
    _add_gz_row(next(given.benzinga.glob(f"earnings-*_{day}.jsonl.gz")),
                {"benzinga_id": "revised", "ticker": SYMBOLS[5], "date": _after(day, -1),
                 "time": "16:05:00", "last_updated": inputs._ny_utc(day, 900),
                 "actual_eps": 0.4, "estimated_eps": 0.1})
    with open(given.fl_facts, "a") as f:
        f.write(f"{SYMBOLS[1]},2,2023-12-31,{_after(day, 2)},{_after(day, 5)},9e8,0.4,1,10-K\n")
    return given


def _future_cases():
    """(name, change to a copy of the as-of snapshot): each puts back something the training's
    `cut_inputs` removes at the minute."""
    late = inputs._ny_utc(LIVE_DAY, LIVE_MINUTE + 1, 1)

    def ticks(snap, given):
        return full.FullInputs(given.tick_file, snap.reference, snap.benzinga, snap.fl_facts)

    def article(snap, _):
        _add_gz_row(next(snap.benzinga.glob("news-*")),
                    {"benzinga_id": "late", "published": late, "tickers": [SYMBOLS[2]],
                     "channels": []})

    def report(snap, _):  # timed later today but already reported: not a scheduled report
        _add_gz_row(next(snap.benzinga.glob("earnings-*")),
                    {"benzinga_id": "late", "ticker": SYMBOLS[3], "date": LIVE_DAY,
                     "time": "16:30:00", "last_updated": inputs._ny_utc(LIVE_DAY, 300),
                     "actual_eps": 0.3, "estimated_eps": 0.1})

    def later_date(snap, _):  # dated after the day in the file that holds the day
        _add_gz_row(next(snap.benzinga.glob("earnings-*")),
                    {"benzinga_id": "late", "ticker": SYMBOLS[3], "date": _after(LIVE_DAY, 1),
                     "time": "08:00:00", "last_updated": inputs._ny_utc(LIVE_DAY, 300),
                     "actual_eps": None, "estimated_eps": 0.1})

    def ref_news(snap, _):
        _append_line(snap.reference / f"news-{LIVE_DAY[:7]}.jsonl",
                     {"id": "late", "published_utc": late,
                      "publisher": {"name": inputs.PUBLISHER}, "tickers": [SYMBOLS[4]]})

    return [("ticks", ticks), ("article", article), ("reported_before_its_time", report),
            ("report_dated_later", later_date), ("reference_news", ref_news)]


def _in_hand(day):
    """Receipts that put every tick row in hand before the session: a tick row of a later
    minute is then not left out by its receipt and meets the training's prefix rule."""
    return lambda source, _record: inputs.early(day) if source == "tk" else None


def _copy(snap, root):
    shutil.copytree(snap.reference, root / "ref")
    shutil.copytree(snap.benzinga, root / "bz")
    shutil.copy(snap.fl_facts, root / "fl.csv")
    return full.FullInputs(snap.tick_file, root / "ref", root / "bz", root / "fl.csv")


@pytest.mark.parametrize(("name", "change"), _future_cases(), ids=lambda x: x
                         if isinstance(x, str) else "")
def test_what_the_training_would_cut_is_refused(live, tmp_path, name, change):
    today, bars, state, given, snap = live
    ctx = full.live_context(inputs.market_day(today))
    assert len(_rows(ctx, bars, LIVE_MINUTE, state, snap, tmp_path / "c1")) > 0
    mine = _copy(snap, tmp_path)
    mine = change(mine, given) or mine
    with pytest.raises(ModelRefused) as exc:
        _rows(ctx, bars, LIVE_MINUTE, state, mine, tmp_path / "c2", late=_in_hand(today))
    assert exc.value.cause == "future_input", name


def test_what_the_training_keeps_passes_on_a_live_day(live, tmp_path):
    today, bars, state, _, snap = live
    mine = _announce(_copy(snap, tmp_path), today)
    got = _rows(full.live_context(inputs.market_day(today)), bars, LIVE_MINUTE, state, mine,
                tmp_path / "c")
    assert len(got) > 0


def test_the_kept_later_records_are_in_the_parity_day(setup, whole_day):
    _, _, _, given = setup
    out, _ = whole_day
    assert '"scheduled"' in gzip.open(next(given.benzinga.glob("earnings-2024-03-07_*")),
                                      "rt").read()
    assert "2024-03-16" in (given.reference / "splits.jsonl").read_text()
    for minute in CUTS:
        assert out.loc[out.m == minute, "er_next"].notna().any(), minute


def test_the_uncut_day_is_refused_not_cut(live, tmp_path):
    today, bars, state, given, _ = live
    with pytest.raises(ModelRefused) as exc:
        _rows(full.live_context(inputs.market_day(today)), bars, LIVE_MINUTE, state, given,
              tmp_path / "c", late=_in_hand(today))
    assert exc.value.cause == "future_input" and exc.value.detail.startswith("ticks")


def test_the_research_code_on_a_live_day_is_still_the_training_s_sealed_day(live, tmp_path):
    """The research import is untouched: on a live day it refuses a Benzinga file named past
    2026-11-19, as in training."""
    today, bars, state, _, snap = live
    code = inputs.research()
    table = features._day2(features.bars_frame(bars, today), today, state, None, want=True)[0]
    prev = np.array([state.prev.get(s, np.nan) for s in table.sym.values], np.float64)
    with pytest.raises(SystemExit, match="sealed_date"), \
            contextlib.redirect_stdout(io.StringIO()):
        code.feat.assemble(table, prev, today, str(snap.tick_file), str(snap.reference),
                           str(snap.benzinga), str(snap.fl_facts), code.settings, str(tmp_path),
                           code.w, code.ref, code.tk)


def test_a_benzinga_file_across_the_sealed_window_refuses(tmp_path):
    today, bars, state = _day(LIVE_DAY)
    given = _inputs(tmp_path / "x", today, "2026-08-14")
    snap = inputs.asof(today, LIVE_MINUTE, given, tmp_path / "cut")
    with pytest.raises(ModelRefused) as exc:
        _rows(full.live_context(inputs.market_day(today)), synthetic.cut(bars, today, LIVE_MINUTE),
              LIVE_MINUTE, state, snap, tmp_path / "c")
    assert exc.value.cause == "feature_code_refused" and "sealed_date" in exc.value.detail


def test_bars_after_the_minute_are_refused(setup, tmp_path):
    today, bars, state, given = setup
    with pytest.raises(ModelRefused) as exc:
        _rows(full.live_context(inputs.market_day(today)), bars, 340, state, given,
              tmp_path / "c", cutoff=full.live_context(inputs.market_day(today)).mark(959))
    assert exc.value.cause == "bar_after_minute"


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(full, "_LIVE", {})


def test_a_changed_pinned_byte_is_refused(monkeypatch, tmp_path, fresh):
    copy = tmp_path / "pinned"
    shutil.copytree(features.PINNED_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
    text = (copy / "l1bz.py").read_bytes()
    (copy / "l1bz.py").write_bytes(text.replace(b"bz_n60", b"bz_n61", 1))
    monkeypatch.setattr(features, "PINNED_DIR", copy)
    with pytest.raises(ModelRefused) as exc:
        full.live_context(inputs.market_day(LIVE_DAY))
    assert exc.value.cause == "table_code_md5" and exc.value.detail == "l1bz.py"


def test_a_changed_settings_byte_is_refused(monkeypatch, fresh):
    monkeypatch.setitem(full.FULL_PINS, "l1feat-settings.json", "0" * 32)
    with pytest.raises(ModelRefused) as exc:
        full.live_context(inputs.market_day(LIVE_DAY))
    assert exc.value.cause == "table_code_md5"


# ---- Codex 109 point 3: the table code without outcomes, half days, receipts, scheduled reports


def _same(a, b):
    a = a.sort_values("sym").reset_index(drop=True)
    b = b.sort_values("sym").reset_index(drop=True)
    if list(a.sym) != list(b.sym):
        return False
    return all(np.array_equal(a[c].to_numpy(np.float64), b[c].to_numpy(np.float64),
                              equal_nan=True) for c in a.columns if c != "sym")


def _boom(*_args, **_kwargs):
    raise AssertionError("an outcome function was reached")


def test_the_live_table_evaluates_no_outcome(setup, whole_day, monkeypatch, tmp_path):
    """Every label, barrier, highest-high and other outcome function of the table code and of
    the builder (in the context and in the research import) raises; the 191 features of the
    live context still equal the training's whole day at every test minute."""
    today, bars, state, given = setup
    out, _ = whole_day
    ctx = full.live_context(inputs.market_day(today))
    rigged = set()
    for stem in full.TABLE:
        module = getattr(ctx.code.table, stem)
        for name, obj in list(vars(module).items()):
            if callable(obj) and not name.startswith("__") and name != "day2":
                monkeypatch.setattr(module, name, _boom)
                rigged.add(f"{stem}.{name}")
    for holder, names in NO_LIVE_PATH.items():
        for name in names:
            monkeypatch.setattr(getattr(ctx.code, holder), name, _boom)
    for stem, names in (("wm", ("labels",)), ("wm2_l1f", ("labels", "barriers", "maxhigh"))):
        for name in names:
            monkeypatch.setattr(sys.modules[stem], name, _boom)
    assert {"wm.labels", "wm2_l1f.labels", "wm2_l1f.barriers", "wm2_l1f.maxhigh",
            "wm2_l1f.uniq", "wm2_l1f.derived", "wm2_l1f.volunits", "wide.outcome",
            "wide.scan"} <= rigged
    cols = ["sym", *full.full_features()]
    for minute in CUTS:
        snap = inputs.asof(today, minute, given, tmp_path / f"cut{minute}")
        got = _rows(ctx, synthetic.cut(bars, today, minute), minute, state, snap,
                    tmp_path / f"c{minute}")
        want = out.loc[out.m == minute, cols]
        assert len(want) > 0 and _same(got, want), minute


def test_the_feature_only_table_is_the_declared_replacement():
    pinned = (features.PINNED_DIR / "wm2_l1f.py").read_text("utf-8")
    text = full.feature_only_text(pinned)
    kept = [a for a in pinned.splitlines() if a not in text.splitlines()]
    assert len(kept) == 8  # the label statements; nothing is added but the row mask's line
    assert "g = rq <= rf - 2; ri = ri[g]; rq = rq[g]; rf = rf[g]; s = SS[rq]" in text
    code = full.live_context(inputs.market_day(LIVE_DAY)).code
    used = set(code.w.day2.__code__.co_names)
    assert not used & {"labels", "barriers", "maxhigh", "LAB", "TG2", "RK", "HZ", "BAR"}
    for name in ("labels", "barriers", "maxhigh", "uniq", "days2", "load"):
        with pytest.raises(ModelRefused) as exc:
            getattr(code.w, name)()
        assert exc.value.cause == "outside_live_context"
    with pytest.raises(ModelRefused) as exc:
        full.feature_only_text(pinned.replace("g = rq <= rf - 2", "g = rq <= rf - 3"))
    assert exc.value.cause == "table_code_replacement"


HALF_DAY = "2026-11-27"  # the bot calendar's early close (13:00 New York)


def test_a_half_day_follows_the_bundle_s_session_policy(tmp_path):
    day = inputs.market_day(HALF_DAY)
    assert day.half_day and day.regular_close.hour == 13
    with pytest.raises(ModelRefused) as exc:
        full.live_context(day)
    assert exc.value.cause == "half_day_outside_session_policy"
    ctx = full.live_context(day, full.HALF_DAYS_UNTIL_CLOSE)
    assert ctx.end_minute == 540  # 13:00 New York, from the calendar
    today, bars, state = _day(HALF_DAY)
    given = _inputs(tmp_path / "x", today, LIVE_FIRST)
    snap = inputs.asof(today, LIVE_MINUTE, given, tmp_path / "cut")
    got = _rows(ctx, synthetic.cut(bars, today, LIVE_MINUTE), LIVE_MINUTE, state, snap,
                tmp_path / "c1")
    table = features._day2(features.bars_frame(synthetic.cut(bars, today, LIVE_MINUTE), today),
                           today, state, None, want=True)[0]
    want = table.loc[table.m == LIVE_MINUTE, ["sym", *features.live_features()]]
    assert len(got) > 0 and _same(got[want.columns], want)
    for minute in (540, 700):
        with pytest.raises(ModelRefused) as exc:
            _rows(ctx, synthetic.cut(bars, today, minute), minute, state, snap,
                  tmp_path / f"c{minute}")
        assert exc.value.cause == "after_exchange_close"


def test_a_full_day_with_other_hours_or_an_unknown_policy_is_refused():
    day = inputs.market_day(LIVE_DAY)
    other = dataclasses.replace(day, regular_close=day.regular_close.replace(hour=15))
    with pytest.raises(ModelRefused) as exc:
        full.live_context(other)
    assert exc.value.cause == "session_hours_differ"
    with pytest.raises(ModelRefused) as exc:
        full.live_context(day, "half_days")
    assert exc.value.cause == "session_policy_unknown"
    assert full.live_context(day, full.HALF_DAYS_UNTIL_CLOSE).end_minute == 960


def _probe_cases(day, sym, ticks):
    """(name, add the record to a copy of the snapshot, the record's (source, match)): one
    record per source for `sym`, each with an event time before the minute's mark."""
    def news(snap):
        _add_gz_row(next(snap.benzinga.glob("news-*")),
                    {"benzinga_id": "probe", "published": inputs._ny_utc(day, LIVE_MINUTE - 10),
                     "tickers": [sym], "channels": ["movers"]})

    def report(snap):
        d = _after(day, -2)
        _add_gz_row(next(snap.benzinga.glob("earnings-*")),
                    {"benzinga_id": "probe", "ticker": sym, "date": d, "time": "16:05:00",
                     "last_updated": inputs._ny_utc(d, 900), "actual_eps": 0.5,
                     "estimated_eps": 0.1})

    def ref_news(snap):
        _append_line(snap.reference / f"news-{day[:7]}.jsonl",
                     {"id": "probe", "published_utc": inputs._ny_utc(day, LIVE_MINUTE - 10),
                      "publisher": {"name": inputs.PUBLISHER}, "tickers": [sym]})

    def shares(snap):
        _append_line(snap.reference / "shares-outstanding-asof-a.jsonl",
                     {"ticker": sym, "date": _after(day, -10), "id": "probe",
                      "share_class_shares_outstanding": 5e7, "list_date": "2010-01-04",
                      "sic_code": "7372"})

    def fact(snap):
        with open(snap.fl_facts, "a") as f:
            f.write(f"{sym},99,2024-06-30,{_after(day, -10)},{_after(day, -8)},4e9,0.9,1,10-K\n")

    last_tick = int(ticks.loc[(ticks.sym == sym) & (ticks.m <= LIVE_MINUTE + 240), "m"].max())
    return [
        ("article", news, ("bz", lambda r: r.get("benzinga_id") == "probe")),
        ("report", report, ("bz", lambda r: r.get("benzinga_id") == "probe")),
        ("reference_news", ref_news, ("ref", lambda r: r.get("id") == "probe")),
        ("shares", shares, ("ref", lambda r: r.get("id") == "probe")),
        ("fl_fact", fact, ("fl", lambda r: r.get("cik") == "99")),
        ("tick_row", lambda _snap: None,
         ("tk", lambda r: r["sym"] == sym and r["m"] == last_tick)),
    ]


def test_a_record_received_after_the_cutoff_is_not_used(live, tmp_path):
    """Each source: a record whose event time is before the mark but whose receipt is after the
    cutoff gives the features of the capture without it, and the record matters (received in
    time it changes them)."""
    import duckdb

    today, bars, state, _, snap = live
    ctx = full.live_context(inputs.market_day(today))
    after = ctx.mark(LIVE_MINUTE) + CUTOFF_LAG + datetime.timedelta(seconds=1)
    base_rows = _rows(ctx, bars, LIVE_MINUTE, state, snap, tmp_path / "base")
    sym = next(s for s in SYMBOLS[2:] if s in set(base_rows.sym))
    ticks = duckdb.sql(f"SELECT sym, m FROM read_parquet('{snap.tick_file}')").df()
    for name, add, (source, match) in _probe_cases(today, sym, ticks):
        mine = _copy(snap, tmp_path / name)
        add(mine)

        def hit(src, record, source=source, match=match):
            return src == source and match(record)

        in_time = _rows(ctx, bars, LIVE_MINUTE, state, mine, tmp_path / f"{name}-in")
        late = _rows(ctx, bars, LIVE_MINUTE, state, mine, tmp_path / f"{name}-late",
                     late=lambda src, r, hit=hit: after if hit(src, r) else None)
        held = inputs.capture(mine, today, tmp_path / f"{name}-none", omit=hit)
        absent = ctx.minute_rows(inputs.received_bars(bars), LIVE_MINUTE, state, None, held,
                                 ctx.mark(LIVE_MINUTE) + CUTOFF_LAG)
        assert _same(late, absent), name
        assert not _same(in_time, late), name
    start = int(bars.loc[bars.ticker == sym, "window_start"].max())
    late = _rows(ctx, bars, LIVE_MINUTE, state, snap, tmp_path / "bar-late",
                 late_bars={(sym, start): after})
    absent = _rows(ctx, bars[~((bars.ticker == sym) & (bars.window_start == start))],
                   LIVE_MINUTE, state, snap, tmp_path / "bar-none")
    assert _same(late, absent) and not _same(base_rows, late)


def test_receipts_are_required_and_the_cutoff_is_not_before_the_mark(live, tmp_path):
    today, bars, state, _, snap = live
    ctx = full.live_context(inputs.market_day(today))
    with pytest.raises(ModelRefused) as exc:
        _rows(ctx, bars, LIVE_MINUTE, state, snap, tmp_path / "c1",
              cutoff=ctx.mark(LIVE_MINUTE) - datetime.timedelta(seconds=1))
    assert exc.value.cause == "cutoff_before_mark"
    with pytest.raises(ModelRefused) as exc:
        ctx.minute_rows(bars, LIVE_MINUTE, state, None,
                        inputs.capture(snap, today, tmp_path / "c2"),
                        ctx.mark(LIVE_MINUTE) + CUTOFF_LAG)
    assert (exc.value.cause, exc.value.detail) == ("receipt_missing", "bars")
    held = inputs.capture(snap, today, tmp_path / "c3")
    path = next(held.benzinga.glob("news-*"))
    rows = [json.loads(x) for x in gzip.open(path, "rt")]
    path.unlink()
    _gz_rows(path, [{k: v for k, v in rows[0].items() if k != full.RECEIPT_FIELD}, *rows[1:]])
    with pytest.raises(ModelRefused) as exc:
        ctx.minute_rows(inputs.received_bars(bars), LIVE_MINUTE, state, None, held,
                        ctx.mark(LIVE_MINUTE) + CUTOFF_LAG)
    assert exc.value.cause == "receipt_missing" and exc.value.detail.startswith("benzinga")


SCHEDULED_TIME = "10:50:00"  # eligible from minute 409 (its mark is 10:50:00 New York)


def test_a_report_scheduled_later_today_is_left_out_then_admitted(setup, tmp_path):
    """A report of the day scheduled for later stays in the capture with its true date and
    receipt: the minutes before its time equal the training's whole day without its effect,
    the minutes from its time on admit it, as the training's cut does."""
    today, bars, state, given = setup
    full_day = _copy(given, tmp_path / "given")
    shutil.copy(given.tick_file, tmp_path / "given" / "tk.parquet")
    full_day = full.FullInputs(tmp_path / "given" / "tk.parquet", full_day.reference,
                               full_day.benzinga, full_day.fl_facts)
    code = inputs.research()
    table = features._day2(features.bars_frame(bars, today), today, state, None, want=True)[0]
    table = table.reset_index(drop=True)
    early_rows = table.loc[table.m == 400, "sym"]
    sym = next(s for s in SYMBOLS[2:] if s in set(early_rows)
               and s in set(table.loc[table.m == 415, "sym"]))
    row = {"benzinga_id": "today", "ticker": sym, "date": today, "time": SCHEDULED_TIME,
           "last_updated": inputs._ny_utc(today, 200), "actual_eps": None,
           "estimated_eps": 0.2}
    _add_gz_row(next(full_day.benzinga.glob(f"earnings-*_{today}.jsonl.gz")), row)
    prev = np.array([state.prev.get(s, np.nan) for s in table.sym.values], np.float64)
    with contextlib.redirect_stdout(io.StringIO()):
        out, _ = code.feat.assemble(table, prev, today, str(full_day.tick_file),
                                    str(full_day.reference), str(full_day.benzinga),
                                    str(full_day.fl_facts), code.settings,
                                    str(tmp_path), code.w, code.ref, code.tk)
    ctx = full.live_context(inputs.market_day(today))
    cols = ["sym", *full.full_features()]
    since = {}
    for minute in (400, 408, 409, 415):
        snap = inputs.asof(today, minute, given, tmp_path / f"cut{minute}")
        _add_gz_row(next(snap.benzinga.glob(f"earnings-*_{today}.jsonl.gz")), row)  # captured
        got = _rows(ctx, synthetic.cut(bars, today, minute), minute, state, snap,
                    tmp_path / f"c{minute}")
        assert _same(got, out.loc[out.m == minute, cols]), minute
        since[minute] = float(got.loc[got.sym == sym, "er_since"].iloc[0])
    assert since[400] > 0 and since[408] > 0 and since[409] == since[415] == 0


def test_table_rows_then_assemble_rows_are_minute_rows(live, tmp_path):
    """The two halves a live caller uses (the cohort first, the features after the ticks are
    converted) give exactly `minute_rows`, and each half keeps every bound of the minute."""
    today, bars, state, _, snap = live
    ctx = full.live_context(inputs.market_day(today))
    held = inputs.capture(snap, today, tmp_path / "c")
    rows = inputs.received_bars(bars)
    cutoff = ctx.mark(LIVE_MINUTE) + CUTOFF_LAG
    whole = ctx.minute_rows(rows, LIVE_MINUTE, state, None, held, cutoff)
    t = ctx.table_rows(rows, LIVE_MINUTE, state, None, cutoff)
    assert len(t) > 0 and (t.m == LIVE_MINUTE).all()
    assert sorted(t.sym) == sorted(whole.sym)
    got = ctx.assemble_rows(t, LIVE_MINUTE, state, held, cutoff)
    pd.testing.assert_frame_equal(got, whole, check_exact=True)
    halves = {
        "table_rows": lambda m, c: ctx.table_rows(rows, m, state, None, c),
        "assemble_rows": lambda m, c: ctx.assemble_rows(t, m, state, held, c),
    }
    early = ctx.mark(LIVE_MINUTE) - datetime.timedelta(seconds=1)
    for name, half in halves.items():
        for minute, when, cause in (
                (LIVE_MINUTE, early, "cutoff_before_mark"),
                (LIVE_MINUTE, cutoff.replace(tzinfo=None), "cutoff_not_aware"),
                (ctx.end_minute, cutoff, "after_exchange_close")):
            with pytest.raises(ModelRefused) as exc:
                half(minute, when)
            assert exc.value.cause == cause, name
    later = LIVE_MINUTE + 1
    with pytest.raises(ModelRefused) as exc:
        ctx.assemble_rows(t, later, state, held, ctx.mark(later) + CUTOFF_LAG)
    assert exc.value.cause == "table_rows_of_another_minute"
