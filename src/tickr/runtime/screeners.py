"""Screener recorder: the three screener lists, read and captured outside discovery.

Discovery reads and captures the gainers, losers and top actives in the regular session. The
recorder covers the rest of the trading day the bot is running in: from the calendar's
pre-market open to the regular open, and from the regular close to the extended close. It reads
the three lists once every `[market_data] screener_record_seconds`, in the loop pass that is the
first of a minute, and appends them to the capture's `screener-recorded.jsonl` with the session
they rank by, so the lists the bot received are kept as they came, with their time, for
research (owner 2026-11-16). The gainers and losers rank by the pre-market's or the
post-market's change (`[market_data] premarket_/postmarket_gainers_losers_rank_type`), not by the
regular session's.

A kind not read (our request budget spent, or the read failed) is written to `events.jsonl`
as `screener-skipped` with its cause, together with the kinds after it, which are not asked
this round. The pre-open history preload (runtime.preload) takes its symbols from the rows
this returns, so the bot reads the screeners once per round, never twice.

Only reads: nothing here decides, places or asks anything of the broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tickr.broker.base import BrokerError, RateLimited
from tickr.calendar import MarketDay
from tickr.config import Config
from tickr.data.base import MarketData, MarketDataError, ScreenerKind, ScreenerRow, ScreenerSession
from tickr.data.capture import SCREENER_RECORDED_FILE, CaptureWriter
from tickr.data.discovery import SKIPPED_BUDGET

RECORDER_SOURCE = "recorder"  # the `source` of the recorder's screener-skipped events


def extended_session(day: MarketDay, now: datetime) -> ScreenerSession | None:
    """PRE or POST when `now` is in the pre-market or the post-market of `day` (the calendar's
    hours), else None."""
    if day.premarket_open <= now < day.regular_open:
        return ScreenerSession.PRE
    if day.regular_close <= now < day.extended_close:
        return ScreenerSession.POST
    return None


class ScreenerRecorder:
    def __init__(self, config: Config, source: MarketData, capture: CaptureWriter | None,
                 ) -> None:
        self.interval = timedelta(seconds=float(config.market_data.screener_record_seconds))
        self.source = source
        self.capture = capture
        self._next: datetime | None = None

    def tick(self, day: MarketDay, now: datetime) -> list[ScreenerRow] | None:
        """One round when due: the rows of all three lists, or None (not due, outside the
        pre-market and post-market, or a kind not read)."""
        session = extended_session(day, now)
        if session is None:
            return None
        if self._next is not None and now < self._next:
            return None
        self._next = now.replace(second=0, microsecond=0) + self.interval
        rows: list[ScreenerRow] = []
        kinds = list(ScreenerKind)
        for number, kind in enumerate(kinds):
            try:
                got = self.source.screener(kind, session)
            except (RateLimited, MarketDataError, BrokerError) as exc:
                cause = SKIPPED_BUDGET if isinstance(exc, RateLimited) else type(exc).__name__
                if self.capture is not None:
                    self.capture.screener_skipped(kinds[number:], cause, RECORDER_SOURCE, now)
                return None
            if self.capture is not None:
                self.capture.screener(got, self.source.provider, now,
                                      file=SCREENER_RECORDED_FILE, session=session)
            rows.extend(got)
        return rows
