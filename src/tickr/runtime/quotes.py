"""The live quotes file: the newest valid L1 quote of every symbol on the bot's quote stream,
written by a daemon thread for the API to serve at GET /quotes (docs/api.md). Display only.

The stream delivers quotes several times a second, the trading loop writes its status only once
a pass (`[orders] detail_poll_seconds`), so the apps' prices go through this file instead: the
thread looks every `[api] quotes_write_seconds` and writes the file only when a quote changed
or another arrived (`stream_received_at`, by which the apps tell the stream still delivers).
It reads the stream inbox (one lock, one dictionary copy) and touches no engine or store state,
so it never delays the trading loop; a failed write keeps the old file (the apps then see its
quotes age) and is journaled once per stretch of failures by the loop."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from tickr.api import contract as api
from tickr.engine.core import Engine
from tickr.types import Quote

# Journal row (by the loop) when the thread could not write the file: `{error}` (type only).
QUOTES_WRITE_FAILED = "quotes-write-failed"
THREAD_NAME = "tickr-quotes-file"
ZERO = 0


class QuoteSource(Protocol):
    def snapshot(self) -> dict[str, Quote]: ...

    def connected_at(self) -> datetime | None: ...


def quotes_document(quotes: dict[str, Quote], written_at: datetime,
                    connected_at: datetime | None) -> api.QuotesV1:
    """Every valid quote (a bid above zero and not above the ask), by symbol; others are left
    out, never shown as a price. `stream_received_at`: the newest receipt of any quote held
    (valid or not), so the apps can tell the stream is still delivering; `stream_connected_at`:
    when the transport last came up, so a quote received before a reconnect is not taken as
    current."""
    received = max((q.receipt_time for q in quotes.values()), default=None)
    rows = tuple(api.LiveQuote(q.symbol, q.bid, q.ask, q.provider_time, q.receipt_time)
                 for _, q in sorted(quotes.items()) if ZERO < q.bid <= q.ask)
    return api.QuotesV1(written_at=written_at, quotes=rows, stream_received_at=received,
                       stream_connected_at=connected_at)


class QuoteFile:
    def __init__(self, source: QuoteSource, path: Path, interval_seconds: float,
                 now: Callable[[], datetime]) -> None:
        self._source = source
        self._path = path
        self._interval = interval_seconds
        self._now = now
        self._lock = threading.Lock()
        # The quotes, stream receipt and connect time last written: rewritten when one changed.
        self._written: tuple[tuple[api.LiveQuote, ...], datetime | None,
                             datetime | None] | None = None
        self._failing = False
        self._failures: list[dict[str, str]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
            self._thread.start()

    def stop(self, timeout_seconds: float) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.write_once()
            self._stop.wait(self._interval)

    def write_once(self) -> bool:
        """Write the file when a quote changed since the last write; True when it wrote. Called
        by the thread; tests call it directly. Never raises: a failure is kept for the loop."""
        try:
            document = quotes_document(self._source.snapshot(), self._now(),
                                       self._source.connected_at())
            written = (document.quotes, document.stream_received_at,
                       document.stream_connected_at)
            if written == self._written:
                return False
            api.write_quotes(self._path, document)
        except Exception as exc:  # noqa: BLE001 - the display thread must outlive a bug
            with self._lock:
                if not self._failing:
                    self._failures.append({"error": type(exc).__name__})
                self._failing = True
            return False
        self._written = written
        with self._lock:
            self._failing = False
        return True

    def journal_failures(self, engine: Engine) -> None:
        """On the loop thread: journal the failures reported since the last call."""
        with self._lock:
            failures, self._failures = self._failures, []
        if failures:
            with engine.store.transaction():
                for body in failures:
                    engine.store.append(QUOTES_WRITE_FAILED, body, run_id=engine.run_id)
