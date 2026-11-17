"""Read the latest past session's files into the API's caches before anyone asks for them.

Picking a past day in Trades asks for its trades, flow, watching, symbols, movers, observers and
skipped signals. The first answer for a day parses its captured bars (hundreds of megabytes on a
busy day), its decision file and its screener file: tens of seconds of CPU, which the owner saw
as a day that would not open. Those files never change once the day is over, and the readers
keep the last past day they read, so the API reads the latest past day once, in a background
thread, the first time it answers on a new New York date (its start, or the first request after
midnight). Later past days are still read when asked for.

Display only: warming never writes, never reads the broker, and a failure only means the first
request for that day reads the files itself and answers with its own cause.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from datetime import date

Warm = Callable[[date, date], None]  # reads one past day (day, today) into the caches


class PastDayWarmer:
    """Starts one warming thread per New York date. Thread safe; shared by both listeners."""

    def __init__(self, latest_past_day: Callable[[date], date | None], warm: Warm,
                 errors: tuple[type[BaseException], ...]) -> None:
        self.latest_past_day = latest_past_day
        self.warm = warm
        self.errors = errors  # what a failed read of the day's files raises
        self.enabled = False  # set by make_server(warm=True); the tests' servers never warm
        self._lock = threading.Lock()
        self._for: date | None = None  # the New York date the last warming was started on
        self.thread: threading.Thread | None = None  # the last one started (tests join it)

    def on_request(self, today: date) -> None:
        """Called on each request: starts warming once per New York date."""
        if not self.enabled:
            return
        with self._lock:
            if self._for == today:
                return
            self._for = today
            self.thread = threading.Thread(target=self._run, args=(today,), daemon=True,
                                           name="warm-past-day")
            self.thread.start()

    def _run(self, today: date) -> None:
        try:
            day = self.latest_past_day(today)
            if day is None:
                return
            started = time.monotonic()
            self.warm(day, today)
            seconds = time.monotonic() - started
            sys.stderr.write(f"api warmed past day {day.isoformat()} in {seconds:.1f} s\n")
        except self.errors as exc:
            sys.stderr.write(f"api could not warm the latest past day: "
                             f"{type(exc).__name__}: {exc}\n")
