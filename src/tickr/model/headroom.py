"""Disk and memory headroom of the replay parity harness (`tickr.model.parity`), which runs on
the owner's Mac beside the live bot and must never take the bot's disk or memory.

Measured on 2026-11-17 (the owner's Mac, `--all-minutes` on the open day): at 18:58 New York
the uncompressed quotes prefix the harness wrote for one minute's conversion passed 5.9 GB, the
disk (about 25 GB free at the start, other processes writing too) filled up and duckdb failed
with errno 28; the run peaked at 12.7 GB resident and a 32.3 GB memory footprint on the 32 GB
Mac, with 39 GB of swap in use. So:

- every conversion writes into its own folder under the run's work folder (`--work-dir`),
  removed as soon as the conversion is done (never only at exit);
- while a conversion runs, the available memory is sampled with its folder (below); if it fell
  below `[parity] memory_reserve_gb` the conversion refuses `memory_insufficient` at its end
  and its result is not used (the 2026-11-17 run went to 6.97 GB inside one);
- BEFORE a conversion writes anything, `Headroom.conversion` refuses `memory_insufficient` when
  the available memory is below `[parity] memory_reserve_gb`, and `disk_space_insufficient` when
  the free space of the work folder minus the bytes the conversion will write would fall below
  `[parity] disk_reserve_gb`. That estimate is the conversion's input rows times the bytes per
  input row MEASURED on this run: the peak size of an earlier conversion folder of the same
  kind (its input files, the converter's own columnar copies and spill, its output) divided by
  its input rows, from the largest conversion so far. A kind's first conversion with more rows
  than `[parity] probe_rows` is preceded by a probe: the same conversion on its first
  `probe_rows` events, measured the same way (a small probe compresses worse, so it
  overestimates);
- the peak of a conversion folder is sampled every `[parity] disk_poll_seconds` and at every
  file removal inside it (Python's `os.remove` audit event: the converter deletes its columnar
  copies at its end), so files that live only inside the conversion are counted;
- every duckdb engine of the harness gets `memory_limit` `[parity] duckdb_memory_gb`, its spill
  (`temp_directory`) inside the work folder and `max_temp_directory_size` the free space above
  the disk reserve; the frozen converter's engine gets the same memory limit through its own
  TICK_MEM and spills into the conversion folder (its `temp_directory` is that folder), which
  the peak above measures.
Free space, available memory and the peak resident size are read by functions the tests
replace. Sizes are decimal gigabytes (10^9 bytes, duckdb's GB).
"""

from __future__ import annotations

import contextlib
import math
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tickr.model.errors import ModelRefused

GB = 10**9  # unit factor: decimal gigabytes, as duckdb's 'GB'
KIB = 1024  # unit factor: /proc/meminfo and Linux ru_maxrss count KiB


@dataclass(frozen=True)
class Limits:
    """The harness's `[parity]` settings, in bytes (config/bot.toml comments)."""

    disk_reserve: int
    memory_reserve: int
    duckdb_memory: int
    probe_rows: int
    poll_seconds: float

    @classmethod
    def from_config(cls, p: Any) -> Limits:
        """From the loaded `[parity]` section."""
        return cls(p.disk_reserve_gb * GB, p.memory_reserve_gb * GB, p.duckdb_memory_gb * GB,
                   p.probe_rows, float(p.disk_poll_seconds))


def free_bytes(path: Path) -> int:
    """Free bytes of the disk holding `path`."""
    return shutil.disk_usage(path).free


def available_memory() -> int:
    """Bytes of memory available to new work without swapping: Linux MemAvailable; macOS the
    free and inactive pages of `vm_stat` (what psutil counts as available there). Any other
    system, or an unreadable answer, refuses `memory_unreadable`."""
    if sys.platform.startswith("linux"):
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * KIB
        raise ModelRefused("memory_unreadable", "MemAvailable missing in /proc/meminfo")
    if sys.platform == "darwin":
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False).stdout
        page, pages = None, {}
        for line in out.splitlines():
            if "page size of" in line:
                page = int(line.split("page size of")[1].split()[0])
            elif ":" in line:
                name, _, value = line.partition(":")
                pages[name.strip()] = value.strip().rstrip(".")
        try:
            return (int(pages["Pages free"]) + int(pages["Pages inactive"])) * int(page or 0)
        except (KeyError, ValueError) as exc:
            raise ModelRefused("memory_unreadable", "vm_stat") from exc
    raise ModelRefused("memory_unreadable", sys.platform)


def peak_rss() -> int:
    """The process's peak resident bytes so far (a high-water mark: it never goes down)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * KIB


def folder_bytes(folder: Path) -> int:
    """Bytes of the files under `folder` now (files that vanish while counted count 0)."""
    total = 0
    for root, _, files in os.walk(folder):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.stat(os.path.join(root, name)).st_size
    return total


class _Peak:
    """The largest size seen of one folder while it is watched."""

    def __init__(self, folder: Path) -> None:
        self.folder = str(folder)
        self.peak = 0
        self.low = math.inf  # the least available memory sampled while watched, bytes
        self.lock = threading.Lock()

    def sample(self) -> None:
        size = folder_bytes(Path(self.folder))
        with self.lock:
            self.peak = max(self.peak, size)


_WATCHED: list[_Peak] = []
_HOOK: list[bool] = []
_HOOK_LOCK = threading.Lock()


def _on_audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "os.remove" or not _WATCHED or not args:
        return
    path = os.fsdecode(args[0]) if isinstance(args[0], str | bytes) else ""
    for watch in list(_WATCHED):
        if path.startswith(watch.folder + os.sep):
            watch.sample()  # before the file goes: the folder at its fullest


def _install_hook() -> None:
    with _HOOK_LOCK:
        if not _HOOK:
            sys.addaudithook(_on_audit)
            _HOOK.append(True)


@dataclass
class Headroom:
    """The guard of one run (module docstring). `work` is the run's work folder."""

    work: Path
    limits: Limits
    # the readers (None: this module's free_bytes / available_memory / peak_rss, looked up
    # when called, so tests can replace them)
    free: Callable[[Path], int] | None = None
    memory: Callable[[], int] | None = None
    rss: Callable[[], int] | None = None
    # per kind: (input rows, bytes per input row) of the largest conversion measured so far
    measured: dict[str, tuple[int, float]] = field(default_factory=dict)
    spill: Path = field(init=False)

    def _free(self) -> int:
        return (self.free or free_bytes)(self.work)

    def _memory(self) -> int:
        return (self.memory or available_memory)()

    def __post_init__(self) -> None:
        self.work = Path(self.work)
        self.spill = self.work / "spill"
        self.spill.mkdir(parents=True, exist_ok=True)

    def duckdb_memory(self) -> str:
        """The memory limit of every duckdb engine of the harness, in duckdb's words."""
        return f"{self.limits.duckdb_memory}B"

    def connect(self, spill: Path | None = None) -> Any:
        """A duckdb connection within the limits: memory_limit, spill in `spill` (default the
        work folder's spill folder) and at most the free space above the disk reserve."""
        import duckdb

        folder = spill or self.spill
        room = max(self._free() - self.limits.disk_reserve, 0)
        con = duckdb.connect()
        con.execute(f"SET memory_limit='{self.duckdb_memory()}'")
        con.execute(f"SET temp_directory='{folder}'")
        con.execute(f"SET max_temp_directory_size='{room}B'")
        return con

    def check(self, kind: str, rows: int) -> dict[str, int]:
        """Refuse a conversion of `rows` input rows of `kind` that would leave less than the
        reserves; the numbers it judged by."""
        available = self._memory()
        if available < self.limits.memory_reserve:
            raise ModelRefused("memory_insufficient",
                               f"{kind}: {available} bytes available, reserve "
                               f"{self.limits.memory_reserve}")
        free = self._free()
        per_row = self.measured[kind][1] if kind in self.measured else 0.0
        need = math.ceil(rows * per_row)
        if free - need < self.limits.disk_reserve:
            raise ModelRefused("disk_space_insufficient",
                               f"{kind}: {rows} rows x {per_row:.1f} bytes measured = {need} "
                               f"bytes, {free} free, reserve {self.limits.disk_reserve}")
        return {"rows": rows, "estimate": need, "free": free, "available": available}

    @contextlib.contextmanager
    def _watched(self, kind: str) -> Iterator[_Peak]:
        """A new conversion folder under the work folder, its peak size watched; removed at
        the end of the block."""
        folder = Path(tempfile.mkdtemp(prefix=f"conv-{kind}-", dir=self.work))
        watch = _Peak(folder)
        _install_hook()
        stop = threading.Event()

        def poll() -> None:
            while not stop.wait(self.limits.poll_seconds):
                watch.sample()
                with contextlib.suppress(ModelRefused):  # unreadable: the end check refuses
                    watch.low = min(watch.low, self._memory())

        sampler = threading.Thread(target=poll, daemon=True)
        _WATCHED.append(watch)
        sampler.start()
        try:
            yield watch
            watch.sample()
        finally:
            stop.set()
            sampler.join()
            _WATCHED.remove(watch)
            shutil.rmtree(folder, ignore_errors=True)

    def _record(self, kind: str, rows: int, peak: int) -> None:
        if rows > 0 and rows >= self.measured.get(kind, (0, 0.0))[0]:
            self.measured[kind] = (rows, peak / rows)

    @contextlib.contextmanager
    def conversion(self, kind: str, rows: int,
                   probe: Callable[[Path, int], int] | None = None) -> Iterator[Path]:
        """A conversion folder for `rows` input rows of `kind`, after the checks; removed when
        the block ends, its peak then measured. `probe(folder, n)` runs the same conversion on
        its first `n` events of each input into `folder` and returns the rows it wrote: called
        first when the kind has no measurement yet and `rows` exceeds `probe_rows` (a smaller
        first conversion is its own probe: only the reserves are checked)."""
        if kind not in self.measured and rows > self.limits.probe_rows:
            if probe is None:
                raise ModelRefused("disk_space_unmeasured", kind)
            self.check(kind, self.limits.probe_rows)
            with self._watched(kind) as watch:
                wrote = probe(Path(watch.folder), self.limits.probe_rows)
            self._record(kind, wrote, watch.peak)
        self.check(kind, rows)
        with self._watched(kind) as watch:
            yield Path(watch.folder)
        self._record(kind, rows, watch.peak)
        self._after(kind, watch)

    def _after(self, kind: str, watch: _Peak) -> None:
        """Refuse memory_insufficient when the memory sampled during a conversion fell below
        the reserve: the conversion's result is not used and the run stops there (a resume
        starts again from that minute)."""
        low = min(watch.low, self._memory())
        if low < self.limits.memory_reserve:
            raise ModelRefused("memory_insufficient",
                               f"{kind}: {int(low)} bytes available during the conversion, "
                               f"reserve {self.limits.memory_reserve}")

    def usage(self) -> dict[str, int]:
        """The run's resources now, for the per-minute results."""
        return {"peak_rss_bytes": (self.rss or peak_rss)(),
                "available_memory_bytes": self._memory(), "free_disk_bytes": self._free()}
