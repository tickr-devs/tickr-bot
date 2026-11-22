"""Answers of a settled (past) day, kept so picking a past day in the apps shows it at once.

A past day's GET /flow, /watching, /symbols, /movers, /observers and /skipped (`ROUTES`) are
built from files the bot no longer writes: the day's decision file (tens of MB on a busy day),
its capture folder (bars, screener and discovery passes, hundreds of MB) and the trades the
database holds for it. The API's readers keep only the last past day parsed, so every pick of
another day, and the first after a restart, cost 10-15 s (measured 2026-11-22); so a past day's
answers are built once and kept:

- The key of a day is a SHA-256 over everything its answers are built from (`day_key`): the
  code (`code_key`: every .py file of the running package, read once at start), the config
  file's digest, the size and modification time of every file in the day's decision and capture
  folders, of the replay settings, of the session calendar and of the shown strategies' files,
  and the day's trades as the database lists them for GET /flow (`History.entries`, a query of a
  few ms). Any change to any of them, a late write, an archived (.gz) file or a release, gives
  another key, and the answers are built again. A key is taken before building, so an input that
  changes meanwhile is caught by the next request.
- Only days before today's New York date are kept; today is always built live.
- An answer is kept only when nothing transient decided it (an unreadable file, a calendar that
  could not be read): one built from a failure is answered once and never kept, and a refused
  read (a 503) is never kept at all.
- Answers are kept as their encoded JSON body, in memory for the `[api] settled_memory_days`
  most recently used days, and on disk under `[api] settled_cache_dir` (one folder per day, one
  file per route named by the key) for the `[api] settled_disk_days` newest days, so a restart
  or a release does not make the first pick slow again. Files are written whole (a temporary
  file, then a rename); older day folders are deleted when a newer day is written.
- One day is built at a time (a lock per day): the apps read the six routes of a pick at once,
  and the first request builds all six from one read of the day's records while the others wait
  for it, instead of six requests parsing the same files at once.

`DayBarsIndex` does the same for the bars GET /days/<date> and /bars read: a past day's captured
bars by symbol, indexed once per bars file. The API builds the latest past day ahead
(`ApiServer._warm_past_day`, api/warm.py), so the morning's first pick of yesterday is already
built. Display only: nothing here is read by the bot.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from tickr.api.contract import BarV1
from tickr.version import CODE_ROOT

# The routes whose past-day answers are kept, by their path.
ROUTES = ("/flow", "/watching", "/symbols", "/movers", "/observers", "/skipped")
BODY_SUFFIX = ".json"
TEMPORARY_SUFFIX = ".tmp"
KEY_SEPARATOR = "-"  # <route>-<key>.json
BARS_FOLDER = "day-bars"  # <day>/day-bars-<key>/: the day's bars by symbol (`DayBarsIndex`)
SYMBOLS_FILE = "symbols.json"  # in a bars folder: the symbols with bars, written last
PACKAGE = CODE_ROOT / "src" / "tickr"  # the running code, hashed once into `code_key`

_Stamp = tuple[str, int, int]  # file name, size in bytes, modification time in ns


def code_key(package: Path = PACKAGE) -> str:
    """SHA-256 of every .py file under `package` (relative path and content), so answers kept
    on disk by another version of the code are never served."""
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def folder_stamps(folder: Path) -> tuple[_Stamp, ...]:
    """(name, size, mtime ns) of every file directly in `folder`; empty when it does not exist.
    OSError when it cannot be listed."""
    if not folder.is_dir():
        return ()
    found = []
    for entry in sorted(os.scandir(folder), key=lambda e: e.name):
        if entry.is_file():
            stat = entry.stat()
            found.append((entry.name, stat.st_size, stat.st_mtime_ns))
    return tuple(found)


def file_stamp(path: Path) -> _Stamp | None:
    """(name, size, mtime ns) of `path`, None when it does not exist."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (path.name, stat.st_size, stat.st_mtime_ns)


def day_key(parts: Iterable[object]) -> str:
    """The key of one day: SHA-256 over the text of its input parts (stamps, digests, rows)."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def route_name(route: str) -> str:
    return route.strip("/")


class SettledAnswers:
    """Encoded answers of past days by (day, route) under one key per day. Thread safe."""

    def __init__(self, folder: Path, memory_days: int, disk_days: int) -> None:
        self.folder = folder  # <data_dir>/<settled_cache_dir>/<environment>
        self.memory_days = memory_days
        self.disk_days = disk_days
        self._lock = threading.Lock()
        # day -> (key, {route: body}), most recently used last.
        self._memory: OrderedDict[date, tuple[str, dict[str, bytes]]] = OrderedDict()
        self._day_locks: dict[date, threading.Lock] = {}

    def day_lock(self, day: date) -> threading.Lock:
        """The lock that lets one request at a time build `day`'s answers."""
        with self._lock:
            return self._day_locks.setdefault(day, threading.Lock())

    def get(self, day: date, key: str, route: str) -> bytes | None:
        """The kept body of `route` on `day` under `key`, from memory or disk; None otherwise."""
        with self._lock:
            held = self._memory.get(day)
            if held is not None and held[0] == key and route in held[1]:
                self._memory.move_to_end(day)
                return held[1][route]
        path = self._file(day, key, route)
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            return None
        self._remember(day, key, {route: body})
        return body

    def put(self, day: date, key: str, bodies: Mapping[str, bytes]) -> None:
        """Keep `bodies` (route -> body) of `day` under `key`, in memory and on disk. OSError
        (a disk that cannot be written) is raised after memory took them."""
        if not bodies:
            return
        self._remember(day, key, dict(bodies))
        folder = self.folder / day.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        for route, body in bodies.items():
            target = self._file(day, key, route)
            _write_whole(target, body)
            prefix = route_name(route) + KEY_SEPARATOR
            for other in folder.iterdir():  # the route's answers under an older key
                if other.name.startswith(prefix) and other != target:
                    other.unlink(missing_ok=True)
        self._trim_disk()

    def _file(self, day: date, key: str, route: str) -> Path:
        name = f"{route_name(route)}{KEY_SEPARATOR}{key}{BODY_SUFFIX}"
        return self.folder / day.isoformat() / name

    def _remember(self, day: date, key: str, bodies: dict[str, bytes]) -> None:
        with self._lock:
            held = self._memory.get(day)
            if held is not None and held[0] == key:
                held[1].update(bodies)
            else:
                self._memory[day] = (key, bodies)
            self._memory.move_to_end(day)
            while len(self._memory) > self.memory_days:
                self._memory.popitem(last=False)

    def _trim_disk(self) -> None:
        """Delete day folders beyond the `disk_days` newest ones."""
        days = []
        for entry in self.folder.iterdir():
            try:
                days.append((date.fromisoformat(entry.name), entry))
            except ValueError:
                continue  # not a day folder: never ours to delete
        for _, entry in sorted(days, reverse=True)[self.disk_days:]:
            shutil.rmtree(entry, ignore_errors=True)


BuildBars = Callable[[], Mapping[str, Sequence[BarV1]]]  # every symbol's bars on the day


class DayBarsIndex:
    """A past day's captured bars that start on that day, by symbol, on disk: GET /days/<date>
    needs only which symbols have bars and the bars of the day's traded symbols, but the captured
    bars file holds every bar of every symbol received that day (hundreds of MB, 5-10 s to
    parse). Built once per bars file from one parse (`build`) into `<day>/day-bars-<key>/`, one
    file per symbol plus `symbols.json`, written last, so a half-written index is never read;
    the key covers the code and the bars file's name, size and modification time. Kept and
    deleted with the day's folder (`SettledAnswers`). Thread safe."""

    def __init__(self, folder: Path, code: str) -> None:
        self.folder = folder  # the same folder as SettledAnswers'
        self.code = code
        self._lock = threading.Lock()
        self._day_locks: dict[date, threading.Lock] = {}

    def symbols(self, day: date, bars_file: Path, build: BuildBars) -> tuple[str, ...]:
        """The symbols with bars on `day`, sorted. Raises what `build` and the disk raise."""
        names = (self._index(day, bars_file, build) / SYMBOLS_FILE).read_bytes()
        return tuple(json.loads(names))

    def bars(self, day: date, bars_file: Path, build: BuildBars, symbol: str) -> list[BarV1]:
        """`symbol`'s bars on `day`, oldest first; empty when it has none."""
        index = self._index(day, bars_file, build)
        try:
            rows = json.loads((index / _symbol_file(symbol)).read_bytes())
        except FileNotFoundError:
            return []
        return [BarV1(datetime.fromisoformat(start), *(Decimal(v) for v in values))
                for start, *values in rows]

    def _index(self, day: date, bars_file: Path, build: BuildBars) -> Path:
        stamp = file_stamp(bars_file)
        index = self.folder / day.isoformat() / (
            f"{BARS_FOLDER}{KEY_SEPARATOR}{day_key((self.code, stamp))}")
        if (index / SYMBOLS_FILE).is_file():
            return index
        with self._lock:
            lock = self._day_locks.setdefault(day, threading.Lock())
        with lock:
            if (index / SYMBOLS_FILE).is_file():  # built while this request waited
                return index
            by_symbol = build()
            index.mkdir(parents=True, exist_ok=True)
            for symbol, bars in by_symbol.items():
                rows = [[b.start.isoformat(), str(b.open), str(b.high), str(b.low), str(b.close),
                         str(b.volume)] for b in bars]
                _write_whole(index / _symbol_file(symbol), json.dumps(rows).encode("utf-8"))
            _write_whole(index / SYMBOLS_FILE, json.dumps(sorted(by_symbol)).encode("utf-8"))
            for other in index.parent.glob(f"{BARS_FOLDER}{KEY_SEPARATOR}*"):
                if other != index:  # another bars file or code: never read again
                    shutil.rmtree(other, ignore_errors=True)
        return index


def _symbol_file(symbol: str) -> str:
    """A symbol's file name in a bars index: its UTF-8 bytes in hex, so no ticker can name a
    path or clash with `SYMBOLS_FILE`."""
    return f"{symbol.encode('utf-8').hex()}{BODY_SUFFIX}"


def _write_whole(target: Path, body: bytes) -> None:
    """Write `body` to a temporary file next to `target`, then rename it over `target`."""
    temporary = target.with_name(target.name + TEMPORARY_SUFFIX)
    temporary.write_bytes(body)
    os.replace(temporary, target)
