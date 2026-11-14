"""Per-day decision record files.

Path: <data_dir>/<decisions_dir>/<environment>/<NY date>/decisions.jsonl.

One line per decision in the format of `aperture.research.records` (the research tools read
these files directly). Lines are appended and flushed one by one; the file of a previous day
is closed when the first record of a new day arrives. Lines are flushed but NOT fsynced: a
power loss can drop the last records. These are research data, not trading state; the trading
journal (store.py) keeps its own durability.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import TextIO

from aperture.calendar import ny_date_of
from aperture.config import Config
from aperture.research.records import DecisionRecord, record_to_json

FILE_NAME = "decisions.jsonl"


class DecisionLog:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._day: date | None = None
        self._handle: TextIO | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: Config) -> DecisionLog:
        base = config.root / config.run.data_dir / config.observers.decisions_dir
        return cls(base / config.environment_name)

    def path(self, day: date) -> Path:
        return self.directory / day.isoformat() / FILE_NAME

    def append(self, record: DecisionRecord) -> None:
        """Write one record (refused if it breaks the record rules) and flush it. Safe to call
        from the trading thread and the observer worker at once (whole lines, one lock)."""
        line = record_to_json(record)
        with self._lock:
            self._append(line, ny_date_of(record.bar_start))

    def _append(self, line: str, day: date) -> None:
        if day != self._day or self._handle is None:
            self._close()
            path = self.path(day)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(path, "a", encoding="utf-8")
            self._day = day
        self._handle.write(line + "\n")
        self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None
        self._day = None
