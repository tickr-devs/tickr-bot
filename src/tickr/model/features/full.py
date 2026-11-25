"""All 191 layer-1 features with the training's own code, from inputs in the training's formats.

Parity by construction, as in `tickr.model.features`: `pinned/` also holds byte copies of the
feature builder of set 7e (`l1feat.py` and what it imports: `l1labels.py`, `l1tk2.py`,
`l1bz.py`, `l1fl.py`, `wm2ref16.py`), `wm2tk_l1.py` (the pinned `wm2tk.py` with the builder's
declared replacement TKPATCH, checked here to be exactly the text `l1feat.modules` writes), the
pinned trading-day calendar `trading-days.txt` that `l1labels.calendar` reads beside itself, and
the builder's settings file `l1feat-settings.json` (the bz and fl definitions: windows, channel
sets, lags). Each is checked against its md5 (FULL_PINS) before the first import. Nothing is
re-implemented: `minute_rows_full` cuts the inputs with `l1feat.cut_inputs` and runs
`l1feat.assemble` on the rows of one minute.

Computable here, not live: no feed fills these inputs yet and nothing at runtime calls this
module. `tickr.model.bundle` still serves only `LIVE_GROUPS`; COMPUTABLE_GROUPS is the list of
the groups this module can compute when it is handed the files below.

INPUTS (the contract the live feeds must fill; formats are the training's, unchanged):

- bars, lists, market, cross, hist (65): the day's minute bars up to minute m and the DayState of
  the earlier days, exactly as `tickr.model.features.minute_rows` takes them.
- company (15, `wm2ref16.Ref`): `reference`, a prepared reference folder as `l1feat.refprep`
  writes it: `manifest.json` = {"plain": [names], "files": {name: [bytes, md5]}, ...}; the plain
  files `splits.jsonl` (ticker, execution_date, split_from, split_to), `shares-outstanding-asof
  *.jsonl` (ticker, date, share_class_shares_outstanding or weighted_shares_outstanding,
  list_date, sic_code) and `short-interest*.jsonl` (ticker, settlement_date, short_interest,
  days_to_cover); and one news file per month `news-YYYY-MM.jsonl` (id, published_utc,
  publisher.name, tickers) for every month the 3 calendar days before the day reach. Every file
  must match its manifest line. The previous close of each row is DayState.prev.
- tk (73, `wm2tk_l1.Tick`) and tk2 (21, `l1tk2.add`): `tick_file`, the day's tick table as the
  frozen converter wm2tick v11 (c1c1ead1) writes it: parquet, one row per (sym, m) with m the
  minute of the New York day 0..1439 (INTEGER), sym text, and its 61 value columns (tn, qn
  BIGINT, the others DOUBLE). A (sym, m) twice refuses.
- bz (13, `l1bz.News` / `l1bz.Earnings`): `benzinga`, a folder of `news-<first>_<last>.jsonl.gz`
  (benzinga_id, published as UTC text with Z or an offset, tickers, channels) and
  `earnings-<first>_<last>.jsonl.gz` (benzinga_id, ticker, date, time HH:MM:SS New York,
  last_updated UTC, actual_eps, estimated_eps). Every date from the day before the 20th prior
  trading day to the day must be covered by a news file, and every date from the first earnings
  file to the day by an earnings file; a missing day refuses, never reads as "no news".
- fl (4, `l1fl.Fl`): `fl_facts`, the facts csv `l1fl.facts` writes (header ticker, cik, end,
  filed, known, float_usd, share, src, form). It is made before the session.

THE PREFIX RULE (`l1feat.cut_inputs`, the training's own): for minute m the tick table keeps
rows with m_of_day <= m + 240; Benzinga articles published after the mark (end of minute m) and
reports timed after it are dropped from the files that hold the day; the reference news month of
the day is cut at the same mark. Files dated by day stay whole. Bars after m are refused, as in
`minute_rows`.

SEALED DATES. The training treats every date after `l1labels.HARD_LAST` (2026-11-19, the last day
of the research archive) as sealed: the bz group and the five nw_* columns are missing on such a
day and a Benzinga file whose name reaches past it refuses. A live day is after it by nature, and
the data of a live day is the bot's present, not a held-out future; the prefix rule above keeps
everything after the minute out. So for a day after HARD_LAST the bot runs the training code with
HARD_LAST moved to that day (LIVE_LAST_RULE): a declared runtime replacement, made under the
module lock and undone after the call. The sealed window SEAL (2026-07-01 .. 2026-08-14) is kept
unchanged: the training's values for a live day are then the training's own (a split factor
over the window, an earnings history that spans it, stay missing as in training). For a day on
or before HARD_LAST nothing is replaced. Benzinga files must then be named up to the day itself
(`<kind>-<first>_<day>.jsonl.gz` for the running month), and the calendar ends 2026-12-31: a
later day refuses (`unknown_trading_day`) until a new pinned calendar arrives.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tickr.model import features as base
from tickr.model.errors import ModelRefused

# md5 of every pinned file of the feature builder (l1features-set7e/md5-code.txt, the same as the
# set 6 job folder; wm2tk_l1.py is the text l1feat.modules writes from wm2tk.py e480668b).
FULL_PINS = {
    "l1feat.py": "99a29f90294a81db5c32c85fe4c27d71",
    "l1labels.py": "0a22137f176d1537236aee7c3d2ac59c",
    "l1tk2.py": "77ded73e8ff3e907b87d00044750b65c",
    "l1bz.py": "dcd7e887b9fac4c41d6b1123536b6a90",
    "l1fl.py": "ccca61d4e79c51c8efca1fdfa010b4a2",
    "wm2ref16.py": "961333483dc8c06dd72a6de6142ff139",
    "wm2tk_l1.py": "3da194430a10cf9d531df9d538d9f1ec",
    "trading-days.txt": "3c22591622d1f06ce1611f9fcf5c702d",
    "l1feat-settings.json": "b316c01cf7761b21483a9bfdd11e0bcb",
}
SETTINGS_FILE = "l1feat-settings.json"
# The groups in the training's order: the day record stores them with sorted keys, and the
# trainer's feature list follows the record (feed-coverage-v2b/feature-schema-191.csv).
COMPUTABLE_GROUPS = ("bars", "bz", "company", "cross", "fl", "hist", "lists", "market", "tk",
                     "tk2")
# SHA-256 of the group map as the README of feature-schema-191.csv computes it, and the md5 of
# the names joined by newlines (the trainer's feature_list_md5).
GROUP_MAP_SHA256 = "15b8237c72e7d30d5bfa9332f80a0199ed3957becaaebbb9e6ca8524ae93a772"
FEATURE_LIST_MD5 = "098e7eb91329bdaf66505b9501961256"
LIVE_LAST_RULE = "a day after l1labels.HARD_LAST runs with HARD_LAST = that day; SEAL unchanged"

_CODE: dict[str, Any] = {}


@dataclass(frozen=True)
class FullInputs:
    """The day's non-bar inputs in the training's file formats (see the module docstring)."""

    tick_file: Path
    reference: Path
    benzinga: Path
    fl_facts: Path


@contextlib.contextmanager
def _training_refusals() -> Iterator[None]:
    """The training code refuses with SystemExit('REFUSED: <cause>...'): one typed refusal."""
    try:
        yield
    except SystemExit as exc:
        raise ModelRefused("feature_code_refused", str(exc)) from None


def full_digests() -> dict[str, str]:
    """md5 of every pinned feature-builder file as it is on disk now."""
    return {name: base._md5(base.PINNED_DIR / name) for name in sorted(FULL_PINS)}


def _tk_text(l1feat: Any) -> str:
    """The text `l1feat.modules` writes to wm2tk_l1.py: TKPATCH on the pinned wm2tk.py, each
    replacement applying exactly once, as the builder checks."""
    src = (base.PINNED_DIR / "wm2tk.py").read_text("utf-8")
    new = src
    for name, old, replacement in l1feat.TKPATCH:
        if src.count(old) != 1 or new.count(old) != 1:
            raise ModelRefused("table_code_replacement", name)
        new = new.replace(old, replacement)
    return new


def full_code() -> SimpleNamespace:
    """The pinned feature builder, imported once after every check: the table code's checks
    (`tickr.model.features.table_code`), every file of FULL_PINS, wm2tk_l1.py = TKPATCH applied
    to wm2tk.py, no module shadowed, the builder's settings read by its own reader, and the
    group map equal to the training's."""
    w = base.table_code()
    with base._LOCK:
        if "full" in _CODE:
            return _CODE["full"]
        got = full_digests()
        differ = sorted(name for name, digest in got.items() if digest != FULL_PINS[name])
        if differ:
            raise ModelRefused("table_code_md5", ", ".join(differ))
        with _training_refusals():
            import l1feat  # type: ignore[import-not-found]
            import wm2ref16  # type: ignore[import-not-found]
            import wm2tk_l1  # type: ignore[import-not-found]
        for name in FULL_PINS:
            if not name.endswith(".py"):
                continue
            module = sys.modules.get(name.removesuffix(".py"))
            where = getattr(module, "__file__", None)
            if where is None or Path(where).resolve() != (base.PINNED_DIR / name).resolve():
                raise ModelRefused("table_code_shadowed", f"{name} imported from {where}")
        if Path(wm2tk_l1.__file__).read_text("utf-8") != _tk_text(l1feat):
            raise ModelRefused("table_code_replacement", "wm2tk_l1.py is not TKPATCH on wm2tk.py")
        labels = sys.modules["l1labels"]
        if (l1feat.PIN["wm2ref16.py"] != FULL_PINS["wm2ref16.py"]
                or any(labels.PIN[k] != base.PINS[k] for k in ("wide.py", "wm.py", "wm2tk.py"))):
            raise ModelRefused("table_code_md5", "the builder's own pins differ from the copies")
        with _training_refusals():
            settings = l1feat.settings(str(base.PINNED_DIR / SETTINGS_FILE))
            groups, _ = l1feat.groups(w, wm2ref16, wm2tk_l1)
        ordered = {g: groups[g] for g in COMPUTABLE_GROUPS}
        if (sorted(groups) != sorted(COMPUTABLE_GROUPS)
                or _group_digest(ordered) != GROUP_MAP_SHA256):
            raise ModelRefused("feature_group_map", "the pinned builder's groups differ")
        names = [c for cs in ordered.values() for c in cs]
        if hashlib.md5("\n".join(names).encode("utf-8")).hexdigest() != FEATURE_LIST_MD5:
            raise ModelRefused("feature_group_map", "the feature list digest differs")
        code = SimpleNamespace(feat=l1feat, labels=labels, ref=wm2ref16, tk=wm2tk_l1, w=w,
                               settings=settings, groups=ordered, names=names)
        _CODE["full"] = code
        return code


def _group_digest(groups: dict[str, list[str]]) -> str:
    text = json.dumps([[g, n] for g, n in groups.items()], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def full_groups() -> dict[str, list[str]]:
    """The ten groups and their features in the training's order."""
    return {g: list(cs) for g, cs in full_code().groups.items()}


def full_features() -> list[str]:
    """The 191 features in the training's order (feature-schema-191.csv)."""
    return list(full_code().names)


@contextlib.contextmanager
def _live_last(labels: Any, day: str) -> Iterator[None]:
    """LIVE_LAST_RULE, under the module lock: HARD_LAST moved to a day after it, then undone."""
    kept = labels.HARD_LAST
    if day > kept:
        labels.HARD_LAST = day
    try:
        yield
    finally:
        labels.HARD_LAST = kept


def minute_rows_full(rows: Any, day: str, minute: int, state: base.DayState,
                     types: set[str] | None, inputs: FullInputs) -> Any:
    """The rows of `minute` of `day` with all 191 features: the table rows of the minute from
    the day's bars so far (as `minute_rows`), then the training's `cut_inputs` at the minute
    and its `assemble` on those rows. Returns `sym` and the 191 features in the training's order
    and stored types (m int16, sess int8, the others float32); an empty frame when no row."""
    import numpy as np
    import pandas as pd

    code = full_code()
    frame = base.bars_frame(rows, day)
    if (frame.m > minute).any():
        raise ModelRefused("bar_after_minute", f"minute {minute}")
    table = base._day2(frame, day, state, types, want=True)[0]
    cols = ["sym", *code.names]
    if table is None or not (table.m == minute).any():
        return pd.DataFrame(columns=cols)
    t = table.loc[table.m == minute].reset_index(drop=True)
    prev = np.array([state.prev.get(s, np.nan) for s in t["sym"].values], np.float64)
    with base._LOCK:
        work = tempfile.mkdtemp(prefix="tickr-l1feat-")
        try:
            with (_training_refusals(), _live_last(code.labels, day),
                  contextlib.redirect_stdout(io.StringIO())):
                cut = code.feat.cut_inputs(day, minute, str(inputs.tick_file),
                                           str(inputs.benzinga), work, str(inputs.reference))
                out, _ = code.feat.assemble(t, prev, day, cut[0], cut[2], cut[1],
                                            str(inputs.fl_facts), code.settings, work, code.w,
                                            code.ref, code.tk)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return out[cols].reset_index(drop=True)
