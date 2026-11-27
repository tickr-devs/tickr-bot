"""All 191 layer-1 features with the training's own code, from inputs in the training's formats.

Parity by construction, as in `tickr.model.features`: `pinned/` also holds byte copies of the
feature builder of set 7e (`l1feat.py` and what it imports: `l1labels.py`, `l1tk2.py`,
`l1bz.py`, `l1fl.py`, `wm2ref16.py`), `wm2tk_l1.py` (the pinned `wm2tk.py` with the builder's
declared replacement TKPATCH, checked here to be exactly the text `l1feat.modules` writes), the
pinned trading-day calendar `trading-days.txt` that `l1labels.calendar` reads beside itself, and
the builder's settings file `l1feat-settings.json` (the bz and fl definitions: windows, channel
sets, lags). Each is checked against its md5 (FULL_PINS) on the bytes that are run. Nothing is
re-implemented: `LiveContext.minute_rows` cuts the inputs with `l1feat.cut_inputs` and runs
`l1feat.assemble` on the rows of one minute, in the live date context described below. The 65
table columns come from the context's own feature-only copy of the table code (see THE TABLE
CODE IN A CONTEXT). `minute_rows` is `table_rows` (the minute's cohort and table columns) then
`assemble_rows` (the cut and `assemble`), each with the minute's bounds and receipt cutoff, for
a live caller that needs the cohort before the ticks are converted.

Computable here, not live: no feed fills these inputs yet and nothing at runtime calls this
module. `tickr.model.bundle` still serves only `LIVE_GROUPS`; COMPUTABLE_GROUPS is the list of
the groups this module can compute when it is handed the files below.

INPUTS (the contract the live feeds must fill; formats are the training's, unchanged):

- bars, lists, market, cross, hist (65): the day's minute bars up to minute m and the DayState of
  the earlier days, as `tickr.model.features.minute_rows` takes them, each bar row with its
  receipt (RECEIPT_COLUMN).
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

Live, every record carries its actual receipt beside the training's fields (Codex 109 point 3:
the training's cut leaves dated reference records and revision fields untouched, so a record it
accepts proves nothing about when it arrived): RECEIPT_COLUMN (UTC nanoseconds, BIGINT) on each
tick row and bar row, RECEIPT_FIELD (UTC text with Z or an offset) on each Benzinga, reference
and splits record and as the last column of the fl facts csv. A record without a readable
receipt refuses (`receipt_missing`). The reference manifest names the files as captured.

EACH MINUTE HAS TWO BOUNDS (Codex 109 point 3): the event-time mark (end of minute m) and the
actual receipt cutoff (`cutoff`, at or after the mark, else `cutoff_before_mark`). First every
record received after the cutoff is left out, even when its event time is earlier (it was not
in hand); the receipts are then removed and the training's own prefix rule runs on the rest.

THE PREFIX RULE (`l1feat.cut_inputs`, the training's own): for minute m the tick table keeps
rows with m_of_day <= m + 240; Benzinga articles published after the mark (end of minute m) and
reports timed after it are dropped from the files that hold the day; the reference news month of
the day is cut at the same mark. Files dated by day stay whole. Bars after m are refused, as in
`minute_rows`.

THE LIVE DATE CONTEXT (Codex 107/108 point 4, 2026-11-25). The training treats every date after
`l1labels.HARD_LAST` (2026-11-19, the last day of the research archive) as sealed, and so must
the research code that imports these files. The bot never imports them under their research
names. `live_context(market_day)` loads the builder privately for one live trading day D, taken
from the bot's session calendar (a `tickr.calendar.MarketDay`, never the wall clock alone):

- One declared, md5-checked replacement (DATE_RULE): the pinned `HARD_LAST = '2026-11-19'`
  becomes `HARD_LAST = 'D'`. Everything after D stays sealed, D itself is open. SEAL
  (2026-07-01 .. 2026-08-14) and every lookback rule built on it are the pinned bytes: a
  Benzinga file may not span the window, a split factor or an earnings history across it stays
  missing (er_next stays missing until the reports it uses all lie after the window). D must be a
  day of the context's calendar (`unknown_trading_day`, see THE CALENDAR) and not a sealed one
  (`sealed_day`).
- The seven builder modules are executed from the checked bytes into private module objects
  whose imports of each other resolve inside the context. They are never placed in
  `sys.modules`; a research import of `l1labels` (training, parity harness, research tools)
  still gets the pinned file with 2026-11-19, and two contexts of two days share nothing.
- Inference only: of each module only the as-of feature path stays callable (LIVE_SURFACE:
  `cut_inputs`, `assemble` and what they call). Label simulation and label/return builders
  (`l1labels.core`, `run`, `build`, `frame`, `publish` ...), the files that compare with labels
  (`l1feat.keys_equal`, `day_`, `prefix`), file writers and command-line modes are replaced by a
  typed refusal (`outside_live_context`), and an import of a research-only module refuses too.
- Future-dated input refused (`future_input`), never cut silently: the inputs must be the
  snapshot as of the mark of the minute (end of minute m). The training's own `cut_inputs` runs
  on them, and whatever it would remove refuses the minute with its source named: tick rows
  after the minute, articles published after the mark in the Benzinga files that hold the day,
  items after the mark in the reference news month of the day, a report timed after the mark
  that is not a scheduled one (below). Nothing more: what the training keeps at the minute
  (announced splits, scheduled reports in later files, revisions, records dated later that the
  builders' own as-of rules skip) passes, and a file named past D meets the training's own
  `sealed_date` refusal.
- A report of the day scheduled for later in the day (Codex 109 point 3) is not a refusal: an
  earnings row dated D, timed after the mark, with no actual EPS yet. It stays in the capture
  with its true date and receipt; the training's cut leaves it out of this minute's features
  and admits it from the first minute whose mark reaches its time, as `cut_inputs` does for the
  training. A row timed after the mark that already has an actual EPS, or that is dated after D
  in a file holding D, is not this case and refuses (`future_input`).
- Nothing is captured here: the cut files live in a temporary folder removed after the minute.
  Inputs the bot records for live days belong to the sealed storage of the capture tools.

THE TABLE CODE IN A CONTEXT (Codex 109 point 3). The 65 table columns come from the pinned table
code (`wide`, `wm`, `wm2ref`, `wm2tk`, `wm2_l1f`, md5-checked as `tickr.model.features.PINS`),
also run from its checked bytes as private modules of the context, never imported under the
research names. Its `day2` builds the label columns while it builds the table (`wm.labels`,
`barriers`, `maxhigh`, the excess returns and label ranks). The context's copy is a feature-only
entry point: a declared, md5-checked replacement (OUTCOME_RULE, result WM2_L1F_FEATURE_ONLY_MD5)
removes those statements and keeps the training's row mask as it is (rows of the session's row
rule up to 2 minutes before its flatten: `g = rq <= rf - 2`, which never depended on a label).
Every other function of the table modules (`labels`, `barriers`, `maxhigh`, `uniq`, `derived`,
`volunits`, the day and file readers) is a typed refusal in the context.

SESSIONS (Codex 109 point 3). The context takes the day's actual hours from the bot's calendar
(`MarketDay`). The pinned table code has the sessions of a full day (TRAINED_SESSION_MINUTES);
the training has no early-close day. A full day whose hours differ refuses
(`session_hours_differ`). A half day follows the bundle's session policy (SESSION_POLICIES,
`tickr.model.bundle.Bundle.session_policy`): `full_days` (the default) refuses it
(`half_day_outside_session_policy`); `half_days_until_close` serves the minutes before the
actual regular close with the pinned sessions unchanged (so `tofl` and the row rule still count
to the pinned 15:50 flatten: what the bundle accepts by declaring it), and a minute at or after
the actual close refuses (`after_exchange_close`).

THE CALENDAR. The pinned `trading-days.txt` (training bytes, md5-checked by the pinned
`l1labels.calendar`) ends 2026-12-31. The context's private `l1labels.calendar()` returns it
followed by the md5-pinned NYSE extension (`calendar_ext`, 2027-01-04 .. 2028-12-29, early closes
as sessions), appended once; the research import of `l1labels` keeps the pinned function and
file. A day after the extension, or a day of it that is no session, refuses
(`unknown_trading_day`). The extension carries its early-close times: for a day it covers, the
bot calendar's `MarketDay` must agree (a half day exactly on the extension's early closes, at
their time), else `calendar_disagrees`, before the half day meets the session policy.

`LiveContext(...)` itself refuses (`live_context_outside_bot`) unless `live_context` builds it.
"""

from __future__ import annotations

import builtins
import contextlib
import csv
import datetime
import gzip
import hashlib
import inspect
import io
import json
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from tickr.calendar import NEW_YORK, MarketDay
from tickr.model import features as base
from tickr.model.errors import ModelRefused
from tickr.model.features import calendar_ext

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
# The builder's modules in load order (each imports only modules before it).
BUILDER = ("l1labels", "l1tk2", "wm2ref16", "wm2tk_l1", "l1bz", "l1fl", "l1feat")
# What stays callable of each module in a live context: the as-of feature path, `cut_inputs`
# and `assemble` and every function or class they reach (traced on the tests' days). Every other
# function or class of the module (label simulation, label and return builders, comparisons with
# label files, file writers, command-line modes, self tests) is replaced by a typed refusal.
LIVE_SURFACE = {
    "l1labels": frozenset({"calendar", "md5", "md5b", "plain", "raw", "sealed"}),
    "l1tk2": frozenset({"add", "values"}),
    "wm2ref16": frozenset({"Ref", "_day"}),
    "wm2tk_l1": frozenset({"Tick", "_nm"}),
    "l1bz": frozenset({"Earnings", "News", "_list", "day_add", "files", "files_from", "ny_s",
                       "refuse", "rows", "settings_ok", "span_sealed", "utc_s"}),
    "l1fl": frozenset({"Fl", "day_add", "dnum", "refuse"}),
    "l1feat": frozenset({"assemble", "cut_inputs", "groups", "ident", "ref_view", "refuse",
                         "settings"}),
}
# Research-only modules a builder file names in an import (wm2 / wm2_l1: the label table code,
# refshard2b: the reference sharder); with the other pinned files' names they refuse in a context.
RESEARCH_IMPORTS = ("refshard2b", "wm2", "wm2_l1")
# The live date rule, the one declared replacement in the pinned l1labels.py: (name, exact old
# text, new text with the live day). Applied once per context; the text with the placeholder is
# checked against L1LABELS_DATE_TEMPLATE_MD5 before the day is put in.
DATE_RULE = ("the research archive's last open day becomes the live trading day: every day "
             "after it stays sealed", "HARD_LAST = '2026-11-19'", "HARD_LAST = '{day}'")
L1LABELS_DATE_TEMPLATE_MD5 = "e9403fc7c71e5a8d6edca3f401f740dc"
LIVE_MODULE_PREFIX = "tickr_live_"  # private module names, never registered in sys.modules
# The table code in a context, in load order (each imports only modules before it), and what stays
# callable of each: only the feature-only `day2` (its bar reader `load` is handed the bars).
TABLE = ("wide", "wm", "wm2ref", "wm2tk", "wm2_l1f")
TABLE_SURFACE = {"wide": frozenset(), "wm": frozenset(), "wm2ref": frozenset(),
                 "wm2tk": frozenset(), "wm2_l1f": frozenset({"day2"})}
# The feature-only replacement in the pinned wm2_l1f.py day2: (name, exact old text, new text).
OUTCOME_RULE = (
    ("the training's row mask without the label simulation (the mask never read a label)",
     "lab = labels(C, L, O, ri.astype(np.int64), rq.astype(np.int64), rf, TR, HZ); "
     "g0_ = ~np.isnan(lab[:, 0]); g = rq <= rf - 2; lab[~g0_, 2] = rq[~g0_]; ri = ri[g]; "
     "rq = rq[g]; rf = rf[g]; lab = lab[g]; s = SS[rq]",
     "g = rq <= rf - 2; ri = ri[g]; rq = rq[g]; rf = rf[g]; s = SS[rq]"),
    ("no label columns",
     "    for k_, nm in enumerate(LAB): t[nm] = lab[:, k_] if nm in ('pb', 'ps') else "
     "(lab[:, k_].astype(np.int16) if nm == 'nq' else lab[:, k_].astype(np.float32))\n",
     ""),
    ("no barrier, highest-high, excess-return or label-rank columns",
     "    lb2 = barriers(C, L, ri.astype(np.int64), rq.astype(np.int64), rf.astype(np.int64), "
     "lab[:, 2].astype(np.int64), lab[:, 0], np.asarray(N['vu'], np.float64), BAR)\n"
     "    for k_, nm in enumerate(TG2[:len(BAR)]): t[nm] = lb2[:, k_].astype(np.float32)\n"
     "    mh = maxhigh(H, C, ri.astype(np.int64), rq.astype(np.int64), rf.astype(np.int64), "
     "lab[:, 2].astype(np.int64), lab[:, 0], HZ); spy = 1 + MK['SPY'][0]\n"
     "    for k_, hz_ in enumerate(HZ): t['u%d' % hz_] = mh[:, k_].astype(np.float32)\n"
     "    with np.errstate(divide='ignore', invalid='ignore'):\n"
     "        for hz_ in (10, 30): t['x%d' % hz_] = (t['h%d' % hz_].values - "
     "(spy[np.minimum(rq + hz_, rf)] / spy[rq] - 1)).astype(np.float32)\n"
     "    for nm, src_ in RK: t[nm] = t.groupby('m')[src_].rank(pct=True)"
     ".where(t.groupby('m')[src_].transform('count') >= RKMIN).astype(np.float32)\n",
     ""),
)
WM2_L1F_FEATURE_ONLY_MD5 = "d1dcb116efadef87553e4b765b756207"
# The sessions of every training day as table minutes from 04:00 New York: premarket open,
# regular open, regular close, extended close (04:00, 09:30, 16:00, 20:00; the pinned day2's
# 330:720 regular slices, its NM = 960 and wide.SESS). The pinned table has no early close.
TRAINED_SESSION_MINUTES = (0, 330, 720, 960)
# What a bundle says about half days (Codex 109 point 3): `full_days` refuses them (the default:
# the training has none); `half_days_until_close` serves minutes before the actual regular close
# with the pinned sessions unchanged.
FULL_DAYS = "full_days"
HALF_DAYS_UNTIL_CLOSE = "half_days_until_close"
SESSION_POLICIES = (FULL_DAYS, HALF_DAYS_UNTIL_CLOSE)
# Where a live record carries its actual receipt (Codex 109 point 3): a column of UTC nanoseconds
# on tick and bar rows; a field of UTC text on JSON records and the last column of the fl csv.
RECEIPT_COLUMN = "received_ns"
RECEIPT_FIELD = "received"

_KEY = object()  # only live_context builds a LiveContext
_LIVE_LOCK = threading.Lock()
_LIVE: dict[str, Any] = {}


@dataclass(frozen=True)
class FullInputs:
    """The day's non-bar inputs in the training's file formats (see the module docstring)."""

    tick_file: Path
    reference: Path
    benzinga: Path
    fl_facts: Path


@contextlib.contextmanager
def _training_refusals() -> Any:
    """The training code refuses with SystemExit('REFUSED: <cause>...'): one typed refusal."""
    try:
        yield
    except SystemExit as exc:
        raise ModelRefused("feature_code_refused", str(exc)) from None


def full_digests() -> dict[str, str]:
    """md5 of every pinned feature-builder file as it is on disk now."""
    return {name: base._md5(base.PINNED_DIR / name) for name in sorted(FULL_PINS)}


def _pinned_texts() -> dict[str, str]:
    """Every pinned builder and table file read once; its md5 is checked on the bytes that are
    run."""
    pins = {**FULL_PINS, **base.PINS}
    got = {name: (base.PINNED_DIR / name).read_bytes() for name in pins}
    differ = sorted(n for n, b in got.items() if hashlib.md5(b).hexdigest() != pins[n])
    if differ:
        raise ModelRefused("table_code_md5", ", ".join(differ))
    return {name: b.decode("utf-8") for name, b in got.items()}


def _replaced(src: str, patch: tuple) -> str:
    """`src` with every declared replacement applied, each old text exactly once."""
    new = src
    for name, old, replacement in patch:
        if src.count(old) != 1 or new.count(old) != 1:
            raise ModelRefused("table_code_replacement", name)
        new = new.replace(old, replacement)
    return new


def dated_text(src: str, day: str | None) -> str:
    """The pinned l1labels.py text for the live day `day` (DATE_RULE, md5-checked); `None`
    keeps the pinned date (used only to read the group map, never with inputs)."""
    name, _, placeholder = DATE_RULE
    template = _replaced(src, (DATE_RULE,))
    if hashlib.md5(template.encode("utf-8")).hexdigest() != L1LABELS_DATE_TEMPLATE_MD5:
        raise ModelRefused("table_code_replacement", name)
    if day is None:
        return src
    return template.replace(placeholder, placeholder.format(day=day))


def feature_only_text(src: str) -> str:
    """The pinned wm2_l1f.py text with OUTCOME_RULE applied (md5-checked): `day2` builds the
    table rows and their features and evaluates no outcome."""
    text = _replaced(src, OUTCOME_RULE)
    if hashlib.md5(text.encode("utf-8")).hexdigest() != WM2_L1F_FEATURE_ONLY_MD5:
        raise ModelRefused("table_code_replacement", "wm2_l1f.py feature-only day2")
    return text


def _importer(modules: dict[str, ModuleType]) -> Any:
    """`__import__` of a context's modules: names of `modules` resolve to the context's own
    modules, another pinned or research-only name refuses, everything else imports as usual."""
    pinned = {p.stem for p in base.PINNED_DIR.glob("*.py")} | set(RESEARCH_IMPORTS)
    real = builtins.__import__

    def imp(name: str, globals: Any = None, locals: Any = None, fromlist: Any = (),
            level: int = 0) -> Any:
        top = name.partition(".")[0]
        if level == 0 and top in modules:
            return modules[top]
        if level == 0 and top in pinned:
            raise ModelRefused("outside_live_context", f"import {name}")
        return real(name, globals, locals, fromlist, level)

    return imp


def _refusal(stem: str, name: str) -> Any:
    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise ModelRefused("outside_live_context", f"{stem}.{name}")

    refused.__name__ = name
    return refused


def _private_module(stem: str, text: str, modules: dict[str, ModuleType],
                    keep: frozenset[str], imported: bool = False) -> ModuleType:
    """`text` run as a module object of its own (never in sys.modules), then everything outside
    `keep` replaced by a refusal: the module's own functions and classes and, with `imported`,
    also those it took from another private module (`from wm import labels`)."""
    path = base.PINNED_DIR / f"{stem}.py"
    module = ModuleType(LIVE_MODULE_PREFIX + stem)
    module.__file__ = str(path)  # l1labels.calendar reads the pinned calendar beside it
    module.__dict__["__builtins__"] = {**vars(builtins), "__import__": _importer(modules)}
    with _training_refusals():
        # the pinned bytes, md5-checked by _pinned_texts (with their declared replacements)
        exec(compile(text, str(path), "exec"), module.__dict__)
    if not keep <= set(vars(module)):
        raise ModelRefused("table_code_shadowed", f"{stem}: {sorted(keep - set(vars(module)))}")
    for name, obj in list(vars(module).items()):
        own = getattr(obj, "py_func", obj)  # a numba dispatcher wraps its Python function
        where = getattr(own, "__module__", None) or ""
        mine = where == module.__name__ or (imported and where.startswith(LIVE_MODULE_PREFIX))
        if name not in keep and (inspect.isfunction(own) or inspect.isclass(own)) and mine:
            setattr(module, name, _refusal(stem, name))
    return module


def _table(texts: dict[str, str]) -> dict[str, ModuleType]:
    """The pinned table code as private modules: the feature-only `day2` (OUTCOME_RULE), every
    other function a refusal, the training's settings and no reference or tick columns (as
    `tickr.model.features.table_code` checks for the research import)."""
    base.refuse_table_variables()
    modules: dict[str, ModuleType] = {}
    for stem in TABLE:
        text = texts[f"{stem}.py"]
        if stem == "wm2_l1f":
            text = feature_only_text(text)
        modules[stem] = _private_module(stem, text, modules, TABLE_SURFACE[stem], imported=True)
    w = modules["wm2_l1f"]
    w._CAP = {}
    w.load = _refusal("wm2_l1f", "load")
    for name, value in base.TABLE_DEFAULTS.items():
        if getattr(w, name) != value:
            raise ModelRefused("table_code_settings", f"{name} = {getattr(w, name)}")
    if w.REF is not None or w.TK is not None or w.TSPAN is not None:
        raise ModelRefused("table_code_settings", "reference, tick or TSPAN columns are on")
    return modules


def _builder(day: str | None) -> SimpleNamespace:
    """The pinned feature builder and table code loaded privately for `day` after every check:
    every file of FULL_PINS and PINS, the feature-only table code, wm2tk_l1.py = TKPATCH applied
    to wm2tk.py, the builder's settings read by its own reader, and the group map equal to the
    training's."""
    texts = _pinned_texts()
    table = _table(texts)
    w = table["wm2_l1f"]
    modules: dict[str, ModuleType] = {}
    for stem in BUILDER:
        text = texts[f"{stem}.py"]
        if stem == "l1labels":
            text = dated_text(text, day)
        modules[stem] = _private_module(stem, text, modules, LIVE_SURFACE[stem])
    labels, feat = modules["l1labels"], modules["l1feat"]
    _extend_calendar(labels)
    if texts["wm2tk_l1.py"] != _replaced(texts["wm2tk.py"], feat.TKPATCH):
        raise ModelRefused("table_code_replacement", "wm2tk_l1.py is not TKPATCH on wm2tk.py")
    if any(getattr(modules[s], "L", labels) is not labels for s in ("l1feat", "l1bz", "l1fl")):
        raise ModelRefused("table_code_shadowed", "a module holds another l1labels")
    if (feat.PIN["wm2ref16.py"] != FULL_PINS["wm2ref16.py"]
            or any(labels.PIN[k] != base.PINS[k] for k in ("wide.py", "wm.py", "wm2tk.py"))):
        raise ModelRefused("table_code_md5", "the builder's own pins differ from the copies")
    with _training_refusals():
        settings = feat.settings(str(base.PINNED_DIR / SETTINGS_FILE))
        groups, _ = feat.groups(w, modules["wm2ref16"], modules["wm2tk_l1"])
    ordered = {g: groups[g] for g in COMPUTABLE_GROUPS if g in groups}
    if sorted(groups) != sorted(COMPUTABLE_GROUPS) or _group_digest(ordered) != GROUP_MAP_SHA256:
        raise ModelRefused("feature_group_map", "the pinned builder's groups differ")
    names = [c for cs in ordered.values() for c in cs]
    if hashlib.md5("\n".join(names).encode("utf-8")).hexdigest() != FEATURE_LIST_MD5:
        raise ModelRefused("feature_group_map", "the feature list digest differs")
    return SimpleNamespace(feat=feat, labels=labels, bz=modules["l1bz"], fl=modules["l1fl"],
                           tk2=modules["l1tk2"], ref=modules["wm2ref16"], tk=modules["wm2tk_l1"],
                           w=w, table=SimpleNamespace(**table), settings=settings,
                           groups=ordered, names=names)


def _extend_calendar(labels: ModuleType) -> None:
    """The private `l1labels.calendar()` (THE CALENDAR): the pinned function, which still reads
    and md5-checks the training file, followed by the NYSE extension once. With a file of its
    own (`p`, the fixtures' argument) it is the pinned function unchanged."""
    pinned = labels.calendar

    def calendar(p: str | None = None, pin: str = labels.CAL_MD5) -> list[str]:
        days = pinned(p, pin)
        return calendar_ext.extend(days) if p is None else days

    labels.calendar = calendar


def _check_extension_day(day: str, market_day: MarketDay) -> None:
    """For a day the extension covers, the bot calendar's half day and regular close agree with
    the extension's early closes (THE CALENDAR)."""
    ext = calendar_ext.load()
    if day not in ext.days:
        return
    early = ext.early_close.get(day)
    close = market_day.regular_close.strftime("%H:%M")
    if (early is not None) != market_day.half_day or (early is not None and early != close):
        raise ModelRefused("calendar_disagrees", f"{day}: extension early close {early}, bot "
                           f"calendar half day {market_day.half_day} closing {close}")


def _group_digest(groups: dict[str, list[str]]) -> str:
    text = json.dumps([[g, n] for g, n in groups.items()], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _group_code() -> SimpleNamespace:
    with _LIVE_LOCK:
        if "groups" not in _LIVE:
            _LIVE["groups"] = _builder(None)
        return _LIVE["groups"]


def full_groups() -> dict[str, list[str]]:
    """The ten groups and their features in the training's order."""
    return {g: list(cs) for g, cs in _group_code().groups.items()}


def full_features() -> list[str]:
    """The 191 features in the training's order (feature-schema-191.csv)."""
    return list(_group_code().names)


def _session_minutes(market_day: MarketDay) -> tuple[int, ...]:
    """The day's premarket open, regular open, regular close and extended close as table
    minutes (from 04:00 New York)."""
    times = (market_day.premarket_open, market_day.regular_open, market_day.regular_close,
             market_day.extended_close)
    return tuple(t.hour * 60 + t.minute - base.DAY_START_MINUTE for t in times)


def live_context(market_day: MarketDay, session_policy: str = FULL_DAYS) -> LiveContext:
    """The live date context of the trading day `market_day` (from the bot's session calendar):
    the builder loaded privately with its date boundary at that day, served up to the day's
    actual close under the bundle's `session_policy` (see SESSIONS). One context is kept, the
    latest day's."""
    if not isinstance(market_day, MarketDay):
        raise ModelRefused("live_day_not_from_calendar", type(market_day).__name__)
    if session_policy not in SESSION_POLICIES:
        raise ModelRefused("session_policy_unknown", repr(session_policy))
    day = market_day.ny_date.isoformat()
    _check_extension_day(day, market_day)
    hours = _session_minutes(market_day)
    if market_day.half_day:
        if session_policy != HALF_DAYS_UNTIL_CLOSE:
            raise ModelRefused("half_day_outside_session_policy", f"{day}: {session_policy}")
        if (hours[:2] != TRAINED_SESSION_MINUTES[:2]
                or not hours[1] < hours[2] < TRAINED_SESSION_MINUTES[2]):
            raise ModelRefused("session_hours_differ", f"{day}: {hours}")
        end = hours[2]
    elif hours != TRAINED_SESSION_MINUTES:
        raise ModelRefused("session_hours_differ", f"{day}: {hours}")
    else:
        end = hours[3]
    key = f"{day} {session_policy}"
    with _LIVE_LOCK:
        ctx = _LIVE.get(key)
        if ctx is not None:
            return ctx
        code = _builder(day)
        seal = code.labels.SEAL
        if seal[0] <= day <= seal[1]:
            raise ModelRefused("sealed_day", day)
        with _training_refusals():
            calendar = code.labels.calendar()
        if day not in calendar:
            raise ModelRefused("unknown_trading_day", day)
        ctx = LiveContext(_KEY, day, code, end, market_day, session_policy)
        for old in [k for k in _LIVE if k != "groups"]:
            del _LIVE[old]
        _LIVE[key] = ctx
        return ctx


class LiveContext:
    """The pinned feature builder bounded by one live trading day (see the module docstring).
    Built only by `live_context`. `end_minute`: the first table minute it does not serve (the
    actual extended close of a full day, the actual regular close of a half day).
    `market_day` / `session_policy`: what `live_context` was given (a worker process builds the
    same context again from them)."""

    def __init__(self, key: object, day: str, code: SimpleNamespace, end_minute: int,
                 market_day: MarketDay | None = None, session_policy: str = FULL_DAYS) -> None:
        if key is not _KEY:
            raise ModelRefused("live_context_outside_bot", "build it with live_context()")
        self.day = day
        self.code = code
        self.end_minute = end_minute
        self.market_day = market_day
        self.session_policy = session_policy

    @property
    def names(self) -> list[str]:
        return list(self.code.names)

    def mark(self, minute: int) -> datetime.datetime:
        """The event-time mark of `minute`: the end of that minute, aware UTC."""
        start = datetime.datetime.combine(datetime.date.fromisoformat(self.day),
                                          datetime.time(base.DAY_START_MINUTE // 60), NEW_YORK)
        return (start + datetime.timedelta(minutes=minute + 1)).astimezone(datetime.UTC)

    def table(self, frame: Any, state: base.DayState, types: set[str] | None) -> Any:
        """The day's table from `frame` by the context's feature-only `day2`; no outcome is
        evaluated."""
        w = self.code.w
        with base._LOCK:
            w.load = lambda _path: frame  # the declared replacement of the bar read
            w._CAP.clear()
            try:
                return w.day2(self.day, state.prev, types, state.hist, state.mprev, want=True)[0]
            finally:
                w.load = _refusal("wm2_l1f", "load")
                w._CAP.clear()

    def _bounds(self, minute: int, cutoff: datetime.datetime) -> int:
        """The checks every minute of the context passes first: before the day's close, a
        cutoff that is aware and at or after the minute's mark. Returns the cutoff in UTC ns."""
        if minute >= self.end_minute:
            raise ModelRefused("after_exchange_close", f"minute {minute}, close {self.end_minute}")
        if cutoff.tzinfo is None:
            raise ModelRefused("cutoff_not_aware", cutoff.isoformat())
        if cutoff < self.mark(minute):
            raise ModelRefused("cutoff_before_mark", f"{cutoff.isoformat()}, minute {minute}")
        return _ns(cutoff)

    def received_bars(self, rows: Any, minute: int, cutoff: datetime.datetime) -> Any:
        """The day's bar rows received by `cutoff`, without their receipt column, after the
        minute's bounds (`_bounds`)."""
        return _received_bars(rows, self._bounds(minute, cutoff))

    def table_rows(self, rows: Any, minute: int, state: base.DayState, types: set[str] | None,
                   cutoff: datetime.datetime) -> Any:
        """The table rows of `minute` (every column of the feature-only `day2` table, one row
        per symbol the training's row rule keeps: the minute's cohort) from the day's bars
        received by `cutoff` (a bar after `minute` refuses); None when the table has no row at
        all."""
        frame = base.bars_frame(self.received_bars(rows, minute, cutoff), self.day,
                                self.code.w.NM)
        if (frame.m > minute).any():
            raise ModelRefused("bar_after_minute", f"minute {minute}")
        table = self.table(frame, state, types)
        if table is None:
            return None
        return table.loc[table.m == minute].reset_index(drop=True)

    def assemble_rows(self, t: Any, minute: int, state: base.DayState, inputs: FullInputs,
                      cutoff: datetime.datetime) -> Any:
        """`minute_rows` from the table rows `t` of `minute` (`table_rows`, or rows equal to
        them): the training's `cut_inputs` at the minute on the inputs received by `cutoff`
        (anything it would cut refuses, see `_refuse_cut`, also when `t` has no row) and its
        `assemble` on `t`."""
        import numpy as np
        import pandas as pd

        code, day = self.code, self.day
        limit = self._bounds(minute, cutoff)
        if t is not None and len(t) and (t.m != minute).any():
            raise ModelRefused("table_rows_of_another_minute", f"minute {minute}")
        cols = ["sym", *code.names]
        with base._LOCK:
            work = tempfile.mkdtemp(prefix="tickr-l1feat-")
            try:
                held = _as_received(code, inputs, limit, Path(work) / "received")
                with _training_refusals(), contextlib.redirect_stdout(io.StringIO()):
                    cut = code.feat.cut_inputs(day, minute, str(held.tick_file),
                                               str(held.benzinga), work, str(held.reference))
                _refuse_cut(code, held, cut, day)
                if t is None or not len(t):
                    return pd.DataFrame(columns=cols)
                t = t.reset_index(drop=True)
                prev = np.array([state.prev.get(s, np.nan) for s in t["sym"].values],
                                np.float64)
                with _training_refusals(), contextlib.redirect_stdout(io.StringIO()):
                    out, _ = code.feat.assemble(t, prev, day, cut[0], cut[2], cut[1],
                                                str(held.fl_facts), code.settings, work,
                                                code.w, code.ref, code.tk)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        return out[cols].reset_index(drop=True)

    def minute_rows(self, rows: Any, minute: int, state: base.DayState,
                    types: set[str] | None, inputs: FullInputs,
                    cutoff: datetime.datetime) -> Any:
        """The rows of `minute` of the context's day with all 191 features, from what was
        received by `cutoff` (aware; at or after the minute's mark): the table rows of the
        minute from the day's bars so far (`table_rows`), the training's `cut_inputs` at the
        minute (anything it would cut refuses, see `_refuse_cut`) and its `assemble` on those
        rows (`assemble_rows`). Returns `sym` and the 191 features in the training's order and
        stored types (m int16, sess int8, the others float32); an empty frame when no row."""
        t = self.table_rows(rows, minute, state, types, cutoff)
        return self.assemble_rows(t, minute, state, inputs, cutoff)


_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


def _ns(instant: datetime.datetime) -> int:
    return (instant - _EPOCH) // datetime.timedelta(microseconds=1) * 1000


def _receipt(record: dict, source: str) -> int:
    """The receipt of a JSON or csv record as UTC nanoseconds; none or unreadable refuses."""
    value = record.get(RECEIPT_FIELD)
    try:
        instant = datetime.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        instant = None
    if instant is None or instant.tzinfo is None:
        raise ModelRefused("receipt_missing", source)
    return _ns(instant)


def _received_bars(rows: Any, limit: int) -> Any:
    """The bar rows received by the cutoff, without their receipt column."""
    if RECEIPT_COLUMN not in rows.columns or rows[RECEIPT_COLUMN].isna().any():
        raise ModelRefused("receipt_missing", "bars")
    return rows.loc[rows[RECEIPT_COLUMN] <= limit].drop(columns=[RECEIPT_COLUMN])


def _held_json(lines: Any, limit: int, source: str) -> list[str]:
    """JSON records received by the cutoff, without their receipt, one text line each."""
    out = []
    for record in lines:
        if _receipt(record, source) <= limit:
            out.append(json.dumps({k: v for k, v in record.items() if k != RECEIPT_FIELD}) + "\n")
    return out


def _as_received(code: SimpleNamespace, given: FullInputs, limit: int,
                 root: Path) -> FullInputs:
    """The captured inputs reduced to what was received by the cutoff `limit` (UTC ns), in the
    training's formats (receipts removed). A record received later is left out even when its
    event time is earlier."""
    import duckdb

    root.mkdir()
    tick = root / "tk.parquet"
    con = duckdb.connect()  # its own connection: the training's cut runs on the default one
    try:
        src = str(given.tick_file).replace("'", "''")
        names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}')")
                 .fetchall()]
        if RECEIPT_COLUMN not in names or con.execute(
                f"SELECT count(*) FROM read_parquet('{src}') WHERE {RECEIPT_COLUMN} IS NULL"
        ).fetchall()[0][0]:
            raise ModelRefused("receipt_missing", "ticks")
        con.execute(f"COPY (SELECT * EXCLUDE ({RECEIPT_COLUMN}) FROM read_parquet('{src}') "
                    f"WHERE {RECEIPT_COLUMN} <= {int(limit)}) TO '{tick}' (FORMAT parquet)")
    finally:
        con.close()
    bz = root / "bz"
    bz.mkdir()
    for path in sorted(Path(given.benzinga).glob("*.jsonl.gz")):
        with _training_refusals():
            kept = _held_json(code.bz.rows(str(path)), limit, f"benzinga {path.name}")
        with gzip.open(bz / path.name, "wt", encoding="utf-8") as f:
            f.write("".join(kept))
    ref = root / "ref"
    ref.mkdir()
    manifest_path = Path(given.reference) / "manifest.json"
    if not manifest_path.is_file():
        raise ModelRefused("feature_code_refused", "REFUSED: reference_manifest_missing")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for name, ident in sorted(manifest["files"].items()):
        path = Path(given.reference) / name
        if not path.is_file():
            continue  # the training's own ref_view refuses a needed file that is missing
        if code.feat.ident(str(path)) != ident:
            raise ModelRefused("feature_code_refused", f"REFUSED: reference_file_changed: {name}")
        with path.open(encoding="utf-8") as f:
            lines = (json.loads(x) for x in f if x.strip())
            (ref / name).write_text("".join(_held_json(lines, limit, f"reference {name}")),
                                    "utf-8")
        manifest["files"][name] = code.feat.ident(str(ref / name))
    (ref / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), "utf-8")
    facts = root / "fl.csv"
    with open(given.fl_facts, newline="", encoding="utf-8") as f, \
            facts.open("w", newline="", encoding="utf-8") as g:
        reader, writer = csv.reader(f), csv.writer(g, lineterminator="\n")
        head = next(reader, [])
        if not head or head[-1] != RECEIPT_FIELD:
            raise ModelRefused("receipt_missing", "fl facts")
        writer.writerow(head[:-1])
        for row in reader:
            if _receipt(dict(zip(head, row, strict=True)), "fl facts") <= limit:
                writer.writerow(row[:-1])
    return FullInputs(tick, ref, bz, facts)


def _rows_in(con: Any, path: str) -> int:
    return con.execute("SELECT count(*) FROM read_parquet(?)", [path]).fetchall()[0][0]


def _scheduled(row: dict, day: str) -> bool:
    """An earnings row of the day scheduled for later in the day: dated `day`, no actual EPS
    yet (the training's cut removed it because its time lies after the mark)."""
    return row.get("date") == day and row.get("actual_eps") is None


def _refuse_cut(code: SimpleNamespace, inputs: FullInputs, cut: tuple, day: str) -> None:
    """The training's prefix rule found something to cut: the snapshot handed in was not as of
    the minute's mark. Refused (`future_input`), never cut silently. Exactly what `cut_inputs`
    removes is refused: tick rows after the minute, articles published after the mark in the
    Benzinga files that hold the day, items after the mark in the reference news month of the
    day, reports timed after the mark except a report of the day scheduled for later in the
    day (`_scheduled`: left out of this minute, admitted from the minute its time is reached).
    Whatever the training keeps at the minute (announced splits, scheduled reports in later
    files, revisions, records the builders' own as-of rules skip) passes."""
    import collections

    import duckdb

    tk_cut, bz_cut, _, dropped = cut
    con = duckdb.connect()  # its own connection: the training's cut runs on the default one
    try:
        late = _rows_in(con, str(inputs.tick_file)) - _rows_in(con, tk_cut)
    finally:
        con.close()
    if late:
        raise ModelRefused("future_input", f"ticks: {late} rows after the minute")
    for path in sorted(Path(bz_cut).iterdir()):
        if path.is_symlink():
            continue  # a file that does not hold the day: the training keeps it whole
        with _training_refusals():
            kept = collections.Counter(json.dumps(r, sort_keys=True)
                                       for r in code.bz.rows(str(path)))
            given = collections.Counter(json.dumps(r, sort_keys=True) for r in
                                        code.bz.rows(str(Path(inputs.benzinga) / path.name)))
        removed = [json.loads(r) for r in (given - kept).elements()]
        if path.name.startswith("earnings-"):
            removed = [r for r in removed if not _scheduled(r, day)]
        if removed:
            raise ModelRefused("future_input", f"benzinga {path.name}: {len(removed)} rows "
                                               "after the mark")
    if dropped:
        raise ModelRefused("future_input", f"reference news: {dropped} items after the mark")
