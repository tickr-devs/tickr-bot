"""Synthetic trades and quotes in the layout of Massive's us_stocks_sip flat files (trades_v1,
quotes_v1): every column of those files, realistic codes (sale conditions, corrections and the
later records about them, off-exchange prints with a TRF id, odd lots, ISO, quotes with equal
timestamps, locked / crossed / one-sided quotes, indicator codes), spread over 04:00..20:00 New
York with most activity in the regular session."""

from __future__ import annotations

import numpy as np
import pandas as pd

NY = "America/New_York"
# The flat files' columns, in their order (Massive us_stocks_sip trades_v1 / quotes_v1 headers).
TRADE_FILE_COLUMNS = ["ticker", "conditions", "correction", "exchange", "id",
                      "participant_timestamp", "price", "sequence_number", "sip_timestamp", "size",
                      "tape", "trf_id", "trf_timestamp"]
QUOTE_FILE_COLUMNS = ["ticker", "ask_exchange", "ask_price", "ask_size", "bid_exchange",
                      "bid_price", "bid_size", "conditions", "indicators", "participant_timestamp",
                      "sequence_number", "sip_timestamp", "tape", "trf_timestamp"]
CONDITIONS = np.array(["", "", "", "", "", "", "37", "37", "12", "14", "14,37", "41", "2", "15",
                       "16", "8", "25", "10", "53,37", "12,37"], dtype=object)
INDICATORS = np.array(["", "", "", "", "", "", "", "", "", "1", "604", "2,604"], dtype=object)
QCONDITIONS = np.array(["", "", "", "", "", "", "", "", "", "", "1", "20"], dtype=object)


def _stamps(rs: np.random.RandomState, day: str, n: int) -> np.ndarray:
    """n SIP nanosecond stamps of New York `day`: 6% pre-market, 90% regular session with more
    near the open and the close, 4% after hours."""
    t0 = pd.Timestamp(day + " 04:00", tz=NY).value
    part = rs.choice(3, size=n, p=[0.06, 0.90, 0.04])
    sec = np.empty(n)
    k = part == 0
    sec[k] = rs.uniform(0, 330 * 60, k.sum())
    k = part == 1
    u = rs.beta(0.6, 0.6, k.sum())  # U shape over 09:30..16:00
    sec[k] = 330 * 60 + u * 390 * 60
    k = part == 2
    sec[k] = rs.uniform(720 * 60, 960 * 60 - 1e-3, k.sum())
    ts = t0 + (sec * 1e9).astype(np.int64)
    # some events share a stamp (equal SIP timestamps happen, the sequence number decides)
    dup = rs.random_sample(n) < 0.02
    ts[1:][dup[1:]] = ts[:-1][dup[1:]]
    return np.sort(ts)


def day_events(day: str, symbols: int, trades: int, quotes: int,
               seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(trades, quotes) frames of one day: `trades` / `quotes` events per symbol on average
    (symbols differ by a factor up to 4), typed as the flat files read: integers for stamps,
    sequence numbers, codes; decimals for prices; text for condition lists."""
    rs = np.random.RandomState(seed)
    tickers = ["SPY", "QQQ"] + [f"S{i:04d}" for i in range(symbols - 2)]
    tparts, qparts = [], []
    seq = 0
    for ticker in tickers:
        weight = rs.uniform(0.25, 1.75)
        nt, nq = max(1, int(trades * weight)), max(1, int(quotes * weight))
        price0 = rs.uniform(2, 300) if ticker not in ("SPY", "QQQ") else 500.0
        qs = _stamps(rs, day, nq)
        mid = price0 * np.exp(np.cumsum(rs.normal(0, 2e-4, nq)))
        tick = np.where(mid >= 1, 0.01, 0.0001)
        half = tick * rs.randint(1, 4, nq)
        bid = np.round((mid - half) / tick) * tick
        ask = np.round((mid + half) / tick) * tick
        odd = rs.random_sample(nq)
        ask[odd < 0.01] = bid[odd < 0.01]  # locked
        ask[(odd >= 0.01) & (odd < 0.015)] -= 2 * tick[(odd >= 0.01) & (odd < 0.015)]  # crossed
        bsz = rs.randint(1, 30, nq).astype(float)
        asz = rs.randint(1, 30, nq).astype(float)
        bsz[(odd >= 0.015) & (odd < 0.02)] = 0  # one-sided
        bid[(odd >= 0.015) & (odd < 0.02)] = 0
        q = pd.DataFrame({
            "ticker": ticker, "ask_exchange": rs.randint(1, 20, nq), "ask_price": np.round(ask, 4),
            "ask_size": asz, "bid_exchange": rs.randint(1, 20, nq), "bid_price": np.round(bid, 4),
            "bid_size": bsz, "conditions": rs.choice(QCONDITIONS, nq),
            "indicators": rs.choice(INDICATORS, nq),
            "participant_timestamp": qs - rs.randint(1000, 900000, nq),
            "sequence_number": np.arange(seq, seq + nq), "sip_timestamp": qs, "tape": 1,
            "trf_timestamp": pd.array([pd.NA] * nq, dtype="Int64")})
        seq += nq
        ts = _stamps(rs, day, nt)
        j = np.clip(np.searchsorted(qs, ts) - 1, 0, nq - 1)
        px = mid[j] + rs.normal(0, 1, nt) * half[j]
        # off-exchange retail-like prints carry a sub-penny fraction
        off = rs.random_sample(nt) < 0.35
        px = np.round(np.where(off & (px >= 1), px, np.round(px / tick[j]) * tick[j]), 4)
        size = np.where(rs.random_sample(nt) < 0.4, rs.randint(1, 100, nt),
                        100 * rs.randint(1, 20, nt))
        corr = np.zeros(nt, dtype=np.int64)
        c = rs.random_sample(nt)
        corr[c < 0.002] = 1
        corr[(c >= 0.002) & (c < 0.003)] = 12
        corr[(c >= 0.003) & (c < 0.0035)] = 10
        trf = pd.array(np.where(off, rs.choice([201, 202, 203], nt), 0), dtype="Int64")
        trf[~off] = pd.NA
        t = pd.DataFrame({
            "ticker": ticker, "conditions": rs.choice(CONDITIONS, nt), "correction": corr,
            "exchange": np.where(off, 4, rs.randint(1, 20, nt)), "id": np.arange(nt),
            "participant_timestamp": ts - rs.randint(1000, 900000, nt), "price": px,
            "sequence_number": np.arange(seq, seq + nt), "sip_timestamp": ts, "size": size,
            "tape": 1, "trf_id": trf,
            "trf_timestamp": pd.array(np.where(off, ts - 500, 0), dtype="Int64")})
        t.loc[~off, "trf_timestamp"] = pd.NA
        seq += nt
        tparts.append(t)
        qparts.append(q)
    return pd.concat(tparts, ignore_index=True), pd.concat(qparts, ignore_index=True)


def before(frame: pd.DataFrame, stamp: int) -> pd.DataFrame:
    """The events with a SIP stamp before `stamp` (what a live reader holds at that time)."""
    return frame[frame.sip_timestamp < stamp].reset_index(drop=True)


def stamp(day: str, hhmmss: str) -> int:
    return pd.Timestamp(f"{day} {hhmmss}", tz=NY).value
