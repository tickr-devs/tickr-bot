"""All 191 features: the pinned feature builder run on one minute with its own prefix rule equals
the training's whole-day `l1feat.assemble` on the same training-format inputs."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
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
LIVE_DAY = "2026-12-02"  # after l1labels.HARD_LAST: the live date rule applies
LIVE_MINUTE = 400  # 10:40 New York
LIVE_FIRST = "2026-08-15"  # a Benzinga file may not span the sealed window SEAL (it refuses)
SYMBOLS = ["SPY", "QQQ"] + [f"T{i:03d}" for i in range(12)]
TICK_MINUTES = range(300, 420)  # table minutes with tick rows (09:00 .. 10:59)
NEWS_MINUTES = [320, 338, 339, 340, 352, 360, 371, 384, 386, 400]
CUTS = [339, 340, 360, 385]  # 09:39 .. 10:25, around the news and with ticks after each


def _inputs(root, day, first):
    root.mkdir()
    return full.FullInputs(
        tick_file=inputs.tick_file(root / "tk.parquet", SYMBOLS, TICK_MINUTES),
        reference=inputs.reference(root / "ref", day, SYMBOLS, NEWS_MINUTES),
        benzinga=inputs.benzinga(root / "bz", day, SYMBOLS, NEWS_MINUTES, first),
        fl_facts=inputs.fl_facts(root / "fl.csv", day, SYMBOLS))


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    days = synthetic.market(HISTORY_DAYS + 1, last=DAY)
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    return today, bars, state, _inputs(tmp_path_factory.mktemp("in") / "x", today, "2023-01-02")


@pytest.fixture(scope="module")
def whole_day(setup, tmp_path_factory):
    """The training's way: the whole day's table and uncut inputs through l1feat.assemble."""
    today, bars, state, given = setup
    code = full.full_code()
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
def test_a_minute_with_cut_inputs_equals_the_whole_day(setup, whole_day, minute):
    today, bars, state, given = setup
    out, _ = whole_day
    got = full.minute_rows_full(synthetic.cut(bars, today, minute), today, minute, state, None,
                                given)
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


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    days = synthetic.market(HISTORY_DAYS + 1, last=LIVE_DAY)
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    given = _inputs(tmp_path_factory.mktemp("live") / "x", today, LIVE_FIRST)
    return today, synthetic.cut(bars, today, LIVE_MINUTE), state, given


def test_the_live_replacement_is_declared_and_checked():
    code = full.full_code()
    assert code.labels.HARD_LAST == "9999-12-31" and code.labels.SEAL == ("2026-07-01",
                                                                         "2026-08-14")
    assert code.labels._TICKR_LIVEPATCH == tuple(n for n, _, _ in full.LIVEPATCH)
    for module in (code.feat, sys.modules["l1bz"], sys.modules["l1fl"]):
        assert module.L is code.labels
    pinned = (features.PINNED_DIR / "l1labels.py").read_text("utf-8")
    assert pinned.count("HARD_LAST = '2026-11-19'") == 1  # the pinned bytes are untouched
    with pytest.raises(ModelRefused) as exc:
        full.live_text(pinned.replace("HARD_LAST = '2026-11-19'", "HARD_LAST = '2026-11-20'"))
    assert exc.value.cause == "table_code_replacement"
    with pytest.raises(ModelRefused):
        full.live_text(pinned + "\nHARD_LAST = '2026-11-19'\n")


def test_a_live_day_with_the_replacement_has_news_values(live):
    today, bars, state, given = live
    got = full.minute_rows_full(bars, today, LIVE_MINUTE, state, None, given)
    assert len(got) > 0
    assert got["bz_n1d"].notna().all() and got["nw_n"].notna().all()


def test_a_live_day_without_the_replacement_is_the_training_s_sealed_day(live, monkeypatch):
    """The other behaviour: the unpatched pinned l1labels on a live day gives every bz and nw_*
    value missing (and refuses a Benzinga file named past 2026-11-19)."""
    today, bars, state, given = live
    code = full.full_code()
    path = features.PINNED_DIR / "l1labels.py"
    spec = importlib.util.spec_from_file_location("l1labels_unpatched", path)
    plain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plain)
    assert plain.HARD_LAST == "2026-11-19"
    for module in (code.feat, sys.modules["l1bz"], sys.modules["l1fl"]):
        monkeypatch.setattr(module, "L", plain)
    with pytest.raises(ModelRefused) as exc:
        full.minute_rows_full(bars, today, LIVE_MINUTE, state, None, given)
    assert "sealed_date" in exc.value.detail
    folder = given.benzinga.parent / "bz-old-names"
    folder.mkdir()
    for f in given.benzinga.iterdir():
        shutil.copy(f, folder / f.name.replace(today, "2026-11-19"))
    old = full.FullInputs(given.tick_file, given.reference, folder, given.fl_facts)
    got = full.minute_rows_full(bars, today, LIVE_MINUTE, state, None, old)
    names = full.full_groups()["bz"] + ["nw_n", "nw_3d", "nw_min", "nw_any", "nw_rk"]
    assert len(got) > 0 and got[names].isna().all().all()


def test_a_benzinga_file_across_the_sealed_window_refuses(tmp_path):
    days = synthetic.market(HISTORY_DAYS + 1, last=LIVE_DAY)
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    given = _inputs(tmp_path / "x", today, "2026-08-14")
    with pytest.raises(ModelRefused) as exc:
        full.minute_rows_full(synthetic.cut(bars, today, LIVE_MINUTE), today, LIVE_MINUTE,
                              state, None, given)
    assert exc.value.cause == "feature_code_refused" and "sealed_date" in exc.value.detail


def test_bars_after_the_minute_are_refused(setup):
    today, bars, state, given = setup
    with pytest.raises(ModelRefused) as exc:
        full.minute_rows_full(bars, today, 340, state, None, given)
    assert exc.value.cause == "bar_after_minute"


def test_a_changed_pinned_byte_is_refused(monkeypatch, tmp_path):
    full.full_code()
    copy = tmp_path / "pinned"
    shutil.copytree(features.PINNED_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
    text = (copy / "l1bz.py").read_bytes()
    (copy / "l1bz.py").write_bytes(text.replace(b"bz_n60", b"bz_n61", 1))
    monkeypatch.setattr(features, "PINNED_DIR", copy)
    monkeypatch.setattr(full, "_CODE", {})
    with pytest.raises(ModelRefused) as exc:
        full.full_code()
    assert exc.value.cause == "table_code_md5" and exc.value.detail == "l1bz.py"


def test_a_changed_settings_byte_is_refused(monkeypatch):
    monkeypatch.setitem(full.FULL_PINS, "l1feat-settings.json", "0" * 32)
    monkeypatch.setattr(full, "_CODE", {})
    with pytest.raises(ModelRefused) as exc:
        full.full_code()
    assert exc.value.cause == "table_code_md5"
