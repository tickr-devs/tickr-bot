"""Today's decision records for the API, parsed once and then read on from where the last read
stopped.

The apps poll GET /flow, /watching and /skipped every few seconds, and each answer needs the
day's decision records. `DecisionLog.records` parses the whole file every time; by the afternoon
of a day with a few dozen symbols that file holds tens of thousands of records, and the reparse
was most of the API's CPU. The bot only ever appends to today's file, so this cache keeps the
records parsed so far and the byte offset after the last complete line, and on each call parses
only the complete lines added since.

Only today's plain file is read on this way. It starts over from the top whenever the file is no
longer the one it read (another inode, or shorter than what was read, or a first line that is not
the one it read). The bot only appends to today's file. Reading never writes; the bot never waits
for it.

A past day's file (plain or archived) is parsed once and kept until another past day is asked
for or the file changes (another inode, size or modification time): picking a past day in Trades
asks for its flow, watching, symbols, movers, observers and skipped signals at once, and each
reparsed the whole file (tens of megabytes on a busy day, seconds each). Only the last past day
asked for is kept. Requests for it that come while it is being parsed wait for that one parse.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tickr.archive import existing, open_text
from tickr.observers.log import DecisionLog, filter_keys
from tickr.research.errors import RecordFileError
from tickr.research.records import DecisionRecord, record_from_mapping
from tickr.strategy.base import Role

NEWLINE = b"\n"
BadLine = tuple[int, str, str]  # line number, text and cause of a malformed line


@dataclass(frozen=True)
class _PastDay:
    """A past day's file as parsed: which file (path, device, inode, size, modification time),
    its records in file order and its malformed lines."""

    file: tuple[str, int, int, int, int]
    records: tuple[DecisionRecord, ...]
    bad: tuple[BadLine, ...]


def _filtered(path: Path, records: tuple[DecisionRecord, ...] | list[DecisionRecord],
              bad: tuple[BadLine, ...] | list[BadLine], symbol: str | None,
              role: Role | None) -> tuple[DecisionRecord, ...]:
    """What `DecisionLog.records(symbol=, role=)` returns from these parsed lines: a malformed
    line refuses exactly the reads whose filter keys its text holds."""
    keys = filter_keys(symbol, role)
    for number, text, cause in bad:
        if all(key in text for key in keys):
            raise RecordFileError(str(path), number, cause)
    return tuple(r for r in records
                 if (symbol is None or r.symbol == symbol) and (role is None or r.role is role))


class DecisionCache:
    def __init__(self, log: DecisionLog) -> None:
        self.log = log
        self._lock = threading.Lock()
        self._file: tuple[str, int, int] | None = None  # path, st_dev, st_ino of what was read
        self._offset = 0  # bytes of the file parsed so far: complete lines only
        self._lines = 0  # lines parsed so far, for the line number of a malformed record
        self._records: list[DecisionRecord] = []
        self._bad: list[BadLine] = []
        self._head = b""  # the file's first line as read; a different one means another file
        self._past_lock = threading.Lock()  # one parse of a past day at a time; today never waits
        self._past: _PastDay | None = None

    def records(self, day: date, today: date, *, symbol: str | None = None,
                role: Role | None = None) -> tuple[DecisionRecord, ...]:
        """What `DecisionLog.records(day, symbol=, role=)` returns, read incrementally when
        `day` is `today` and its file is plain. A malformed line refuses exactly the reads
        DecisionLog.records refuses for it: those whose filter keys the line's text holds.
        Raises RecordFileError (malformed line) or OSError like DecisionLog.records."""
        path = self.log.path(day)
        if day == today:
            with self._lock:
                try:
                    read = self._read_on(str(path))
                except FileNotFoundError:  # no file yet, or archived meanwhile
                    read = None
                if read is not None:
                    return _filtered(path, read, self._bad, symbol, role)
            return self.log.records(day, symbol=symbol, role=role)
        found = existing(path)
        if found is None:
            return ()
        with self._past_lock:
            past = self._past_day(path, found)
        return _filtered(path, past.records, past.bad, symbol, role)

    def _past_day(self, path: Path, found: Path) -> _PastDay:
        """The parsed past day file `found` (the plain or archived file of `path`), parsed now
        unless it is the one kept. Parses as `DecisionLog.records` reads: a line without its
        newline and blank lines are not read."""
        stat = found.stat()
        file = (str(found), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if self._past is not None and self._past.file == file:
            return self._past
        records: list[DecisionRecord] = []
        bad: list[BadLine] = []
        with open_text(path) as handle:
            for number, text in enumerate(handle, start=1):
                if not text.endswith("\n") or not text.strip():
                    continue
                try:
                    records.append(record_from_mapping(json.loads(text)))
                except ValueError as exc:  # JSONDecodeError is a ValueError
                    bad.append((number, text, str(exc)))
        self._past = _PastDay(file, tuple(records), tuple(bad))
        return self._past

    def _read_on(self, path: str) -> list[DecisionRecord]:
        with open(path, "rb") as handle:
            stat = os.fstat(handle.fileno())
            file = (path, stat.st_dev, stat.st_ino)
            head = handle.read(len(self._head)) if self._head else b""
            if (file != self._file or stat.st_size < self._offset
                    or head != self._head):  # a recreated file can reuse the inode
                self._file, self._offset, self._lines, self._head = file, 0, 0, b""
                self._records, self._bad = [], []
            handle.seek(self._offset)
            added = handle.read()
        end = added.rfind(NEWLINE) + 1  # a last line still being written is not read yet
        number = self._lines
        records: list[DecisionRecord] = []
        bad: list[BadLine] = []
        for raw in added[:end].split(NEWLINE)[:-1]:
            number += 1
            text = raw.decode("utf-8", errors="replace")
            if not text.strip():  # str.strip, as DecisionLog.records
                continue
            try:
                records.append(record_from_mapping(json.loads(raw.decode("utf-8"))))
            except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
                bad.append((number, text, str(exc)))
        # Only a fully read chunk is kept: anything else raised leaves the cache as it was.
        self._records.extend(records)
        self._bad.extend(bad)
        if not self._head and end:
            self._head = added[:added.find(NEWLINE) + 1]  # the first line, to know the file again
        self._offset += end
        self._lines = number
        return self._records

