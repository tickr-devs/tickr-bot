"""Live features end to end: a synthetic day through a stand-in Massive stream (trades and quotes
over the real TickStream, minute bars as AM messages into DayBars) and a stand-in REST backfill
into LiveFeatures, compared with the training's own whole-day run on the same events (the
converter's `local` mode, `wm2_l1f.day2` on the bar files, `l1feat.assemble`), as in the #818
parity harness; then every refusal. No network, no credentials."""

from __future__ import annotations

import dataclasses
import json
import queue
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import model_bars as bars_maker
import model_inputs as inputs
import model_shared
import model_ticks as tick_maker
import pandas as pd
import pytest
import test_parity as tp

from tickr import config
from tickr.model import features, live, massive, parity
from tickr.model import ticks as feed
from tickr.model.errors import ModelRefused
from tickr.model.features import full, ticks

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
DAY = tp.DAY
MS = 10**6
# The live parity minutes are checked on three days, each warmed up once: 09:44 and 09:45 in
# test_a_close_uses_only_what_had_arrived_by_its_cutoff, 12:00 (QUIET's stale quote) in
# test_the_marker_stays_inside_the_weighting, 15:30 here; pre-market (04:30) only by the replay
# (test_parity, test_parity_fast), for time: its cohort needs 21 closes
MINUTES = ["15:30"]
LATER_RECORDS = (10, 11, 12)  # correction codes the stream is not documented to send
# Every cohort symbol has ticks here: a symbol enters the conversion only once its snapshot
# proves a stream row of each kind after its subscription (TickSnapshot.refusal), so the day is
# denser than test_parity's (events per symbol-day).
TRADES, QUOTES = 1500, 2500


def _names(frame):
    """day_events' names as the bar tickers (S0000 -> T000 ...), its last symbol NO_BARS."""
    n = len(bars_maker.TICKERS) - 2
    names = {f"S{i:04d}": f"T{i:03d}" for i in range(n)}
    names[f"S{n:04d}"] = tp.NO_BARS
    return frame.assign(ticker=frame.ticker.map(lambda t: names.get(t, t)))


def with_model(**changes) -> config.Config:
    return dataclasses.replace(CFG, model=dataclasses.replace(CFG.model, **changes))


@pytest.fixture(autouse=True)
def one_thread_and_a_key(monkeypatch):
    monkeypatch.setenv("TICK_THREADS", "1")
    monkeypatch.setenv(CFG.massive.api_key_variable, "api-canary-2")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The parity day of test_parity with events a live reader can hold exactly: stamps on the
    millisecond (the stream's unit) and no later cancel / error / correction records."""
    w = model_shared.once(tmp_path_factory, "live-world", _world)
    return SimpleNamespace(**vars(w), context=full.live_context(inputs.market_day(DAY)))


def _world(root):
    labels = inputs.research().labels
    cal = labels.calendar()
    i = cal.index(DAY)
    folder = root / "bars"
    folder.mkdir()
    closes: dict[str, float] = {}
    for k, d in enumerate(cal[i - labels.HIST:i + 1]):
        bars_maker.day_bars(d, k, closes, d == DAY).to_csv(  # level 1: same text, faster setup
            folder / f"{d}.csv.gz", index=False, compression={"method": "gzip", "compresslevel": 1})
    trades, quotes = tick_maker.day_events(DAY, len(bars_maker.TICKERS) + 1, TRADES, QUOTES)
    trades, quotes = _names(trades), _names(quotes)
    quiet = ((quotes.ticker == tp.QUIET)
             & (quotes.sip_timestamp >= tick_maker.stamp(DAY, "11:55:00"))
             & (quotes.sip_timestamp < tick_maker.stamp(DAY, "12:01:00")))
    quotes = quotes[~quiet]
    trades = trades[~trades.correction.isin(LATER_RECORDS)]
    trades, quotes = (f.assign(sip_timestamp=f.sip_timestamp // MS * MS).reset_index(drop=True)
                      for f in (trades, quotes))
    tf, qf = root / "trades.csv.gz", root / "quotes.csv.gz"
    ticks.write_flat_file(trades, tf, tick_maker.TRADE_FILE_COLUMNS)
    ticks.write_flat_file(quotes, qf, tick_maker.QUOTE_FILE_COLUMNS)
    whole = root / "tk-whole.parquet"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TICK_THREADS", "1")
        subprocess.run([sys.executable, str(ticks.PINNED_DIR / ticks.CONVERTER_FILE), "local",
                        str(tf), str(qf), str(whole)], check=True, capture_output=True)
    source = root / "source"
    source.mkdir()
    given = parity.ReplayInputs(
        bars=folder, types=None, trades=tf, quotes=qf,
        reference=inputs.reference(source / "reference", DAY, tp.SYMBOLS, tp.NEWS_MINUTES),
        benzinga=inputs.benzinga(source / "benzinga", DAY, tp.SYMBOLS, tp.NEWS_MINUTES,
                                 "2023-01-02"),
        fl_facts=inputs.fl_facts(source / "fl-facts.csv", DAY, tp.SYMBOLS), table=root / "x")
    table = parity.reference_table(tp._training_table(root, given, whole), DAY)
    # the prepared folders as the capture holds them: every record with its receipt, a news
    # record of the day received one second after it was published
    (root / "prepared").mkdir()
    inputs.capture(full.FullInputs(whole, given.reference, given.benzinga, given.fl_facts), DAY,
                   root / "prepared" / DAY, late=inputs.published_receipts(DAY))
    need = parity.day_files(DAY, folder)
    frames = [(d, massive.read_day_file((folder / f"{d}.csv.gz").read_bytes(), d)) for d in need]
    state = features.history(frames[:-1], parity._splits(given.reference), DAY, None)
    return SimpleNamespace(trades=trades, quotes=quotes, whole=whole, table=table, state=state,
                           bars=frames[-1][1], prepared=root / "prepared")


PREPARED = inputs.CAPTURED


def _minute(hm):
    return parity.table_minutes([hm])[0]


def _label(m):
    """Table minute `m` (from 04:00) as HH:MM New York."""
    t = m + features.DAY_START_MINUTE
    return f"{t // 60:02d}:{t % 60:02d}"


# --- stand-ins ----------------------------------------------------------------------------


class QueueSocket:
    """The stream's socket: the test puts a batch and waits until the stream has read it (the
    next recv marks the previous batch done)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.sent: list = []
        self._open = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        if self._open:
            self.q.task_done()
            self._open = False
        try:
            item = self.q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        self._open = True
        return item

    def push(self, batch):
        self.q.put(json.dumps(batch))
        self.q.join()


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _codes(text):
    return [int(c) for c in str(text).split(",")] if isinstance(text, str) and text else None


def _value(v):
    if v is None or v is pd.NA or (isinstance(v, float) and v != v):
        return None
    return v.item() if hasattr(v, "item") else v


class Rest:
    """/v3/trades/{t} and /v3/quotes/{t} served from the day's events (flat-file names), one page;
    a symbol in `fail` answers 500 once."""

    def __init__(self, w, fail=()):
        self.w, self.fail, self.requests = w, set(fail), []

    def __call__(self, request, timeout):
        self.requests.append(request.full_url)
        parts = urllib.parse.urlsplit(request.full_url)
        kind, sym = parts.path.split("/")[2:4]
        if sym in self.fail:
            self.fail.discard(sym)
            raise urllib.error.HTTPError(request.full_url, 500, "x", {}, None)
        q = urllib.parse.parse_qs(parts.query)
        lo, hi = int(q["timestamp.gte"][0]), int(q["timestamp.lt"][0])
        f = self.w.trades if kind == "trades" else self.w.quotes
        f = f[(f.ticker == sym) & (f.sip_timestamp >= lo) & (f.sip_timestamp < hi)]
        cols = [c for c in feed.columns(kind)[1:-2]]
        results = []
        for r in f.to_dict("records"):
            row = {c: _value(r[c]) for c in cols}
            for c in feed.CODE_COLUMNS:
                if c in row:
                    row[c] = _codes(row[c])
            results.append({k: v for k, v in row.items() if v is not None})
        return Response(json.dumps({"status": "OK", "results": results}).encode())


def _message(kind, r):
    if kind == "trades":
        m = {"ev": "T", "sym": r["ticker"], "x": int(r["exchange"]), "p": r["price"],
             "s": int(r["size"]), "t": int(r["sip_timestamp"]) // MS,
             "q": int(r["sequence_number"]), "c": _codes(r["conditions"]),
             "trfi": _value(r["trf_id"]), "pt": int(r["participant_timestamp"]) // MS}
    else:
        m = {"ev": "Q", "sym": r["ticker"], "ap": r["ask_price"], "as": r["ask_size"],
             "t": int(r["sip_timestamp"]) // MS, "q": int(r["sequence_number"]),
             "c": (_codes(r["conditions"]) or [None])[0], "i": _codes(r["indicators"])}
        if r["bid_price"] or r["bid_size"]:  # an empty side is absent on the stream
            m.update(bp=r["bid_price"], bs=r["bid_size"])
    return {k: v for k, v in m.items() if v is not None}


class Harness:
    """One live day: the stream thread, the stores, the backfill and LiveFeatures, with a wall
    clock the test moves to just after each minute's end."""

    def __init__(self, w, tmp, cfg=CFG, fail=(), inputs_root=None):
        self.w = w
        self.now = feed.day_bounds_ns(DAY)[0]
        clock = lambda: self.now  # noqa: E731
        self.bars = massive.DayBars(DAY)
        self.store = feed.TickStore(DAY)
        self.sock = QueueSocket()
        self.stream = feed.TickStream(cfg, self.store, lambda url, max_size: self.sock, clock)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.stream.session, args=(self.stop,),
                                       daemon=True)
        self.thread.start()
        self.sock.push([{"ev": "status", "status": "auth_success"}])
        self.rest = Rest(w, fail)
        back = feed.TickBackfill(cfg, self.store, self.rest, clock=clock)
        self.live = live.LiveFeatures(cfg, w.context, w.state, None, self.bars, self.store,
                                      self.stream, back,
                                      live.FolderInputs(inputs_root or w.prepared, PREPARED),
                                      tmp / "ticks", clock)
        self.fed = self.now

    def feed_to(self, minute):
        """Bars and stream events up to the end of `minute`, as the server would send them:
        events of a subscribed symbol stamped after its subscription."""
        end = self.live._end(minute)
        b = self.w.bars
        for r in b[(b.window_start >= self.fed) & (b.window_start < end)].to_dict("records"):
            self.bars.add({"ev": "AM", "sym": r["ticker"], "v": r["volume"], "o": r["open"],
                           "c": r["close"], "h": r["high"], "l": r["low"],
                           "s": r["window_start"] // MS}, 0.0)
        batch = []
        for kind, f in (("trades", self.w.trades), ("quotes", self.w.quotes)):
            f = f[(f.sip_timestamp >= self.fed) & (f.sip_timestamp < end)]
            for r in f.to_dict("records"):
                info = self.live.symbols.get(r["ticker"])
                if r["ticker"] in self.stream.symbols and r["sip_timestamp"] >= info.since:
                    batch.append(_message(kind, r))
        self.fed = end
        self.now = end + MS  # received just after the minute's end, before its close
        if batch and self.thread.is_alive():  # a closed connection receives nothing
            self.sock.push(batch)

    def close(self, minute, wanted=()):
        self.feed_to(minute)
        return self.live.close(minute, wanted)

    def warm(self, first, last):
        """Close every minute from `first` to `last` (HH:MM): a cohort symbol is subscribed at
        the first close it has a row, backfilled at the next, and enters at the one after."""
        return [self.close(m) for m in range(_minute(first), _minute(last) + 1)]

    def entered(self):
        return {s for s, i in self.live.symbols.items() if i.entered is not None}

    def shut(self):
        if not self.thread.is_alive():
            return
        self.stop.set()
        self.sock.q.put("[]")
        self.thread.join()


@pytest.fixture
def harness(world, tmp_path):
    made = []

    def make(**kw):
        h = Harness(world, tmp_path / f"h{len(made)}", **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.shut()


# --- parity -------------------------------------------------------------------------------


# each parity minute closes after the closes that let its cohort enter (Harness.warm): on this
# day the cohort's symbols join until 09:43 and 11:50 when the closes start at 09:35 and 11:40.
# Each start here is the latest measured on this day with no refused row in the checked minutes
# (15:30: 15:22, not 15:23; 11:58 to 12:01: 11:48, not 11:49): each close runs the pinned
# converter, so a warm-up close costs most of a second
OPEN_WARM = ("09:35", "09:44")  # a fresh day's closes before a 09:45 with every symbol entered
NOON_START = "11:48"  # the first close of a fresh day whose cohort is all entered from 11:58
LATE_START = "15:22"  # the first close of a fresh day whose cohort is all entered at 15:30
WARM = {"15:30": LATE_START}


def _equals_training(world, got, m):
    """A close of table minute `m` (with `wanted=[tp.NO_BARS]`) gives the training's rows."""
    hm = _label(m)
    want = world.table[world.table.m == m]
    assert got.refused == {tp.NO_BARS: live.Refusal("not_in_cohort")}, hm
    r = parity.compare(got.rows, want, full.full_groups(), None)
    assert r["rows_compared"] == len(want) == len(got.rows) > 0, hm
    assert r["columns"] == {} and r["bar_groups_exact"], hm
    assert list(got.rows.columns) == ["sym", *full.full_features()]


def test_a_live_day_equals_the_training_run_with_the_marker(world, harness):
    """15:30 on a day whose closes start at LATE_START; 09:44 and 09:45 are checked the same way
    in test_a_close_uses_only_what_had_arrived_by_its_cutoff, 12:00 in
    test_the_marker_stays_inside_the_weighting (one warm-up per day)."""
    h = harness(cfg=with_model(live_marker_quote=True))
    closed = []
    for hm in MINUTES:
        m = parity.table_minutes([hm])[0]
        if WARM[hm] != hm:
            h.warm(WARM[hm], _label(m - 1))
        closed.extend(range(_minute(WARM[hm]), m + 1))
        _equals_training(world, h.close(m, wanted=[tp.NO_BARS]), m)
    # the parts are the training's whole-day tick file up to the last close, row for row, each
    # with the receipt of the close that wrote it (the instant that close took the clock)
    import duckdb

    con = duckdb.connect()
    parts = con.execute(f"SELECT * FROM read_parquet('{h.live.tick_file}') ORDER BY sym, m").df()
    whole = con.execute(f"SELECT * FROM read_parquet('{world.whole}') ORDER BY sym, m").df()
    con.close()
    last = parity.table_minutes([MINUTES[-1]])[0] + features.DAY_START_MINUTE
    whole = whole[whole.sym.isin(h.entered()) & (whole.m <= last)]
    closes = {h.live._end(m) + MS for m in closed}
    assert set(parts.pop(full.RECEIPT_COLUMN)) <= closes  # a close with no tick row: no part row
    pd.testing.assert_frame_equal(parts, whole.reset_index(drop=True), check_exact=True)
    assert tp.NO_BARS not in h.stream.symbols
    # every backfill is a symbol's day from New York midnight (TickBackfill.initial), of an
    # entered symbol, and sent only by a close whose snapshot lacked it
    start = f"timestamp.gte={feed.day_bounds_ns(DAY)[0]}"
    assert all(start in u for u in h.rest.requests)
    asked = {urllib.parse.urlsplit(u).path.split("/")[3] for u in h.rest.requests}
    assert asked == h.entered() == set(h.live.symbols)


# the converter's columns whose value weighs a quote by how long it stood (v11's `d`)
WEIGHTED = ("qspr", "qbs", "qas", "qimb", "qbad", "qbd", "qad")


def _parts(h):
    import duckdb

    con = duckdb.connect()
    try:
        return con.execute(f"SELECT * FROM read_parquet('{h.live.tick_file}') "
                           "ORDER BY sym, m").df()
    finally:
        con.close()


def test_the_marker_stays_inside_the_weighting(world, harness, monkeypatch):
    """codex-108 point 1: the marker is added only for a symbol with a real quote among the
    events a close converts (on a one-minute close: in that minute), at the start of the next
    minute, and stays out of the raw capture, the carry, every count and the next minute.
    Without it (the default, off with the model) only the tick groups differ from training."""
    assert CFG.model.live_marker_quote is False
    plain, mark = harness(), harness(cfg=with_model(live_marker_quote=True))
    made = []
    real = ticks.marker_quotes

    def spy(quotes, at_ns):
        made.append((set(quotes.ticker), at_ns))
        return real(quotes, at_ns)

    monkeypatch.setattr(ticks, "marker_quotes", spy)
    q = world.quotes
    served = {}
    for m in range(_minute(NOON_START), _minute("12:01") + 1):
        hm = _label(m)
        before = mark.entered()
        got = {"plain": plain.close(m), "marker": mark.close(m)}
        entering = mark.entered() - before
        assert plain.entered() == mark.entered(), hm
        start, end = mark.live._end(m - 1), mark.live._end(m)
        if made:
            symbols, at = made.pop()
            assert not made and at == end, hm  # once per close, at the start of the next minute
        else:  # no quote among the converted events: no marker
            symbols = set()
        quoted = set(q[q.ticker.isin(before) & (q.sip_timestamp >= start)
                       & (q.sip_timestamp < end)].ticker)
        # an entered symbol: a real quote in the minute; an entering one (its day so far is
        # converted at this close): a real quote today
        assert symbols == quoted | (entering & set(q[q.sip_timestamp < end].ticker)), hm
        assert symbols - entering == quoted, hm
        # raw capture and carry: identical with and without the marker
        assert plain.store.versions == mark.store.versions, hm
        if mark.live.carry is not None:
            for a, b in zip(plain.live.carry, mark.live.carry, strict=True):
                pd.testing.assert_frame_equal(a, b, check_exact=True)
            assert not (mark.live.carry[1].sip_timestamp >= end).any(), hm
        if hm < "11:58":
            continue  # the cohort enters (Harness.warm)
        assert not got["plain"].refused and not got["marker"].refused, hm
        served[hm] = got
        if hm == "12:00":  # entered, no quote in the minute: no marker (11:54 keeps weight 0)
            assert tp.QUIET in before and tp.QUIET not in symbols
            r = parity.compare(got["plain"].rows, world.table[world.table.m == m],
                               full.full_groups(), None)
            assert r["bar_groups_exact"] and r["columns"] and tp.QUIET in r["symbols"]
            assert {c["group"] for c in r["columns"].values()} <= tp.TICK_GROUPS
            # with the marker: the training's rows, exactly
            want = world.table[world.table.m == m]
            r = parity.compare(got["marker"].rows, want, full.full_groups(), None)
            assert r["rows_compared"] == len(want) == len(got["marker"].rows) > 0
            assert r["columns"] == {} and r["bar_groups_exact"]
            assert list(got["marker"].rows.columns) == ["sym", *full.full_features()]
    # counts and the next minute: the stored parts differ only in the weighted columns, and
    # nothing after the last closed minute is stored
    a, b = _parts(plain), _parts(mark)
    last = _minute("12:01") + features.DAY_START_MINUTE
    assert a.m.max() == b.m.max() == last
    pd.testing.assert_frame_equal(a[["sym", "m"]], b[["sym", "m"]])
    same = [c for c in a.columns if c not in WEIGHTED]
    pd.testing.assert_frame_equal(a[same], b[same], check_exact=True)
    assert not a[list(WEIGHTED)].equals(b[list(WEIGHTED)])  # the marker did act
    for hm, got in served.items():
        x, y = got["plain"].rows, got["marker"].rows
        assert list(x.sym) == list(y.sym), hm
        counts = ["k_tn1", "k_tn5", "k_tn30", "k_qn1", "k_qn5", "k_qn30"]
        pd.testing.assert_frame_equal(x[counts], y[counts], check_exact=True)


# --- the cutoff -----------------------------------------------------------------------------


def _store_row(snap, kind, symbol, lo, hi):
    """One chosen version of `symbol` with SIP time in [lo, hi) as flat-file columns."""
    f = snap.frame(kind, symbol)
    r = f[(f.sip_timestamp >= lo) & (f.sip_timestamp < hi)].iloc[0].to_dict()
    return {c: r[c] for c in feed.columns(kind)[:-2]}


def test_a_close_uses_only_what_had_arrived_by_its_cutoff(world, harness, monkeypatch):
    """codex-109 point 4: a close reads the store's snapshot as of its own instant. A stream row
    that arrives while the close runs and a later REST correction of a print it converted stay
    out of it, the snapshot it read never changes, and the next close refuses the symbols whose
    converted minute they would change (`tick_late`; the correction: #793's `tick_conflict`)."""
    h = harness(cfg=with_model(live_marker_quote=True))
    h.warm(OPEN_WARM[0], "09:43")
    for hm in ("09:44", "09:45"):  # the training's rows (test_a_live_day_equals_...)
        _equals_training(world, h.close(_minute(hm), wanted=[tp.NO_BARS]), _minute(hm))
    m = _minute("09:46")
    lo, hi = h.live._end(m - 1), h.live._end(m)
    h.feed_to(m)
    held = h.store.snapshot(h.now)  # what is in hand at the close's instant
    extra = {**_store_row(held, "trades", "T002", lo, hi), "sequence_number": 10**9}
    fixed = _store_row(held, "trades", "T003", lo, hi)
    fixed["price"] = fixed["price"] * 1.01
    real = h.bars.frame

    def during(*a, **k):  # the stream thread keeps adding after the close took the clock
        h.bars.frame = real
        h.now += MS
        h.sock.push([_message("trades", extra)])
        return real(*a, **k)

    monkeypatch.setattr(h.bars, "frame", during)
    got = h.live.close(m)
    snap = h.live.snapshot
    assert not got.refused and snap.cutoff_ns == hi + MS
    keys = {(r[0], r[TRADE_SEQ]) for r in snap.rows["trades"]}
    assert ("T002", 10**9) not in keys
    assert snap.rows == held.rows  # exactly what had arrived by the cutoff
    frozen = dict(snap.rows)
    h.store.add("trades", [fixed], h.now + MS, feed.REST)  # a REST correction, received later
    got = h.close(m + 1)
    assert dict(snap.rows) == frozen  # the earlier snapshot is immutable
    assert got.refused["T002"] == live.Refusal("tick_late", "trades")
    # the correction differs from the version converted: an unresolved conflict of an input
    # (TickSnapshot.refusal), sticky once the symbol entered
    assert got.refused["T003"] == live.Refusal("tick_conflict")
    assert h.live.snapshot.frame("trades", "T003").price.isin([fixed["price"]]).any()
    assert not snap.frame("trades", "T003").price.isin([fixed["price"]]).any()


TRADE_SEQ = feed.columns("trades").index("sequence_number")


# --- refusals -----------------------------------------------------------------------------


def test_tick_refusals_are_typed_sticky_and_refuse_the_rank_peers(world, harness):
    h = harness()
    got = h.close(_minute(OPEN_WARM[0]), wanted=["ZZZZ"])
    cohort = set(got.refused) - {"ZZZZ"}
    assert got.refused.pop("ZZZZ") == live.Refusal("not_in_cohort") and got.rows.empty
    # subscribed after the cutoff: nothing proven at it
    assert set(got.refused.values()) == {live.Refusal("tick_not_subscribed")}
    # three symbols with a row (the training table's) at every minute this test closes
    minutes = [_minute(OPEN_WARM[0]) + k for k in (0, 1)] + [_minute(x) for x in (
        "09:45", "09:46", "09:47", "09:48")]
    a, b, c = sorted(set.intersection(
        cohort, *(set(world.table[world.table.m == x].sym) for x in minutes)))[:3]
    h.rest.fail = {a}  # its first backfill fails: sent once, never retried
    # a stream gap before the symbol enters: its backfill from midnight covers it
    h.store.down([c], h.now, "ConnectionError", 0)
    h.store.up(h.now + 1, 1)
    got = h.close(_minute(OPEN_WARM[0]) + 1)
    assert got.refused[a] == live.Refusal("backfill_missing", "http_status")
    assert got.refused[b] == live.Refusal("backfill_missing", "tick_no_initial_state")
    h.warm(_label(_minute(OPEN_WARM[0]) + 2), OPEN_WARM[1])
    got = h.close(_minute("09:45"))
    assert got.refused[a] == live.Refusal("backfill_missing", "http_status")  # sticky
    assert {b, c} <= h.entered() and a not in h.entered()
    assert all(got.refused[s] == live.Refusal("rank_peer_refused", a)
               for s in set(got.refused) - {a})
    # a gap after it entered refuses it for the rest of the day (here still open; a closed one
    # stays refused too: test_missing_inputs_receipts_stream_and_bar_gaps_refuse_every_row)
    h.store.down([b], h.now, "ConnectionError", 0)
    got = h.close(_minute("09:46"))
    assert got.refused[b] == live.Refusal("tick_gap", "ConnectionError")
    # an event that arrives after its minute was converted
    end = h.live._end(_minute("09:46"))
    h.close(_minute("09:47"))
    late = world.quotes[world.quotes.ticker == c].iloc[0].to_dict()
    late.update(sip_timestamp=end - 5 * MS, sequence_number=10**9)
    h.sock.push([_message("quotes", late)])
    got = h.close(_minute("09:48"))
    assert got.refused[c] == live.Refusal("tick_late", "quotes")
    assert got.refused[b] == live.Refusal("tick_gap", "ConnectionError")
    assert got.refused[a].cause == "backfill_missing"


def test_missing_inputs_receipts_stream_and_bar_gaps_refuse_every_row(world, harness, tmp_path):
    """A missing training-format input, prepared inputs without their receipts (`full`
    INPUTS), a tick stream that is down and a bar-stream gap each refuse every row of the
    minute. One day in that order (a bar gap refuses every later row first; the stream gap stays
    once the symbols entered), so the cohort warms up once."""
    bare = tmp_path / "bare" / DAY
    bare.mkdir(parents=True)
    for name in ("reference", "benzinga"):
        (bare / PREPARED[name]).symlink_to(world.prepared / DAY / PREPARED[name])
    h = harness(cfg=with_model(live_marker_quote=True), inputs_root=bare.parent)
    h.warm(*OPEN_WARM)
    got = h.close(_minute("09:45"))
    assert got.rows.empty and set(got.refused.values()) == {live.Refusal("input_missing",
                                                                         "fl_facts")}
    fl = bare / PREPARED["fl_facts"]
    head, *body = (world.prepared / DAY / PREPARED["fl_facts"]).read_text().splitlines()
    cut = head.rsplit(",", 1)[0]
    fl.write_text("\n".join([cut, *(x.rsplit(",", 1)[0] for x in body)]) + "\n")
    got = h.close(_minute("09:46"))
    assert got.rows.empty and got.refused
    assert set(got.refused.values()) == {live.Refusal("receipt_missing", "fl facts")}
    fl.unlink()
    fl.symlink_to(world.prepared / DAY / PREPARED["fl_facts"])
    assert not h.close(_minute("09:47")).refused  # the inputs are whole again
    # the stream: the connection ends, TickStream.run records the drop of every subscribed key
    h.shut()
    h.store.down(sorted(h.stream.symbols), h.now, "ConnectionError", 0)
    got = h.close(_minute("09:48"))
    assert got.rows.empty and got.refused
    assert set(got.refused.values()) == {live.Refusal("tick_gap", "ConnectionError")}
    h.store.up(h.now, 1)  # subscribed again: the gap is not filled, the refusal stays
    got = h.close(_minute("09:49"))
    assert set(got.refused.values()) == {live.Refusal("tick_gap", "ConnectionError")}
    h.bars.gap(1.0, 2.0, "ConnectionError")
    got = h.close(_minute("09:50"))
    assert got.rows.empty and {r.cause for r in got.refused.values()} == {"bar_gap"}


def test_the_wait_is_unmeasured_and_a_restart_or_a_repeat_refuses(world, harness, tmp_path):
    h = harness()
    with pytest.raises(ModelRefused) as exc:
        h.live.due(_minute("09:45"), 0)
    assert exc.value.cause == "feature_wait_unmeasured"
    h.close(_minute("04:30"))
    with pytest.raises(ModelRefused) as exc:
        h.live.close(_minute("04:30"))
    assert exc.value.cause == "minute_order"
    with pytest.raises(ModelRefused) as exc:
        live.LiveFeatures(CFG, world.context, world.state, None, h.bars, h.store, h.stream,
                          h.live.backfill, h.live.inputs, h.live.parts.parent)
    assert exc.value.cause == "live_ticks_exist"
    h.live.wait = config.live_wait_seconds(with_model(live_wait_after_minute_seconds="2.5")
                                           .model)
    end = h.live._end(_minute("09:45"))
    assert not h.live.due(_minute("09:45"), end + 2 * 10**9)
    assert h.live.due(_minute("09:45"), end + 25 * 10**8)


def test_a_converter_refusal_loses_the_day_and_says_so(world, harness, monkeypatch):
    h = harness()

    def refuse(*a, **k):
        raise ModelRefused("tick_converter_refused", "x")

    with monkeypatch.context() as mp, pytest.raises(ModelRefused):
        mp.setattr(ticks, "tick_rows", refuse)
        h.close(_minute("09:44"))
    with pytest.raises(ModelRefused) as exc:
        h.close(_minute("09:45"))
    assert exc.value.cause == "live_state_lost" and "tick_converter_refused" in exc.value.detail


def test_a_minute_closed_before_its_end_refuses_and_changes_nothing(world, harness):
    """The close's instant is the minute's receipt cutoff: before the minute's mark it refuses
    (`cutoff_before_mark`) before anything is consumed; the same minute closes once it is over."""
    h = harness()
    m = _minute("09:45")
    h.feed_to(m)
    h.now = h.live._end(m) - MS
    with pytest.raises(ModelRefused) as exc:
        h.live.close(m)
    assert exc.value.cause == "cutoff_before_mark"
    assert h.live.closed == -1 and not h.live.symbols and h.live.lost is None
    assert h.store.counts["late"] == 0 and h.live.snapshot is None
    h.now = h.live._end(m) + MS
    got = h.live.close(m)
    assert h.live.closed == m and got.refused and h.live.symbols
    assert {r.cause for r in got.refused.values()} == {"tick_not_subscribed"}
