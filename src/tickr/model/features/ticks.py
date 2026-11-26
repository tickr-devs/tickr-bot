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
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
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


def write_flat_file(rows: Any, path: str | Path, columns: list[str]) -> None:
    """Write `rows` (a pandas frame) as a flat file the converter reads: header, `columns` in
    that order, values as text. Integer columns must hold integers (a float would print as
    `1.0` and change how the converter reads trf_id or correction); a missing value is an empty
    field, as in the flat files."""
    import pandas as pd

    missing = [c for c in columns if c not in rows.columns]
    if missing:
        raise ModelRefused("tick_columns_missing", ", ".join(missing))
    for c in columns:
        series = rows[c]
        if pd.api.types.is_float_dtype(series) and c not in FLOAT_COLUMNS:
            raise ModelRefused("tick_value_invalid", f"{c} is a float column")
    rows.loc[:, columns].to_csv(path, index=False)

