"""Synthetic full-market minute bars for the model tests: frames with the columns of a Massive
minute aggregates flat file, several tickers (SPY and QQQ among them), sparse minutes."""

from __future__ import annotations

import numpy as np
import pandas as pd

NY = "America/New_York"
MINUTES = 960  # 04:00 .. 19:59 New York, the table code's day
TICKERS = ["SPY", "QQQ"] + [f"T{i:03d}" for i in range(36)]
# first minute of the last tickers of the list (index from the end), m from 04:00 New York
LATE_START = {-1: 700, -2: 450, -3: 400, -4: 340}


def trading_days(count: int, last: str = "2026-11-18") -> list[str]:
    days = pd.bdate_range(end=last, periods=count)
    return [d.date().isoformat() for d in days]


def day_bars(day: str, seed: int, closes: dict[str, float], late: bool) -> pd.DataFrame:
    """One day of bars; `closes` carries each ticker's last price into the next day; `late`:
    the last tickers start trading late (LATE_START), as on the target day."""
    rs = np.random.RandomState(seed)
    start = pd.Timestamp(day + " 04:00", tz=NY).value
    rows = []
    for k, ticker in enumerate(TICKERS):
        price = closes.get(ticker, float(rs.uniform(0.5, 80)))
        busy = 0.95 if k < 2 else rs.uniform(0.2, 0.9)
        # the last tickers first trade late in the day: a whole-day table knows them earlier
        # than the live minute does, which is what the prefix rule must not leak
        first = LATE_START.get(k - len(TICKERS), 0) if late else 0
        for m in range(first, MINUTES):
            regular = 330 <= m < 720
            if rs.rand() > (busy if regular else busy / 3):
                continue
            o = price
            c = max(0.05, o * float(np.exp(rs.normal(0, 0.004 if k < 2 else 0.012))))
            hi = max(o, c) * (1 + abs(rs.normal(0, 0.002)))
            lo = min(o, c) * (1 - abs(rs.normal(0, 0.002)))
            vol = int(rs.lognormal(7, 1.2))
            rows.append((ticker, vol, o, c, hi, lo, start + m * 60 * 10**9, int(rs.randint(1, 50))))
            price = c
        closes[ticker] = price
    cols = ["ticker", "volume", "open", "close", "high", "low", "window_start", "transactions"]
    return pd.DataFrame(rows, columns=cols)


def market(days: int, seed: int = 7, last: str = "2026-11-18") -> list[tuple[str, pd.DataFrame]]:
    closes: dict[str, float] = {}
    return [(d, day_bars(d, seed + i, closes, i == days - 1))
            for i, d in enumerate(trading_days(days, last))]


def cut(frame: pd.DataFrame, day: str, minute: int) -> pd.DataFrame:
    """The day's bars up to and including `minute` (m from 04:00 New York)."""
    start = pd.Timestamp(day + " 04:00", tz=NY).value
    return frame[frame.window_start <= start + minute * 60 * 10**9].reset_index(drop=True)
