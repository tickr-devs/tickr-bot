"""Replay parity: the bot's feature path replayed on one open historical day, minute by minute,
against the training's own feature table of that day (file-to-file parity; actual receipt-time
parity needs a separate authorized capture, codex-108 point 9).

The tool for "parity on a real minute": no live feed, only the day's files. It is run by the
owner (Mac or AWS) on one open day, e.g. the open archive day 2026-11-17 in the research
bucket's layout, copied to a local folder:

    python -m tickr.model.parity --day 2026-11-17 --folder <local copy> --types <types.csv> \
        --reference <prepared reference folder> --benzinga <Benzinga folder> --fl <facts csv> \
        --out <report folder> [--minutes HH:MM ... | --all-minutes] [--mode plain|marker|both|all]
        [--ticks-only] [--rtol R] [--atol A] [--engine full-prefix|incremental]
        [--verify-full-prefix N] [--table-workers K] [--workers W] [--resume] [--rebuild-report]
        [--work-dir W] [--config bot.toml] [--disk-reserve-gb G] [--memory-reserve-gb G]
        [--duckdb-memory-gb G]

INPUTS. `--folder` gives the defaults of the research layout (each may be named instead):
`--bars` = <folder>/minute_aggs (minute_aggs day files `YYYY-MM-DD.csv.gz`: the day and the 20
trading sessions before it); `--table` = the one file <folder>/l1/feat/<pin>/tables/YYYY/<day>.
parquet (the training's feature table: date, sym, m, sess and the 191 features); `--tk61` =
<folder>/tk61/YYYY/<day>.parquet (the training's v11 tick table of the whole day, full market);
`--trades` / `--quotes` = <folder>/tickraw-audit/YYYY/<day>-trades.parquet / -quotes.parquet.
`--types`: the training's types file (`-` = no type filter); its md5 is reported beside the
training's pin. `--reference`, `--benzinga`, `--fl`: the training-format inputs of
`tickr.model.features.full` (input contract there); the splits are read from
`<reference>/splits.jsonl`, which must match its manifest line, with the training's parser.

RAW TICKS, two forms (`--tick-form`, default from the file name: .parquet = audit):
- `flat`: Massive's us_stocks_sip day files (csv or csv.gz, header row), every ticker;
- `audit`: the research bucket's audit copy (parquet, every column stored as text, a subset of
  tickers). v11 reads its input as text (`read_csv(..., all_varchar=true)`) and casts each
  column itself, so the audit text is handed over as the text of a flat file: an empty text
  becomes the empty field (missing), as a flat file's empty field reads. Before any use every
  value is checked against what v11 casts: sip_timestamp and sequence_number as BIGINT, the
  prices and sizes as DOUBLE, conditions and indicators as comma-separated codes; a value that
  fails refuses `tick_value_invalid` naming the column (never dropped or repaired).
  The audit copy holds only some tickers (COVERED): the tick groups (tk, tk2) are compared only
  for covered symbols; every other group is compared for all rows. The minute's tick table
  then takes the rows of every uncovered row symbol from `--tk61` (cut at the minute), so the
  kr_* ranks among the minute's rows see the whole cohort; the audit form refuses without it.
Per-symbol tick extraction reads only the union of the symbols that have a row at a chosen
minute (and are covered), with all their events of the day before the last chosen minute's end
(their full required history); v11 partitions every column by symbol, so this is exact.

DATES. Before any file is opened, the day must pass the pinned training rules (`l1labels.needed`
on the bar folder's NAMES only, the pinned l1labels.py imported the research way, as the training
imports it): a sealed day (2026-07-01 .. 2026-08-14, or after the research archive's last day,
the pinned HARD_LAST, not a live context's date), a day
off the calendar, the embargo after the sealed window, history reaching into the sealed window
or before the archive, or a missing bar file refuses with the training's own cause. Sealed data
is never read. The open day 2026-11-17 passes.

MINUTES (table minutes, HH:MM 04:00 .. 19:59). `--minutes` names them; `--all-minutes` takes
every minute of the table day; by default: 04:30, 09:45, 12:00 and 15:30 (codex-108 point 9),
plus, over the CANDIDATES (covered tickers in the audit form; otherwise the row symbols of
those four minutes): the minute of each candidate's final quote of the day (when it lies in the
table day), the first bar minute of each candidate that has an event before 04:00 (the earliest
minute it can have a row, read with the pre-04:00 carry), and the regular-session minute (09:30
.. 15:59) with the most candidate events (the peak). A minute without rows is reported empty.

REPLAY, for each chosen minute m, as the bot would see it at the end of m, rebuilt from scratch
(complete market bars and cohort dependencies for every comparison):
1. bars: the day's bars with window_start <= m, the DayState from the 20 sessions before
   (`features.history`, splits applied) -> `features.minute_rows`: the rows (symbols) of m;
2. ticks: the events of the row symbols before the end of m -> the frozen converter v11 through
   `ticks.tick_rows` (TICK_THREADS as set; 1 gives bit-stable rows) -> the tick table so far;
3. the bot's live date context of the day (`full.live_context`, the training's full-day
   sessions: the pinned table has no early close) on the snapshot of the inputs as of the mark
   of m: the training's own `l1feat.cut_inputs` cuts the reference and Benzinga inputs at the
   mark (the capture's job live), and every record of the snapshot, the bars through m and the
   tick table carry the mark as their receipt; the receipt cutoff is the mark. So
   `LiveContext.minute_rows` (incremental engine: `LiveContext.assemble_rows` on the table
   rows) runs every check of the live path and finds nothing to refuse. The snapshot is made
   in the run's temporary folder and removed when the minute's features are built.

VARIANTS (`--mode`; both = plain and marker, all = the three):
- `plain`: the prefix as it is: the latest quote update of a stock weighs 0 (v11's `d` needs a
  later update, last-quote-minute.md);
- `marker` (codex-108 point 1, the copied boundary quote): before converting, ONE copy of a
  symbol's latest real quote, stamped at the start of minute m+1 (sip_timestamp = end of m in
  ns, sequence_number + 1, prices and sizes copied, conditions and indicators empty), for every
  symbol with a real quote in the prefix. It acts only on v11's weight `d`, capped at the end of
  the quote's own minute: the latest quote of the minute k holding it weighs to the end of k,
  exactly what a marker at the start of k+1, added at the close of k for a symbol that quoted in
  k, gives, and what the live path (tickr.model.live, which appends each minute's rows at its
  close) holds. Its own row (minute m+1) is dropped; no count, lag or trade join reads it (lag
  columns read earlier updates, trades join quotes strictly before them). Compared with the
  archive, only a symbol's FINAL quote of the day can then differ (weight 0 in the archive).
- `marker-minute`: the literal reading of "only for a symbol with a real quote in that minute"
  applied to the prefix: the marker only for symbols with a real quote in minute m; a symbol
  whose latest quote lies in an earlier minute keeps the plain 0 weight there.

ENGINES (`--engine`; codex-108 points 1, 2 and 9 over all minutes of a day):
- `full-prefix` (the default and the reference): each minute rebuilt from scratch as above, v11
  on the whole prefix and the table code on the whole market's day (minutes cost ever more:
  seconds at 04:30, minutes at 15:30 on a real day);
- `incremental`: one walk over the day (`tickr.model.parity_walk`): each closed minute's tick
  rows from the carry of #820 (`ticks.tail_carry` / `minute_rows`), the marker applied per
  closed minute as the live path (#826) does, each variant's table composed from those rows
  (exactly the full-prefix table, see parity_walk); the table rows of a minute from
  `incremental.DayTable` (#832: the whole-market part carried minute by minute, the pinned
  day2 on the cohort only; `--table-workers` processes), computed once per minute for every
  variant; the cohort union for the tick extraction from the carried row rule
  (`incremental.Market`).
- `--workers W` (either engine): the features of up to 2 W minutes are assembled in W worker
  processes while the main process walks on (the walk and the table rows are sequential by
  nature); each minute's results are journaled when its features are done.
- `--verify-full-prefix N` (incremental only): at N chosen minutes spread from the first to the
  last, the full-prefix engine runs too and the two must be EXACTLY equal: the table rows
  (keys, order, types, every value) and each variant's tick table. A difference is the
  discrepancy class `engine` (report `engine_check`, the cells in the exceptions csv with
  table `engine`), never accepted; the features of both engines are then compared as well, to
  show which cells the difference moves.

RESULTS AS THEY ARE MADE (`Journal`): `<out>/parity-<day>.jsonl` gets a header line (the run's
settings and inputs) and one line per finished minute (every variant's comparison, the tick
comparison, final-quote cases, versus plain, the engine check, the seconds of each step),
written and synced as soon as the minute is done; the minute's exception cells go to the
exceptions csv just before its line. `--resume` continues a journal in `<out>` whose header
equals the new run's (else `replay_resume_mismatch`), skipping its finished minutes (the
incremental walk still converts them, which its carry needs); a line cut by a stop is dropped,
with the cells of minutes without a line. The report is built from the journal alone
(`build_report`; `--rebuild-report` writes it from `<out>` without running anything, also for
a stopped run: `minutes_done`, `complete`).

DISK AND MEMORY (`tickr.model.headroom`, `[parity]` of --config; the harness runs beside the
live bot): every temporary file lies under `--work-dir` (default --out) in a run folder removed
at the end; each minute's conversion writes the prefix as zstd csv (v11's read_csv reads it as
it reads csv.gz; a third of the plain csv on the synthetic test day, not yet measured on a
real day) into its own folder, removed when the conversion is done, and the minute's tick file
is removed once its features are built. Before each conversion the guard refuses
`memory_insufficient` or `disk_space_insufficient` (estimate = input rows x the bytes per row
measured on this run; the incremental walk's minute and span conversions, the full-prefix
ones and the engine check's are guarded alike, each kind measured on its own); the run then
stops: the journal gets a `stop` line (minute, cause, detail, resources), the report holds
the minutes done and `stopped`, and the command exits 3 (`--resume` continues after the
cause is fixed, redoing the stopped minute). Every duckdb engine runs with `duckdb_memory_gb`
and spills inside the work folder. Each minute's journal line has `resources`: the process's
peak resident bytes so far (a high-water mark: the minute where it rises is the one that
needed it), free disk and available memory.

REPORT (`<out>/parity-<day>.json`, `.txt` and `-exceptions.csv`; an existing one refuses). The
threshold is EXACT (codex-108 point 2: pending the per-column bound table); every discrepancy
cell is an exception, listed in the csv (variant, minute, table, symbol, column, group, class,
reference, replay, abs, rel, ulp). Per variant and minute:
- keys: row counts, symbols in one table only, whether the common symbols come in the same
  order (`order_equal`), and whether keys and order are exact (`keys_exact`);
- per column and per group, the cells by DISCREPANCY CLASS: `missing` (a missing value on one
  side only, counted per side), `rank` (kr_*, r_*, *_rk: a rank among the minute's rows),
  `flag` (the 0/1 columns declared in `FLAGS` from the builders' definitions; the training
  table must agree, else `replay_flag_schema`), `categorical` (sess, sic2, m), `value` (every
  other float); for value cells the largest absolute, relative (|a-b| / max(|a|,|b|)), float32
  ULP (`ulp`: the model's input type) and float64 ULP (`ulp64`: the tables' native type)
  difference, and apart those where one side is 0 (near zero, where a relative difference
  says nothing);
- deterministic columns (the bar groups) must be exact: `bar_groups_exact`;
- with `--tk61`: our v11 rows of minute m against the tk61 rows of minute m for the symbols
  whose ticks were converted (61 columns, tn / qn counts included), the same classes;
- final-quote cases: every (symbol, minute) where a converted row symbol's final quote of the
  day lies in m, with the symbol's quote count of the day and its final quote's time (sparse
  and halted symbols are among them: a stock that stops quoting before 16:00 is marked
  `stopped_before_close`); per case whether a feature value (value / flag / categorical
  class), a missing-value flag or the symbol's own rank differs from the archive, and the count
  of OTHER rows of that minute whose rank differs (downstream);
- with two variants or more, a section comparing each variant with `plain` cell by cell.
`--rtol` / `--atol` (numpy `isclose`) add, beside the exact counts, the cells beyond that
tolerance; neither has a default. `--ticks-only` runs only the tick table comparison (needs
`--tk61`), skipping the features. Values are compared as the table stores them (float32
features, int m and sess). v11 with several engine threads (TICK_THREADS) is not
bit-deterministic (last-bit differences); the training's tick files carry that noise.

NATIVE PRECISION (codex-110 points 1 and 2; no tolerance or bound is added anywhere, every
delivered check stays exact). The tk and tk2 values (NATIVE_GROUPS) are read at their native
computed type, float64, before the pinned code casts them to float32 (`native_values`: the
arrays of wm2tk_l1.Tick.add's final cast, the kr_* ranks included, and l1tk2.values; the pinned
bytes are run unchanged). Each exception cell carries `replay_native` (ours) beside the
delivered `replay` and the training's `reference`; with `--tk61`, at a minute with a tk or tk2
exception, also `reference_native` (the same pinned code on the training's own v11 tick table
for the training table's rows of the minute) and `native_abs` / `native_ulp64`; each variant's
comparison then counts per column the cells whose native values differ while the delivered
float32 agree (`native`). An empty native field = not captured (the bar, company, news and
fact groups: their arrays are float32 inside the pinned code or cast on assignment, and the
harness does not read them; the training table holds float32 only).
- Ranks: at each minute with a kr_* exception, `ranks` (`rank_recompute`) recomputes the
  minute's pct ranks from native values over every row of the minute, with the training's
  native k_X put in for the symbols whose k_X differ, and reports the original mismatches that
  remain, the previously matching ranks that change, the training's own native ranks against
  its table, and the symbols whose native k_X differ under equal float32. Without --tk61 it
  says what is missing instead.
- Engine check: where the tick tables of the two engines are not bit-equal, the features of
  both are compared delivered (exact) and native: native-only differences are cells of class
  `engine-native` and counted in `engine_check.native_cells_delivered_equal`. Two classes of
  their own are listed apart, never passed silently: `zero_row_schema` (no row in either
  engine, with one engine having no table or another empty schema) and `diagnostic` (a cell
  declared in config [parity] engine_diagnostics, matched on day, symbol, row minute, column
  and its exact float64 ULP: the 2026-11-17 WDC tburst reproducer). A declared cell still makes
  the delivered features of that minute checked, and an undeclared one is an engine difference.

OUTCOMES: the harness reads no label, return or outcome. Of the training's table it reads only
date, sym and the 191 features (`_table_columns`); of the pinned l1labels.py only the day rules,
the minute parser, the splits and types readers and two constants; the table rows come from the
feature-only table code (full.LiveContext).
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import datetime
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tickr import config
from tickr.calendar import NEW_YORK, MarketDay
from tickr.model import features, headroom
from tickr.model.errors import ModelRefused
from tickr.model.features import full, ticks

# The groups computed from the minute bars alone: the live path and the training share every
# input there, so any difference is a defect, never noise.
BAR_GROUPS = features.LIVE_GROUPS
# The groups computed from the tick table: compared only for symbols whose raw ticks are given.
TICK_GROUPS = ("tk", "tk2")
VARIANTS = ("plain", "marker", "marker-minute")
ENGINES = ("full-prefix", "incremental")
MODES = {"plain": ("plain",), "marker": ("marker",), "both": ("plain", "marker"),
         "all": VARIANTS}
BASE_MINUTES = ("04:30", "09:45", "12:00", "15:30")  # codex-108 point 9
# The marker's quote values (module docstring): SQL over the symbol's latest quote of the prefix.
MARKER_VALUES = {"ask_price": "ask_price", "ask_size": "ask_size", "bid_price": "bid_price",
                 "bid_size": "bid_size", "conditions": "NULL", "indicators": "NULL"}
CATEGORICAL = ("m", "sess", "sic2")
# The 0/1 features, declared from the pinned builders' definitions, never inferred from a day's
# values (codex-109 point 1): inl (wm2_l1f: inl.astype(float64), list membership), bz_solo and
# the four bz_c_* channel flags (l1bz: "1 when ...", NaN without an article for bz_solo),
# nw_any (wm2ref16: nw_n > 0), fl_has (l1fl: "1 when a fact is known ..., else 0").
FLAGS = ("inl", "bz_solo", "bz_c_earn", "bz_c_anl", "bz_c_deal", "bz_c_mov", "nw_any", "fl_has")
RANK_PREFIXES, RANK_SUFFIX = ("kr_", "r_"), "_rk"
CLASSES = ("missing", "rank", "flag", "categorical", "value")
FORMS = ("flat", "audit")
# What v11 casts each raw column to (its day()): checked on the audit copy before any use.
BIGINT_COLUMNS = ("sip_timestamp", "sequence_number")
DOUBLE_COLUMNS = ("price", "size", "bid_price", "ask_price", "bid_size", "ask_size")
CODE_COLUMNS = ("conditions", "indicators")
CODE_PATTERN = "^[0-9]+(,[0-9]+)*$"
# Refusals of the run's disk and memory guard (tickr.model.headroom): the run stops there and
# the report holds the minutes done and the refusal.
STOP_CAUSES = ("disk_space_insufficient", "memory_insufficient")
DEFAULT_CONFIG = Path("config/bot.toml")  # the bot's config, relative to the repo root
REGULAR = ("09:30", "15:59")  # the regular session, for the peak minute
EXCEPTION_FIELDS = ("variant", "minute", "table", "sym", "column", "group", "class",
                    "reference", "replay", "abs", "rel", "ulp", "ulp64", "replay_native",
                    "reference_native", "native_abs", "native_ulp64")
# The groups whose values the harness reads at their native computed type (float64), before
# the pinned code casts them to the model's float32 (codex-110 points 1 and 2): tk (`out[c]` of
# wm2tk_l1.Tick.add, the kr_* ranks included, ranked in float64) and tk2 (`l1tk2.values`). Every
# other group is not captured: its exception cells leave the native fields empty.
NATIVE_GROUPS = ("tk", "tk2")
NATIVE_FIELDS = ("replay_native", "reference_native", "native_abs", "native_ulp64")
# The rank columns whose ranks the harness recomputes from native values (codex-110 point 1):
# kr_X is the pct rank of k_X among the minute's rows (wm2tk_l1.py, the RANKED columns).
KR_PREFIX, K_PREFIX = "kr_", "k_"
# A provisional diagnostic of the engine check (codex-110 point 2): one declared tick-table cell
# (config [parity] engine_diagnostics: name, day, symbol, row minute, column, float64 ULP).
DIAGNOSTIC_FIELDS = ("name", "day", "sym", "row_minute", "column", "ulp64")
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
_CSV = "read_csv(?, header=true, all_varchar=true, delim=',', quote='\"', escape='\"')"


@dataclass(frozen=True)
class ReplayInputs:
    """The day's files (see the module docstring); `types` None = no type filter; `tk61` None =
    no tick table comparison; `tick_form` flat or audit."""

    bars: Path
    types: Path | None
    trades: Path
    quotes: Path
    reference: Path
    benzinga: Path
    fl_facts: Path
    table: Path
    tk61: Path | None = None
    tick_form: str = "flat"


@contextlib.contextmanager
def _training(cause: str):
    """A refusal of the training's code (SystemExit('REFUSED: ...')) as one typed refusal."""
    try:
        yield
    except SystemExit as exc:
        raise ModelRefused(cause, str(exc)) from None


def research() -> Any:
    """The pinned feature builder imported the way the training imports it (by its module
    names, from the pinned folder that the table code puts on sys.path), after the md5 check of
    every pinned file: the day rules of a replay are the training's, with its own HARD_LAST."""
    features.table_code()
    differ = sorted(n for n, d in full.full_digests().items() if d != full.FULL_PINS[n])
    if differ:
        raise ModelRefused("table_code_md5", ", ".join(differ))
    with _training("replay_code_refused"):
        import l1feat  # type: ignore[import-not-found]
        import l1labels  # type: ignore[import-not-found]
    return SimpleNamespace(labels=l1labels, feat=l1feat)


def archive_last() -> str:
    """HARD_LAST of the pinned l1labels.py: the research archive's last open day."""
    return str(research().labels.HARD_LAST)


def market_day(day: str) -> MarketDay:
    """The replayed day with the training's sessions (`full.TRAINED_SESSION_MINUTES`): the
    training's table of an archive day has no early close, so neither has its replay."""
    d = datetime.date.fromisoformat(day)
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=NEW_YORK)
    at = [start + datetime.timedelta(minutes=features.DAY_START_MINUTE + m)
          for m in full.TRAINED_SESSION_MINUTES]
    return MarketDay(d, *at, False, "replay of an archive day: the training's sessions")


def context(day: str) -> full.LiveContext:
    """The bot's live date context of the replayed day (one per process, cached by `full`)."""
    return full.live_context(market_day(day))


def day_files(day: str, bars: Path) -> list[str]:
    """The training's day rule on the bar folder's names: the 20 sessions before `day` and
    `day`, or the training's refusal (cause `replay_day_refused`, the training's text)."""
    labels = research().labels
    with _training("replay_day_refused"):
        labels.day_ok(day)
        if day > archive_last():
            raise SystemExit(f"REFUSED: sealed_day: {day} is after the research archive")
        if not bars.is_dir():
            raise SystemExit(f"REFUSED: bar_source_not_a_folder: {bars}")
        have = labels.bar_names(str(bars))[0]
        return list(labels.needed(labels.calendar(), have, day))


def layout(root: Path, day: str) -> dict[str, Path | None]:
    """The research bucket's layout of `day` under `root` (module docstring): bars, table,
    tk61, trades, quotes; a file that is not there is None, two feature tables refuse."""
    year = day[:4]
    tables = sorted(root.glob(f"l1/feat/*/tables/{year}/{day}.parquet"))
    if len(tables) > 1:
        raise ModelRefused("replay_layout", f"{len(tables)} feature tables of {day}")

    def there(p: Path) -> Path | None:
        return p if p.exists() else None

    return {"bars": there(root / "minute_aggs"), "table": tables[0] if tables else None,
            "tk61": there(root / "tk61" / year / f"{day}.parquet"),
            "trades": there(root / "tickraw-audit" / year / f"{day}-trades.parquet"),
            "quotes": there(root / "tickraw-audit" / year / f"{day}-quotes.parquet")}


def table_minutes(hhmm: Sequence[str]) -> list[int]:
    """Table minutes (from 04:00) of HH:MM texts, by the training's `cut_minute`."""
    labels = research().labels
    with _training("replay_minute_invalid"):
        got = [labels.cut_minute(x) for x in hhmm]
    if not got or len(set(got)) != len(got):
        raise ModelRefused("replay_minute_invalid", "minutes must be given once each")
    return sorted(got)


def hhmm(minute: int) -> str:
    total = minute + features.DAY_START_MINUTE
    return f"{total // 60:02d}:{total % 60:02d}"


def _splits(reference: Path) -> dict[str, list[tuple[str, float]]]:
    labels = research().labels
    man = json.loads((reference / "manifest.json").read_text("utf-8"))
    path = reference / "splits.jsonl"
    body = path.read_bytes()
    if man.get("files", {}).get(path.name) != [len(body), hashlib.md5(body).hexdigest()]:
        raise ModelRefused("replay_input_changed", path.name)
    with _training("replay_input_unreadable"):
        return labels.splits_parse(body)


def _types(path: Path | None) -> tuple[set[str] | None, str | None]:
    if path is None:
        return None, None
    labels = research().labels
    with _training("replay_input_unreadable"):
        got = labels.read_types(str(path))
        return got, labels.md5b(labels.raw(str(path)))


def _read(path: Path, cause: str, sql: str = "SELECT * FROM read_parquet(?)",
          params: Sequence[Any] = ()) -> Any:
    import duckdb

    con = duckdb.connect()
    try:
        return con.execute(sql, [str(path), *params]).df()
    except duckdb.Error as exc:
        raise ModelRefused(cause, type(exc).__name__) from exc
    finally:
        con.close()


def _table_columns() -> str:
    """The columns the harness reads of the training's table: date, sym and the 191 features
    (m and sess among them), never any other column a table file may hold (a label, return or
    outcome column is never read: codex-110 point 3)."""
    names = list(dict.fromkeys(["date", "sym", *full.full_features()]))
    return ", ".join('"' + c.replace('"', '""') + '"' for c in names)


def reference_table(path: Path, day: str) -> Any:
    """The training's feature table of `day`: one row per (sym, m), every feature present
    (only the key and feature columns are read)."""
    import duckdb

    con = duckdb.connect()
    try:
        have = [r[0] for r in con.execute("SELECT name FROM parquet_schema(?)",
                                          [str(path)]).fetchall()]
    except duckdb.Error as exc:
        raise ModelRefused("replay_table_unreadable", type(exc).__name__) from exc
    finally:
        con.close()
    missing = [c for c in ["date", "sym", *full.full_features()] if c not in have]
    if missing:
        raise ModelRefused("replay_table_columns", ", ".join(missing))
    frame = _read(path, "replay_table_unreadable",
                  f"SELECT {_table_columns()} FROM read_parquet(?)")
    if (frame["date"].astype(str) != day).any():
        raise ModelRefused("replay_table_wrong_day", day)
    if frame.duplicated(["sym", "m"]).any():
        raise ModelRefused("replay_table_key_duplicate", day)
    return frame


class ReferenceTable:
    """The training's feature table of the day kept on disk: the run reads one minute's rows
    when it compares that minute (`minute`), instead of holding the whole day in memory (the
    2026-11-17 Mac run held it from its first minute). Opening checks what `reference_table`
    checks, over the file."""

    def __init__(self, path: Path, day: str) -> None:
        import duckdb

        self.path = path
        con = duckdb.connect()
        try:
            have = [r[0] for r in con.execute("SELECT name FROM parquet_schema(?)",
                                              [str(path)]).fetchall()]
            missing = [c for c in ["date", "sym", *full.full_features()] if c not in have]
            if missing:
                raise ModelRefused("replay_table_columns", ", ".join(missing))
            other, rows, keys = con.execute(
                "SELECT count(*) FILTER (WHERE CAST(date AS VARCHAR) <> ?), count(*), "
                "count(DISTINCT (sym, m)) FROM read_parquet(?)", [day, str(path)]).fetchone()
        except duckdb.Error as exc:
            raise ModelRefused("replay_table_unreadable", type(exc).__name__) from exc
        finally:
            con.close()
        if other:
            raise ModelRefused("replay_table_wrong_day", day)
        if rows != keys:
            raise ModelRefused("replay_table_key_duplicate", day)

    def minute(self, m: int) -> Any:
        """The rows of table minute `m`, in file order: the key and feature columns only."""
        return _read(self.path, "replay_table_unreadable",
                     f"SELECT {_table_columns()} FROM read_parquet(?) WHERE m = ?", [int(m)])

    def check_flags(self) -> None:
        """Refuse replay_flag_schema when a declared flag (`FLAGS`) holds anything but 0, 1 or
        missing in the training table: the declaration and the archive disagree."""
        sql = " + ".join(f'count(*) FILTER (WHERE "{c}" IS NOT NULL AND NOT '
                         f'isnan(CAST("{c}" AS DOUBLE)) AND "{c}" NOT IN (0, 1))' for c in FLAGS)
        bad = _read(self.path, "replay_table_unreadable",
                    f"SELECT {sql} AS n FROM read_parquet(?)")
        if int(bad.n.iloc[0]):
            raise ModelRefused("replay_flag_schema", f"{int(bad.n.iloc[0])} cells")


def tick_table(path: Path, symbols: set[str], through: int) -> Any:
    """The rows of `symbols` up to minute `through` (of the New York day) of a v11 tick file
    (tk61), checked to hold v11's columns in v11's order."""
    import pandas as pd

    sql = ("SELECT * FROM read_parquet(?) WHERE sym IN (SELECT unnest(?)) AND m <= ? "
           "ORDER BY sym, m")
    frame = _read(path, "replay_tk61_unreadable", sql, [sorted(symbols), through])
    want = ["sym", "m", *_tick_value_columns()]
    if list(frame.columns) != want:
        raise ModelRefused("replay_tk61_columns", "not v11's columns")
    return frame.astype({"sym": object}) if len(frame) else pd.DataFrame(columns=want)


def _tick_value_columns() -> list[str]:
    """v11's 61 value columns in its order (from a run on an empty day: its own schema)."""
    if "_tk" not in _CACHE:
        with tempfile.TemporaryDirectory(prefix="parity-schema-") as tmp:
            t, q = Path(tmp) / "t.csv", Path(tmp) / "q.csv"
            t.write_text(",".join(ticks.trade_columns()) + "\n", "utf-8")
            q.write_text(",".join(ticks.quote_columns()) + "\n", "utf-8")
            _CACHE["_tk"] = [c for c in ticks.tick_rows(t, q, "2024-03-06", 0,
                                                         groups=ticks.default_groups()).columns
                             if c not in ("sym", "m")]
    return list(_CACHE["_tk"])


_CACHE: dict[str, Any] = {}


def _ns_end(day: str, minute: int) -> int:
    """SIP ns of the end of table minute `minute` (from 04:00 New York)."""
    return ticks._bounds(day, minute + features.DAY_START_MINUTE)[1]


# --- native values (codex-110 points 1 and 2) ------------------------------------------------


class _Float32Casts:
    """numpy as the pinned wm2tk_l1 sees it while `native_values` reads it: every array handed
    to np.asarray(..., dtype=np.float32) (Tick.add's final cast, one call per TKCOLS column in
    that order) is copied before the cast. Every other name is numpy's own."""

    def __init__(self, numpy: Any) -> None:
        self._numpy = numpy
        self.seen: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._numpy, name)

    def asarray(self, a: Any, dtype: Any = None, **kw: Any) -> Any:
        if dtype is self._numpy.float32:
            self.seen.append(self._numpy.array(a, copy=True))
        return self._numpy.asarray(a, dtype=dtype, **kw)


@contextlib.contextmanager
def native_values(code: Any) -> Iterator[dict[str, Any]]:
    """While the pinned code of `code` (a date context's builder) runs inside, the values of
    `NATIVE_GROUPS` at their native computed type, before the cast to float32, aligned with the
    rows handed to the builder: tk from the arrays of wm2tk_l1.Tick.add's final cast, tk2 from
    l1tk2.values. Nothing of the pinned bytes changes and no value is recomputed; the harness
    only reads what the code computes. The dict is filled when the block ends; a capture that
    does not match the code's own column list, or a value that is not float64, refuses
    `native_capture_mismatch`."""
    import numpy as np

    tk, tk2 = code.tk, code.tk2
    numpy, values = tk.np, tk2.values
    casts = _Float32Casts(numpy)
    found: dict[str, Any] = {}

    def recorded(k: Any) -> Any:
        out = values(k)
        found.update({c: np.array(v, copy=True) for c, v in out.items()})
        return out

    tk.np, tk2.values = casts, recorded
    try:
        yield found
    finally:
        tk.np, tk2.values = numpy, values
    if casts.seen:
        cols = list(tk.TKCOLS)
        if len(casts.seen) != len(cols):
            raise ModelRefused("native_capture_mismatch",
                               f"{len(casts.seen)} float32 casts, {len(cols)} tk columns")
        found.update(zip(cols, casts.seen, strict=True))
    if found and sorted(found) != sorted([*tk.TKCOLS, *tk2.TK2COLS]):
        raise ModelRefused("native_capture_mismatch", "not the tk and tk2 columns")
    bad = sorted(c for c, v in found.items() if v.dtype != np.float64)
    if bad:
        raise ModelRefused("native_capture_mismatch", "not float64: " + ", ".join(bad))


def _native_frame(found: dict[str, Any], syms: Any) -> Any:
    """The captured native values as a frame indexed by the rows' symbols (no column when
    nothing was captured: a minute without rows)."""
    import pandas as pd

    return pd.DataFrame(found, index=pd.Index(list(syms), name="sym"))


def reference_natives(tk61: Path, day: str, rows: Any, work: Path) -> Any:
    """The training's tk and tk2 values of `rows` (the training table's rows of one minute: sym
    and m) at their native type, recomputed by the same pinned code (`native_values`) from the
    training's own v11 tick table of the day (`--tk61`), cut to the rows' symbols and minute
    (each value reads its own and earlier minutes only, so the cut changes none). The table
    stores float32 only; without tk61 there is no native training value."""
    code = context(day).code
    through = int(rows.m.max()) + features.DAY_START_MINUTE if len(rows) else 0
    table = tick_table(tk61, set(rows.sym), through)
    folder = Path(tempfile.mkdtemp(prefix="parity-tk61-", dir=work))
    try:
        (folder / day[:4]).mkdir()
        path = write_tick_file(table, folder / day[:4] / f"{day}.parquet")
        frame = rows[["sym", "m"]].reset_index(drop=True).copy()
        with features._LOCK, native_values(code) as found, \
                contextlib.redirect_stdout(io.StringIO()), _training("replay_tk61_unreadable"):
            code.tk.Tick(str(folder)).add(frame, day)
            code.tk2.add(frame, str(path))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return _native_frame(found, frame.sym)


# --- raw ticks ------------------------------------------------------------------------------


def _source(inputs: ReplayInputs, kind: str) -> tuple[str, list[str]]:
    """SQL text of one raw file as v11 reads it (text columns, an empty text = missing) and
    the converter's columns of that kind."""
    w = ticks.converter()
    cols = list(w.TCOLS if kind == "trades" else w.QCOLS)
    path = str(inputs.trades if kind == "trades" else inputs.quotes).replace("'", "''")
    if inputs.tick_form == "flat":
        return _CSV.replace("?", f"'{path}'"), cols
    sel = ", ".join(f"NULLIF(CAST({c} AS VARCHAR), '') AS {c}" for c in cols)
    return f"(SELECT {sel} FROM read_parquet('{path}'))", cols


def check_audit(inputs: ReplayInputs) -> None:
    """Every value of the audit copy is what v11 casts (module docstring), or one refusal."""
    import duckdb

    con = duckdb.connect()
    try:
        for kind in ("trades", "quotes"):
            path = inputs.trades if kind == "trades" else inputs.quotes
            have = {r[0] for r in con.execute(
                "SELECT name FROM parquet_schema(?)", [str(path)]).fetchall()}
            src, cols = _source(inputs, kind)
            lost = [c for c in cols if c not in have]
            if lost:
                raise ModelRefused("tick_columns_missing", f"{kind}: {', '.join(lost)}")
            tests = [(c, f"TRY_CAST({c} AS BIGINT) IS NULL") for c in cols if c in BIGINT_COLUMNS]
            tests += [(c, f"TRY_CAST({c} AS DOUBLE) IS NULL") for c in cols
                      if c in DOUBLE_COLUMNS]
            tests += [(c, f"NOT regexp_full_match({c}, '{CODE_PATTERN}')") for c in cols
                      if c in CODE_COLUMNS]
            for c, bad in tests:
                n = con.execute(f"SELECT count(*) FROM {src} WHERE {c} IS NOT NULL AND {bad}"
                                ).fetchone()[0]
                if n:
                    raise ModelRefused("tick_value_invalid", f"{kind}.{c}: {n} values")
            for c in BIGINT_COLUMNS:
                n = con.execute(f"SELECT count(*) FROM {src} WHERE {c} IS NULL").fetchone()[0]
                if n:
                    raise ModelRefused("tick_value_invalid", f"{kind}.{c}: {n} missing")
    except duckdb.Error as exc:
        raise ModelRefused("tick_file_unreadable", type(exc).__name__) from exc
    finally:
        con.close()


def scan_events(inputs: ReplayInputs, day: str, symbols: set[str] | None) -> dict[str, Any]:
    """Per ticker (of `symbols`, None = every ticker): first event, final quote and quote count
    of the day; and the events per table minute of the regular session."""
    import duckdb

    start = _ns_end(day, -1)
    lo, hi = table_minutes(list(REGULAR))
    con = duckdb.connect()
    try:
        keep = ""
        if symbols is not None:
            con.execute("CREATE TEMP TABLE syms AS SELECT unnest(?) AS sym", [sorted(symbols)])
            keep = " WHERE ticker IN (SELECT sym FROM syms)"
        tsrc, _ = _source(inputs, "trades")
        qsrc, _ = _source(inputs, "quotes")
        both = (f"SELECT ticker, CAST(sip_timestamp AS BIGINT) AS ts, 'q' AS k FROM {qsrc}{keep}"
                f" UNION ALL SELECT ticker, CAST(sip_timestamp AS BIGINT), 't' FROM {tsrc}{keep}")
        con.execute(f"CREATE TEMP TABLE ev AS {both}")
        per = con.execute("SELECT ticker, min(ts), max(CASE WHEN k = 'q' THEN ts END), "
                          "count(*) FILTER (WHERE k = 'q') FROM ev GROUP BY 1 ORDER BY 1"
                          ).fetchall()
        minute = f"CAST((ts - {start}) // {features.NS_PER_MINUTE} AS BIGINT)"
        busy = con.execute(f"SELECT {minute} AS m, count(*) FROM ev WHERE {minute} BETWEEN "
                           f"{lo} AND {hi} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1").fetchall()
    except duckdb.Error as exc:
        raise ModelRefused("tick_file_unreadable", type(exc).__name__) from exc
    finally:
        con.close()
    return {"first": {r[0]: r[1] for r in per},
            "final_quote": {r[0]: r[2] for r in per if r[2] is not None},
            "quotes": {r[0]: r[3] for r in per},
            "peak": busy[0][0] if busy else None}


def covered_tickers(inputs: ReplayInputs) -> set[str] | None:
    """The tickers of the audit copy (None in the flat form: every ticker is there)."""
    if inputs.tick_form == "flat":
        return None
    import duckdb

    con = duckdb.connect()
    try:
        got = set()
        for kind in ("trades", "quotes"):
            src, _ = _source(inputs, kind)
            got |= {r[0] for r in con.execute(f"SELECT DISTINCT ticker FROM {src}").fetchall()}
        return got
    except duckdb.Error as exc:
        raise ModelRefused("tick_file_unreadable", type(exc).__name__) from exc
    finally:
        con.close()


def extract_events(inputs: ReplayInputs, symbols: set[str], before: int, work: Path,
                   room: headroom.Headroom | None = None) -> None:
    """One pass over each day file: the converter's columns of the events of `symbols` stamped
    before `before` (ns), kept as the file's text in work/trades.parquet, work/quotes.parquet,
    written in event order (SIP stamp, then sequence number) so the incremental walk streams
    them without sorting again.
    With `room`, the engine runs within its limits and refuses to start below its reserves."""
    import duckdb
    import pandas as pd

    if room is not None:
        room.check("extract", 0)
    con = room.connect() if room is not None else duckdb.connect()
    try:
        con.register("syms", pd.DataFrame({"sym": sorted(symbols)}, dtype=object))
        for kind in ("trades", "quotes"):
            src, cols = _source(inputs, kind)
            out = work / f"{kind}.parquet"
            con.execute(f"COPY (SELECT {', '.join(cols)} FROM {src} WHERE ticker IN (SELECT sym "
                        f"FROM syms) AND CAST(sip_timestamp AS BIGINT) < {int(before)} ORDER BY "
                        "CAST(sip_timestamp AS BIGINT), CAST(sequence_number AS BIGINT)) TO "
                        f"'{out}' (FORMAT parquet)")
    except duckdb.Error as exc:
        raise ModelRefused("tick_file_unreadable", type(exc).__name__) from exc
    finally:
        con.close()


def minute_ticks(room: headroom.Headroom, day: str, minute: int, marker: bool | str) -> Any:
    """The tick table of `day` as the bot holds it at the end of table minute `minute`: the
    events of <work>/*.parquet before that end (with the marker quotes of the variant: True or
    "marker" for every symbol with a quote, "marker-minute" for the symbols with a quote in the
    minute) through the frozen converter; rows after the minute are dropped. The prefix goes to
    the converter as zstd csv in a conversion folder of `room` (checked before it is written,
    removed after; `headroom`)."""
    w = ticks.converter()
    end, begin = _ns_end(day, minute), _ns_end(day, minute - 1)
    through = minute + features.DAY_START_MINUTE
    trades, quotes_file = room.work / "trades.parquet", room.work / "quotes.parquet"
    pick = f"CAST(sip_timestamp AS BIGINT) < {end}"
    if marker:
        through += 1  # the marker stands at the start of the next minute

    def selects(limit: int | None) -> tuple[str, str]:
        cut = "" if limit is None else (" ORDER BY CAST(sip_timestamp AS BIGINT), "
                                        f"CAST(sequence_number AS BIGINT) LIMIT {int(limit)}")
        t = f"SELECT {', '.join(w.TCOLS)} FROM read_parquet('{trades}') WHERE {pick}{cut}"
        q = (f"SELECT * FROM (SELECT {', '.join(w.QCOLS)} FROM read_parquet('{quotes_file}') "
             f"WHERE {pick}{cut})")
        if marker:
            made = {"ticker": "ticker", "sip_timestamp": f"'{end}'",
                    "sequence_number": "CAST(CAST(sequence_number AS BIGINT) + 1 AS VARCHAR)",
                    **MARKER_VALUES}
            latest = (f"SELECT * FROM ({q}) QUALIFY row_number() OVER (PARTITION BY ticker "
                      "ORDER BY CAST(sip_timestamp AS BIGINT) DESC, "
                      "CAST(sequence_number AS BIGINT) DESC) = 1")
            if marker == "marker-minute":
                latest = f"SELECT * FROM ({latest}) WHERE CAST(sip_timestamp AS BIGINT) >= {begin}"
            q = (f"{q} UNION ALL SELECT "
                 f"{', '.join(f'{made[c]} AS {c}' for c in w.QCOLS)} FROM ({latest})")
        return t, q

    def convert(folder: Path, limit: int | None) -> tuple[Any, int]:
        t, q = folder / "t.csv.zst", folder / "q.csv.zst"
        con = room.connect(folder)
        try:
            ts, qs = selects(limit)
            wrote = con.execute(f"COPY ({ts}) TO '{t}' (HEADER, DELIMITER ',')").fetchone()[0]
            wrote += con.execute(f"COPY ({qs}) TO '{q}' (HEADER, DELIMITER ',')").fetchone()[0]
        finally:
            con.close()
        return ticks.tick_rows(t, q, day, through, groups=ticks.default_groups(),
                               tmp_dir=folder, memory=room.duckdb_memory()), int(wrote)

    con = room.connect()
    try:
        rows = con.execute(f"SELECT (SELECT count(*) FROM read_parquet('{trades}') WHERE {pick})"
                           f" + (SELECT count(*) + count(DISTINCT ticker) FROM read_parquet("
                           f"'{quotes_file}') WHERE {pick})").fetchone()[0]
    finally:
        con.close()
    with room.conversion("prefix", int(rows), lambda f, n: convert(f, n)[1]) as folder:
        table = convert(folder, None)[0]
    return table[table.m <= minute + features.DAY_START_MINUTE].reset_index(drop=True)


def write_tick_file(table: Any, path: Path) -> Path:
    """The tick table as the training's tick file (parquet, v11's column types)."""
    import duckdb

    con = duckdb.connect()
    try:
        con.register("tk", table)
        con.execute(f"COPY (SELECT * FROM tk) TO '{path}' (FORMAT parquet)")
    finally:
        con.close()
    return path


# --- comparison -----------------------------------------------------------------------------


def _num(x: float) -> float | str:
    return x if math.isfinite(x) else str(x)


def column_kinds(names: Sequence[str]) -> dict[str, str]:
    """The discrepancy class of each column (module docstring), from its name only: rank,
    categorical, flag (`FLAGS`) or value."""
    kinds = {}
    for c in names:
        if c.startswith(RANK_PREFIXES) or c.endswith(RANK_SUFFIX):
            kinds[c] = "rank"
        elif c in CATEGORICAL:
            kinds[c] = "categorical"
        else:
            kinds[c] = "flag" if c in FLAGS else "value"
    return kinds


def _ulps64(x: Any, y: Any) -> Any:
    """Distance in float64 units in the last place: the native type of the tick and bar tables
    (codex-109 point 1: a float64 reduction is diagnosed in float64, before any cast)."""
    import numpy as np

    def ordered(v):
        i = np.asarray(v, np.float64).view(np.int64)
        return np.where(i < 0, np.iinfo(np.int64).min - i, i).astype(object)

    with np.errstate(invalid="ignore", over="ignore"):
        return np.abs(ordered(x) - ordered(y))


def _ulps(x: Any, y: Any) -> Any:
    """Distance in float32 units in the last place: the model's input type, the training
    table's stored type."""
    import numpy as np

    def ordered(v):
        i = np.asarray(v, np.float32).view(np.int32).astype(np.int64)
        return np.where(i < 0, np.int64(-(2**31)) - i, i)

    with np.errstate(invalid="ignore", over="ignore"):
        return np.abs(ordered(x) - ordered(y))


def compare(got: Any, want: Any, groups: dict[str, list[str]],
            tolerance: tuple[float, float] | None, kinds: dict[str, str] | None = None,
            tick_symbols: set[str] | None = None,
            exceptions: list[dict[str, Any]] | None = None, native: Any = None,
            ref_native: Any = None,
            native_cells: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Every feature column of `got` (replay) against `want` (training), joined on sym, exact;
    the tick groups only for `tick_symbols` when given. Each discrepancy cell is appended to
    `exceptions` (sym, column, group, class, values and differences). `native` / `ref_native`
    (frames indexed by sym, `native_values`): each side's values at their native type; an
    exception cell then carries them beside the delivered float32 values (empty when a side or
    the column has none), and `native` in the result counts, per column, the cells whose
    native values differ although the delivered values agree (each such cell appended to
    `native_cells`, class `native`). No tolerance: the delivered checks stay exact."""
    import numpy as np

    kinds = kinds or column_kinds([c for cs in groups.values() for c in cs])
    a = got.set_index("sym")
    b = want.set_index("sym")
    common = [s for s in b.index if s in set(a.index)]
    order_equal = [s for s in a.index if s in set(b.index)] == common
    a, b = a.loc[common], b.loc[common]
    syms = np.array(common, dtype=object)
    out: dict[str, Any] = {
        "rows_reference": len(want), "rows_replay": len(got), "rows_compared": len(common),
        "only_in_reference": sorted(set(want.sym) - set(got.sym)),
        "only_in_replay": sorted(set(got.sym) - set(want.sym)), "order_equal": order_equal,
        "groups": {}, "columns": {}, "symbols": {}, "classes": dict.fromkeys(CLASSES, 0),
        "tick_rows_not_compared": 0, "native": {"columns": {}, "cells_delivered_equal": 0}}
    out["keys_exact"] = (order_equal and not out["only_in_reference"]
                         and not out["only_in_replay"])

    def aligned(frame: Any) -> Any:
        return None if frame is None or not len(frame.columns) else frame.reindex(common)

    nat, ref_nat = aligned(native), aligned(ref_native)

    def native_of(frame: Any, c: str) -> Any:
        return None if frame is None or c not in frame.columns else frame[c].to_numpy(np.float64)
    per_symbol = np.zeros(len(common), np.int64)
    covered = np.ones(len(common), bool)
    if tick_symbols is not None:
        covered = np.isin(syms, sorted(tick_symbols))
        out["tick_rows_not_compared"] = int((~covered).sum())
    for group, cols in groups.items():
        g = {"columns": len(cols), "columns_differ": 0, "value_mismatches": 0,
             "nan_pattern": 0, "max_abs": 0.0, "max_rel": 0.0}
        rows = covered if group in TICK_GROUPS else np.ones(len(common), bool)
        for c in cols:
            x = a[c].to_numpy(np.float64)
            y = b[c].to_numpy(np.float64)
            nx, ny = np.isnan(x), np.isnan(y)
            both = ~nx & ~ny & rows
            bad = both & (x != y)
            pattern = (nx != ny) & rows
            fx, fy = native_of(nat, c), native_of(ref_nat, c)
            if fx is not None and fy is not None:
                apart = ~((fx == fy) | (np.isnan(fx) & np.isnan(fy))) & rows
                hidden = apart & ~bad & ~pattern
                out["native"]["columns"][c] = {"differ": int(apart.sum()),
                                               "delivered_equal": int(hidden.sum())}
                out["native"]["cells_delivered_equal"] += int(hidden.sum())
                if native_cells is not None:
                    native_cells.extend(
                        {"sym": str(syms[i]), "column": c, "group": group, "class": "native",
                         "reference": float(y[i]), "replay": float(x[i]),
                         **_native_fields(fx[i], fy[i])} for i in np.nonzero(hidden)[0])
            if not (bad.any() or pattern.any()):
                continue
            with np.errstate(invalid="ignore", over="ignore"):
                diff = np.where(bad, np.abs(x - y), 0.0)
                scale = np.maximum(np.abs(x), np.abs(y))
                rel = np.where(bad, diff / np.where(scale > 0, scale, 1.0), 0.0)
            # an infinite value against another value: an infinite difference, both ways
            diff = np.where(np.isnan(diff) | np.isinf(diff), math.inf, diff)
            rel = np.where(np.isinf(diff), math.inf, rel)
            ulp = np.where(bad, _ulps(x, y), 0)
            ulp64 = np.where(bad, _ulps64(x, y), 0)
            zero = bad & ((x == 0) | (y == 0))
            kind = kinds.get(c, "value")
            col = {"group": group, "class": kind, "value_mismatches": int(bad.sum()),
                   "nan_only_reference": int((ny & ~nx & rows).sum()),
                   "nan_only_replay": int((nx & ~ny & rows).sum()),
                   "max_abs": float(diff.max()) if len(diff) else 0.0,
                   "max_rel": float(np.nanmax(rel)) if len(rel) else 0.0,
                   "max_ulp": int(ulp.max()) if len(ulp) else 0,
                   "max_ulp64": int(ulp64.max()) if len(ulp64) else 0,
                   "one_side_zero": int(zero.sum()),
                   "max_abs_one_side_zero": float(np.where(zero, diff, 0.0).max())
                   if len(diff) else 0.0,
                   "worst_symbol": str(syms[int(diff.argmax())]) if bad.any()
                   else str(syms[int(pattern.argmax())])}
            if tolerance is not None:
                close = np.isclose(x, y, rtol=tolerance[0], atol=tolerance[1])
                col["beyond_tolerance"] = int((both & ~close).sum())
            out["classes"][kind] += int(bad.sum())
            out["classes"]["missing"] += int(pattern.sum())
            if exceptions is not None:
                for i in np.nonzero(bad | pattern)[0]:
                    exceptions.append({
                        "sym": str(syms[i]), "column": c, "group": group,
                        "class": kind if bad[i] else "missing",
                        "reference": float(y[i]), "replay": float(x[i]),
                        "abs": float(diff[i]) if bad[i] else math.nan,
                        "rel": float(rel[i]) if bad[i] else math.nan,
                        "ulp": int(ulp[i]) if bad[i] else -1,
                        "ulp64": int(ulp64[i]) if bad[i] else -1,
                        **_native_fields(None if fx is None else fx[i],
                                         None if fy is None else fy[i])})
            per_symbol += bad | pattern
            g["columns_differ"] += 1
            g["value_mismatches"] += col["value_mismatches"]
            g["nan_pattern"] += int(pattern.sum())
            g["max_abs"] = max(g["max_abs"], col["max_abs"])
            g["max_rel"] = max(g["max_rel"], col["max_rel"])
            out["columns"][c] = {k: _num(v) if isinstance(v, float) else v
                                 for k, v in col.items()}
        out["groups"][group] = {k: _num(v) if isinstance(v, float) else v for k, v in g.items()}
    out["symbols"] = {str(s): int(n) for s, n in zip(syms, per_symbol, strict=True) if n}
    out["bar_groups_exact"] = (out["keys_exact"]
                               and all(out["groups"][g]["columns_differ"] == 0
                                       for g in BAR_GROUPS if g in out["groups"]))
    out["exact"] = out["keys_exact"] and not out["columns"]
    return out


def _native_fields(ours: Any, theirs: Any) -> dict[str, Any]:
    """The native fields of an exception cell: each side's native value ("" = not captured),
    and with both their absolute and float64 ULP difference ("" unless both are numbers)."""
    out: dict[str, Any] = {"replay_native": "" if ours is None else float(ours),
                           "reference_native": "" if theirs is None else float(theirs),
                           "native_abs": "", "native_ulp64": ""}
    if ours is not None and theirs is not None and not (math.isnan(ours) or math.isnan(theirs)):
        gap = abs(float(ours) - float(theirs))
        out["native_abs"] = 0.0 if ours == theirs else (gap if math.isfinite(gap) else math.inf)
        out["native_ulp64"] = int(_ulps64([ours], [theirs])[0])
    return out


def final_quote_cases(minute: int, rows: Any, scan: dict[str, Any], ticked: set[str],
                      cells: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """The final-quote cases of `minute` (module docstring) from the exception cells of that
    minute against the archive."""
    import datetime

    begin, end = _ns_end(day, minute - 1), _ns_end(day, minute)
    close = _ns_end(day, table_minutes(["15:59"])[0])
    finals = sorted(s for s in rows if s in ticked
                    and begin <= scan["final_quote"].get(s, -1) < end)
    out = []
    for s in finals:
        own = [c for c in cells if c["sym"] == s]
        ts = scan["final_quote"][s]
        out.append({
            "sym": s, "minute": hhmm(minute), "quotes_of_day": scan["quotes"].get(s, 0),
            "final_quote": datetime.datetime.fromtimestamp(ts / 1e9, ticks.NEW_YORK)
            .strftime("%H:%M:%S.%f"),
            "stopped_before_close": ts < close,
            "value_changed": any(c["class"] in ("value", "flag", "categorical") for c in own),
            "missing_changed": any(c["class"] == "missing" for c in own),
            "rank_changed": any(c["class"] == "rank" for c in own),
            "other_rows_rank_changed": len({c["sym"] for c in cells
                                            if c["class"] == "rank" and c["sym"] not in finals})})
    return out


def _summary(cases: list[dict[str, Any]]) -> dict[str, int]:
    return {"cases": len(cases),
            "value_changed": sum(c["value_changed"] for c in cases),
            "missing_changed": sum(c["missing_changed"] for c in cases),
            "rank_changed": sum(c["rank_changed"] for c in cases),
            "minutes_with_downstream_rank_change": len({c["minute"] for c in cases
                                                        if c["other_rows_rank_changed"]}),
            "stopped_before_close": sum(c["stopped_before_close"] for c in cases)}


# --- the replay -----------------------------------------------------------------------------


def choose_minutes(day: str, bars: Any, start: int, rows_at: Any, scan_for: Any,
                   covered: set[str] | None) -> tuple[list[int], dict[str, list[str]]]:
    """The default minutes (module docstring) and why each was chosen."""
    labels = research().labels
    nm = context(day).code.w.NM
    base = table_minutes(list(BASE_MINUTES))
    candidates = covered
    if candidates is None:
        candidates = set().union(*(set(rows_at(m).sym) for m in base))
    scan = scan_for(candidates)
    why: dict[str, list[str]] = {hhmm(m): ["base"] for m in base}

    def add(m: int, reason: str) -> None:
        if 0 <= m < nm:
            why.setdefault(hhmm(m), []).append(reason)

    for s, ts in scan["final_quote"].items():
        add(int((ts - start) // features.NS_PER_MINUTE), f"final quote {s}")
    for s, ts in scan["first"].items():
        if ts < start:
            own = bars[bars.ticker == s].window_start
            if len(own):
                add(int((own.min() - start) // features.NS_PER_MINUTE), f"pre-04:00 carry {s}")
    if scan["peak"] is not None:
        add(int(scan["peak"]), "regular-session peak")
    with _training("replay_minute_invalid"):
        chosen = sorted(labels.cut_minute(x) for x in why)
    return chosen, why




def sample_minutes(chosen: Sequence[int], n: int) -> list[int]:
    """`n` of the chosen minutes, evenly spread from the first to the last (the last always
    among them: the longest prefix); all of them when n is at least their count."""
    if n <= 0:
        return []
    if n >= len(chosen):
        return list(chosen)
    if n == 1:
        return [chosen[-1]]
    span = len(chosen) - 1
    return [chosen[i] for i in sorted({(k * span + (n - 1) // 2) // (n - 1) for k in range(n)})]


def cohort_union(bars: Any, day: str, state: features.DayState, types: set[str] | None,
                 chosen: Sequence[int]) -> set[str]:
    """The symbols with a row at any chosen minute, from the row rule carried minute by minute
    over the whole market (`incremental.Market`, #832) instead of the table code's whole day at
    every minute. The rows themselves are checked against the table code at the minutes
    `--verify-full-prefix` samples."""
    from tickr.model.features import incremental

    mk = incremental.Market(context(day).code.w, state, types)
    frame = features.bars_frame(bars[bars.window_start <= _ns_end(day, -1) + chosen[-1]
                                     * features.NS_PER_MINUTE], day)
    by = dict(tuple(frame.groupby("m", sort=True))) if len(frame) else {}
    empty = frame.iloc[:0]
    want, got = set(chosen), set()
    for m in range(chosen[-1] + 1):
        mk.step(m, by.get(m, empty), want=False)
        if m in want:
            got |= set(mk.tick[mk.cohort()])
    return got


def exact_diff(mine: Any, ref: Any, keys: list[str], table: str,
               cells: list[dict[str, Any]],
               diagnostics: dict[tuple[str, str, str], tuple[str, int]] | None = None
               ) -> dict[str, Any]:
    """`mine` (incremental engine) against `ref` (full-prefix engine), EXACT: keys, their
    order, column names and types, and every value (missing equals missing). Each differing
    cell is appended to `cells` as class `engine`.

    Two cases are classes of their own, listed apart and never counted as passed silently:
    - `zero_row_schema` (codex-110 point 2): both engines have no row, and one has no table at
      all (None) or the two empty tables differ in columns or types (the 2026-11-17 run's
      "types DIFFER" with 0 rows on both sides at 19:59). The part is then exact (no row, no
      value), with `zero_row_schema` naming each side ("no table" / "empty table") and whether
      the empty schemas agree; with rows on either side the full check applies.
    - `diagnostic`: a cell declared in `diagnostics` ((sym, row minute HH:MM, column) -> (name,
      float64 ULP), from config [parity] engine_diagnostics for the run's day) whose difference
      is exactly the declared number of float64 ULP. It is listed in `diagnostic` (and appended
      to `cells` as class `diagnostic`), not counted in `cells`; any other difference of that
      cell, and every undeclared cell, stays an engine difference. No bound is derived."""
    import numpy as np
    import pandas as pd

    shape = {"incremental": "no table" if mine is None else
             ("empty table" if len(mine) == 0 else "rows"),
             "full_prefix": "no table" if ref is None else
             ("empty table" if len(ref) == 0 else "rows")}
    # an engine that has no table (None) has no rows: it stands for an empty table of the
    # other engine's columns and types when the other has one; rows always count
    empty = pd.DataFrame(columns=keys)
    if mine is None:
        mine = ref.iloc[:0] if ref is not None else empty
    if ref is None:
        ref = mine.iloc[:0] if len(mine) == 0 else empty
    out: dict[str, Any] = {"rows_incremental": len(mine), "rows_full_prefix": len(ref),
                           "columns_equal": list(mine.columns) == list(ref.columns),
                           "dtypes_equal": [str(t) for t in mine.dtypes]
                           == [str(t) for t in ref.dtypes], "columns": {}, "diagnostic": [],
                           "zero_row_schema": None}
    if len(mine) == 0 and len(ref) == 0 and ("no table" in shape.values()
                                             or not out["columns_equal"]
                                             or not out["dtypes_equal"]):
        out["zero_row_schema"] = {**shape, "columns_equal": out["columns_equal"],
                                  "dtypes_equal": out["dtypes_equal"]}
        out.update(only_incremental=0, only_full_prefix=0, order_equal=True, cells=0,
                   exact=True)
        return out
    a, b = mine.set_index(keys), ref.set_index(keys)
    out["only_incremental"] = len(a.index.difference(b.index))
    out["only_full_prefix"] = len(b.index.difference(a.index))
    out["order_equal"] = (len(a) == len(b) and bool((a.index == b.index).all()))
    common = b.index[b.index.isin(a.index)]
    a, b = a.loc[common], b.loc[common]
    n = 0
    for c in (c for c in b.columns if c in a.columns):
        x, y = a[c].to_numpy(), b[c].to_numpy()
        if pd.api.types.is_numeric_dtype(a[c]) and pd.api.types.is_numeric_dtype(b[c]):
            x, y = x.astype(np.float64), y.astype(np.float64)
            bad = ~((x == y) | (np.isnan(x) & np.isnan(y)))
        else:
            bad = np.array([not (u == v or (pd.isna(u) and pd.isna(v)))
                            for u, v in zip(x, y, strict=True)], dtype=bool)
        if not bad.any():
            continue
        counted = 0
        for i in np.nonzero(bad)[0]:
            key = common[i]
            num = isinstance(x[i], float)
            with np.errstate(invalid="ignore", over="ignore"):
                diff = abs(x[i] - y[i]) if num else math.nan
            ulp64 = int(_ulps64(x[i:i + 1], y[i:i + 1])[0]) if num else -1
            sym = str(key[0] if isinstance(key, tuple) else key)
            row = hhmm(int(key[1]) - features.DAY_START_MINUTE) if isinstance(key, tuple) else ""
            declared = (diagnostics or {}).get((sym, row, c))
            kind = "engine"
            if declared is not None and num and ulp64 == declared[1]:
                kind = "diagnostic"
                out["diagnostic"].append({"name": declared[0], "sym": sym, "row_minute": row,
                                          "column": c, "ulp64": ulp64,
                                          "incremental": float(x[i]),
                                          "full_prefix": float(y[i])})
            else:
                counted += 1
            cells.append({"sym": sym,
                          "column": c if not isinstance(key, tuple) else f"{c}@m={key[1]}",
                          "group": table, "class": kind,
                          "reference": y[i] if num else str(y[i]),
                          "replay": x[i] if num else str(x[i]), "abs": diff,
                          "rel": diff / max(abs(x[i]), abs(y[i])) if num and diff else math.nan,
                          "ulp": int(_ulps(x[i:i + 1], y[i:i + 1])[0]) if num else -1,
                          "ulp64": ulp64})
        if counted:
            out["columns"][c] = counted
            n += counted
    out["cells"] = n
    out["exact"] = (n == 0 and out["columns_equal"] and out["dtypes_equal"]
                    and out["order_equal"] and not out["only_incremental"]
                    and not out["only_full_prefix"])
    return out


def engine_diagnostics(entries: Sequence[str], day: str
                       ) -> dict[tuple[str, str, str], tuple[str, int]]:
    """The provisional diagnostics of config [parity] engine_diagnostics that name `day`: each
    entry is "name day sym HH:MM column ulp64" (DIAGNOSTIC_FIELDS); one that does not read so
    refuses `parity_diagnostic_invalid`."""
    out: dict[tuple[str, str, str], tuple[str, int]] = {}
    for entry in entries:
        parts = entry.split()
        ok = len(parts) == len(DIAGNOSTIC_FIELDS)
        if ok:
            name, when, sym, row, column, ulp = parts
            try:
                datetime.date.fromisoformat(when)
                datetime.time.fromisoformat(row)
                ok = ulp.isdigit() and int(ulp) > 0 and len(row) == len("HH:MM")
            except ValueError:
                ok = False
        if not ok:
            raise ModelRefused("parity_diagnostic_invalid", entry)
        if when == day:
            out[(sym, row, column)] = (name, int(ulp))
    return out


# --- the results journal ---------------------------------------------------------------------


class Journal:
    """The run's results as they are made: `<out>/parity-<day>.jsonl` (a header line, then one
    line per finished minute) and the exception cells in `<out>/parity-<day>-exceptions.csv`
    (written, flushed and synced before the minute's line, so a minute with a line has all its
    cells). `resume`: an existing journal of the same run (equal header) is continued; its
    minutes are skipped; a last line without its newline (cut by a stop) and the cells of
    minutes without a line are dropped. Without `resume` an existing journal refuses."""

    def __init__(self, out: Path, day: str, resume: bool = False) -> None:
        self.lines = out / f"parity-{day}.jsonl"
        self.cells = out / f"parity-{day}-exceptions.csv"
        self.resume = resume
        self.done: dict[str, dict[str, Any]] = {}

    def open(self, header: dict[str, Any]) -> None:
        header = json.loads(json.dumps({"kind": "header", **header}, default=_plain))
        if self.lines.exists() and self.resume:
            old, records = read_journal(self.lines, cut=True)
            if old != header:
                diff = sorted(k for k in set(old) | set(header) if old.get(k) != header.get(k))
                raise ModelRefused("replay_resume_mismatch", ", ".join(diff))
            self.done = {r["minute"]: r for r in records if r["kind"] == "minute"}
            self._keep_cells(set(self.done))
            return
        for f in (self.lines, self.cells):
            if f.exists():
                raise ModelRefused("output_exists", str(f))
        with self.cells.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=EXCEPTION_FIELDS).writeheader()
        self._append(self.lines, json.dumps(header, sort_keys=True) + "\n")

    def _keep_cells(self, minutes: set[str]) -> None:
        if not self.cells.exists():
            raise ModelRefused("replay_resume_mismatch", f"{self.cells.name} missing")
        tmp = self.cells.with_name(self.cells.name + ".tmp")
        with self.cells.open(newline="", encoding="utf-8") as f, \
                tmp.open("w", newline="", encoding="utf-8") as g:
            out = csv.DictWriter(g, fieldnames=EXCEPTION_FIELDS)
            out.writeheader()
            out.writerows(r for r in csv.DictReader(f) if r["minute"] in minutes)
        os.replace(tmp, self.cells)

    @staticmethod
    def _append(path: Path, body: str) -> None:
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())

    def add(self, record: dict[str, Any], cells: list[dict[str, Any]]) -> None:
        if cells:
            buffer = io.StringIO()
            csv.DictWriter(buffer, fieldnames=EXCEPTION_FIELDS).writerows(cells)
            self._append(self.cells, buffer.getvalue())
        self._append(self.lines, json.dumps({"kind": "minute", **record}, sort_keys=True,
                                            default=_plain) + "\n")
        self.done[record["minute"]] = record

    def stop(self, record: dict[str, Any]) -> None:
        """The run stops at a guard's refusal (`STOP_CAUSES`): one `stop` line, synced."""
        self._append(self.lines, json.dumps({"kind": "stop", **record}, sort_keys=True,
                                            default=_plain) + "\n")


def _plain(x: Any) -> Any:
    """numpy scalars in a record as JSON numbers."""
    if hasattr(x, "item"):
        return x.item()
    raise TypeError(type(x).__name__)


def read_journal(path: Path, cut: bool = False) -> tuple[dict[str, Any],
                                                         list[dict[str, Any]]]:
    """The header and minute records of a journal. A last line without its newline is an
    unfinished write: dropped (and, with `cut`, removed from the file); any other unreadable
    line refuses."""
    body = path.read_bytes()
    keep = body[:body.rfind(b"\n") + 1]
    if cut and keep != body:
        with path.open("r+b") as f:
            f.truncate(len(keep))
            f.flush()
            os.fsync(f.fileno())
    lines = keep.decode("utf-8").splitlines()
    try:
        rows = [json.loads(x) for x in lines]
    except ValueError as exc:
        raise ModelRefused("replay_journal_unreadable", path.name) from exc
    if not rows or rows[0].get("kind") != "header" or any(r.get("kind") not in ("minute", "stop")
                                                          for r in rows[1:]):
        raise ModelRefused("replay_journal_unreadable", path.name)
    return rows[0], rows[1:]


def build_report(header: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """The report of a run from its journal: the header and the records of the finished
    minutes, in minute order. A run stopped early gives the report of its finished minutes
    (`minutes_done` against `minutes`); `stopped` is the run's last stop line (a guard's
    refusal) when no minute was finished after it, else None."""
    report = {k: v for k, v in header.items() if k != "kind"}
    stops = [i for i, r in enumerate(records) if r["kind"] == "stop"]
    last = records[stops[-1]] if stops and stops[-1] == len(records) - 1 else None
    report["stopped"] = None if last is None else {k: v for k, v in last.items() if k != "kind"}
    records = sorted((r for r in records if r["kind"] == "minute"), key=lambda r: r["m"])
    report["minutes_done"] = [r["minute"] for r in records]
    report["complete"] = report["minutes_done"] == report["minutes"]
    report.update(variants={}, ticks={}, final_quote={}, versus_plain={})
    run = report["variants_run"]
    for variant in run:
        if not report["ticks_only"]:
            report["variants"][variant] = {r["minute"]: r["variants"][variant] for r in records}
            cases = [c for r in records for c in r["final_quote"][variant]]
            report["final_quote"][variant] = {"summary": _summary(cases), "cases": cases}
        if any(variant in r["ticks"] for r in records):
            report["ticks"][variant] = {r["minute"]: r["ticks"][variant] for r in records}
        if variant != "plain" and any(variant in r["versus_plain"] for r in records):
            report["versus_plain"][variant] = {r["minute"]: r["versus_plain"][variant]
                                               for r in records}
    checked = {r["minute"]: r["engine"] for r in records if r["engine"]}
    report["engine_check"] = {
        "minutes": sorted(checked, key=lambda hm: table_minutes([hm])[0]),
        "differing_minutes": [hm for hm, e in checked.items()
                              if not all(x["exact"] for x in _engine_parts(e))],
        "diagnostic": [{"minute": hm, **d} for hm, e in checked.items()
                       for v, x in e.get("variants", {}).items()
                       for d in [{"variant": v, **c} for c in x["ticks"].get("diagnostic", [])]],
        "zero_row_schema": [{"minute": hm, "table": name, **x["zero_row_schema"]}
                            for hm, e in checked.items() for name, x in _engine_named(e)
                            if x.get("zero_row_schema")],
        "native_cells_delivered_equal": {
            hm: {v: x["features"]["native_cells_delivered_equal"]
                 for v, x in e.get("variants", {}).items() if "features" in x}
            for hm, e in checked.items()},
        "per_minute": checked}
    report["ranks"] = {r["minute"]: r["ranks"] for r in records if r.get("ranks")}
    report["exceptions"] = sum(r["cells"] for r in records)
    report["seconds"] = {r["minute"]: r["seconds"] for r in records}
    report["resources"] = {r["minute"]: r["resources"] for r in records if "resources" in r}
    return report


def _engine_parts(check: dict[str, Any]) -> list[dict[str, Any]]:
    """The exact parts of a minute's engine check: the bar table, each variant's tick table
    and, where the tick tables were not bit-equal, the delivered features they moved."""
    parts = [check["bars"]] if "bars" in check else []
    for v in check.get("variants", {}).values():
        parts.append(v["ticks"])
        if "features" in v:
            parts.append(v["features"])
    return parts


def _engine_named(check: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    named = [("bars", check["bars"])] if "bars" in check else []
    return named + [(f"ticks {v}", x["ticks"]) for v, x in check.get("variants", {}).items()]


# --- the replay -------------------------------------------------------------------------------


def replay(day: str, minutes: Sequence[str] | None, inputs: ReplayInputs, *,
           variants: Sequence[str] = ("plain",), work: Path,
           tolerance: tuple[float, float] | None = None, all_minutes: bool = False,
           ticks_only: bool = False, exceptions: list[dict[str, Any]] | None = None,
           engine: str = "full-prefix", verify: int = 0, journal: Journal | None = None,
           table_workers: int = 1, workers: int = 1,
           room: headroom.Headroom | None = None,
           diagnostics: Sequence[str] | None = None) -> dict[str, Any]:
    """The parity report of `day` at `minutes` (HH:MM; None = the default choice): see the
    module docstring. Exception cells are appended to `exceptions` when given; each finished
    minute goes to `journal` when given (its finished minutes are skipped). `room` guards disk
    and memory (default: `[parity]` of config/bot.toml on `work`); a minute it refuses stops
    the run, recorded in the journal and the report's `stopped`. `diagnostics`: the
    provisional engine diagnostics (default: config/bot.toml's [parity] engine_diagnostics)."""
    from tickr.model import massive

    need = day_files(day, inputs.bars)  # before any file is opened
    if inputs.tick_form not in FORMS:
        raise ModelRefused("replay_tick_form", inputs.tick_form)
    if any(v not in VARIANTS for v in variants) or not variants:
        raise ModelRefused("replay_variant", ", ".join(variants))
    if engine not in ENGINES:
        raise ModelRefused("replay_engine", engine)
    if verify and (engine != "incremental" or ticks_only):
        raise ModelRefused("replay_engine", "--verify-full-prefix needs --engine incremental "
                           "and the features")
    covered = covered_tickers(inputs)
    if (covered is not None or ticks_only) and inputs.tk61 is None:
        raise ModelRefused("replay_tk61_missing", "the audit form and --ticks-only need tk61")
    if inputs.tick_form == "audit":
        check_audit(inputs)
    if minutes is not None:
        chosen = table_minutes(minutes)
    labels = research().labels
    ctx = context(day)
    groups = full.full_groups()
    want = None if ticks_only else ReferenceTable(inputs.table, day)
    if want is not None:
        want.check_flags()
    kinds = None if want is None else column_kinds(full.full_features())
    types, types_md5 = _types(inputs.types)
    splits = _splits(inputs.reference) if not ticks_only else {}
    frames = [(d, massive.read_day_file((inputs.bars / f"{d}.csv.gz").read_bytes(), d))
              for d in need]
    state = features.history(frames[:-1], splits, day, types)
    bars = frames[-1][1]
    start = _ns_end(day, -1)

    def bars_through(minute: int) -> Any:
        return bars[bars.window_start <= start + minute * features.NS_PER_MINUTE]

    rows: dict[int, Any] = {}

    def rows_at(m: int) -> Any:
        if m not in rows:
            rows[m] = features.minute_rows(bars_through(m), day, m, state, types)
        return rows[m]

    scans: dict[str, Any] = {}

    def scan_for(symbols: set[str] | None) -> dict[str, Any]:
        scans["scan"] = scan_events(inputs, day, symbols)
        return scans["scan"]

    why: dict[str, list[str]] = {}
    if all_minutes:
        chosen = list(range(ctx.code.w.NM))
    elif minutes is None:
        chosen, why = choose_minutes(day, bars, start, rows_at, scan_for, covered)
    if engine == "incremental":
        cohort = cohort_union(bars, day, state, types, chosen)
    else:
        cohort = set().union(*(set(rows_at(m).sym) for m in chosen))
    ticked = cohort if covered is None else cohort & covered
    if "scan" not in scans:
        scan_for(ticked)
    if room is None:
        limits = headroom.Limits.from_config(config.load(DEFAULT_CONFIG).parity)
        room = headroom.Headroom(work, limits)
    if diagnostics is None:
        diagnostics = config.load(DEFAULT_CONFIG).parity.engine_diagnostics
    declared = engine_diagnostics(diagnostics, day)
    extract_events(inputs, ticked, _ns_end(day, chosen[-1]), work, room)
    last = chosen[-1] + features.DAY_START_MINUTE
    fill = (tick_table(inputs.tk61, cohort - ticked, last)
            if covered is not None and cohort - ticked else None)
    tk61 = tick_table(inputs.tk61, ticked, last) if inputs.tk61 is not None else None
    checks = sample_minutes(chosen, verify)
    header: dict[str, Any] = {
        "day": day, "minutes": [hhmm(m) for m in chosen], "chosen_because": why,
        "history": need[:-1], "inputs": {k: str(v) for k, v in inputs.__dict__.items()},
        "types_md5": types_md5, "types_is_training_pin": types_md5 == labels.TYPES_MD5,
        "tick_threads": os.environ.get("TICK_THREADS"), "tick_groups": ticks.default_groups(),
        "threshold": "exact", "tolerance": None if tolerance is None
        else {"rtol": tolerance[0], "atol": tolerance[1]},
        "tick_form": inputs.tick_form, "covered_tickers": None if covered is None
        else len(covered), "converted_symbols": len(ticked),
        "tick_rows_from_tk61_symbols": len(cohort - ticked) if fill is not None else 0,
        "bar_groups": list(BAR_GROUPS), "column_classes": kinds, "variants_run": list(variants),
        "ticks_only": ticks_only, "engine": engine,
        "verify_full_prefix": [hhmm(m) for m in checks],
        "engine_diagnostics": sorted(" ".join([n, day, sym, row, col, str(u)])
                                     for (sym, row, col), (n, u) in declared.items()),
        "native_groups": list(NATIVE_GROUPS),
        "reference_native": "tk61" if inputs.tk61 is not None else None}
    if journal is None:
        journal = Journal(work, day)
    journal.open(header)
    walk = table = None
    if engine == "incremental":
        from tickr.model import parity_walk
        from tickr.model.features import incremental

        walk = parity_walk.TickWalk(room, day, plain=any(v != "marker" for v in variants),
                                    groups=ticks.default_groups())
        if not ticks_only:
            table = incremental.DayTable(ctx, state, types, table_workers)
    run = _Run(day, tuple(variants), work, walk, table, state, types, inputs, want, groups,
               kinds, tolerance, covered, tk61, fill, ticked, scans["scan"], ticks_only,
               bars_through, room, declared)
    pool: Any = None
    pending: list[tuple[Any, dict[str, Any], list[dict[str, Any]]]] = []

    at = [chosen[0]]  # the minute being worked, for a stop

    def finish(job: Any, record: dict[str, Any], cells: list[dict[str, Any]]) -> None:
        at[0] = record["m"]
        got = job.result() if pool is not None else job
        _finish(run, record, got, cells)
        record["resources"] = room.usage()
        if exceptions is not None:
            exceptions.extend(cells)
        journal.add(record, cells)

    try:
        if workers > 1 and not ticks_only:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            pool = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                                       initializer=_worker_start, initargs=(tempfile.tempdir,))
        try:
            for m in chosen:
                at[0] = m
                through = m + features.DAY_START_MINUTE
                walked = walk.seconds if walk is not None else 0.0
                if walk is not None:
                    walk.advance(through)
                if hhmm(m) in journal.done:
                    continue
                cells: list[dict[str, Any]] = []
                record, job = _prepare(run, m, m in checks, cells)
                if walk is not None:  # the walk's conversion since the previous chosen minute
                    record["seconds"]["walk"] = round(walk.seconds - walked, 3)
                if job is not None and pool is not None:
                    pending.append((pool.submit(_assemble, *job), record, cells))
                    while len(pending) > 2 * workers or (pending and pending[0][0].done()):
                        finish(*pending.pop(0))
                else:
                    finish(None if job is None else _assemble(*job), record, cells)
            while pending:
                finish(*pending.pop(0))
        except ModelRefused as exc:
            if exc.cause not in STOP_CAUSES:
                raise
            # the run stops here; minutes still pending get no line (a resume redoes them)
            journal.stop({"minute": hhmm(at[0]), "m": at[0], "cause": exc.cause,
                          "detail": exc.detail, "limits": room.limits.__dict__,
                          "resources": room.usage()})
    finally:
        for future, _, _ in pending:
            future.cancel()
        if pool is not None:
            pool.shutdown(cancel_futures=True)
        if walk is not None:
            walk.close()
        if table is not None:
            table.close()
    return build_report(*read_journal(journal.lines))


@dataclass
class _Run:
    """What every minute of one replay reads."""

    day: str
    variants: tuple[str, ...]
    work: Path
    walk: Any
    table: Any
    state: features.DayState
    types: set[str] | None
    inputs: ReplayInputs
    want: Any
    groups: dict[str, list[str]]
    kinds: Any
    tolerance: tuple[float, float] | None
    covered: set[str] | None
    tk61: Any
    fill: Any
    ticked: set[str]
    scan: dict[str, Any]
    ticks_only: bool
    bars_through: Any
    room: headroom.Headroom
    diagnostics: dict[tuple[str, str, str], tuple[str, int]]


def _prepare(run: _Run, m: int, check: bool, cells: list[dict[str, Any]]) -> tuple[
        dict[str, Any], tuple[Any, ...] | None]:
    """One minute up to the features: the table rows, each variant's tick table (with `check`
    the engine check of both), the tick comparison with tk61, and the arguments of
    `_assemble` (None with --ticks-only); its record so far."""
    import time

    hm = hhmm(m)
    through = m + features.DAY_START_MINUTE
    record: dict[str, Any] = {"minute": hm, "m": m, "variants": {}, "ticks": {},
                              "final_quote": {}, "versus_plain": {}, "engine": {},
                              "seconds": {}}
    bars_now = run.bars_through(m)
    rows = None
    if run.table is not None:
        t0 = time.perf_counter()
        ctx = context(run.day)
        mark = ctx.mark(m)
        received = received_bars(bars_now, mark)
        rows = run.table.rows(received, m, mark) if len(bars_now) else None
        _clock(record, "bars", t0)
        if check:
            t0 = time.perf_counter()
            ref = (ctx.table_rows(received, m, run.state, run.types, mark)
                   if len(bars_now) else None)
            differ: list[dict[str, Any]] = []
            record["engine"]["bars"] = exact_diff(rows, ref, ["sym"], "engine-bars", differ)
            _collect(cells, differ, "all", m, "engine")
            _clock(record, "verify", t0)
    files = {}
    for variant in run.variants:
        arg = False if variant == "plain" else variant
        t0 = time.perf_counter()
        mine = run.walk.table(variant, through) if run.walk is not None else None
        if mine is None:
            mine = minute_ticks(run.room, run.day, m, arg)
        _clock(record, "ticks", t0)
        if run.walk is not None and check:
            t0 = time.perf_counter()
            ref = minute_ticks(run.room, run.day, m, arg)
            differ = []
            record["engine"].setdefault("variants", {})[variant] = {
                "ticks": exact_diff(mine, ref, ["sym", "m"], "engine-ticks", differ,
                                    run.diagnostics)}
            _collect(cells, differ, variant, m, "engine")
            _clock(record, "verify", t0)
        if run.tk61 is not None:
            one: list[dict[str, Any]] = []
            r = compare(mine[mine.m == through].drop(columns="m"),
                        run.tk61[run.tk61.m == through].drop(columns="m"),
                        {"ticks": _tick_value_columns()}, run.tolerance,
                        dict.fromkeys(_tick_value_columns(), "value"), None, one)
            record["ticks"][variant] = {"minute": m, **r}
            _collect(cells, one, variant, m, "ticks")
        if not run.ticks_only:
            t0 = time.perf_counter()
            files[variant] = _tick_file(mine, run.fill, through,
                                        run.work / f"tk-{variant}-{m}.parquet")
            _clock(record, "ticks", t0)
    if run.ticks_only:
        return record, None
    if run.table is None:  # full-prefix: the table code on the whole market's day
        return record, (run.day, m, files, None, run.state, run.types, run.inputs, bars_now)
    prev = features.DayState(prev={s: run.state.prev[s] for s in
                                   ([] if rows is None else rows.sym) if s in run.state.prev},
                             mprev={}, hist=collections.deque())
    return record, (run.day, m, files, rows, prev, run.types, run.inputs, None)


def _worker_start(tmp: str | None) -> None:
    """A feature worker's temporary folders in the run's work folder, as the main process's."""
    tempfile.tempdir = tmp


def _assemble(day: str, m: int, files: dict[str, Path], rows: Any, state: features.DayState,
              types: set[str] | None, inputs: ReplayInputs,
              bars_now: Any) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Each variant's 191 columns of minute m and its native values (`bot_rows`) from its tick
    file (deleted afterwards): from
    the table rows `rows` (incremental) or from the bars so far (full-prefix). Runs in a
    worker process with --workers."""
    import time

    t0 = time.perf_counter()
    got, natives = {}, {}
    for variant, tick_file in files.items():
        got[variant], natives[variant] = bot_rows(day, m, tick_file, inputs, state, types,
                                                  bars_now=bars_now, rows=rows)
        tick_file.unlink()
    return got, natives, time.perf_counter() - t0


def bot_rows(day: str, m: int, tick_file: Path, inputs: ReplayInputs,
             state: features.DayState, types: set[str] | None, *, bars_now: Any = None,
             rows: Any = None) -> tuple[Any, Any]:
    """The bot's 191 columns of minute m in the live date context of the day (module
    docstring, REPLAY 3): `LiveContext.minute_rows` on the bars so far (`bars_now`), or
    `LiveContext.assemble_rows` on the table rows `rows`, from the snapshot as of the mark.
    With them, the NATIVE_GROUPS values of the same rows at their native type
    (`native_values`), indexed by symbol."""
    ctx = context(day)
    mark = ctx.mark(m)
    work = Path(tempfile.mkdtemp(prefix="parity-snapshot-"))
    try:
        given = snapshot(day, m, tick_file, inputs.reference, inputs.benzinga, inputs.fl_facts,
                         mark, work / "in")
        # Not under features._LOCK: minute_rows / assemble_rows take that (non-reentrant) lock
        # themselves, and ctx.code is the context's private modules, walked serially per process.
        with native_values(ctx.code) as found:
            if bars_now is not None:
                got = ctx.minute_rows(received_bars(bars_now, mark), m, state, types, given,
                                      mark)
            else:
                got = ctx.assemble_rows(rows, m, state, given, mark)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return got, _native_frame(found, got.sym if found else [])


def received_bars(bars: Any, received: datetime.datetime) -> Any:
    """The bars with `received` as their receipt (`full.RECEIPT_COLUMN`)."""
    ns = (received - _EPOCH) // datetime.timedelta(microseconds=1) * 1000
    return bars.assign(**{full.RECEIPT_COLUMN: ns})


def snapshot(day: str, minute: int, tick_file: Path, reference: Path, benzinga: Path,
             fl_facts: Path, received: datetime.datetime, work: Path) -> full.FullInputs:
    """The inputs as the bot holds them at the mark of `minute`: cut by the training's own
    `l1feat.cut_inputs` at the minute, every record carrying `received` as its receipt
    (`full.RECEIPT_COLUMN` / `full.RECEIPT_FIELD`), in the training's formats. The cut copies
    are removed once stamped."""
    import duckdb

    feat = research().feat
    work.mkdir()
    cut = work / "cut"
    cut.mkdir()
    with _training("replay_input_unreadable"), contextlib.redirect_stdout(io.StringIO()):
        tk, bz, ref, _ = feat.cut_inputs(day, minute, str(tick_file), str(benzinga), str(cut),
                                         str(reference))
    stamp = received.astimezone(datetime.UTC).isoformat()
    ns = (received - _EPOCH) // datetime.timedelta(microseconds=1) * 1000
    root = work / "received"
    root.mkdir()
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT *, CAST({int(ns)} AS BIGINT) AS {full.RECEIPT_COLUMN} FROM "
                    f"read_parquet(?)) TO '{root / 'tk.parquet'}' (FORMAT parquet)", [tk])
    finally:
        con.close()
    Path(tk).unlink()

    def stamped(line: str) -> str:
        return json.dumps({**json.loads(line), full.RECEIPT_FIELD: stamp}) + "\n"

    (root / "bz").mkdir()
    for path in sorted(Path(bz).iterdir()):
        with gzip.open(path, "rt", encoding="utf-8") as f, \
                gzip.open(root / "bz" / path.name, "wt", encoding="utf-8") as g:
            g.write("".join(stamped(x) for x in f if x.strip()))
    (root / "ref").mkdir()
    manifest = json.loads((Path(ref) / "manifest.json").read_text("utf-8"))
    for name in manifest["files"]:
        if not (Path(ref) / name).is_file():
            continue  # the training's own ref_view refuses a needed file that is missing
        lines = (Path(ref) / name).read_text("utf-8").splitlines()
        (root / "ref" / name).write_text("".join(stamped(x) for x in lines if x.strip()),
                                         "utf-8")
        manifest["files"][name] = feat.ident(str(root / "ref" / name))
    (root / "ref" / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), "utf-8")
    lines = Path(fl_facts).read_text("utf-8").splitlines()
    (root / "fl.csv").write_text("\n".join([f"{lines[0]},{full.RECEIPT_FIELD}",
                                            *(f"{x},{stamp}" for x in lines[1:])]) + "\n",
                                 "utf-8")
    shutil.rmtree(cut, ignore_errors=True)
    return full.FullInputs(root / "tk.parquet", root / "ref", root / "bz", root / "fl.csv")


def _finish(run: _Run, record: dict[str, Any], assembled: Any,
            cells: list[dict[str, Any]]) -> None:
    """The minute's comparisons from its features (`_assemble`'s result, None with
    --ticks-only): each variant against the archive (with the native values of both sides
    when the minute has a tk or tk2 exception and --tk61 is given), its final-quote cases, the
    rank recompute of its kr_* exceptions, the engine difference's feature cells (delivered
    and native), and each variant against plain."""
    import time

    m = record["m"]
    record["cells"] = len(cells)
    if assembled is None:
        return
    delivered, natives, spent = assembled
    record["seconds"]["features"] = round(spent, 3)
    want = run.want.minute(m)
    theirs: list[Any] = []  # the training's native values of the minute, made once if needed

    def reference_native() -> Any:
        if not theirs:
            theirs.append(reference_natives(run.inputs.tk61, run.day, want, run.work))
        return theirs[0]

    record["ranks"] = {}
    for variant in run.variants:
        got = delivered[variant]
        t0 = time.perf_counter()
        one: list[dict[str, Any]] = []
        r = compare(got, want, run.groups, run.tolerance, run.kinds, run.covered, one,
                    natives[variant])
        if run.inputs.tk61 is not None and any(c["group"] in NATIVE_GROUPS for c in one):
            one = []
            r = compare(got, want, run.groups, run.tolerance, run.kinds, run.covered, one,
                        natives[variant], reference_native())
        record["variants"][variant] = {"minute": m, **r}
        _collect(cells, one, variant, m, "features")
        record["final_quote"][variant] = final_quote_cases(m, set(got.sym), run.scan,
                                                           run.ticked, one, run.day)
        if any(c["column"].startswith(KR_PREFIX) for c in one):
            record["ranks"][variant] = rank_recompute(
                got, natives[variant], want, theirs[0] if theirs else None, one)
        _clock(record, "compare", t0)
        parts = record["engine"].get("variants", {}).get(variant)
        if parts is not None and not (parts["ticks"]["exact"] and not parts["ticks"]["diagnostic"]
                                      and record["engine"]["bars"]["exact"]):
            # the engines differ in an input (a declared diagnostic cell included): the feature
            # cells that difference moves, delivered (exact) and native (raw, reported even
            # when the delivered float32 agree)
            t0 = time.perf_counter()
            arg = False if variant == "plain" else variant
            through = m + features.DAY_START_MINUTE
            ref_ticks = _tick_file(minute_ticks(run.room, run.day, m, arg), run.fill, through,
                                   run.work / f"tk-ref-{variant}-{m}.parquet")
            ref, ref_native = bot_rows(run.day, m, ref_ticks, run.inputs, run.state, run.types,
                                       bars_now=run.bars_through(m))
            ref_ticks.unlink()
            moved: list[dict[str, Any]] = []
            raw: list[dict[str, Any]] = []
            r = compare(got, ref, run.groups, None, run.kinds, None, moved, natives[variant],
                        ref_native, raw)
            parts["features"] = {"exact": r["exact"], "classes": r["classes"],
                                 "columns": sorted(r["columns"]),
                                 "native_cells_delivered_equal":
                                     r["native"]["cells_delivered_equal"],
                                 "native_columns": sorted(c for c, n in
                                                          r["native"]["columns"].items()
                                                          if n["differ"])}
            _collect(cells, [{**c, "class": "engine", "group": "engine-features"}
                             for c in moved], variant, m, "engine")
            _collect(cells, [{**c, "class": "engine-native", "group": "engine-native"}
                             for c in raw], variant, m, "engine")
            _clock(record, "verify", t0)
    if "plain" in delivered:
        for variant in run.variants:
            if variant != "plain":
                record["versus_plain"][variant] = compare(
                    delivered[variant], delivered["plain"], run.groups, None, run.kinds,
                    run.covered)["classes"]
    record["cells"] = len(cells)


def rank_recompute(got: Any, native: Any, want: Any, ref_native: Any,
                      cells: list[dict[str, Any]]) -> dict[str, Any]:
    """codex-110 point 1, for each kr_* column with an exception at this minute: the minute's
    pct ranks recomputed from NATIVE values (float64, before the float32 cast) over the whole
    cohort, every row of the minute, the training's way (wm2tk_l1.py: `pd.Series(out[c])
    .groupby(rq).rank(pct=True)` on float64, then the cast). Our native k_X of the minute, with
    the training's native k_X put in for the SUBSTITUTED symbols (those with a k_X exception at
    this minute), is ranked and cast; against the training table's kr_X:
    - `remaining`: original mismatches that remain after the substitution;
    - `changed`: previously matching ranks that the substitution changes;
    - `training_rank_mismatches`: the training's own native k_X of its rows ranked the same way
      against its stored kr_X (0 = the native training values reproduce the table's ranks);
    - `hidden_native`: symbols not substituted whose native k_X differ although the float32
      values agree (they move ranks that float32 values cannot explain).
    Each list holds sym, our delivered rank, the training's and the recomputed one. When a
    native input is missing, `missing` says exactly which, and nothing is approximated."""
    import numpy as np
    import pandas as pd

    def same(u: Any, v: Any) -> Any:
        return (u == v) | (np.isnan(u) & np.isnan(v))

    out: dict[str, Any] = {}
    for col in sorted({c["column"] for c in cells if c["column"].startswith(KR_PREFIX)}):
        src = K_PREFIX + col[len(KR_PREFIX):]
        r: dict[str, Any] = {"source": src}
        missing = []
        if src not in native.columns:
            missing.append(f"our native {src}: not captured at this minute")
        if ref_native is None:
            missing.append(f"the training's native {src}: the training table stores float32 "
                           "only; it is recomputed from --tk61 (the archived v11 tick table), "
                           "which this run was not given")
        elif src not in ref_native.columns:
            missing.append(f"the training's native {src}: not computed from --tk61")
        if missing:
            out[col] = {**r, "missing": missing}
            continue
        syms = got.sym.to_numpy(object)
        theirs = ref_native[src]
        subst = sorted({c["sym"] for c in cells if c["column"] == src
                        and c["class"] in ("value", "missing")} & set(theirs.index))
        x = native[src].reindex(syms).to_numpy(np.float64).copy()
        where = {s: k for k, s in enumerate(syms)}
        for s in subst:
            x[where[s]] = theirs[s]
        rq = got.m.to_numpy(np.int64)
        again = pd.Series(pd.Series(x).groupby(rq).rank(pct=True).to_numpy().astype(np.float32),
                          index=syms)
        own = pd.Series(pd.Series(theirs.to_numpy(np.float64)).groupby(
            want.m.to_numpy(np.int64)).rank(pct=True).to_numpy().astype(np.float32),
            index=want.sym.to_numpy(object))
        common = [s for s in want.sym if s in where]
        ours = got.set_index("sym")[col].reindex(common).to_numpy(np.float32)
        table = want.set_index("sym")[col].reindex(common).to_numpy(np.float32)
        new = again.reindex(common).to_numpy(np.float32)
        before, after = same(ours, table), same(new, table)
        lost = native[src].reindex(common).to_numpy(np.float64)
        found = theirs.reindex(common).to_numpy(np.float64)
        hidden = ~same(lost, found) & same(lost.astype(np.float32), found.astype(np.float32))
        put = set(subst)
        rows = list(zip(common, ours, table, new, strict=True))

        def listed(mask: Any, rows: list[Any] = rows) -> list[dict[str, Any]]:
            return [{"sym": str(rows[k][0]), "replay": float(rows[k][1]),
                     "reference": float(rows[k][2]), "recomputed": float(rows[k][3])}
                    for k in np.nonzero(mask)[0]]

        ref_rows = want.sym.to_numpy(object)
        own_bad = ~same(own.to_numpy(np.float32), want[col].to_numpy(np.float32))
        r.update({"rows_replay": len(got), "rows_reference": len(want),
                  "cohort_equal": list(syms) == list(ref_rows),
                  "substituted": subst, "original_mismatches": int((~before).sum()),
                  "resolved": int((~before & after).sum()),
                  "remaining": listed(~before & ~after), "changed": listed(before & ~after),
                  "training_rank_mismatches": int(own_bad.sum()),
                  "hidden_native": [str(common[k]) for k in np.nonzero(hidden)[0]
                                    if common[k] not in put]})
        out[col] = r
    return out


def _clock(record: dict[str, Any], name: str, t0: float) -> None:
    import time

    spent = record["seconds"]
    spent[name] = round(spent.get(name, 0.0) + time.perf_counter() - t0, 3)


def _tick_file(mine: Any, fill: Any, through: int, path: Path) -> Path:
    table = mine
    if fill is not None:
        table = (_concat(mine, fill[fill.m <= through])
                 .sort_values(["sym", "m"], kind="stable").reset_index(drop=True))
    return write_tick_file(table, path)


def _concat(a: Any, b: Any) -> Any:
    import pandas as pd

    return pd.concat([a, b.astype(a.dtypes.to_dict())], ignore_index=True) if len(b) else a


def _collect(into: list[dict[str, Any]] | None, cells: list[dict[str, Any]], variant: str,
             minute: int, table: str) -> None:
    if into is not None:
        into.extend({"variant": variant, "minute": hhmm(minute), "table": table, **c}
                    for c in cells)


def text(report: dict[str, Any]) -> str:
    """The short text form of a report."""
    lines = [f"replay parity {report['day']}: minutes {' '.join(report['minutes'])}; "
             f"variants {', '.join(report['variants']) or 'none (ticks only)'}; TICK_THREADS "
             f"{report['tick_threads'] or 'unset'}; types md5 {report['types_md5'] or 'none'} "
             f"(training pin: {'yes' if report['types_is_training_pin'] else 'no'}); tick form "
             f"{report['tick_form']}, converted symbols {report['converted_symbols']}, tick rows "
             f"from tk61 for {report['tick_rows_from_tk61_symbols']}; threshold exact; engine "
             f"{report['engine']}; minutes done {len(report['minutes_done'])} of "
             f"{len(report['minutes'])}"]
    check = report["engine_check"]
    if report["verify_full_prefix"]:
        lines.append(f"engine check (incremental against full-prefix, exact) at "
                     f"{len(check['minutes'])} minutes: "
                     + ("all exact" if not check["differing_minutes"] else
                        f"ENGINE DIFFERENCE at {' '.join(check['differing_minutes'])}")
                     + f"; zero-row schema parts {len(check.get('zero_row_schema', []))}")
        for hm in check["differing_minutes"]:
            e = check["per_minute"][hm]
            lines.append(f"  {hm}: bars {_engine_text(e['bars'])}; " + "; ".join(
                f"{v} ticks {_engine_text(x['ticks'])}"
                + (f", features {x['features']['classes']}" if "features" in x else "")
                for v, x in e.get("variants", {}).items()))
        for d in check.get("diagnostic", []):
            lines.append(f"  PROVISIONAL DIAGNOSTIC {d['name']} at {d['minute']} ({d['variant']}):"
                         f" {d['column']} {d['sym']} row {d['row_minute']}, {d['ulp64']} float64 "
                         f"ULP ({d['incremental']!r} / {d['full_prefix']!r})")
        for z in check.get("zero_row_schema", []):
            lines.append(f"  ZERO-ROW SCHEMA at {z['minute']} {z['table']}: incremental "
                         f"{z['incremental']}, full-prefix {z['full_prefix']}, columns "
                         f"{'equal' if z['columns_equal'] else 'DIFFER'}, types "
                         f"{'equal' if z['dtypes_equal'] else 'DIFFER'}")
        for hm, per in check.get("native_cells_delivered_equal", {}).items():
            for v, n in per.items():
                if n:
                    lines.append(f"  NATIVE at {hm} ({v}): {n} feature cells differ at their "
                                 "native type while the delivered float32 agree")
    for variant, per in report["variants"].items():
        for hm, r in per.items():
            lines.append(
                f"{variant} {hm}: rows reference {r['rows_reference']} replay "
                f"{r['rows_replay']} compared {r['rows_compared']}; only in reference "
                f"{len(r['only_in_reference'])}, only in replay {len(r['only_in_replay'])}; "
                f"order equal {'yes' if r['order_equal'] else 'NO'}; bar groups exact: "
                f"{'yes' if r['bar_groups_exact'] else 'NO'}; exact: "
                f"{'yes' if r['exact'] else 'no'}; cells by class {r['classes']}")
            for group, g in r["groups"].items():
                if g["columns_differ"]:
                    lines.append(f"  {group}: {g['columns_differ']} of {g['columns']} columns "
                                 f"differ, {g['value_mismatches']} value cells, "
                                 f"{g['nan_pattern']} missing-pattern cells, max abs "
                                 f"{g['max_abs']}, max rel {g['max_rel']}")
            for name, c in r["columns"].items():
                extra = (f", beyond tolerance {c['beyond_tolerance']}"
                         if "beyond_tolerance" in c else "")
                lines.append(f"    {name} ({c['class']}): {c['value_mismatches']} values, "
                             f"missing only in reference {c['nan_only_reference']} / replay "
                             f"{c['nan_only_replay']}, max abs {c['max_abs']}, max rel "
                             f"{c['max_rel']}, max ulp {c['max_ulp']}, one side zero "
                             f"{c['one_side_zero']}, worst {c['worst_symbol']}{extra}")
        fq = report["final_quote"].get(variant)
        if fq:
            lines.append(f"{variant} final-quote cases: {fq['summary']}")
    for hm, per in report.get("ranks", {}).items():
        for variant, cols in per.items():
            for col, r in cols.items():
                if "missing" in r:
                    lines.append(f"ranks {variant} {hm} {col}: NOT REPRODUCED, missing: "
                                 + "; ".join(r["missing"]))
                    continue
                lines.append(f"ranks {variant} {hm} {col} (native {r['source']}, whole cohort): "
                             f"original mismatches {r['original_mismatches']}, resolved "
                             f"{r['resolved']}, remaining {len(r['remaining'])}, previously "
                             f"matching changed {len(r['changed'])}, training's own native "
                             f"ranks against its table {r['training_rank_mismatches']}, hidden "
                             f"native differences {len(r['hidden_native'])}, cohort equal "
                             f"{'yes' if r['cohort_equal'] else 'NO'}")
    for variant, per in report["ticks"].items():
        for hm, r in per.items():
            lines.append(f"ticks {variant} {hm}: rows tk61 {r['rows_reference']} ours "
                         f"{r['rows_replay']}; exact: {'yes' if r['exact'] else 'no'}; "
                         f"cells by class {r['classes']}; columns {sorted(r['columns'])}")
    for variant, per in report["versus_plain"].items():
        lines.append(f"{variant} versus plain, cells by class: "
                     + "; ".join(f"{hm} {c}" for hm, c in per.items()))
    per = report.get("resources") or {}
    if per:
        hm, top = max(per.items(), key=lambda x: x[1]["peak_rss_bytes"])
        lines.append(f"resources: peak resident {top['peak_rss_bytes']} bytes (reached by {hm}), "
                     f"least free disk {min(x['free_disk_bytes'] for x in per.values())} bytes, "
                     f"least available memory "
                     f"{min(x['available_memory_bytes'] for x in per.values())} bytes")
    if report.get("stopped") is not None:
        st = report["stopped"]
        lines.append(f"STOPPED at {st['minute']}: {st['cause']}: {st['detail']}")
    return "\n".join(lines) + "\n"


def _engine_text(d: dict[str, Any]) -> str:
    return ("exact" if d["exact"] else
            f"rows {d['rows_incremental']} / {d['rows_full_prefix']}, only incremental "
            f"{d['only_incremental']}, only full-prefix {d['only_full_prefix']}, order "
            f"{'equal' if d['order_equal'] else 'DIFFERS'}, types "
            f"{'equal' if d['dtypes_equal'] else 'DIFFER'}, cells {d['cells']} in "
            f"{sorted(d['columns'])}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tickr.model.parity",
                                description="Replay one open day minute by minute through the "
                                            "bot's feature path and compare with the "
                                            "training's feature table.")
    p.add_argument("--day", required=True, help="YYYY-MM-DD, an open (non-sealed) day")
    p.add_argument("--folder", type=Path, help="a local copy of the research layout")
    p.add_argument("--minutes", nargs="+", help="HH:MM inside 04:00 .. 19:59 (default: see doc)")
    p.add_argument("--all-minutes", action="store_true", help="every minute of the table day")
    p.add_argument("--bars", type=Path, help="minute_aggs folder")
    p.add_argument("--types", help="the training's types file, or - for none")
    for name in ("trades", "quotes", "reference", "benzinga", "fl", "table", "tk61"):
        p.add_argument(f"--{name}", type=Path)
    p.add_argument("--tick-form", choices=FORMS, help="flat files or the audit parquet copy")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--mode", choices=sorted(MODES), default="both", help="variants to run")
    p.add_argument("--ticks-only", action="store_true", help="only our v11 rows against tk61")
    p.add_argument("--rtol", type=float, help="relative tolerance to count cells beyond")
    p.add_argument("--atol", type=float, help="absolute tolerance to count cells beyond")
    p.add_argument("--work-dir", "--work", dest="work", type=Path,
                   help="folder for every temporary file (a new run folder is made in it and "
                        "removed at the end; conversion folders are removed per minute; "
                        "default: --out)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="bot.toml whose [parity] guards disk and memory")
    for name, key in (("disk-reserve-gb", "disk_reserve_gb"),
                      ("memory-reserve-gb", "memory_reserve_gb"),
                      ("duckdb-memory-gb", "duckdb_memory_gb")):
        p.add_argument(f"--{name}", type=int, dest=key,
                       help=f"overrides [parity] {key} (see config/bot.toml; the reserves "
                            "protect the live bot's disk and memory)")
    p.add_argument("--engine", choices=ENGINES, default="full-prefix",
                   help="full-prefix (the reference) or incremental (one walk over the day)")
    p.add_argument("--verify-full-prefix", type=int, default=0, metavar="N",
                   help="with --engine incremental: also run full-prefix at N sampled minutes "
                        "and require exact equality")
    p.add_argument("--table-workers", type=int, default=1,
                   help="with --engine incremental: processes for the table rows (#832)")
    p.add_argument("--workers", type=int, default=1,
                   help="processes that assemble the features of minutes side by side")
    p.add_argument("--resume", action="store_true",
                   help="continue the journal in --out, skipping its finished minutes")
    p.add_argument("--rebuild-report", action="store_true",
                   help="only write the report from the journal in --out")
    return p


def _limits(args: argparse.Namespace) -> headroom.Limits:
    """[parity] of --config with the command line's overrides."""
    import dataclasses

    p = config.load(args.config).parity
    given = {k: getattr(args, k) for k in ("disk_reserve_gb", "memory_reserve_gb",
                                            "duckdb_memory_gb") if getattr(args, k) is not None}
    if any(v < 1 for v in given.values()):
        raise ModelRefused("replay_limit_invalid", ", ".join(sorted(given)))
    return headroom.Limits.from_config(dataclasses.replace(p, **given))


def _inputs(args: argparse.Namespace) -> ReplayInputs:
    found = layout(args.folder, args.day) if args.folder is not None else {}
    pick = {k: getattr(args, k) or found.get(k) for k in ("bars", "table", "tk61", "trades",
                                                          "quotes")}
    need = ["bars", "trades", "quotes"] + ([] if args.ticks_only else ["table"])
    lost = [k for k in need if pick[k] is None]
    lost += [k for k in ("reference", "benzinga", "fl")
             if getattr(args, k) is None and not args.ticks_only]
    if args.types is None:
        lost.append("types")
    if lost:
        raise ModelRefused("replay_input_missing", ", ".join(lost))
    form = args.tick_form or ("audit" if str(pick["trades"]).endswith(".parquet") else "flat")
    none = Path(os.devnull)
    return ReplayInputs(pick["bars"], None if args.types == "-" else Path(args.types),
                        pick["trades"], pick["quotes"], args.reference or none,
                        args.benzinga or none, args.fl or none, pick["table"] or none,
                        pick["tk61"], form)


def write_report(report: dict[str, Any], out: Path, day: str) -> str:
    """The report's json and text files in `out` (an existing one refuses); the text."""
    js, tx = out / f"parity-{day}.json", out / f"parity-{day}.txt"
    for f in (js, tx):
        if f.exists():
            raise ModelRefused("output_exists", str(f))
    body = text(report)
    js.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", "utf-8")
    tx.write_text(body, "utf-8")
    return body


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tolerance = None
    if args.rtol is not None or args.atol is not None:
        tolerance = (args.rtol or 0.0, args.atol or 0.0)
    try:
        journal = Journal(args.out, args.day, resume=args.resume)
        if args.rebuild_report:
            if not journal.lines.exists():
                raise ModelRefused("replay_journal_missing", str(journal.lines))
            print(write_report(build_report(*read_journal(journal.lines)), args.out,
                               args.day), end="")
            return 0
        if args.minutes and args.all_minutes:
            raise ModelRefused("replay_minute_invalid", "--minutes or --all-minutes, not both")
        if min(args.table_workers, args.workers) < 1 or args.verify_full_prefix < 0:
            raise ModelRefused("replay_engine", "--table-workers and --workers at least 1, "
                               "--verify-full-prefix at least 0")
        for f in (args.out / f"parity-{args.day}.json", args.out / f"parity-{args.day}.txt"):
            if f.exists():
                raise ModelRefused("output_exists", str(f))
        inputs = _inputs(args)
        limits = _limits(args)
        args.out.mkdir(parents=True, exist_ok=True)
        if args.work is not None:
            args.work.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="parity-work-", dir=args.work or args.out))
        saved = tempfile.tempdir
        try:
            room = headroom.Headroom(work, limits)
            # the pinned training code's own temporary folders land in the work folder too
            (work / "tmp").mkdir()
            tempfile.tempdir = str(work / "tmp")
            report = replay(args.day, args.minutes, inputs, variants=MODES[args.mode],
                            work=work, tolerance=tolerance, all_minutes=args.all_minutes,
                            ticks_only=args.ticks_only, engine=args.engine,
                            verify=args.verify_full_prefix, journal=journal,
                            table_workers=args.table_workers, workers=args.workers, room=room,
                            diagnostics=config.load(args.config).parity.engine_diagnostics)
        finally:
            tempfile.tempdir = saved
            shutil.rmtree(work, ignore_errors=True)
        print(write_report(report, args.out, args.day), end="")
    except ModelRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if report["stopped"] is not None:
        print(f"STOPPED: {report['stopped']['cause']}: {report['stopped']['detail']}",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
