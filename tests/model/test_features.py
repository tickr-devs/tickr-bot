"""The shared feature code: the pinned training table code, the prefix rule, the refusals."""

from __future__ import annotations

import model_bars as synthetic
import numpy as np
import pandas as pd
import pytest

from tickr.model import features
from tickr.model.errors import ModelRefused

HISTORY_DAYS = 6  # the table code needs at least 5 earlier days (MINH) for the hist group


@pytest.fixture(scope="module")
def days():
    return synthetic.market(HISTORY_DAYS + 1)


@pytest.fixture(scope="module")
def whole_day(days):
    """The training's way: the whole day's file through day2 at once."""
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    frame = features.bars_frame(bars, today)
    table = features._day2(frame, today, state, None, want=True)[0]
    return today, bars, table


def test_pinned_files_are_the_training_bytes():
    assert features.pinned_digests() == features.PINS


def test_the_live_groups_are_the_training_groups_and_order():
    groups = features.feature_groups()
    assert {k: len(v) for k, v in groups.items()} == {
        "bars": 41, "lists": 3, "market": 9, "cross": 4, "hist": 8}
    w = features.table_code()
    assert sorted(features.live_features()) == sorted(w.FEAT2)
    assert features.live_features()[:3] == ["m", "sess", "c"]


@pytest.mark.parametrize("hhmm", ["04:30", "09:36", "09:45", "12:00", "15:30", "17:10"])
def test_a_minute_from_the_bars_so_far_equals_the_whole_day(days, whole_day, hhmm):
    """The prefix rule: bars cut after minute m give the same rows and features at m as the
    whole day (the feature builder's prefix test, here on the live path)."""
    today, bars, table = whole_day
    h, mm = map(int, hhmm.split(":"))
    minute = h * 60 + mm - 240
    state = features.history(days[:-1], {}, today, None)
    got = features.minute_rows(synthetic.cut(bars, today, minute), today, minute, state, None)
    cols = ["sym", *features.live_features()]
    want = table.loc[table.m == minute, cols].sort_values("sym").reset_index(drop=True)
    got = got.sort_values("sym").reset_index(drop=True)
    assert len(want) > 0
    assert list(got.sym) == list(want.sym)
    for c in cols[1:]:
        assert np.array_equal(got[c].to_numpy(), want[c].to_numpy(), equal_nan=True), c


def test_history_gives_the_hist_group_values(days, whole_day):
    _, _, table = whole_day
    assert table.vu.notna().any() and table.rv20.notna().any() and table.pd_ret.notna().any()


def test_a_split_rescales_the_carried_state(days):
    today = days[-1][0]
    plain = features.history(days[:-1], {}, today, None)
    split = features.history(days[:-1], {today: [("T001", 0.5)]}, today, None)
    assert split.prev["T001"] == pytest.approx(plain.prev["T001"] * 0.5)
    assert split.prev["T002"] == plain.prev["T002"]


def test_a_bar_after_the_minute_is_refused(days):
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    with pytest.raises(ModelRefused) as exc:
        features.minute_rows(synthetic.cut(bars, today, 400), today, 399, state, None)
    assert exc.value.cause == "bar_after_minute"


@pytest.mark.parametrize("column,value,cause", [
    ("close", 0.0, "bar_value_invalid"),
    ("volume", -1, "bar_value_invalid"),
    ("high", np.inf, "bar_value_invalid"),
])
def test_bad_bars_are_refused_not_dropped(days, column, value, cause):
    today, bars = days[-1]
    bad = bars.copy()
    bad[column] = bad[column].astype(float)
    bad.loc[5, column] = value
    with pytest.raises(ModelRefused) as exc:
        features.bars_frame(bad, today)
    assert exc.value.cause == cause


def test_bars_of_another_day_or_twice_are_refused(days):
    today, bars = days[-1]
    with pytest.raises(ModelRefused) as exc:
        features.bars_frame(bars, days[-2][0])
    assert exc.value.cause == "bar_wrong_day"
    with pytest.raises(ModelRefused) as exc:
        features.bars_frame(pd.concat([bars, bars.iloc[:1]]), today)
    assert exc.value.cause == "bar_key_duplicate"
    with pytest.raises(ModelRefused) as exc:
        features.bars_frame(bars.drop(columns=["high"]), today)
    assert exc.value.cause == "bar_columns_missing"


def test_history_days_must_be_in_order_and_before_today(days):
    today = days[-1][0]
    with pytest.raises(ModelRefused) as exc:
        features.history([days[1], days[0]], {}, today, None)
    assert exc.value.cause == "history_order"


def test_a_changed_pinned_file_is_refused(monkeypatch):
    monkeypatch.setitem(features.PINS, "wm.py", "0" * 32)
    monkeypatch.setattr(features, "_CODE", {})
    with pytest.raises(ModelRefused) as exc:
        features.table_code()
    assert exc.value.cause == "table_code_md5"


@pytest.mark.parametrize("name", features.TABLE_VARIABLES)
def test_a_set_table_variable_is_refused(monkeypatch, name):
    monkeypatch.setenv(name, "1")
    monkeypatch.setattr(features, "_CODE", {})
    with pytest.raises(ModelRefused) as exc:
        features.table_code()
    assert exc.value.cause == "table_variable_set"


def test_a_pinned_name_imported_from_elsewhere_is_refused(monkeypatch, tmp_path):
    import sys
    import types

    features.table_code()  # the pinned modules are imported, whatever ran before in this worker
    stray = types.ModuleType("wide")
    stray.__file__ = str(tmp_path / "wide.py")
    monkeypatch.setitem(sys.modules, "wide", stray)
    monkeypatch.setitem(sys.modules, "wm2_l1f", sys.modules["wm2_l1f"])
    monkeypatch.setattr(features, "_CODE", {})
    with pytest.raises(ModelRefused) as exc:
        features.table_code()
    assert exc.value.cause == "table_code_shadowed"
