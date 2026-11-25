"""All 191 features: the pinned feature builder in the live date context, run on one minute's
snapshot, equals the training's whole-day `l1feat.assemble` on the same training-format inputs;
the context is private to one day, inference only, and refuses inputs from after the minute."""

from __future__ import annotations

import ast
import contextlib
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
import pytest

from tickr.model import features
from tickr.model.errors import ModelRefused
from tickr.model.features import full

HISTORY_DAYS = 6  # the table code needs at least 5 earlier days (MINH) for the hist group
DAY = "2024-03-06"  # a day of the open archive: no date rule of the training is touched
LIVE_DAY = "2026-12-02"  # after the research archive's last day (2026-11-19)
OTHER_LIVE_DAY = "2026-11-27"
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
    return today, bars, state, _inputs(tmp_path_factory.mktemp("in") / "x", today, "2023-01-02")


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
    got = ctx.minute_rows(synthetic.cut(bars, today, minute), minute, state, None, snap)
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


def test_a_live_day_has_news_values(live):
    today, bars, state, _, snap = live
    got = full.live_context(inputs.market_day(today)).minute_rows(bars, LIVE_MINUTE, state,
                                                                  None, snap)
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
        "print(sorted(n for n in full.BUILDER if n in sys.modules))\n"
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
        full.LiveContext(object(), LIVE_DAY, ctx.code)
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
                                            ("2027-01-04", "unknown_trading_day")])
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


def _future_cases():
    """(name, change to a copy of the as-of snapshot that puts something after the mark)."""
    late = inputs._ny_utc(LIVE_DAY, LIVE_MINUTE + 1, 1)

    def ticks(snap, given):
        return full.FullInputs(given.tick_file, snap.reference, snap.benzinga, snap.fl_facts)

    def article(snap, _):
        name = next(snap.benzinga.glob("news-*"))
        rows = [json.loads(x) for x in gzip.open(name, "rt")]
        name.unlink()
        _gz_rows(name, [*rows, {"benzinga_id": "late", "published": late,
                                "tickers": [SYMBOLS[2]], "channels": []}])

    def report(snap, _, field):
        name = next(snap.benzinga.glob("earnings-*"))
        rows = [json.loads(x) for x in gzip.open(name, "rt")]
        row = {"benzinga_id": "late", "ticker": SYMBOLS[3], "date": LIVE_DAY,
               "time": "09:00:00", "last_updated": inputs._ny_utc(LIVE_DAY, 300),
               "actual_eps": 0.1, "estimated_eps": 0.1}
        row[field] = "16:30:00" if field == "time" else late
        name.unlink()
        _gz_rows(name, [*rows, row])

    def bz_name(snap, _):
        _gz_rows(snap.benzinga / "news-2026-12-03_2026-12-04.jsonl.gz", [])

    def ref_news(snap, _):
        _append_line(snap.reference / f"news-{LIVE_DAY[:7]}.jsonl",
                     {"id": "late", "published_utc": late,
                      "publisher": {"name": inputs.PUBLISHER}, "tickers": [SYMBOLS[4]]})

    def ref_month(snap, _):
        (snap.reference / "news-2027-01.jsonl").write_text("")

    def ref_dated(file, row):
        def change(snap, _):
            _append_line(snap.reference / file, row)
        return change

    def fact(snap, _):
        with open(snap.fl_facts, "a") as f:
            f.write(f"{SYMBOLS[5]},9,2026-09-30,2026-12-03,2026-12-04,1e9,0.5,1,10-Q\n")

    after = "2026-12-03"
    return [
        ("ticks", ticks), ("article", article),
        ("report_time", lambda s, g: report(s, g, "time")),
        ("report_revision", lambda s, g: report(s, g, "last_updated")),
        ("benzinga_name", bz_name), ("reference_news", ref_news), ("reference_month", ref_month),
        ("splits", ref_dated("splits.jsonl", {"execution_date": after, "ticker": SYMBOLS[6],
                                              "split_from": 1, "split_to": 4})),
        ("shares", ref_dated("shares-outstanding-asof-a.jsonl",
                             {"ticker": SYMBOLS[7], "date": after,
                              "share_class_shares_outstanding": 5e6})),
        ("short_interest", ref_dated("short-interest-a.jsonl",
                                     {"settlement_date": after, "ticker": SYMBOLS[8],
                                      "short_interest": 1e4, "days_to_cover": 1.0})),
        ("fl_facts", fact),
    ]


@pytest.mark.parametrize(("name", "change"), _future_cases(), ids=lambda x: x
                         if isinstance(x, str) else "")
def test_an_input_from_after_the_minute_is_refused(live, tmp_path, name, change):
    today, bars, state, given, snap = live
    copy = tmp_path / "snap"
    shutil.copytree(snap.reference, copy / "ref")
    shutil.copytree(snap.benzinga, copy / "bz")
    shutil.copy(snap.fl_facts, copy / "fl.csv")
    mine = full.FullInputs(snap.tick_file, copy / "ref", copy / "bz", copy / "fl.csv")
    mine = change(mine, given) or mine
    ctx = full.live_context(inputs.market_day(today))
    assert len(ctx.minute_rows(bars, LIVE_MINUTE, state, None,
                               full.FullInputs(snap.tick_file, snap.reference, snap.benzinga,
                                               snap.fl_facts))) > 0
    with pytest.raises(ModelRefused) as exc:
        ctx.minute_rows(bars, LIVE_MINUTE, state, None, mine)
    assert exc.value.cause == "future_input", name


def test_the_uncut_day_is_refused_not_cut(live):
    today, bars, state, given, _ = live
    with pytest.raises(ModelRefused) as exc:
        full.live_context(inputs.market_day(today)).minute_rows(bars, LIVE_MINUTE, state, None,
                                                                given)
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
        full.live_context(inputs.market_day(today)).minute_rows(
            synthetic.cut(bars, today, LIVE_MINUTE), LIVE_MINUTE, state, None, snap)
    assert exc.value.cause == "feature_code_refused" and "sealed_date" in exc.value.detail


def test_bars_after_the_minute_are_refused(setup):
    today, bars, state, given = setup
    with pytest.raises(ModelRefused) as exc:
        full.live_context(inputs.market_day(today)).minute_rows(bars, 340, state, None, given)
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
