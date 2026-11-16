"""Today's M1 bars, one read shared by discovery and the strategy's evaluation.

Discovery reads the session's M1 bars of several hundred symbols every pass; evaluation needs the
same bars for the candidates each completed minute. `TodayBars` keeps, per symbol, the bars of the
latest read and the time that read was SENT, taken from the local clock before the request.
Evaluation of the minute starting at M uses a symbol's kept bars only when that read was sent
at or after M + 1 minute by our clock; otherwise it reads the symbol itself. This is the same
completeness check as a direct read at that moment, and it has the same limit: it trusts the
local clock, and a bar the provider publishes late is simply absent from the read, so the
strategy then decides on the previous bar (as it did before this cache existed). Discovery's
reserve rule (`[market_data] discovery_bars_reserve_requests`) is unchanged.

Keyed by the regular session's open: the first use for another session drops everything. A put
also drops the reads sent before the minute it was sent in: they can no longer serve the
minute being evaluated, so memory does not grow all session (a remainder of an older minute
then reads for itself). Thread safe (one lock); nothing here sends a request.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import datetime

from tickr.types import Bar


class TodayBars:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: datetime | None = None
        self._reads: dict[str, tuple[datetime, tuple[Bar, ...]]] = {}

    def _of(self, session_open: datetime) -> dict[str, tuple[datetime, tuple[Bar, ...]]]:
        if self._session != session_open:
            self._session, self._reads = session_open, {}
        return self._reads

    def put(self, session_open: datetime, symbols: Sequence[str], bars: Sequence[Bar],
            sent_at: datetime) -> None:
        """The bars one read returned for `symbols` (a symbol with none keeps an empty read),
        sent at `sent_at`. An older read never replaces a newer one."""
        grouped: dict[str, list[Bar]] = {s: [] for s in symbols}
        for bar in bars:
            if bar.symbol in grouped:
                grouped[bar.symbol].append(bar)
        minute = sent_at.replace(second=0, microsecond=0)
        with self._lock:
            reads = self._of(session_open)
            for stale in [s for s, (at, _) in reads.items() if at < minute]:
                del reads[stale]
            for symbol, own in grouped.items():
                kept = reads.get(symbol)
                if kept is None or kept[0] <= sent_at:
                    reads[symbol] = (sent_at, tuple(sorted(own, key=lambda b: b.start)))

    def __len__(self) -> int:
        with self._lock:
            return len(self._reads)

    def complete(self, session_open: datetime, symbol: str,
                 minute_end: datetime) -> tuple[Bar, ...] | None:
        """The kept bars of `symbol` when its latest read was sent at or after `minute_end`
        (the minute in question was over), else None: the caller reads it itself. The bars may
        include later minutes; the caller cuts them at its minute."""
        with self._lock:
            kept = self._of(session_open).get(symbol)
        if kept is None or kept[0] < minute_end:
            return None
        return kept[1]
