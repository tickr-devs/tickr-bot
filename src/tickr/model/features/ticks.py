"""The training's per-minute tick table, built by the training's own frozen converter.

`pinned/wm2tick-v11.py` is a byte copy of the frozen tick converter v11 (md5 below). It turns a
day of Massive trades and quotes in the flat-file layout (us_stocks_sip trades_v1 / quotes_v1,
csv or csv.gz) into one row per stock and minute: keys `sym`, `m` and the 61 columns the tick
features (`wm2tk`, `l1tk2`) read. The bot hands the day's events so far to the converter's own
`day()` (the path of its `local` mode); nothing here re-implements a column. The file is never
edited: a new converter arrives as a new pinned file with a new md5.

What the bot runs is the training's run: the side files (px / s10) are never written, the
stock filter is off, and the converter's variables that change its table must be unset
(`OUTPUT_VARIABLES`). Two engine settings are the bot's, never the environment's (Codex,
codex-108 point 2): one engine thread (`SERIAL_THREADS`: with several, the floating sums are
added in a varying order and two runs of the same files differ in the last bit), and the group
count, the converter's default (24, pinned: codex-109 point 1 withdrew one group for the bot; it
stays only for the measurement tool, `experiment_groups`). With one thread a
run is repeatable, but the group count changes where the engine's 2048-row chunks fall, so a
floating sum may come out in another order: equal on the test day and at 500 synthetic stocks,
one stock-minute of 724,062 differing in the last bit at 1,000 (`tests/model/tick_memory.py`,
which also records the peak memory). An inherited value of either is refused unless it is the
one the bot sets. `TICK_MEM` only caps the engine's memory and may be set by the owner.
Every other TICK_* name the converter reads belongs to its download / upload loop (`main()`),
which the bot never runs; boto3 is imported only there, so the bot path needs duckdb alone.
Variables are asked one NAME at a time, never the whole environment (hard rule 10).

Live parity is the converter's own (its header): a print that was later corrected (correction
1, 7, 8) stays as printed, and the later records about it (10, 11, 12) are dropped.

Minute index: `m` is the minute of the New York day (09:30 = 570), as in the converter's files;
`wm2tk` subtracts 240 to reach the table minute of `tickr.model.features`.

Prefix rule: a closed minute's trade columns depend only on events up to that minute. Its
time-weighted quote columns (qspr, qbs, qas, qimb, qbad, qbd, qad) weigh the minute's last
quote update by how long it stood, which the converter counts only once the stock has a later
update; until then that update weighs 0. `tests/model/test_ticks.py` shows both.

Incremental path (`tail_carry`, `minute_rows`): converting the whole day so far every minute is
too slow (tens of seconds at 500 stocks), so one closed minute is converted from a bounded tail:
the minute's own events plus the few earlier events whose values cross a minute boundary in the
converter's SQL. Read from its statements (the table path, side files off):
- quotes: `lag(bp, ap, bs, az, ts)` and `lead(ts)` over the stock's updates ordered by (ts, sq),
  and the trade's as-of join to the update strictly before it (highest sq of its stamp): the
  stock's last update before the minute is all they read from the past. `lead` reads the future
  only, which a prefix does not hold either.
- trades: `lag(ts)` over the stock's kept trades (tgap) and `lag(px)` over the stock's trades of
  the same last-price class (tvar, tchg). Which trades are kept and their class are functions of
  conditions, correction, the signs of price and size and whether the trade's minute is in the
  regular session (Form T): the last trade of each such key is kept, so the last kept trade and
  the last trade of each class are in the tail without re-stating the converter's rules here.
Every other column uses only the minute's own events. `tests/model/test_ticks_tail.py` proves
row equality with `tick_rows` on the whole prefix, minute by minute.

Reconnect, restart, late corrections (codex-108 point 2): the carry crosses any number of
minutes, so the minutes a stream missed are folded in from a backfill, and `save_carry` /
`load_carry` keep it on disk across a restart (the text the converter reads, refused when
missing, unreadable or of another day or minute). v11 keeps a print as printed (correction 0 or
1 / 7 / 8) and drops the records written later about it (10 / 11 / 12), so these never change
a closed minute; an event stamped in a minute already delivered is refused by `minute_rows`,
and `restated` compares a delivered minute with the same minute recomputed from the corrected
inputs: a difference is refused with the stocks and columns, the delivered rows are never
rewritten. `tests/model/test_ticks_tail.py` covers tgap (the stock's previous kept trade) and
the previous price of the same last-price class across all three.

Marker quote (`marker=True`): one extra quote update per stock that quoted in the minute, at the
first nanosecond of the next minute, lets the minute's last update weigh up to the minute end, as
in the training's day file whenever the stock quotes again later that day; the next minute's rows
it creates are discarded. Only a stock that never quotes again that day differs from the
training's file (its last update weighs 0 there).
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tickr.model.errors import ModelRefused

PINNED_DIR = Path(__file__).with_name("pinned")
CONVERTER_FILE = "wm2tick-v11.py"
# md5 of the frozen converter v11 (training-spec/feed-coverage-v2/src/wm2tick-v11.py).
CONVERTER_MD5 = "c1c1ead1751ba765f494af79235bddd7"
MODULE_NAME = "tickr_pinned_wm2tick_v11"
# Read by the converter and change its table: the large-trade threshold (tbig), the side-file
# switch and the stock filter of its `local` mode. Refused when set.
OUTPUT_VARIABLES = ("TICK_BIG", "TICK_V11", "TICK_TYPES")
# The converter's engine threads (read at each engine connection): the bot sets one; an inherited
# other value is refused.
THREADS_VARIABLE = "TICK_THREADS"
SERIAL_THREADS = "1"
# The converter's stock group count (read once, at import, into its KB): the bot sets it from
# `[model] tick_groups` on the module; any inherited value is refused.
GROUPS_VARIABLE = "TICK_GROUPS"
# One group: withdrawn for the bot (codex-109 point 1: 24 groups pinned); allowed only to the
# measurement tool tests/model/tick_memory.py and its tests, through `experiment_groups`.
ONE_GROUP = 1
# Read by the converter's engine connection only: the memory cap. Allowed.
RESOURCE_VARIABLES = ("TICK_MEM",)
NEW_YORK = ZoneInfo("America/New_York")
NS_PER_SECOND = 10**9
NS_PER_MINUTE = 60 * NS_PER_SECOND
MINUTES_PER_DAY = 24 * 60
# The flat-file columns that hold prices or sizes as decimals; every other numeric column of the
# converter's lists is an integer (timestamps, sequence numbers, codes).
FLOAT_COLUMNS = ("price", "size", "bid_price", "ask_price", "bid_size", "ask_size")
# The flat-file columns that hold text (codes lists, the symbol); with FLOAT_COLUMNS, every other
# column of the converter's lists is an integer.
TEXT_COLUMNS = ("ticker", "conditions", "indicators")

_LOCK = threading.Lock()
_CODE: dict[str, Any] = {}


def _check_variables() -> None:
    """Refuse a set output variable or group count and an inherited thread count other than the
    bot's; then set the bot's thread count. Asked one NAME at a time."""
    set_names = [name for name in (*OUTPUT_VARIABLES, GROUPS_VARIABLE)
                 if os.environ.get(name) is not None]
    if set_names:
        raise ModelRefused("tick_variable_set", ", ".join(set_names))
    if os.environ.get(THREADS_VARIABLE) not in (None, SERIAL_THREADS):
        raise ModelRefused("tick_variable_set", THREADS_VARIABLE)
    os.environ[THREADS_VARIABLE] = SERIAL_THREADS


def converter() -> Any:
    """The pinned converter module, imported once after its checks: the file is the pinned
    bytes, no output variable and no group count is set, the thread count is unset or the one
    the bot sets (it is then set to it), duckdb is installed."""
    with _LOCK:
        _check_variables()
        if "v11" in _CODE:
            return _CODE["v11"]
        path = PINNED_DIR / CONVERTER_FILE
        source = path.read_bytes()
        if hashlib.md5(source).hexdigest() != CONVERTER_MD5:
            raise ModelRefused("table_code_md5", CONVERTER_FILE)
        try:
            import duckdb  # noqa: F401 - the model extra must be installed
        except ImportError as exc:
            raise ModelRefused("model_libraries_missing", exc.name or "") from exc
        spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
        if spec is None or spec.loader is None:
            raise ModelRefused("table_code_call", f"{CONVERTER_FILE} is not importable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = module
        spec.loader.exec_module(module)
        _CODE["v11"] = module
        _CODE["groups"] = module.KB  # the converter's default: TICK_GROUPS was unset
        return module


def default_groups() -> int:
    """The converter's own group count (its KB when TICK_GROUPS is unset): what the training's
    runs used."""
    converter()
    return int(_CODE["groups"])


def _set_groups(w: Any, groups: int, experiment: bool = False) -> None:
    """Set the converter's group count: its default (24, pinned for the bot); one group only in
    an experiment (`tick_rows(experiment_groups=True)`)."""
    if groups != _CODE["groups"] and not (experiment and groups == ONE_GROUP):
        raise ModelRefused("tick_groups_invalid", str(groups))
    w.KB = groups


def trade_columns() -> list[str]:
    """The trades file columns the converter needs (its TCOLS)."""
    return list(converter().TCOLS)


def quote_columns() -> list[str]:
    """The quotes file columns the converter needs (its QCOLS)."""
    return list(converter().QCOLS)


def _bounds(day: str, through: int) -> tuple[int, int]:
    """SIP nanoseconds of the start of New York `day` and of the end of minute `through`."""
    if not 0 <= through < MINUTES_PER_DAY:
        raise ModelRefused("tick_minute_invalid", str(through))
    try:
        date = datetime.date.fromisoformat(day)
    except ValueError as exc:
        raise ModelRefused("tick_day_invalid", day) from exc
    start = datetime.datetime.combine(date, datetime.time(), NEW_YORK)
    end = start + datetime.timedelta(minutes=through + 1)
    return int(start.timestamp()) * NS_PER_SECOND, int(end.timestamp()) * NS_PER_SECOND


def tick_rows(trades_file: str | Path, quotes_file: str | Path, day: str, through: int, *,
              groups: int, experiment_groups: bool = False) -> Any:
    """The converter's table of `day` from the day's trades and quotes so far: files in the
    flat-file layout (header row, at least the converter's columns, csv or csv.gz). Every event
    must lie on New York `day` and before the end of minute `through` (minute of the New York
    day); an event outside is refused, never dropped. `groups` is `[model] tick_groups`.
    Returns a pandas frame: sym, m and the 61 columns, sorted by sym and m, every minute up to
    `through` that has an event."""
    w = converter()
    lo, hi = _bounds(day, through)
    import duckdb

    con = duckdb.connect()
    try:
        for name in (trades_file, quotes_file):
            first, last = con.execute(
                "SELECT min(CAST(sip_timestamp AS BIGINT)), max(CAST(sip_timestamp AS BIGINT)) "
                "FROM read_csv(?, header=true, all_varchar=true, delim=',', quote='\"', "
                "escape='\"')", [str(name)]).fetchone()
            if first is not None and first < lo:
                raise ModelRefused("tick_wrong_day", f"{Path(name).name} before {day}")
            if last is not None and last >= hi:
                raise ModelRefused("tick_after_minute", f"{Path(name).name} after {through}")
    except duckdb.Error as exc:
        raise ModelRefused("tick_file_unreadable", f"{type(exc).__name__}") from exc
    finally:
        con.close()
    with _LOCK, tempfile.TemporaryDirectory(prefix="tickr-ticks-") as tmp:
        out = os.path.join(tmp, "tk.parquet")
        _check_variables()
        _set_groups(w, groups, experiment_groups)
        try:
            w.day(str(trades_file), str(quotes_file), out, tmp)
        except SystemExit as exc:  # the converter's own refusal (a missing column)
            raise ModelRefused("tick_converter_refused", str(exc.code)) from exc
        con = duckdb.connect()
        try:
            return con.execute("SELECT * FROM read_parquet(?) ORDER BY sym, m", [out]).df()
        finally:
            con.close()


def _minute_span(day: str, minute: int) -> tuple[int, int, int]:
    """SIP nanoseconds of the start of New York `day`, and of the start and end of `minute`."""
    lo, hi = _bounds(day, minute)
    start = lo if minute == 0 else _bounds(day, minute - 1)[1]
    return lo, start, hi


def _minutes(stamps: Any) -> Any:
    """Minute of the New York day of SIP nanosecond stamps (the converter's `m`)."""
    import pandas as pd

    local = pd.to_datetime(stamps, unit="ns", utc=True).dt.tz_convert(NEW_YORK)
    return local.dt.hour * 60 + local.dt.minute


def tail_carry(trades: Any, quotes: Any) -> tuple[Any, Any]:
    """The earlier events a later minute's rows read (module text, "Incremental path"): the last
    quote update of each stock by (sip_timestamp, sequence_number), and the last trade of each
    stock and key (conditions, correction, sign of price, sign of size, regular-session minute).
    Called on the previous carry plus a closed minute's events, it gives the next carry. Row
    order is kept."""
    import pandas as pd

    rth = converter().RTH
    trades, quotes = trades.reset_index(drop=True), quotes.reset_index(drop=True)
    q = quotes.sort_values(["sip_timestamp", "sequence_number"], kind="stable")
    q = quotes.loc[sorted(q.groupby("ticker", sort=False).tail(1).index)]
    t = trades.sort_values(["sip_timestamp", "sequence_number"], kind="stable")
    minute = _minutes(t.sip_timestamp)
    keys = pd.DataFrame({
        "ticker": t.ticker, "conditions": t.conditions.fillna("").astype(str),
        "correction": t.correction.astype("string").fillna(""), "price": t.price > 0,
        "size": t["size"] > 0, "rth": (minute >= rth[0]) & (minute <= rth[1])}, index=t.index)
    last = keys.groupby(list(keys.columns), sort=False, dropna=False).tail(1).index
    t = trades.loc[sorted(last)]
    return t.reset_index(drop=True), q.reset_index(drop=True)


def minute_rows(carry: tuple[Any, Any], trades: Any, quotes: Any, day: str, minute: int, *,
                groups: int, marker: bool = False) -> Any:
    """The converter's rows of one closed `minute` of `day` (minute of the New York day) from a
    bounded tail: `carry` from `tail_carry` of every earlier event of the day, and the minute's
    own trades and quotes (frames in the flat-file layout). Equal to the rows of that minute in
    `tick_rows` of the whole day through `minute`; with `marker`, to the training's day file for
    every stock that quotes again later (module text). An event outside its part is refused; the
    marker of the day's last minute (1439) would lie on the next day and is refused too."""
    import pandas as pd

    lo, start, hi = _minute_span(day, minute)
    for name, frame, first, end in (("carry trades", carry[0], lo, start),
                                    ("carry quotes", carry[1], lo, start),
                                    ("trades", trades, start, hi), ("quotes", quotes, start, hi)):
        stamps = frame.sip_timestamp
        if len(frame) and (stamps.min() < first or stamps.max() >= end):
            raise ModelRefused("tick_outside_minute", f"{name} of minute {minute}")
    tail_quotes = [carry[1], quotes]
    if marker and len(quotes):
        mark = quotes.sort_values(["sip_timestamp", "sequence_number"], kind="stable")
        mark = mark.groupby("ticker", sort=False).tail(1).copy()
        mark["sip_timestamp"] = hi
        mark["conditions"] = ""
        mark["indicators"] = ""
        tail_quotes.append(mark)
    with tempfile.TemporaryDirectory(prefix="tickr-tail-") as tmp:
        t, q = os.path.join(tmp, "trades.csv"), os.path.join(tmp, "quotes.csv")
        write_flat_file(pd.concat([carry[0], trades], ignore_index=True), t, trade_columns())
        write_flat_file(pd.concat(tail_quotes, ignore_index=True), q, quote_columns())
        rows = tick_rows(t, q, day, minute + 1 if marker else minute, groups=groups)
    return rows[rows.m == minute].reset_index(drop=True)


def save_carry(carry: tuple[Any, Any], path: str | Path, day: str, minute: int) -> None:
    """Store `carry` durably: the carry of every event of `day` before `minute` (the next minute
    it serves). One JSON file holding the converter's columns of both frames as flat-file text
    (the text the converter reads, so a restored carry gives the same rows, exactly), written to
    a temporary file, flushed to disk and renamed over `path`."""
    target = Path(path)
    body = {"day": day, "minute": minute,
            "trades": _flat_text(carry[0], trade_columns()),
            "quotes": _flat_text(carry[1], quote_columns())}
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    folder = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def load_carry(path: str | Path, day: str, minute: int) -> tuple[Any, Any]:
    """The carry `save_carry` stored for `day` and `minute`. A missing or unreadable file, or
    one of another day or minute, is refused, never replaced by an empty carry."""
    try:
        body = json.loads(Path(path).read_text("utf-8"))
        trades = _from_flat_text(body["trades"], trade_columns())
        quotes = _from_flat_text(body["quotes"], quote_columns())
    except FileNotFoundError as exc:
        raise ModelRefused("tick_carry_missing", Path(path).name) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ModelRefused("tick_carry_unreadable", type(exc).__name__) from exc
    if body["day"] != day or body["minute"] != minute:
        raise ModelRefused("tick_carry_stale",
                           f"{body['day']} minute {body['minute']}, not {day} minute {minute}")
    return trades, quotes


def _flat_text(rows: Any, columns: list[str]) -> str:
    """`rows` as the flat-file text `write_flat_file` writes."""
    buffer = io.StringIO()
    write_flat_file(rows, buffer, columns)
    return buffer.getvalue()


def _from_flat_text(text: str, columns: list[str]) -> Any:
    """A frame from flat-file text: text columns as written (an empty field is ""), the
    decimals of FLOAT_COLUMNS parsed exactly (Python's float of the written shortest text), every
    other column a nullable integer. Columns other than `columns` are refused."""
    import pandas as pd

    frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    if list(frame.columns) != columns:
        raise ValueError("carry columns")
    for c in columns:
        if c in TEXT_COLUMNS:
            continue
        values = [None if v == "" else v for v in frame[c]]
        if c in FLOAT_COLUMNS:
            frame[c] = pd.array([float("nan") if v is None else float(v) for v in values],
                                dtype="float64")
        else:
            frame[c] = pd.array([None if v is None else int(v) for v in values], dtype="Int64")
    return frame


def restated(delivered: Any, recomputed: Any) -> None:
    """Check a delivered minute against the same minute recomputed later from the corrected
    inputs (a late print, a correction, a backfill after a reconnect). A delivered minute is
    never rewritten: any difference in its stocks or in a value (missing equals missing) is
    refused with the stocks and columns that differ; the caller records it and rebuilds the
    carry from the corrected inputs (`tail_carry`)."""
    import pandas as pd

    a = delivered.set_index(["sym", "m"]).sort_index()
    b = recomputed.set_index(["sym", "m"]).sort_index()
    if list(a.columns) != list(b.columns):
        raise ModelRefused("tick_columns_missing", "delivered and recomputed columns differ")
    keys = a.index.symmetric_difference(b.index)
    if len(keys):
        raise ModelRefused("tick_minute_restated",
                           "rows " + ", ".join(f"{s} {m}" for s, m in keys))
    changed = [f"{key[0]} {key[1]} {c}" for c in a.columns
               for key, x, y in zip(a.index, a[c], b[c], strict=True)
               if not (pd.isna(x) and pd.isna(y)) and (pd.isna(x) or pd.isna(y) or x != y)]
    if changed:
        raise ModelRefused("tick_minute_restated", ", ".join(changed))


def write_flat_file(rows: Any, path: Any, columns: list[str]) -> None:
    """Write `rows` (a pandas frame) as a flat file the converter reads (`path`: a file path or
    a text buffer): header, `columns` in that order, values as text. Integer columns must hold
    integers (a float would print as `1.0` and change how the converter reads trf_id or
    correction); a missing value is an empty field, as in the flat files."""
    import pandas as pd

    missing = [c for c in columns if c not in rows.columns]
    if missing:
        raise ModelRefused("tick_columns_missing", ", ".join(missing))
    for c in columns:
        series = rows[c]
        if pd.api.types.is_float_dtype(series) and c not in FLOAT_COLUMNS:
            raise ModelRefused("tick_value_invalid", f"{c} is a float column")
    rows.loc[:, columns].to_csv(path, index=False)

