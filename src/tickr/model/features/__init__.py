"""Shared layer-1 feature code: the training's own table code, run on live minute bars.

Parity by construction, not by a second implementation. `pinned/` holds byte copies of the
research table code that builds the layer-1 feature table (feature build set 7e, 2026-11-23):
`wide.py`, `wm.py`, `wm2_l1f.py` (the label builder's `wm2_l1.py` with the feature builder's
declared `r_rv` replacement, exactly the text `l1feat.load_wm2f` writes), and `wm2ref.py` /
`wm2tk.py`, which `wm2_l1f.py` imports by name but which stay off here (their columns need the
reference and tick feeds). Every file is checked against its md5 before the first import; a
copy that differs refuses (`table_code_md5`). They are never edited in this repo: a new training
feature version arrives as new pinned files with new md5s, so the bot and the training cannot
drift apart silently. Their numbers (list depth 500, 20 history days, windows) are the
training's definition of the features, not bot settings, which is why they live in the pinned
code and not in `config/bot.toml` (hard rule 6 covers the bot's own numbers).

Groups served live today (65 of the 191 training features): `bars` 41, `lists` 3, `market` 9,
`cross` 4, `hist` 8. The `lists` group is the training's own reconstruction of the top-500
lists from the same full-market minute bars: a model input, never the bot's list of symbols to
trade, which stays Webull's (owner, 2026-11-20). `company`, `tk`, `tk2`, `bz` and `fl` need the
reference, tick and news feeds; a bundle that uses one of them is refused at load
(`feature_not_live`, see `tickr.model.bundle`). `tickr.model.features.full` computes all 191
(COMPUTABLE_GROUPS) with the pinned feature builder from inputs in the training's file formats;
no feed fills those inputs yet, so they stay out of LIVE_GROUPS.

How a minute is computed (the training's prefix rule, `l1labels.prefix`): the whole day's bars
up to and including minute m are handed to the pinned `day2` as the day's bar file would be, and
the rows of minute m are taken from its table. The feature builder's prefix test proved every
feature of the rows up to a cut minute equals the whole day's (after the `r_rv` replacement);
`tests/model/test_features.py` repeats that check on synthetic days with this module.

Minute index m counts from 04:00 New York (09:30 = 330), as in the training.
"""

from __future__ import annotations

import collections
import hashlib
import os
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tickr.model.errors import ModelRefused

PINNED_DIR = Path(__file__).with_name("pinned")
# md5 of every pinned file: the research pins (l1labels.PIN, l1feat.PIN and the md5 the feature
# build recorded for wm2_l1f.py in l1features/set-7e-2026-11-23/md5-code.txt).
PINS = {
    "wide.py": "4527914daae6119fab5a4d01163bf480",
    "wm.py": "366fb5ac2bc3aea498f56b30b7257b5a",
    "wm2_l1f.py": "5cac8ba738794bcfc0d2d7399554f751",
    "wm2ref.py": "8c2ce78eb813ddf060141b4e4d3b6200",
    "wm2tk.py": "e480668bb0b49819d4226eb85f26c749",
}
# The variables the pinned code reads by name; the training refuses to run when one of the first
# five is set (l1labels.env_refuse), and so does the bot. WM_PROGRESS (a progress file the pinned
# `progress()` writes) is refused too: the bot never calls it, and a set one means a training
# shell's environment leaked into the bot. Only these names are asked, never the whole
# environment (hard rule 10).
TABLE_VARIABLES = ("WM_TOPN", "WM_LAG", "WM_REF", "WM_TICK", "WM_TSPAN", "WM_PROGRESS")
# The defaults the training runs the table code with (l1labels.settings_ok).
TABLE_DEFAULTS = {"NDAYS": 20, "TOPN": 500, "LAG": 0}
LIST_FEATURES = ("inl", "nin", "tofl")
LIVE_GROUPS = ("bars", "lists", "market", "cross", "hist")
# The bar file columns the table code reads (l1labels.BARCOLS).
BAR_COLUMNS = ("ticker", "volume", "open", "close", "high", "low", "window_start")
PRICE_COLUMNS = ("open", "close", "high", "low")
NEW_YORK = "America/New_York"
NS_PER_MINUTE = 60 * 10**9
DAY_START_MINUTE = 4 * 60  # m = 0 is 04:00 New York

_LOCK = threading.Lock()
_CODE: dict[str, Any] = {}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def pinned_digests() -> dict[str, str]:
    """md5 of every pinned file as it is on disk now."""
    return {name: _md5(PINNED_DIR / name) for name in sorted(PINS)}


def table_code() -> Any:
    """The pinned `wm2_l1f` module, imported once after every check: each pinned file is the
    pinned bytes, no table variable is set, and the module runs with the training's defaults."""
    with _LOCK:
        if "wm2" in _CODE:
            return _CODE["wm2"]
        refuse_table_variables()
        got = pinned_digests()
        differ = sorted(name for name, digest in got.items() if digest != PINS[name])
        if differ:
            raise ModelRefused("table_code_md5", ", ".join(differ))
        try:
            import numpy  # noqa: F401 - the model extra must be installed
            import pandas  # noqa: F401
        except ImportError as exc:
            raise ModelRefused("model_libraries_missing", exc.name or "") from exc
        # The pinned files import each other by their top-level names, as in the training. No
        # other module of the bot has one of these names; a module of the same name imported
        # earlier from elsewhere would win over the pinned file, so each is checked below.
        if str(PINNED_DIR) not in sys.path:
            sys.path.insert(0, str(PINNED_DIR))
        import wm2_l1f  # type: ignore[import-not-found]

        for name in PINS:
            module = sys.modules.get(name.removesuffix(".py"))
            where = getattr(module, "__file__", None)
            if where is None or Path(where).resolve() != (PINNED_DIR / name).resolve():
                raise ModelRefused("table_code_shadowed", f"{name} imported from {where}")
        if not hasattr(wm2_l1f, "_CAP"):
            wm2_l1f._CAP = {}
        wm2_l1f._L1_READ_TYPES = _types_reader_unused
        for name, value in TABLE_DEFAULTS.items():
            if getattr(wm2_l1f, name) != value:
                raise ModelRefused("table_code_settings", f"{name} = {getattr(wm2_l1f, name)}")
        if wm2_l1f.REF is not None or wm2_l1f.TK is not None or wm2_l1f.TSPAN is not None:
            raise ModelRefused("table_code_settings", "reference, tick or TSPAN columns are on")
        _CODE["wm2"] = wm2_l1f
        return wm2_l1f


def refuse_table_variables() -> None:
    """The training refuses to run the table code when one of TABLE_VARIABLES is set; so does
    the bot. Each variable is asked by name, never the whole environment."""
    set_names = [name for name in TABLE_VARIABLES if os.environ.get(name) is not None]
    if set_names:
        raise ModelRefused("table_variable_set", ", ".join(set_names))


def _types_reader_unused(_path: str) -> set[str]:
    raise ModelRefused("table_code_call", "days2 reads files; the bot hands day2 its bars")


def feature_groups() -> dict[str, list[str]]:
    """The live groups and their features in the training's order (l1feat.groups)."""
    w = table_code()
    groups = {"bars": [c for c in w.FEAT if c not in LIST_FEATURES],
              "lists": list(LIST_FEATURES)}
    groups.update({name: list(w.GROUPS[name]) for name in ("market", "cross", "hist")})
    return groups


def live_features() -> list[str]:
    """Every feature this module computes, in the training's group order."""
    return [name for group in feature_groups().values() for name in group]


@dataclass
class DayState:
    """What the day before needs to carry: previous closes (`prev`), the market ETFs' previous
    regular closes (`mprev`) and the summaries of up to 20 earlier days (`hist`, oldest
    first), all on today's split-adjusted scale (l1labels.history)."""

    prev: dict[str, float] = field(default_factory=dict)
    mprev: dict[str, float] = field(default_factory=dict)
    hist: collections.deque = field(default_factory=collections.deque)


def bars_frame(rows: Any, day: str, nm: int | None = None) -> Any:
    """The day's minute bars as the training's bar reader returns them (`l1labels.bar_load`,
    the same checks): every row of New York date `day`, finite numbers, prices above 0, volume
    0 or more, window_start an exact minute, (ticker, minute) unique; adds `m` and keeps
    minutes 0..959. `rows`: a pandas frame with at least the columns of BAR_COLUMNS, the
    columns of a Massive minute aggregates flat file. `nm`: the table code's minutes per day
    (`NM`), given by a live context that holds its own copy of the table code."""
    import numpy as np
    import pandas as pd

    missing = [c for c in BAR_COLUMNS if c not in rows.columns]
    if missing:
        raise ModelRefused("bar_columns_missing", ", ".join(missing))
    d = rows.copy()
    if not pd.api.types.is_integer_dtype(d.window_start):
        raise ModelRefused("bar_value_invalid", "window_start is not an integer")
    for c in BAR_COLUMNS[1:-1]:
        v = pd.to_numeric(d[c], errors="coerce").to_numpy(np.float64)
        bad = ~np.isfinite(v) | (~(v > 0) if c in PRICE_COLUMNS else ~(v >= 0))
        if bad.any():
            raise ModelRefused("bar_value_invalid", f"{c} of {d.ticker.values[bad.argmax()]}")
    if (d.window_start.to_numpy() % NS_PER_MINUTE != 0).any():
        raise ModelRefused("bar_stamp_off_minute")
    ts = pd.to_datetime(d.window_start, unit="ns", utc=True).dt.tz_convert(NEW_YORK)
    if (ts.dt.normalize() != pd.Timestamp(day, tz=NEW_YORK)).any():
        raise ModelRefused("bar_wrong_day", day)
    d["m"] = (ts.dt.hour * 60 + ts.dt.minute - DAY_START_MINUTE).astype(np.int32)
    if d.duplicated(["ticker", "m"]).any():
        raise ModelRefused("bar_key_duplicate", day)
    if nm is None:
        nm = table_code().NM
    return d[(d.m >= 0) & (d.m < nm)]


def _day2(frame: Any, day: str, state: DayState, types: set[str] | None, want: bool) -> Any:
    w = table_code()
    with _LOCK:
        w.load = lambda _path: frame  # the declared replacement of the bar read (as the training)
        w._CAP.clear()
        try:
            return w.day2(day, state.prev, types, state.hist, state.mprev, want=want)
        finally:
            w.load = _no_file_reads
            w._CAP.clear()


def _no_file_reads(_path: str) -> Any:
    raise ModelRefused("table_code_call", "bars are handed in, never read from a file")


def apply_splits(state: DayState, factors: Iterable[tuple[str, float]]) -> None:
    """Put the carried state on the split-adjusted scale of a day's splits, as the training does
    on the execution date (l1labels._split). `factors`: (ticker, split_from / split_to)."""
    import numpy as np

    for ticker, factor in factors:
        if ticker in state.prev:
            state.prev[ticker] *= factor
        if ticker in state.mprev:
            state.mprev[ticker] *= factor
        for summary in state.hist:
            j = np.searchsorted(summary["tick"], ticker)
            if j < len(summary["tick"]) and summary["tick"][j] == ticker:
                for key in ("hi", "lo", "cl", "pc"):
                    summary[key][j] *= factor


def history(days: Sequence[tuple[str, Any]], splits: Mapping[str, Sequence[tuple[str, float]]],
            today: str, types: set[str] | None) -> DayState:
    """The state today's table needs from the earlier trading days, oldest first (the training
    reads the 45 calendar days before, of which the last 20 trading days are kept; the caller
    passes them). `days`: (date, raw bar frame). The splits of every day and of today are
    applied on their execution dates."""
    w = table_code()
    state = DayState(hist=collections.deque(maxlen=w.NDAYS))
    last = ""
    for day, rows in days:
        if day <= last or day >= today:
            raise ModelRefused("history_order", f"{day} after {last or 'start'}, today {today}")
        last = day
        apply_splits(state, splits.get(day, ()))
        _, lastc, summ, mclose = _day2(bars_frame(rows, day), day, state, types, want=False)
        state.prev.update(lastc)
        state.mprev.update(mclose)
        state.hist.append(summ)
    apply_splits(state, splits.get(today, ()))
    return state


def minute_rows(rows: Any, day: str, minute: int, state: DayState,
                types: set[str] | None) -> Any:
    """The feature rows of `minute` of `day` from the day's bars so far (bars after `minute`
    are refused, never dropped: a later bar in hand means the caller's clock is wrong). Returns
    a frame with `sym` and every live feature, one row per symbol the training's row rule
    keeps at that minute, in the training's dtypes (float32); an empty frame when no row."""
    import pandas as pd

    frame = bars_frame(rows, day)
    if (frame.m > minute).any():
        raise ModelRefused("bar_after_minute", f"minute {minute}")
    table = _day2(frame, day, state, types, want=True)[0]
    cols = ["sym", *live_features()]
    if table is None:
        return pd.DataFrame(columns=cols)
    out = table.loc[table.m == minute, cols]
    return out.reset_index(drop=True)

