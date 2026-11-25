"""All 191 layer-1 features with the training's own code, from inputs in the training's formats.

Parity by construction, as in `tickr.model.features`: `pinned/` also holds byte copies of the
feature builder of set 7e (`l1feat.py` and what it imports: `l1labels.py`, `l1tk2.py`,
`l1bz.py`, `l1fl.py`, `wm2ref16.py`), `wm2tk_l1.py` (the pinned `wm2tk.py` with the builder's
declared replacement TKPATCH, checked here to be exactly the text `l1feat.modules` writes), the
pinned trading-day calendar `trading-days.txt` that `l1labels.calendar` reads beside itself, and
the builder's settings file `l1feat-settings.json` (the bz and fl definitions: windows, channel
sets, lags). Each is checked against its md5 (FULL_PINS) on the bytes that are run. Nothing is
re-implemented: `LiveContext.minute_rows` cuts the inputs with `l1feat.cut_inputs` and runs
`l1feat.assemble` on the rows of one minute, in the live date context described below.

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
  day of the pinned calendar (`unknown_trading_day`) and not a sealed one (`sealed_day`).
- The seven builder modules are executed from the checked bytes into private module objects
  whose imports of each other resolve inside the context. They are never placed in
  `sys.modules`; a research import of `l1labels` (training, parity harness, research tools)
  still gets the pinned file with 2026-11-19, and two contexts of two days share nothing.
- Inference only: of each module only the as-of feature path stays callable (LIVE_SURFACE:
  `cut_inputs`, `assemble` and what they call). Label simulation and label/return builders
  (`l1labels.core`, `run`, `build`, `frame`, `publish` ...), the files that compare with labels
  (`l1feat.keys_equal`, `day_`, `prefix`), file writers and command-line modes are replaced by a
  typed refusal (`outside_live_context`), and an import of a research-only module refuses too.
- Future-dated input refused (`future_input`), never dropped: the inputs are the snapshot as of
  the mark of the minute (end of minute m). A tick row after the minute, a Benzinga file named
  past D, an article published, a report timed or a report revision updated after the mark, a
  reference record dated after D (splits, shares, short interest), a reference news month after
  D's or an item published after the mark, an fl fact whose period end, filing or known date is
  after D: each refuses the minute with its source named. The training's own `cut_inputs` then
  runs as in training and finds nothing to cut.
- Nothing is captured here: the cut files live in a temporary folder removed after the minute.
  Inputs the bot records for live days belong to the sealed storage of the capture tools.

`LiveContext(...)` itself refuses (`live_context_outside_bot`) unless `live_context` builds it.
The calendar ends 2026-12-31: a later day refuses (`unknown_trading_day`) until a new pinned
calendar arrives.
"""

from __future__ import annotations

import builtins
import contextlib
import csv
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from tickr.calendar import MarketDay
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
# The date field of each dated reference file (as wm2ref16.Ref reads it) and of the fl facts.
REFERENCE_DATES = (("splits.jsonl", "execution_date"), ("shares-outstanding-asof", "date"),
                   ("short-interest", "settlement_date"))
FACT_DATES = ("end", "filed", "known")
_NEWS_MONTH = re.compile(r"news-([0-9]{4}-[0-9]{2})\.jsonl")
_BZ_FILE = re.compile(r"(news|earnings)-([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{4}-[0-9]{2}-[0-9]{2})"
                      r"\.jsonl\.gz")
_MANIFEST = "manifest.json"

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
    """Every pinned builder file read once; its md5 is checked on the bytes that are run."""
    got = {name: (base.PINNED_DIR / name).read_bytes() for name in FULL_PINS}
    differ = sorted(n for n, b in got.items() if hashlib.md5(b).hexdigest() != FULL_PINS[n])
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


def _importer(modules: dict[str, ModuleType]) -> Any:
    """`__import__` of a context's modules: builder names resolve to the context's own modules,
    another pinned or research-only name refuses, everything else imports as usual."""
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


def _private_module(stem: str, text: str, modules: dict[str, ModuleType]) -> ModuleType:
    """`text` run as a module object of its own (never in sys.modules), then everything outside
    its LIVE_SURFACE replaced by a refusal."""
    path = base.PINNED_DIR / f"{stem}.py"
    module = ModuleType(LIVE_MODULE_PREFIX + stem)
    module.__file__ = str(path)  # l1labels.calendar reads the pinned calendar beside it
    module.__dict__["__builtins__"] = {**vars(builtins), "__import__": _importer(modules)}
    with _training_refusals():
        # the pinned bytes, md5-checked by _pinned_texts (l1labels with DATE_RULE)
        exec(compile(text, str(path), "exec"), module.__dict__)
    keep = LIVE_SURFACE[stem]
    if not keep <= set(vars(module)):
        raise ModelRefused("table_code_shadowed", f"{stem}: {sorted(keep - set(vars(module)))}")
    for name, obj in list(vars(module).items()):
        own = getattr(obj, "py_func", obj)  # a numba dispatcher wraps its Python function
        if (name not in keep and (inspect.isfunction(own) or inspect.isclass(own))
                and own.__module__ == module.__name__):
            setattr(module, name, _refusal(stem, name))
    return module


def _builder(day: str | None) -> SimpleNamespace:
    """The pinned feature builder loaded privately for `day` after every check: the table code's
    checks (`tickr.model.features.table_code`), every file of FULL_PINS, wm2tk_l1.py = TKPATCH
    applied to wm2tk.py, the builder's settings read by its own reader, and the group map equal to
    the training's."""
    w = base.table_code()
    texts = _pinned_texts()
    modules: dict[str, ModuleType] = {}
    for stem in BUILDER:
        text = texts[f"{stem}.py"]
        if stem == "l1labels":
            text = dated_text(text, day)
        modules[stem] = _private_module(stem, text, modules)
    labels, feat = modules["l1labels"], modules["l1feat"]
    tk_src = (base.PINNED_DIR / "wm2tk.py").read_text("utf-8")
    if texts["wm2tk_l1.py"] != _replaced(tk_src, feat.TKPATCH):
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
                           w=w, settings=settings, groups=ordered, names=names)


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


def live_context(market_day: MarketDay) -> LiveContext:
    """The live date context of the trading day `market_day` (from the bot's session calendar):
    the builder loaded privately with its date boundary at that day. One context is kept, the
    latest day's."""
    if not isinstance(market_day, MarketDay):
        raise ModelRefused("live_day_not_from_calendar", type(market_day).__name__)
    day = market_day.ny_date.isoformat()
    with _LIVE_LOCK:
        ctx = _LIVE.get(day)
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
        ctx = LiveContext(_KEY, day, code)
        for old in [k for k in _LIVE if k != "groups"]:
            del _LIVE[old]
        _LIVE[day] = ctx
        return ctx


class LiveContext:
    """The pinned feature builder bounded by one live trading day (see the module docstring).
    Built only by `live_context`."""

    def __init__(self, key: object, day: str, code: SimpleNamespace) -> None:
        if key is not _KEY:
            raise ModelRefused("live_context_outside_bot", "build it with live_context()")
        self.day = day
        self.code = code

    @property
    def names(self) -> list[str]:
        return list(self.code.names)

    def minute_rows(self, rows: Any, minute: int, state: base.DayState,
                    types: set[str] | None, inputs: FullInputs) -> Any:
        """The rows of `minute` of the context's day with all 191 features: the table rows of
        the minute from the day's bars so far (as `minute_rows`), the inputs checked to hold
        nothing after the minute's mark, then the training's `cut_inputs` at the minute and its
        `assemble` on those rows. Returns `sym` and the 191 features in the training's order and
        stored types (m int16, sess int8, the others float32); an empty frame when no row."""
        import numpy as np
        import pandas as pd

        code, day = self.code, self.day
        frame = base.bars_frame(rows, day)
        if (frame.m > minute).any():
            raise ModelRefused("bar_after_minute", f"minute {minute}")
        _refuse_future(code, day, minute, inputs)
        table = base._day2(frame, day, state, types, want=True)[0]
        cols = ["sym", *code.names]
        if table is None or not (table.m == minute).any():
            return pd.DataFrame(columns=cols)
        t = table.loc[table.m == minute].reset_index(drop=True)
        prev = np.array([state.prev.get(s, np.nan) for s in t["sym"].values], np.float64)
        with base._LOCK:
            work = tempfile.mkdtemp(prefix="tickr-l1feat-")
            try:
                with _training_refusals(), contextlib.redirect_stdout(io.StringIO()):
                    cut = code.feat.cut_inputs(day, minute, str(inputs.tick_file),
                                               str(inputs.benzinga), work,
                                               str(inputs.reference))
                    out, _ = code.feat.assemble(t, prev, day, cut[0], cut[2], cut[1],
                                                str(inputs.fl_facts), code.settings, work,
                                                code.w, code.ref, code.tk)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        return out[cols].reset_index(drop=True)


def _future(source: str, what: str) -> ModelRefused:
    return ModelRefused("future_input", f"{source}: {what}")


def _refuse_future(code: SimpleNamespace, day: str, minute: int, inputs: FullInputs) -> None:
    """Refuse inputs that hold anything after the mark of `minute` (the end of the minute, New
    York) or a record dated after `day`: the snapshot handed in must be as of the mark."""
    import duckdb

    bz = code.bz
    hours, minutes = divmod(base.DAY_START_MINUTE, 60)
    mark = bz.ny_s(day, hours, minutes) + (minute + 1) * 60
    try:
        _refuse_future_files(code, day, minute, mark, inputs)
    except (ValueError, KeyError, OSError, duckdb.Error) as exc:
        raise ModelRefused("input_unreadable", type(exc).__name__) from None


def _refuse_future_files(code: SimpleNamespace, day: str, minute: int, mark: int,
                         inputs: FullInputs) -> None:
    import duckdb

    bz = code.bz
    with _training_refusals():
        con = duckdb.connect()  # its own connection: the training's cut runs on the default one
        try:
            late = con.execute("SELECT count(*) FROM read_parquet(?) WHERE m > ?",
                               [str(inputs.tick_file), code.tk2.M0 + minute]).fetchall()[0][0]
        finally:
            con.close()
        if late:
            raise _future("ticks", f"{late} rows after minute {minute}")
        for name in sorted(os.listdir(inputs.benzinga)):
            got = _BZ_FILE.fullmatch(name)
            if got is None:
                continue  # the training's own file checks refuse a foreign name
            kind, first, last = got.groups()
            if first > day or last > day:
                raise _future("benzinga", f"{name} is named past {day}")
            for j in bz.rows(str(Path(inputs.benzinga) / name)):
                if kind == "news":
                    stamps = [bz.utc_s(j["published"])] if j.get("published") else []
                else:
                    stamps = [bz.utc_s(j["last_updated"])] if j.get("last_updated") else []
                    if j.get("date") and j.get("time"):
                        hms = (int(x) for x in j["time"].split(":"))
                        stamps.append(bz.ny_s(j["date"], *hms))
                if any(s > mark for s in stamps):
                    raise _future("benzinga", f"{kind} {j.get('benzinga_id')} after the mark")
        _refuse_future_reference(bz, day, mark, Path(inputs.reference))
        with open(inputs.fl_facts, newline="") as f:
            for row in csv.DictReader(f):
                late_fields = [k for k in FACT_DATES if (row.get(k) or "") > day]
                if late_fields:
                    raise _future("fl facts", f"{row.get('ticker')} {late_fields[0]} after {day}")


def _refuse_future_reference(bz: ModuleType, day: str, mark: int, folder: Path) -> None:
    for path in sorted(folder.iterdir()):
        name = path.name
        if name == _MANIFEST:
            continue
        month = _NEWS_MONTH.fullmatch(name)
        field = next((k for prefix, k in REFERENCE_DATES if name.startswith(prefix)), None)
        if month is None and field is None:
            continue
        if month is not None and month.group(1) > day[:7]:
            raise _future("reference", f"{name} is a month after {day}")
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                j = json.loads(line)
                if month is not None and j.get("published_utc"):
                    if bz.utc_s(j["published_utc"]) > mark:
                        raise _future("reference", f"news {j.get('id')} after the mark")
                elif field is not None and (j.get(field) or "") > day:
                    raise _future("reference", f"{name}: {j.get('ticker')} {field} after {day}")
