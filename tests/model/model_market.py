"""Randomized full-market-like minute bars for the incremental table tests: more tickers than
the table code's list depth (TOPN 500), so the lists churn; sparse and busy tickers, pre- and
post-market, halts, late starts, tickers without a previous close, zero-volume bars, prices
under the table code's MINP and prices on a cent grid (ties in the change ranks)."""

from __future__ import annotations

import numpy as np
import pandas as pd

NY = "America/New_York"
MINUTES = 960  # 04:00 .. 19:59 New York, the table code's day
REGULAR = (330, 720)


def tickers(n: int) -> list[str]:
    return ["SPY", "QQQ"] + [f"S{i:04d}" for i in range(n - 2)]


def day_bars(day: str, names: list[str], seed: int, closes: dict[str, float],
             absent: float) -> pd.DataFrame:
    """One day; `closes` carries last prices into the next day; a share `absent` of the
    tickers does not trade at all that day (so the next day has no previous close for them)."""
    rs = np.random.RandomState(seed)
    n = len(names)
    m = np.arange(MINUTES)
    regular = (m >= REGULAR[0]) & (m < REGULAR[1])
    busy = np.r_[0.98, 0.98, rs.beta(0.5, 1.0, n - 2)]
    act = rs.rand(n, MINUTES) < np.where(regular[None, :], busy[:, None], busy[:, None] / 4)
    act[:2, REGULAR[0]:REGULAR[1]] = True
    act &= m[None, :] >= np.where(rs.rand(n) < 0.05, rs.randint(0, MINUTES, n), 0)[:, None]
    halt = (rs.rand(n) < 0.05)[:, None]
    hs = rs.randint(300, 900, n)[:, None]
    act &= ~(halt & (m[None, :] >= hs) & (m[None, :] < hs + rs.randint(5, 60, n)[:, None]))
    act[2:][rs.rand(n - 2) < absent] = False
    p0 = np.array([closes.get(t, float(np.exp(rs.uniform(np.log(0.03), np.log(300)))))
                   for t in names])
    step = np.where(rs.rand(n, 1) < 0.15, 0.04, 0.01) * rs.normal(0, 1, (n, MINUTES))
    step[:, 0] += np.where(rs.rand(n) < 0.1, rs.normal(0, 0.4, n), 0.0)
    c = np.exp(np.log(p0)[:, None] + np.cumsum(np.where(act, step, 0.0), axis=1))
    cent = rs.rand(n, 1) < 0.5  # half the tickers trade on a cent grid: equal changes
    c = np.where(cent & (c >= 1), np.maximum(np.round(c, 2), 0.01), np.round(c, 4))
    c = np.maximum(c, 0.0001)
    o = np.concatenate([p0[:, None], c[:, :-1]], axis=1)
    hi = np.maximum(o, c) * (1 + np.abs(rs.normal(0, 0.003, (n, MINUTES))))
    lo = np.minimum(o, c) * (1 - np.abs(rs.normal(0, 0.003, (n, MINUTES))))
    v = np.floor(rs.lognormal(5, 1.5, (n, MINUTES)) * np.where(rs.rand(n, 1) < 0.05, 40, 1))
    v[rs.rand(n, MINUTES) < 0.02] = 0
    ii, jj = np.nonzero(act)
    start = pd.Timestamp(day + " 04:00", tz=NY).value
    frame = pd.DataFrame({
        "ticker": np.array(names, dtype=object)[ii], "volume": v[ii, jj], "open": o[ii, jj],
        "close": c[ii, jj], "high": hi[ii, jj], "low": lo[ii, jj],
        "window_start": start + jj.astype(np.int64) * 60 * 10**9, "transactions": 1})
    for i, last in pd.Series(c[ii, jj]).groupby(ii).last().items():
        closes[names[i]] = float(last)
    return frame.sample(frac=1, random_state=seed).reset_index(drop=True)  # any row order


def market(days: list[str], n: int, seed: int) -> list[tuple[str, pd.DataFrame]]:
    closes: dict[str, float] = {}
    names = tickers(n)
    return [(d, day_bars(d, names, seed * 100 + k, closes, 0.03)) for k, d in enumerate(days)]


def cut(frame: pd.DataFrame, day: str, minute: int) -> pd.DataFrame:
    """The day's bars up to and including `minute` (m from 04:00 New York)."""
    start = pd.Timestamp(day + " 04:00", tz=NY).value
    return frame[frame.window_start < start + (minute + 1) * 60 * 10**9].reset_index(drop=True)
