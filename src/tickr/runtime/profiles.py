"""Profile recorder: the instrument profile of every screener instrument, captured as read.

The owner wants to know, for later research on shorts, whether each listed stock could be
shorted and on what terms at the time (2026-11-16): the profile row says `shortable`,
`easy_to_borrow`, `marginable` and the margin requirements. Webull states no borrow quantity
(docs/webull-facts.md), so these flags are all there is.

Each screener round (a discovery pass, or the pre-market and post-market recorder) hands its
rows here. Due are:
- every instrument not asked yet that New York day;
- again, once `[market_data] profile_refresh_seconds` have passed since it was last asked, the
  losers and the gainers whose move from the previous close (`price` / `pre_close` of the
  screener row) is at least `profile_refresh_gainer_move_percent`: the stocks whose borrow
  terms may change during the day.

At most `profile_max_requests_per_round` profile requests a round, each leaving
`profile_reserve_requests` of the route free for discovery's security-type lookups. A request
refused (budget or a failed read) ends the round with a `profiles-skipped` event in
`events.jsonl`: the request's `symbols`, its `cause` and how many more were `waiting`; the
round cap alone writes one with cause `round-cap` and the count. Everything not read stays due
for the next round. The reads run on a thread of their own (`submit`): neither a discovery
pass nor the loop ever waits for them. A row whose price or
previous close is missing or not a number is never a "big gainer" (no guess), but is still
captured when first listed.

Only reads market data; nothing here decides, places or asks anything of the broker's orders.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from tickr.broker.base import BrokerError, RateLimited
from tickr.calendar import ny_date_of
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError, ScreenerKind, ScreenerRow
from tickr.data.capture import PROFILES_SKIPPED, CaptureWriter
from tickr.data.discovery import SKIPPED_BUDGET

PERCENT = Decimal(100)
PRICE_FIELD = "price"  # screener row: last price, string (docs/webull-facts.md)
PREVIOUS_CLOSE_FIELD = "pre_close"  # screener row: previous close, string
SYMBOL_SEPARATOR = ","  # the `symbols` of a profiles-skipped event
# The cause of a profiles-skipped event for instruments due beyond the round's request cap.
SKIPPED_ROUND_CAP = "round-cap"
PROFILE_THREAD = "tickr-profiles"


def move_percent(row: ScreenerRow) -> Decimal | None:
    """The row's move from the previous close in percent, or None when not stated."""
    try:
        price = Decimal(row.fields[PRICE_FIELD])
        previous = Decimal(row.fields[PREVIOUS_CLOSE_FIELD])
    except (KeyError, InvalidOperation):
        return None
    if not previous.is_finite() or not price.is_finite() or previous <= 0:
        return None
    return (price - previous) / previous * PERCENT


Spawn = Callable[[Callable[[], None]], threading.Thread | None]


def profile_thread(target: Callable[[], None]) -> threading.Thread:
    """Run `target` on a daemon thread of its own: never keeps the process alive."""
    thread = threading.Thread(target=target, name=PROFILE_THREAD, daemon=True)
    thread.start()
    return thread


class ProfileRecorder:
    """Rounds are handed over with `submit` (by the loop: a collected discovery pass, or a
    recorder round) and read on a thread of their own, so a slow profile route never delays a
    discovery pass, its candidates or the loop. One round runs at a time; rows submitted
    meanwhile replace any earlier ones still waiting (the newest lists are what is due). The
    thread never touches the store: a failure is kept for `take_errors`, which the loop
    journals. `stop` ends the round in flight before its next request."""

    def __init__(self, config: Config, source: MarketData, capture: CaptureWriter | None,
                 now: Callable[[], datetime], spawn: Spawn = profile_thread) -> None:
        md = config.market_data
        self.refresh = timedelta(seconds=float(md.profile_refresh_seconds))
        self.gainer_move = Decimal(md.profile_refresh_gainer_move_percent)
        self.max_requests = md.profile_max_requests_per_round
        self.reserve = md.profile_reserve_requests
        self.chunk = md.profile_symbols_per_request
        self.source = source
        self.capture = capture
        self.now = now
        self._spawn = spawn
        self._lock = threading.Lock()  # guards the hand-over state below
        self._pending: tuple[ScreenerRow, ...] | None = None
        self._running = False
        self._errors: list[Exception] = []
        self._thread: threading.Thread | None = None
        self.stopping = threading.Event()
        # Used by the round in flight only (one at a time).
        self._day: date | None = None
        # instrument_id -> when last asked that day (a row the provider did not return is not
        # asked again before the refresh time either: no request spent every round on it).
        self._asked: dict[str, datetime] = {}

    def submit(self, rows: Sequence[ScreenerRow]) -> None:
        """Hand a round over; returns at once."""
        if self.capture is None or not rows:
            return
        with self._lock:
            if self.stopping.is_set():
                return
            self._pending = tuple(rows)
            if self._running:
                return  # the running thread takes it next
            self._running = True
        try:
            self._thread = self._spawn(self._drain)
        except BaseException:
            with self._lock:
                self._running = False
            raise

    def _drain(self) -> None:
        while True:
            with self._lock:
                rows, self._pending = self._pending, None
                if rows is None or self.stopping.is_set():
                    self._running = False
                    return
            try:
                self.record(rows, self.now())
            except Exception as exc:  # noqa: BLE001 - kept for the loop to journal
                with self._lock:
                    self._errors.append(exc)

    def take_errors(self) -> list[Exception]:
        """Failures of rounds since the last call (a capture write, for example)."""
        with self._lock:
            errors, self._errors = self._errors, []
        return errors

    def stop(self, timeout_seconds: float) -> bool:
        """No new round; True when none is left running after at most `timeout_seconds`."""
        self.stopping.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
            return not thread.is_alive()
        return True

    def due(self, rows: Sequence[ScreenerRow], now: datetime) -> list[str]:
        """The symbols to read this round, first-listed before refreshes, in row order."""
        first: list[str] = []
        again: list[str] = []
        for row in rows:
            last = self._asked.get(row.instrument_id)
            if last is None:
                first.append(row.symbol)
            elif now - last >= self.refresh and self._watched(row):
                again.append(row.symbol)
        return list(dict.fromkeys(first + again))

    def _watched(self, row: ScreenerRow) -> bool:
        if row.kind is ScreenerKind.LOSERS:
            return True
        move = move_percent(row) if row.kind is ScreenerKind.GAINERS else None
        return move is not None and move >= self.gainer_move

    def record(self, rows: Sequence[ScreenerRow], now: datetime) -> None:
        """One round for `rows`, on the calling thread."""
        if self.capture is None or not rows:
            return
        today = ny_date_of(now)
        if self._day != today:
            self._day, self._asked = today, {}
        symbols = self.due(rows, now)
        limit = self.max_requests * self.chunk
        asked, later = symbols[:limit], symbols[limit:]
        wanted = {r.instrument_id: r.symbol for r in rows}
        for start in range(0, len(asked), self.chunk):
            chunk = asked[start:start + self.chunk]
            if self.stopping.is_set():
                return  # the run is ending: no request is started now
            try:
                got = self.source.profiles(chunk, budget_reserve=self.reserve)
            except (RateLimited, MarketDataError, BrokerError) as exc:
                cause = SKIPPED_BUDGET if isinstance(exc, RateLimited) else type(exc).__name__
                waiting = len(asked) - start - len(chunk) + len(later)
                self._skipped(chunk, waiting, cause, now)
                return
            # Only the listed instruments: a stale second row of a symbol is not theirs.
            kept = [p for p in got if wanted.get(p.instrument_id) == p.symbol]
            self.capture.profiles(kept, self.source.provider, now)
            for instrument_id, symbol in wanted.items():
                if symbol in chunk:
                    self._asked[instrument_id] = now
        if later:
            self._skipped([], len(later), SKIPPED_ROUND_CAP, now)

    def _skipped(self, symbols: Sequence[str], waiting: int, cause: str,
                 now: datetime) -> None:
        if self.capture is not None:
            self.capture.event(PROFILES_SKIPPED, now, symbols=SYMBOL_SEPARATOR.join(symbols),
                               waiting=str(waiting), cause=cause)
