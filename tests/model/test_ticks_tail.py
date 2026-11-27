"""The incremental tick path: one closed minute from a bounded tail (`tail_carry` +
`minute_rows`) gives the same rows as the converter on the whole day so far, minute by minute,
on randomized days; with the marker quote, the training's whole-day rows."""

from __future__ import annotations

from pathlib import Path

import model_ticks as synthetic
import numpy as np
import pandas as pd
import pytest

from tickr import config
from tickr.model.errors import ModelRefused
from tickr.model.features import ticks

DAY = "2026-11-18"
NS_PER_MINUTE = 60 * 10**9
ROOT = Path(__file__).resolve().parents[2]
GROUPS = config.load(ROOT / "config" / "bot.toml", root=ROOT).model.tick_groups
TIME_WEIGHTED = ["qspr", "qbs", "qas", "qimb", "qbad", "qbd", "qad"]
# windows of a randomized day: across the regular-session open (Form T counts for prices only
# outside it), mid-day, across the close; minutes of the New York day
FIRST_MINUTES = (9 * 60 + 24, 11 * 60, 15 * 60 + 54)
SPAN = 10  # minutes of each randomized day
CONDITIONS = np.array(["", "", "", "", "", "12", "12", "12,37", "37", "14", "14,37", "15", "16",
                       "38", "10", "22", "5", "2", "25", "8", "53,37"], dtype=object)
CORRECTIONS = np.array([0] * 12 + [1, 7, 8, 10, 11, 12])
QCOND = np.array(["", "", "", "", "", "1", "20"], dtype=object)
QIND = np.array(["", "", "", "", "", "1", "604", "2,604"], dtype=object)


@pytest.fixture(autouse=True)
def bot_thread_count(monkeypatch):
    """The bot sets the converter's one engine thread (test_ticks.py); each test starts with the
    variable unset, as a fresh process does."""
    monkeypatch.delenv(ticks.THREADS_VARIABLE, raising=False)


def _stamps(rs: np.random.RandomState, t0: int, n: int) -> np.ndarray:
    """n stamps over the window: some on a minute's first or last nanosecond, some shared."""
    ts = t0 + rs.randint(0, SPAN * NS_PER_MINUTE, n).astype(np.int64)
    edge = rs.random_sample(n)
    ts[edge < 0.05] = ts[edge < 0.05] // NS_PER_MINUTE * NS_PER_MINUTE
    ts[(edge >= 0.05) & (edge < 0.1)] = (ts[(edge >= 0.05) & (edge < 0.1)] // NS_PER_MINUTE + 1) \
        * NS_PER_MINUTE - 1
    ts = np.minimum(ts, t0 + SPAN * NS_PER_MINUTE - 1)
    tie = rs.random_sample(n) < 0.15
    ts = np.sort(ts)
    ts[1:][tie[1:]] = ts[:-1][tie[1:]]
    return ts


def _silence(rs: np.random.RandomState, t0: int, ts: np.ndarray) -> np.ndarray:
    """Drop a random stretch of minutes (a halt, a quiet stock): the kept stamps."""
    a = t0 + rs.randint(0, SPAN) * NS_PER_MINUTE
    b = a + rs.randint(1, 5) * NS_PER_MINUTE
    return ts[(ts < a) | (ts >= b)]


def random_day(seed: int) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """A small randomized day in the flat-file layout: several stocks over SPAN minutes with
    halts and quiet stretches, events on minute edges, equal stamps (between quotes, between
    trades and quotes, between trades) told apart by sequence numbers, every correction code,
    dropped and price-class conditions, Form T across the open, zero and empty quote sides,
    locked and crossed quotes, cheap and sub-penny prints, zero prices and sizes, rows in a
    random order. Returns (trades, quotes, first minute)."""
    rs = np.random.RandomState(seed)
    first = int(FIRST_MINUTES[seed % len(FIRST_MINUTES)])
    t0 = synthetic.stamp(DAY, f"{first // 60:02d}:{first % 60:02d}:00")
    tparts, qparts = [], []
    for k in range(rs.randint(3, 7)):
        ticker = f"R{k}"
        qs = _stamps(rs, t0, rs.randint(0, 150))
        if rs.random_sample() < 0.7:
            qs = _silence(rs, t0, qs)
        tt = _stamps(rs, t0, rs.randint(0, 120))
        if len(qs) and len(tt):
            share = rs.random_sample(len(tt)) < 0.15  # a trade on a quote's exact stamp
            tt[share] = rs.choice(qs, share.sum())
            tt = np.sort(tt)
        if rs.random_sample() < 0.5:
            tt = _silence(rs, t0, tt)
        # sequence numbers of the stock: increasing with the stamp, random order within one
        stamps = np.concatenate([qs, tt])
        order = np.lexsort((rs.random_sample(len(stamps)), stamps))
        seq = np.empty(len(stamps), dtype=np.int64)
        seq[order] = 1000 * k + np.arange(len(stamps))
        nq, nt = len(qs), len(tt)
        price0 = rs.choice([0.37, 3.5, 42.0, 410.0])
        tick = 0.0001 if price0 < 1 else 0.01
        mid = price0 * np.exp(np.cumsum(rs.normal(0, 1e-3, max(nq, 1))))[:nq]
        bid = np.round((mid - tick) / tick) * tick
        ask = np.round((mid + tick * rs.randint(1, 3, nq)) / tick) * tick
        bsz = rs.randint(1, 9, nq).astype(float)
        asz = rs.randint(1, 9, nq).astype(float)
        odd = rs.random_sample(nq)
        ask[odd < 0.05] = bid[odd < 0.05]  # locked
        ask[(odd >= 0.05) & (odd < 0.08)] = bid[(odd >= 0.05) & (odd < 0.08)] - tick  # crossed
        zero = (odd >= 0.08) & (odd < 0.12)
        bid[zero], bsz[zero] = 0.0, 0.0  # a side written as 0
        empty = (odd >= 0.12) & (odd < 0.16)
        ask[empty], asz[empty] = np.nan, np.nan  # a side left empty
        qparts.append(pd.DataFrame({
            "ticker": ticker, "ask_exchange": rs.randint(1, 20, nq), "ask_price": ask,
            "ask_size": asz, "bid_exchange": rs.randint(1, 20, nq), "bid_price": bid,
            "bid_size": bsz, "conditions": rs.choice(QCOND, nq), "indicators": rs.choice(QIND, nq),
            "participant_timestamp": qs - 1000, "sequence_number": seq[:nq], "sip_timestamp": qs,
            "tape": 1, "trf_timestamp": pd.array([pd.NA] * nq, dtype="Int64")}))
        px = price0 * np.exp(rs.normal(0, 2e-3, nt))
        off = rs.random_sample(nt) < 0.4
        px = np.where(off, np.round(px, 4), np.round(px / tick) * tick)
        size = np.where(rs.random_sample(nt) < 0.4, rs.randint(1, 100, nt),
                        100 * rs.randint(1, 30, nt)).astype(float)
        bad = rs.random_sample(nt)
        px[bad < 0.02] = 0.0
        size[(bad >= 0.02) & (bad < 0.04)] = 0.0
        trf = pd.array(np.where(off, rs.choice([201, 202, 0], nt), 0), dtype="Int64")
        trf[~off] = pd.NA
        tparts.append(pd.DataFrame({
            "ticker": ticker, "conditions": rs.choice(CONDITIONS, nt),
            "correction": rs.choice(CORRECTIONS, nt), "exchange": np.where(off, 4, 11),
            "id": np.arange(nt), "participant_timestamp": tt - 500, "price": px,
            "sequence_number": seq[nq:], "sip_timestamp": tt, "size": size, "tape": 1,
            "trf_id": trf, "trf_timestamp": pd.array([pd.NA] * nt, dtype="Int64")}))
    trades = pd.concat(tparts, ignore_index=True)
    quotes = pd.concat(qparts, ignore_index=True)
    trades = trades.sample(frac=1, random_state=rs).reset_index(drop=True)
    quotes = quotes.sample(frac=1, random_state=rs).reset_index(drop=True)
    return trades, quotes, first


def tick_rows(t, q, day, through):
    return ticks.tick_rows(t, q, day, through, groups=GROUPS)


def same(got, want):
    """Exact equality of two row sets, value for value. The frame types are not compared: the
    engine's frame makes an integer column nullable (Int64) when ANY row of the whole table is
    missing there, so a subset of rows can carry int64 in one table and Int64 in another."""
    got, want = got.reset_index(drop=True), want.reset_index(drop=True)
    assert list(got.columns) == list(want.columns)
    assert len(got) == len(want)
    if len(got):
        pd.testing.assert_frame_equal(got, want, check_exact=True, check_dtype=False)


def files(folder, trades, quotes):
    t, q = folder / "t.csv", folder / "q.csv"
    ticks.write_flat_file(trades, t, synthetic.TRADE_FILE_COLUMNS)
    ticks.write_flat_file(quotes, q, synthetic.QUOTE_FILE_COLUMNS)
    return t, q


def in_minute(frame, minute):
    m = minute_of_stamps(frame.sip_timestamp)
    return frame[m == minute].reset_index(drop=True)


def minute_of_stamps(stamps):
    local = pd.to_datetime(stamps, unit="ns", utc=True).dt.tz_convert(synthetic.NY)
    return local.dt.hour * 60 + local.dt.minute


def end_of(minute):
    return synthetic.stamp(DAY, f"{minute // 60:02d}:{minute % 60:02d}:00") + NS_PER_MINUTE


def walk(folder, trades, quotes, minutes):
    """Minute by minute: the tail's rows (plain and with the marker) and the converter's rows
    on the whole day so far, from the first minute of `minutes` on."""
    carry = (trades.iloc[:0], quotes.iloc[:0])
    for minute in minutes:
        tm, qm = in_minute(trades, minute), in_minute(quotes, minute)
        plain = ticks.minute_rows(carry, tm, qm, DAY, minute, groups=GROUPS)
        marked = ticks.minute_rows(carry, tm, qm, DAY, minute, marker=True, groups=GROUPS)
        stamp = end_of(minute)
        prefix = tick_rows(*files(folder, synthetic.before(trades, stamp),
                                        synthetic.before(quotes, stamp)), DAY, minute)
        yield minute, plain, marked, prefix[prefix.m == minute].reset_index(drop=True)
        carry = ticks.tail_carry(pd.concat([carry[0], tm]), pd.concat([carry[1], qm]))


@pytest.mark.parametrize("seed", range(6))
def test_the_tail_equals_the_whole_prefix_every_minute(tmp_path, seed):
    trades, quotes, first = random_day(seed)
    whole = tick_rows(*files(tmp_path, trades, quotes), DAY, first + SPAN)
    minutes = range(first - 1, first + SPAN + 1)  # one empty minute on each side
    rows = 0
    for minute, plain, marked, want in walk(tmp_path, trades, quotes, minutes):
        rows += len(want)
        same(plain, want)
        # marker: the training's whole-day row for every stock that quotes again later today;
        # a stock that never does keeps its last update at weight 0, as without the marker
        later = set(quotes.ticker[quotes.sip_timestamp >= end_of(minute)])
        day_rows = whole[whole.m == minute].reset_index(drop=True)
        again = marked.sym.isin(later)
        same(marked[again], day_rows[day_rows.sym.isin(later)])
        same(plain[~plain.sym.isin(later)], day_rows[~day_rows.sym.isin(later)])
        steady = [c for c in plain.columns if c not in TIME_WEIGHTED]
        same(marked[steady], plain[steady])
    assert rows > 0


@pytest.fixture(scope="module")
def big_day():
    return synthetic.day_events(DAY, 12, 3000, 4000, seed=11)


@pytest.mark.parametrize("hhmm", ["09:29", "12:00", "16:00"])
def test_the_tail_equals_the_whole_prefix_on_a_full_day(tmp_path, big_day, hhmm):
    """A whole day of the common synthetic layout: the carry is built from every event before
    the minute, and stays small (one quote per stock, one trade per stock and key)."""
    trades, quotes = big_day
    first = int(hhmm[:2]) * 60 + int(hhmm[3:])
    start = end_of(first - 1)
    carry = ticks.tail_carry(synthetic.before(trades, start), synthetic.before(quotes, start))
    assert len(carry[1]) == quotes.ticker.nunique()
    # bounded by the keys (stock, conditions, correction; three yes / no flags), not by time
    keys = trades[["ticker", "conditions", "correction"]].drop_duplicates()
    assert len(carry[0]) <= 8 * len(keys) and len(carry[0]) < len(trades) // 50
    seen = 0
    for minute in (first, first + 1):
        tm, qm = in_minute(trades, minute), in_minute(quotes, minute)
        stamp = end_of(minute)
        prefix = tick_rows(*files(tmp_path, synthetic.before(trades, stamp),
                                        synthetic.before(quotes, stamp)), DAY, minute)
        want = prefix[prefix.m == minute].reset_index(drop=True)
        same(ticks.minute_rows(carry, tm, qm, DAY, minute, groups=GROUPS), want)
        seen += len(want)
        carry = ticks.tail_carry(pd.concat([carry[0], tm]), pd.concat([carry[1], qm]))
    assert seen > 0


def test_without_the_carry_the_rows_differ(tmp_path):
    """The carry is needed: the minute's events alone lose qgap / qofi / tgap / tvar / the
    quote-signed trade columns of the minute's first events."""
    trades, quotes, first = random_day(4)
    differs = 0
    for minute in range(first + 1, first + SPAN):
        tm, qm = in_minute(trades, minute), in_minute(quotes, minute)
        start = end_of(minute - 1)
        carry = ticks.tail_carry(synthetic.before(trades, start), synthetic.before(quotes, start))
        empty = (trades.iloc[:0], quotes.iloc[:0])
        alone = ticks.minute_rows(empty, tm, qm, DAY, minute, groups=GROUPS)
        full = ticks.minute_rows(carry, tm, qm, DAY, minute, groups=GROUPS)
        differs += not alone.equals(full)
    assert differs > 0


def test_an_event_outside_its_part_is_refused():
    trades, quotes, first = random_day(1)
    tm, qm = in_minute(trades, first + 2), in_minute(quotes, first + 2)
    empty = (trades.iloc[:0], quotes.iloc[:0])
    with pytest.raises(ModelRefused) as exc:
        ticks.minute_rows(empty, tm, qm, DAY, first + 3, groups=GROUPS)
    assert exc.value.cause == "tick_outside_minute"
    with pytest.raises(ModelRefused) as exc:
        ticks.minute_rows((tm, empty[1]), tm, qm, DAY, first + 2, groups=GROUPS)
    assert exc.value.cause == "tick_outside_minute"
    with pytest.raises(ModelRefused) as exc:
        ticks.minute_rows(empty, tm, qm, "2026-11-17", first + 2, groups=GROUPS)
    assert exc.value.cause == "tick_outside_minute"


# Reconnect, restart and a late correction (codex-108 point 2): in v11 tgap reads the stock's
# previous kept trade and tvar / tchg the previous trade of the same last-price class; both may
# lie minutes back, across a stream gap, a restart or a correction.
GAP = (3, 5)  # minutes after the first of the window the stream misses (reconnect)
RESTART = 8  # minute after the first at which the bot restarts from its stored carry
LATE = 6  # minute after the first in which the correction records arrive
STOCK = "G"  # the crafted stock whose previous kept / eligible trade lies in the gap


def gap_stock(first: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """G: a quote at the first minute; eligible trades at first+1 and in the gap (first+4);
    then nothing until first+7, where a last-price-ineligible print (condition 10) and an
    eligible one follow: their tgap and the eligible one's previous price lie in the gap."""
    def at(minute, second):
        return end_of(first + minute - 1) + second * 10**9

    ts = [at(1, 10), at(4, 20), at(7, 5), at(7, 30)]
    trades = pd.DataFrame({
        "ticker": STOCK, "conditions": ["", "", "10", ""], "correction": 0, "exchange": 11,
        "id": np.arange(4), "participant_timestamp": np.array(ts) - 500,
        "price": [10.0, 10.5, 10.25, 10.75], "size": 100.0,
        "sequence_number": 900001 + np.arange(4), "sip_timestamp": ts, "tape": 1,
        "trf_id": pd.array([pd.NA] * 4, dtype="Int64"),
        "trf_timestamp": pd.array([pd.NA] * 4, dtype="Int64")})
    quotes = pd.DataFrame({
        "ticker": [STOCK], "ask_exchange": 11, "ask_price": 10.6, "ask_size": 3.0,
        "bid_exchange": 11, "bid_price": 10.4, "bid_size": 3.0, "conditions": "",
        "indicators": "", "participant_timestamp": [at(0, 1) - 500],
        "sequence_number": [900000], "sip_timestamp": [at(0, 1)], "tape": 1,
        "trf_timestamp": pd.array([pd.NA], dtype="Int64")})
    return trades, quotes


def with_corrections(trades: pd.DataFrame, first: int,
                     rs: np.random.RandomState) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(stream trades, final trades, the late record inside a delivered minute). The stream saw
    a few earlier prints with correction 0; the final inputs mark them 1 / 7 / 8 (corrected,
    error, cancelled later) and add the records written later about them (10 / 11 / 12), at
    minute first+LATE, and one more such record stamped inside the delivered minute first+2."""
    early = trades[(trades.sip_timestamp < end_of(first + LATE - 2)) & (trades.ticker != STOCK)]
    picked = early.sample(n=min(4, len(early)), random_state=rs)
    stream = trades.copy()
    stream.loc[picked.index, "correction"] = 0
    final = stream.copy()
    final.loc[picked.index, "correction"] = rs.choice([1, 7, 8], len(picked))
    records = picked.copy()
    records["correction"] = rs.choice([10, 11, 12], len(picked))
    records.loc[records.correction == 12, "price"] = np.round(records.price * 1.01, 4)
    stamp = end_of(first + LATE - 1) + rs.randint(0, 59, len(picked)) * 10**9
    records["sip_timestamp"] = stamp
    records["participant_timestamp"] = stamp - 500
    records["sequence_number"] = 800000 + np.arange(len(picked))
    inside = picked.iloc[:1].copy()
    inside["correction"] = 10
    inside["sip_timestamp"] = end_of(first + 1) + 7 * 10**9
    inside["sequence_number"] = 850000
    stream = pd.concat([stream, records], ignore_index=True)
    final = pd.concat([final, records, inside], ignore_index=True)
    return stream, final, inside


def run_stream(folder, trades, quotes, minutes, first):
    """The incremental path as a live bot meets it: minute by minute, except that the stream
    misses the GAP minutes and backfills them at the reconnect (their rows late, the carry
    advanced over all of them at once), and that the bot restarts at first+RESTART from the
    carry it stored on disk. Returns {minute: rows}."""
    carry = (trades.iloc[:0], quotes.iloc[:0])
    path = folder / "carry.json"
    out = {}
    gap = range(first + GAP[0], first + GAP[1] + 1)
    for minute in minutes:
        if minute in gap:
            continue
        if minute == gap[-1] + 1:  # reconnect: backfill the missed minutes, then go on
            back = carry
            for late in gap:
                out[late] = ticks.minute_rows(back, in_minute(trades, late),
                                              in_minute(quotes, late), DAY, late, groups=GROUPS)
                back = ticks.tail_carry(pd.concat([back[0], in_minute(trades, late)]),
                                        pd.concat([back[1], in_minute(quotes, late)]))
            missed = (trades.sip_timestamp >= end_of(gap[0] - 1)) & (
                trades.sip_timestamp < end_of(gap[-1]))
            missed_q = (quotes.sip_timestamp >= end_of(gap[0] - 1)) & (
                quotes.sip_timestamp < end_of(gap[-1]))
            carry = ticks.tail_carry(pd.concat([carry[0], trades[missed]]),
                                     pd.concat([carry[1], quotes[missed_q]]))
        if minute == first + RESTART:  # restart: the in-memory carry is gone
            ticks.save_carry(carry, path, DAY, minute)
            del carry
            carry = ticks.load_carry(path, DAY, minute)
        tm, qm = in_minute(trades, minute), in_minute(quotes, minute)
        out[minute] = ticks.minute_rows(carry, tm, qm, DAY, minute, groups=GROUPS)
        carry = ticks.tail_carry(pd.concat([carry[0], tm]), pd.concat([carry[1], qm]))
    return out


def prefix_rows(folder, trades, quotes, minute):
    stamp = end_of(minute)
    rows = tick_rows(*files(folder, synthetic.before(trades, stamp),
                            synthetic.before(quotes, stamp)), DAY, minute)
    return rows[rows.m == minute].reset_index(drop=True)


@pytest.mark.parametrize("seed", range(len(FIRST_MINUTES)))  # each window once
def test_reconnect_restart_and_late_corrections_equal_the_full_prefix(tmp_path, seed):
    """Every closed minute of the stream's incremental rows equals, exactly, the full-prefix
    conversion of the final inputs (late correction records and the later correction codes of
    the originals included): v11 keeps a print as printed and drops the records about it."""
    trades, quotes, first = random_day(seed)
    gt, gq = gap_stock(first)
    trades = pd.concat([trades, gt], ignore_index=True)
    quotes = pd.concat([quotes, gq], ignore_index=True)
    stream, final, inside = with_corrections(trades, first, np.random.RandomState(seed))
    minutes = range(first - 1, first + SPAN + 1)
    got = run_stream(tmp_path, stream, quotes, minutes, first)
    assert sorted(got) == list(minutes)
    for minute in minutes:
        same(got[minute], prefix_rows(tmp_path, final, quotes, minute))
    # G's first minute after the gap reads the gap: tgap from the kept trade at first+4 (165 s
    # before the condition-10 print), the eligible print's previous price 10.5 from the gap
    row = got[first + 7].set_index("sym").loc[STOCK]
    assert row.tgap == pytest.approx(165.0, abs=1e-9) and row.tchg == 1
    assert row.tvar == pytest.approx(np.log(10.75 / 10.5) ** 2, rel=1e-12)
    # the record stamped inside the delivered minute first+2 cannot be folded in live ...
    late = pd.concat([in_minute(stream, first + LATE), inside], ignore_index=True)
    carry = ticks.tail_carry(synthetic.before(stream, end_of(first + LATE - 1)),
                             synthetic.before(quotes, end_of(first + LATE - 1)))
    with pytest.raises(ModelRefused) as exc:
        ticks.minute_rows(carry, late, in_minute(quotes, first + LATE), DAY, first + LATE,
                          groups=GROUPS)
    assert exc.value.cause == "tick_outside_minute"
    # ... and the delivered minute recomputed with it is unchanged: nothing to report
    ticks.restated(got[first + 2], prefix_rows(tmp_path, final, quotes, first + 2))


def test_a_carry_that_skips_the_gap_gives_other_rows(tmp_path):
    """Without the backfilled minutes G's tgap, tvar and tchg after the gap differ: the gap's
    events reach the next minutes only through the carry."""
    trades, quotes, first = random_day(0)
    gt, gq = gap_stock(first)
    trades = pd.concat([trades, gt], ignore_index=True)
    quotes = pd.concat([quotes, gq], ignore_index=True)
    gap = (trades.sip_timestamp >= end_of(first + GAP[0] - 1)) & (
        trades.sip_timestamp < end_of(first + GAP[1]))
    minute = first + 7
    start = end_of(minute - 1)
    tm, qm = in_minute(trades, minute), in_minute(quotes, minute)
    full = ticks.tail_carry(synthetic.before(trades, start), synthetic.before(quotes, start))
    skipped = ticks.tail_carry(synthetic.before(trades[~gap], start),
                               synthetic.before(quotes, start))
    a = ticks.minute_rows(full, tm, qm, DAY, minute, groups=GROUPS).set_index("sym").loc[STOCK]
    b = ticks.minute_rows(skipped, tm, qm, DAY, minute, groups=GROUPS).set_index("sym").loc[STOCK]
    assert a.tgap != b.tgap and a.tvar != b.tvar
    same(ticks.minute_rows(full, tm, qm, DAY, minute, groups=GROUPS),
         prefix_rows(tmp_path, trades, quotes, minute))


def test_a_late_print_in_a_delivered_minute_is_reported_not_rewritten(tmp_path):
    """A print the stream missed, stamped in a minute already delivered, changes that minute:
    the check reports the stock and the columns; the delivered rows stay as they were, and a
    carry rebuilt from the corrected inputs gives the later minutes of the full prefix."""
    trades, quotes, first = random_day(2)
    minute = first + 2
    stock = trades.ticker[trades.price > 0].iloc[0]
    missed = trades[(trades.ticker == stock) & (trades.price > 0)].iloc[:1].copy()
    missed["correction"] = 0
    missed["conditions"] = ""
    missed["price"] = np.round(missed.price * 1.05, 2)
    missed["size"] = 700.0
    missed["sip_timestamp"] = end_of(minute - 1) + 31 * 10**9
    missed["sequence_number"] = 870000
    final = pd.concat([trades, missed], ignore_index=True)
    delivered = prefix_rows(tmp_path, trades, quotes, minute)
    kept = delivered.copy()
    with pytest.raises(ModelRefused) as exc:
        ticks.restated(delivered, prefix_rows(tmp_path, final, quotes, minute))
    assert exc.value.cause == "tick_minute_restated"
    assert f"{stock} {minute}" in exc.value.detail
    pd.testing.assert_frame_equal(delivered, kept, check_exact=True)
    later = first + LATE
    start = end_of(later - 1)
    carry = ticks.tail_carry(synthetic.before(final, start), synthetic.before(quotes, start))
    for m in (later, later + 1):
        tm, qm = in_minute(final, m), in_minute(quotes, m)
        same(ticks.minute_rows(carry, tm, qm, DAY, m, groups=GROUPS),
             prefix_rows(tmp_path, final, quotes, m))
        carry = ticks.tail_carry(pd.concat([carry[0], tm]), pd.concat([carry[1], qm]))


def test_the_stored_carry_restores_the_same_rows(tmp_path):
    """The carry read back from disk is the stored one, value for value in the converter's
    columns (decimals exactly), and gives the same rows."""
    trades, quotes = synthetic.day_events(DAY, 6, 1500, 2500, seed=5)
    minute = 12 * 60
    start = end_of(minute - 1)
    carry = ticks.tail_carry(synthetic.before(trades, start), synthetic.before(quotes, start))
    path = tmp_path / "carry.json"
    ticks.save_carry(carry, path, DAY, minute)
    back = ticks.load_carry(path, DAY, minute)
    for got, want, columns in ((back[0], carry[0], ticks.trade_columns()),
                               (back[1], carry[1], ticks.quote_columns())):
        assert list(got.columns) == columns
        pd.testing.assert_frame_equal(got, want[columns].reset_index(drop=True),
                                      check_exact=True, check_dtype=False)
    tm, qm = in_minute(trades, minute), in_minute(quotes, minute)
    same(ticks.minute_rows(back, tm, qm, DAY, minute, groups=GROUPS),
         ticks.minute_rows(carry, tm, qm, DAY, minute, groups=GROUPS))
    assert not (tmp_path / "carry.json.tmp").exists()


def test_a_stored_carry_of_another_minute_or_a_broken_one_is_refused(tmp_path):
    trades, quotes, first = random_day(3)
    start = end_of(first + 2)
    carry = ticks.tail_carry(synthetic.before(trades, start), synthetic.before(quotes, start))
    path = tmp_path / "carry.json"
    with pytest.raises(ModelRefused) as exc:
        ticks.load_carry(path, DAY, first + 3)
    assert exc.value.cause == "tick_carry_missing"
    ticks.save_carry(carry, path, DAY, first + 3)
    for day, minute in ((DAY, first + 4), ("2026-11-17", first + 3)):
        with pytest.raises(ModelRefused) as exc:
            ticks.load_carry(path, day, minute)
        assert exc.value.cause == "tick_carry_stale"
    path.write_text(path.read_text("utf-8")[:-40], "utf-8")
    with pytest.raises(ModelRefused) as exc:
        ticks.load_carry(path, DAY, first + 3)
    assert exc.value.cause == "tick_carry_unreadable"
