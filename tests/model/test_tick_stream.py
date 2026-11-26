"""Massive trades and quotes: stream and REST backfill into flat-file rows; no network
(a stand-in socket and a stand-in HTTP opener)."""

from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from tickr import config
from tickr.model import ticks
from tickr.model.errors import ModelRefused

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
KEY = "api-canary-1"
DAY = "2026-11-18"
START, END = ticks.day_bounds_ns(DAY)
MS = 10**6
T0 = START + (9 * 60 + 30) * 60 * 10**9  # 09:30 New York
ALL = 10**30  # a cutoff after every receipt of a test


def frame(store, kind, cutoff=ALL):
    return store.snapshot(cutoff).frame(kind)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv(CFG.massive.api_key_variable, KEY)


def with_mode(mode: str) -> config.Config:
    return dataclasses.replace(
        CFG, massive=dataclasses.replace(CFG.massive, tick_subscription=mode))


def trade(sym="AAPL", q=100, t=T0 // MS, **extra):
    msg = {"ev": "T", "sym": sym, "x": 4, "i": "7", "z": 3, "p": 114.125, "s": 100,
           "c": [12, 37], "t": t, "pt": t - 1, "q": q}
    msg.update(extra)
    return msg


def quote(sym="AAPL", q=200, t=T0 // MS, **extra):
    msg = {"ev": "Q", "sym": sym, "bx": 4, "bp": 114.1, "bs": 100, "ax": 7, "ap": 114.2,
           "as": 160, "c": 1, "i": [604], "t": t, "pt": t - 1, "q": q, "z": 3}
    msg.update(extra)
    return msg


AUTH = {"ev": "status", "status": "auth_success"}


class FakeSocket:
    def __init__(self, batches, drop=False):
        self.incoming = [json.dumps(b) for b in batches]
        self.sent = []
        self.drop = drop
        self.stop = None
        self.on_empty = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        if self.incoming:
            return self.incoming.pop(0)
        if self.on_empty is not None:
            hook, self.on_empty = self.on_empty, None
            hook()
            raise TimeoutError
        if self.drop:
            raise ConnectionError("closed")
        self.stop.set()
        raise TimeoutError


def make_stream(sockets, cfg=CFG):
    stop = threading.Event()
    store = ticks.TickStore(DAY)
    made = []
    times = iter(range(1, 10**6))

    def connect(url, max_size):
        assert url == f"wss://{cfg.massive.stream_host}{cfg.massive.stream_path}"
        assert max_size == cfg.massive.stream_max_message_bytes
        sock = sockets.pop(0)
        sock.stop = stop
        made.append(sock)
        return sock

    monos = iter(range(10**9, 10**9 + 10**6))
    stream = ticks.TickStream(cfg, store, connect, clock=lambda: next(times),
                              mono=lambda: next(monos))
    return stream, stop, store, made


def test_stream_messages_become_flat_file_rows_with_receipt_times(creds):
    sock = FakeSocket([[AUTH], [trade(trfi=202, q=101), quote()], trade(q=102)])
    stream, stop, store, made = make_stream([sock])
    stream.add("AAPL")
    stream.session(stop)
    assert made[0].sent == [{"action": "auth", "params": KEY},
                            {"action": "subscribe", "params": "T.AAPL,Q.AAPL"}]
    assert store.subscribed == {"AAPL": 2}  # when the subscription was sent
    t = frame(store, "trades")
    assert list(t.columns) == list(ticks.TRADE_COLUMNS) + ["recv_ns", "source", "provisional"]
    first = t.iloc[0].to_dict()
    assert first == {"ticker": "AAPL", "conditions": "12,37", "correction": None,
                     "exchange": 4, "price": 114.125, "size": 100, "sip_timestamp": T0,
                     "sequence_number": 101, "trf_id": 202, "participant_timestamp": T0 - MS,
                     "recv_ns": 3, "source": "stream", "provisional": True}
    assert t.iloc[1]["trf_id"] is None and t.iloc[1]["recv_ns"] == 4  # absent, never filled
    assert type(t.iloc[0]["participant_timestamp"]) is int  # exact nanoseconds, no float
    q = frame(store, "quotes").iloc[0].to_dict()
    assert q == {"ticker": "AAPL", "ask_price": 114.2, "ask_size": 160, "bid_price": 114.1,
                 "bid_size": 100, "sip_timestamp": T0, "sequence_number": 200,
                 "conditions": "1", "indicators": "604", "recv_ns": 3, "source": "stream",
                 "provisional": True}


def test_an_empty_quote_side_stays_empty():
    row = ticks.stream_row("quotes", {k: v for k, v in quote().items()
                                      if k not in ("ap", "as", "ax", "i")})
    assert row["ask_price"] is None and row["ask_size"] is None and row["indicators"] is None


@pytest.mark.parametrize("kind,msg,key", [("trades", trade(), "q"), ("trades", trade(), "p"),
                                          ("trades", trade(), "t"), ("quotes", quote(), "q"),
                                          ("quotes", quote(), "sym")])
def test_a_missing_required_field_is_refused(kind, msg, key):
    msg = {k: v for k, v in msg.items() if k != key}
    with pytest.raises(ModelRefused) as exc:
        ticks.stream_row(kind, msg)
    assert exc.value.cause == "tick_unreadable" and key in exc.value.detail


def test_a_wrongly_typed_field_is_refused():
    with pytest.raises(ModelRefused):
        ticks.stream_row("trades", trade(p="114.1"))
    with pytest.raises(ModelRefused):
        ticks.stream_row("quotes", quote(i=["604"]))


def test_symbols_joining_during_the_day_are_subscribed_on_the_open_connection(creds):
    sock = FakeSocket([[AUTH]])
    stream, stop, store, _ = make_stream([sock])
    assert stream.add("AAPL") is True
    sock.on_empty = lambda: (stream.add("MSFT"), stream.add("AAPL"))
    stream.session(stop)
    assert sock.sent[1:] == [{"action": "subscribe", "params": "T.AAPL,Q.AAPL"},
                             {"action": "subscribe", "params": "T.MSFT,Q.MSFT"}]
    assert stream.symbols == {"AAPL", "MSFT"}


def test_no_subscribe_before_login_and_ticks_before_login_are_refused(creds):
    stream, stop, _, made = make_stream([FakeSocket([[trade()]])])
    stream.add("AAPL")
    with pytest.raises(ModelRefused) as exc:
        stream.session(stop)
    assert exc.value.cause == "tick_unreadable"
    assert made[0].sent == [{"action": "auth", "params": KEY}]


def test_the_full_market_path_subscribes_every_symbol(creds):
    sock = FakeSocket([[AUTH], [trade(sym="ZZZ")]])
    stream, stop, store, _ = make_stream([sock], with_mode("market"))
    stream.session(stop)
    assert sock.sent[1] == {"action": "subscribe", "params": "T.*,Q.*"}
    stream.add("AAPL")  # nothing more to send: every symbol already arrives
    assert len(sock.sent) == 2 and list(frame(store, "trades").ticker) == ["ZZZ"]


def test_a_refused_login_stops_the_stream(creds):
    stream, stop, _, _ = make_stream([FakeSocket([[{"ev": "status", "status": "auth_failed"}]])])
    with pytest.raises(ModelRefused) as exc:
        stream.run(stop)
    assert exc.value.cause == "stream_refused"


@pytest.mark.parametrize("mode,keys", [("symbols", ["AAPL", "MSFT"]), ("market", ["*"])])
def test_a_drop_is_a_gap_of_every_subscribed_symbol_and_the_next_login_resubscribes(
        creds, monkeypatch, mode, keys):
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)
    first = FakeSocket([[AUTH], [trade()]], drop=True)
    second = FakeSocket([[AUTH], [trade(q=101)]])
    stream, stop, store, made = make_stream([first, second], with_mode(mode))
    stream.add("AAPL")
    stream.add("MSFT")
    stream.run(stop)
    assert len(made) == 2
    assert sorted(store.gaps) == keys
    gap = store.gaps[keys[0]][0]  # opened at the drop, its drop ended by the resubscribe
    assert gap.down_ns < gap.up_ns and gap.cause == "ConnectionError"
    assert gap.duration_ns is not None and gap.duration_ns > 0  # monotonic, kept apart
    assert gap.before == {("trades", "AAPL"): T0}  # the SIP time of the last row seen
    assert second.sent[1] == first.sent[1]  # the same subscription again
    assert len(frame(store, "trades")) == 2


def test_a_tick_of_another_day_is_counted_not_kept(creds):
    sock = FakeSocket([[AUTH], [trade(t=END // MS)]])
    stream, stop, store, _ = make_stream([sock])
    stream.add("AAPL")
    stream.session(stop)
    assert store.counts["other_day"] == 1 and frame(store, "trades").empty


# --- backfill --------------------------------------------------------------------------------


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class Wire:
    """Stand-in for urlopen: a list of answers per kind, popped in order."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        kind = urllib.parse.urlsplit(request.full_url).path.split("/")[2]
        answer = self.answers[kind].pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "x", {}, None)
        return Response(json.dumps(answer).encode())


def rest_trade(seq, ts, **extra):
    row = {"conditions": [12, 37], "exchange": 4, "id": str(seq), "participant_timestamp": ts - 5,
           "price": 114.125, "sequence_number": seq, "sip_timestamp": ts, "size": 100,
           "tape": 3}
    row.update(extra)
    return row


def rest_quote(seq, ts, **extra):
    row = {"ask_exchange": 7, "ask_price": 114.2, "ask_size": 160, "bid_exchange": 4,
           "bid_price": 114.1, "bid_size": 100, "conditions": [1], "indicators": [604],
           "sequence_number": seq,
           "sip_timestamp": ts, "tape": 3}
    row.update(extra)
    return row


NEXT = f"https://{CFG.massive.api_host}/v3/trades/AAPL?cursor=abc"


def test_backfill_pages_both_kinds_and_keeps_every_version_with_its_own_receipt(creds):
    store = ticks.TickStore(DAY)
    # the stream already holds trade 101 (millisecond time) and quote 200
    store.add("trades", [ticks.stream_row("trades", trade(q=101))], 50, ticks.STREAM)
    store.add("quotes", [ticks.stream_row("quotes", quote(q=200))], 50, ticks.STREAM)
    wire = Wire({
        "trades": [{"status": "OK", "results": [rest_trade(100, T0 - 7, correction=1)],
                    "next_url": NEXT},
                   {"status": "OK", "results": [rest_trade(101, T0 + 123, trf_id=202)]}],
        "quotes": [{"status": "OK", "results": [rest_quote(199, T0 - 9),
                                                rest_quote(200, T0 + 456)]}]})
    times = iter([70, 80, 90])
    monos = iter([1000, 1500, 2000, 2250])
    back = ticks.TickBackfill(CFG, store, wire, clock=lambda: next(times),
                              mono=lambda: next(monos))
    windows = back.backfill("AAPL", START, T0 + MS)
    assert windows["trades"] == ticks.Window("AAPL", "trades", START, T0 + MS, 80, 2, None, 500)
    assert windows["quotes"] == ticks.Window("AAPL", "quotes", START, T0 + MS, 90, 1, None, 250)
    assert store.windows == [windows["trades"], windows["quotes"]]
    assert back.requests == 3 and back.rate_limited == 0
    first = urllib.parse.urlsplit(wire.requests[0].full_url)
    assert first.path == "/v3/trades/AAPL"
    assert urllib.parse.parse_qs(first.query) == {
        "timestamp.gte": [str(START)], "timestamp.lt": [str(T0 + MS)], "order": ["asc"],
        "sort": ["timestamp"], "limit": [str(CFG.massive.tick_page_limit)]}
    assert wire.requests[1].full_url == NEXT
    assert all(r.get_header("Authorization") == "Bearer " + KEY for r in wire.requests)
    # both versions of trade 101 are kept, each with its own receipt and source
    held = store.versions["trades"][("AAPL", 101)]
    assert [(v[-2], v[-1]) for v in held] == [(50, "stream"), (80, "rest")]
    t = frame(store, "trades")
    assert list(t.sequence_number) == [100, 101]  # one chosen version per key
    kept = t.iloc[1].to_dict()
    # the REST version is used, with its own (later) receipt: nothing is backdated
    assert kept["sip_timestamp"] == T0 + 123 and kept["trf_id"] == 202
    assert kept["source"] == "rest" and kept["recv_ns"] == 80
    assert t.iloc[0]["correction"] == 1 and t.iloc[0]["recv_ns"] == 70
    q = frame(store, "quotes")
    assert list(q.sequence_number) == [199, 200] and q.iloc[1]["recv_ns"] == 90
    # the trf id the stream did not send is a conflict between the two versions of trade 101
    assert [(c.kind, c.sequence_number, c.fields) for c in store.conflicts] == [
        ("trades", 101, ("trf_id",))]
    # the stream repeating a row the backfill holds: a third version, REST is still the one used
    store.add("trades", [ticks.stream_row("trades", trade(q=101))], 99, ticks.STREAM)
    assert len(store.versions["trades"][("AAPL", 101)]) == 3
    assert frame(store, "trades").iloc[1]["source"] == "rest"


OK_EMPTY = {"status": "OK", "results": []}


def fill(store, symbol, from_ns, to_ns, at, trades=(), quotes=()):
    """A complete backfill of both kinds received at `at`."""
    wire = Wire({"trades": [{"status": "OK", "results": list(trades)}],
                 "quotes": [{"status": "OK", "results": list(quotes)}]})
    return ticks.TickBackfill(CFG, store, wire, clock=lambda: at).backfill(symbol, from_ns, to_ns)


def streamed(store, sym, recv, q=1, t=T0 // MS, **extra):
    """A stream trade and quote of `sym` received at `recv`."""
    store.add("trades", [ticks.stream_row("trades", trade(sym=sym, q=q, t=t, **extra))], recv,
              ticks.STREAM)
    store.add("quotes", [ticks.stream_row("quotes", quote(sym=sym, q=q, t=t))], recv,
              ticks.STREAM)


def test_a_rest_revision_from_a_complete_window_is_authoritative_and_refuses_nothing(creds):
    """codex-109 point 4: complete REST data received by the cutoff is authoritative."""
    store = ticks.TickStore(DAY)
    store.subscribe(["AAPL"], 10)
    streamed(store, "AAPL", 50, q=101)
    fill(store, "AAPL", START, END, 60, trades=[rest_trade(101, T0 + 7, size=99)])
    (c,) = store.conflicts
    assert c == ticks.Conflict(
        "trades", "AAPL", 101, ("size",), ("stream", 50), ("rest", 60), ticks.REVISION,
        uncompared=("correction", "participant_timestamp"), lossy=("sip_timestamp",),
        inputs=True, sip_range=(T0, T0 + MS))
    before, after = store.snapshot(55), store.snapshot(60)
    assert before.refusal("AAPL") == "tick_no_initial_state"
    assert before.frame("trades").iloc[0][["size", "provisional"]].tolist() == [100, True]
    assert after.refusal("AAPL") is None and after.resolutions == ((c, 60),)
    assert after.frame("trades").iloc[0][["size", "provisional"]].tolist() == [99, False]
    # the stream quote REST did not return stays a provisional stream value
    assert after.frame("quotes").iloc[0]["provisional"] is True
    # a stream copy after the REST version does not reopen the symbol
    streamed(store, "AAPL", 70, q=101)
    assert store.conflicts[-1].authority_ns == 60 and store.snapshot(70).refusal("AAPL") is None


def test_an_input_conflict_refuses_only_its_symbol_and_its_ranks_until_rest_restores_it(creds):
    """codex-109 point 4: the affected symbol and the ranks holding it, not the rest of the day,
    with the receipt time from which it is usable again."""
    store = ticks.TickStore(DAY)
    store.subscribe(["AAPL", "MSFT"], T0)
    for sym in ("AAPL", "MSFT"):
        streamed(store, sym, T0 + 1, q=101)
        fill(store, sym, START, T0 + MS, T0 + 2)
    earlier = store.snapshot(T0 + 2)
    assert earlier.refusal("AAPL") is None and earlier.refusal("MSFT") is None
    # the stream repeats AAPL trade 101 at another price: a revision of unproven fidelity
    streamed(store, "AAPL", T0 + 3, q=101, p=113.5)
    (c,) = store.conflicts
    assert (c.fields, c.identity, c.inputs) == (("price",), ticks.REVISION, True)
    snap = store.snapshot(T0 + 3)
    assert snap.refusal("AAPL") == "tick_conflict" and snap.refusal("MSFT") is None
    assert snap.resolutions == ((c, None),)
    with pytest.raises(ModelRefused) as exc:
        snap.require_ranks(["MSFT", "AAPL"])
    assert exc.value.cause == "tick_ranks_unavailable" and "AAPL: tick_conflict" in exc.value.detail
    assert earlier.refusal("AAPL") is None  # the earlier decision's view is unchanged
    # authoritative history of the affected range restores the symbol from its receipt on
    fill(store, "AAPL", T0, T0 + MS, T0 + 9, trades=[rest_trade(101, T0 + 5, price=113.5)])
    assert store.snapshot(T0 + 8).refusal("AAPL") == "tick_conflict"
    later = store.snapshot(T0 + 9)
    assert later.refusal("AAPL") is None
    assert [r for _, r in later.resolutions] == [T0 + 9] * len(store.conflicts)
    later.require_ranks(["AAPL", "MSFT"])
    assert later.frame("trades", "AAPL").iloc[0][["price", "source"]].tolist() == [113.5, "rest"]


def test_identity_and_non_input_differences_are_told_apart(creds):
    store = ticks.TickStore(DAY)
    store.subscribe(["AAPL"], T0)
    streamed(store, "AAPL", T0 + 1, q=101)
    fill(store, "AAPL", START, T0 + MS, T0 + 2)
    # a stream copy that differs only in the participant time (px side files, no model input)
    streamed(store, "AAPL", T0 + 3, q=101, pt=T0 // MS - 2)
    (pt,) = store.conflicts
    assert (pt.fields, pt.identity, pt.inputs) == (("participant_timestamp",), ticks.REVISION,
                                                   False)
    assert store.snapshot(T0 + 3).refusal("AAPL") is None  # recorded, not refused
    # REST names sequence 101 at another millisecond: two prints may share the key
    store.add("trades", [ticks.rest_row("trades", "AAPL", rest_trade(101, T0 + 5 * MS + 3))],
              T0 + 4, ticks.REST)
    odd = store.conflicts[-1]
    assert (odd.identity, odd.fields, odd.inputs) == (ticks.UNCERTAIN, ("sip_timestamp",), True)
    assert odd.sip_range == (T0, T0 + 6 * MS)
    assert store.snapshot(T0 + 4).refusal("AAPL") == "tick_conflict"
    # a complete window that misses the stream version's millisecond does not resolve it
    fill(store, "AAPL", T0 + MS, T0 + 6 * MS, T0 + 5)
    assert store.snapshot(T0 + 5).refusal("AAPL") == "tick_conflict"
    fill(store, "AAPL", T0, T0 + 6 * MS, T0 + 6)
    assert store.snapshot(T0 + 6).refusal("AAPL") is None


def test_a_later_rest_correction_never_changes_an_earlier_cutoffs_snapshot(creds):
    """codex-109 point 4: prior decision snapshots are immutable."""
    store = ticks.TickStore(DAY)
    store.subscribe(["AAPL"], T0)
    streamed(store, "AAPL", T0 + 1, q=101)
    fill(store, "AAPL", START, T0 + MS, T0 + 2, trades=[rest_trade(101, T0 + 4)])
    snap = store.snapshot(T0 + 2)
    taken = snap.frame("trades").to_dict("records")
    assert taken[0]["correction"] is None and snap.refusal("AAPL") is None
    # REST later corrects the print (correction 1, another price) and the fill is complete
    fill(store, "AAPL", START, T0 + MS, T0 + 20,
         trades=[rest_trade(101, T0 + 4, correction=1, price=115.0)])
    assert store.conflicts[-1].fields == ("correction", "price")
    assert snap.frame("trades").to_dict("records") == taken and snap.conflicts == ()
    assert snap.refusal("AAPL") is None
    # taken again as of the same cutoff, the view is the same; at a later cutoff it is repaired
    assert store.snapshot(T0 + 2).frame("trades").to_dict("records") == taken
    now = store.snapshot(T0 + 20).frame("trades").iloc[0]
    assert [now["correction"], now["price"], now["recv_ns"]] == [1, 115.0, T0 + 20]


def test_an_empty_quote_side_and_absent_codes_are_no_conflict():
    store = ticks.TickStore(DAY)
    side = {k: v for k, v in quote(q=7).items() if k not in ("ap", "as", "i")}
    store.add("quotes", [ticks.stream_row("quotes", side)], 1, ticks.STREAM)
    store.add("quotes", [ticks.rest_row("quotes", "AAPL", rest_quote(
        7, T0 + 5, ask_price=0, ask_size=0, indicators=[]))], 2, ticks.REST)
    assert store.conflicts == []


def test_the_snapshot_uses_what_had_arrived_by_its_cutoff():
    store = ticks.TickStore(DAY)
    store.add("trades", [ticks.stream_row("trades", trade(q=101))], 50, ticks.STREAM)
    store.add("trades", [ticks.rest_row("trades", "AAPL", rest_trade(101, T0 + 3))], 70,
              ticks.REST)
    store.add("trades", [ticks.stream_row("trades", trade(q=101))], 90, ticks.STREAM)
    assert store.snapshot(49).frame("trades").empty
    assert store.snapshot(69).frame("trades").iloc[0][["source", "recv_ns"]].tolist() == [
        "stream", 50]
    # REST arrived by the cutoff: it is used, with its own receipt, also after a later stream copy
    for cutoff in (70, 95):
        row = store.snapshot(cutoff).frame("trades").iloc[0]
        assert [row["source"], row["recv_ns"], row["sip_timestamp"]] == ["rest", 70, T0 + 3]


def test_a_decision_snapshot_never_changes_after_later_arrivals():
    store = ticks.TickStore(DAY)
    store.add("trades", [ticks.stream_row("trades", trade(q=101))], T0 + 50, ticks.STREAM)
    snap = store.snapshot(T0 + 60)
    taken = snap.frame("trades").to_dict("records")
    # a REST copy with other content and a new print, both of SIP time before the cutoff
    store.add("trades", [ticks.rest_row("trades", "AAPL", rest_trade(101, T0, size=7)),
                         ticks.rest_row("trades", "AAPL", rest_trade(102, T0 + 1))], T0 + 80,
              ticks.REST)
    assert snap.frame("trades").to_dict("records") == taken and snap.conflicts == ()
    assert store.counts["late"] == 2  # received after a cutoff that their SIP time preceded
    assert len(store.snapshot(T0 + 80).frame("trades")) == 2
    with pytest.raises((AttributeError, TypeError)):
        snap.rows["trades"] = ()  # type: ignore[index]


def test_a_symbol_entering_late_with_no_recent_quote_gets_its_state_from_midnight(creds):
    """It joins at 14:00; its last quote is from 03:55, before the 04:00 session start."""
    sock = FakeSocket([[AUTH]])
    store = ticks.TickStore(DAY)
    at_1400 = START + 14 * 3600 * 10**9
    clock = iter(range(at_1400, at_1400 + 100 * MS, MS))
    stream = ticks.TickStream(CFG, store, lambda url, max_size: sock, clock=lambda: next(clock))
    stop = threading.Event()
    sock.stop = stop
    sock.on_empty = lambda: stream.add("LATE")
    stream.session(stop)
    subscribed = store.subscribed["LATE"]
    assert store.snapshot(subscribed).refusal("LATE") == "tick_no_initial_state"
    q0355 = START + (3 * 60 + 55) * 60 * 10**9
    wire = Wire({"trades": [{"status": "OK", "results": [rest_trade(5, q0355 - 10**9)]}],
                 "quotes": [{"status": "OK", "results": [rest_quote(9, q0355)]}]})
    windows = ticks.TickBackfill(CFG, store, wire, clock=lambda: next(clock)).initial("LATE")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(wire.requests[0].full_url).query)
    assert query["timestamp.gte"] == [str(START)]  # 00:00 New York, not 04:00
    assert all(w.from_ns == START and w.to_ns > subscribed and w.cause is None
               for w in windows.values())
    got = max(w.recv_ns for w in windows.values())
    # the subscription was sent, but no stream row has shown that delivery began
    assert store.snapshot(got).refusal("LATE") == "tick_coverage_unverified"
    # its first stream rows: their SIP millisecond lies inside the window, the hand-over holds
    streamed(store, "LATE", got + 1, q=10, t=subscribed // MS)
    snap = store.snapshot(got + 1)
    assert snap.refusal("LATE") is None
    assert snap.frame("quotes", "LATE").sip_timestamp.tolist() == [q0355, subscribed]
    assert snap.refusal("OTHER") == "tick_not_subscribed"


def test_a_stream_gap_refuses_until_a_complete_backfill_spans_it(creds):
    store = ticks.TickStore(DAY)
    store.subscribe(["AAPL"], T0)
    streamed(store, "AAPL", T0 + 1, q=1)
    fill(store, "AAPL", START, T0 + MS, T0 + 2)
    assert store.snapshot(T0 + 2).refusal("AAPL") is None
    streamed(store, "AAPL", T0 + 3, q=2, t=T0 // MS + 4)  # the last rows seen before the drop
    store.down(["AAPL"], T0 + 10, "ConnectionError", 500)
    assert store.snapshot(T0 + 15).refusal("AAPL") == "tick_gap"  # still down
    store.up(T0 + 20, 520)
    assert store.gaps["AAPL"][0].duration_ns == 20
    # resubscribed, but delivery after it is not verified by the send
    assert store.snapshot(T0 + 25).refusal("AAPL") == "tick_coverage_unverified"
    streamed(store, "AAPL", T0 + 26, q=3, t=T0 // MS + 9)
    snap = store.snapshot(T0 + 26)
    assert snap.refusal("AAPL") == "tick_gap"  # delivering, the hole open
    assert snap.repair_from("AAPL") == T0 + 4 * MS  # from the last row seen, in SIP time
    # a window that starts after the last row seen before the drop leaves a hole
    fill(store, "AAPL", T0 + 5 * MS, T0 + 10 * MS, T0 + 27)
    assert store.snapshot(T0 + 27).refusal("AAPL") == "tick_gap"
    # one that ends at the first row seen after it does not overlap it
    fill(store, "AAPL", T0 + 4 * MS, T0 + 9 * MS, T0 + 28)
    assert store.snapshot(T0 + 28).refusal("AAPL") == "tick_gap"
    fill(store, "AAPL", T0 + 4 * MS, T0 + 10 * MS, T0 + 30)
    assert store.snapshot(T0 + 29).refusal("AAPL") == "tick_gap"  # the fill arrived later
    assert store.snapshot(T0 + 30).refusal("AAPL") is None


def test_receipt_times_are_never_compared_with_sip_times_as_coverage(creds):
    """codex-109 point 6: the local clock may run ahead of or behind SIP time; coverage is the
    SIP time of rows seen, whatever the receipt times of the drop and the resubscription."""
    store = ticks.TickStore(DAY)
    ahead = 3600 * 10**9  # the local clock one hour ahead of SIP time
    store.subscribe(["AAPL"], T0 + ahead)
    streamed(store, "AAPL", T0 + ahead + 1, q=1)
    fill(store, "AAPL", START, T0 + MS, T0 + ahead + 2)
    streamed(store, "AAPL", T0 + ahead + 3, q=2, t=T0 // MS + 4)
    store.down(["AAPL"], T0 + ahead + 10, "ConnectionError", 500)
    store.up(T0 + ahead + 20, 520)
    streamed(store, "AAPL", T0 + ahead + 21, q=3, t=T0 // MS + 9)
    # a window spanning the drop's and the resubscription's receipt times proves nothing
    fill(store, "AAPL", T0 + ahead + 10, T0 + ahead + 20 + MS, T0 + ahead + 22)
    assert store.snapshot(T0 + ahead + 22).refusal("AAPL") == "tick_gap"
    # the SIP span of the rows seen around the gap does
    fill(store, "AAPL", T0 + 4 * MS, T0 + 10 * MS, T0 + ahead + 23)
    snap = store.snapshot(T0 + ahead + 23)
    assert snap.refusal("AAPL") is None
    # the receipt-minus-SIP estimate is recorded (clock offset plus delay), not used as proof
    assert snap.lag("AAPL") == (T0 + ahead + 21, ahead + 21 - 9 * MS)


def test_a_reset_after_new_york_midnight_is_a_gap_repaired_from_midnight(creds):
    """codex-109 point 6: Massive's reset just after 00:00 New York loses the date's first
    events (before 04:00 too); the repair starts at midnight and must reach the first row."""
    store = ticks.TickStore(DAY)
    store.subscribe(["AAPL"], START + 1)  # subscribed at 00:00:00 on this date
    store.down(["AAPL"], START + 5 * 10**9, "ConnectionClosed", 500)  # reset at 00:00:05
    store.up(START + 30 * 10**9, 525)
    first = START + 2 * 3600 * 10**9  # the first row seen after it, at 02:00
    streamed(store, "AAPL", first + 1, q=9, t=first // MS)
    assert store.snapshot(first + 1).repair_from("AAPL") == START
    at_0400 = START + 4 * 3600 * 10**9
    fill(store, "AAPL", at_0400, at_0400 + MS, first + 2)  # a session-hours repair is not one
    assert store.snapshot(first + 2).refusal("AAPL") == "tick_no_initial_state"
    q0100 = START + 3600 * 10**9
    fill(store, "AAPL", START, first + MS, first + 3, quotes=[rest_quote(5, q0100)])
    snap = store.snapshot(first + 3)
    assert snap.refusal("AAPL") is None
    assert snap.frame("quotes", "AAPL").sip_timestamp.tolist() == [q0100, first]


def test_a_reconnect_acknowledgment_does_not_prove_the_gap_was_delivered(creds, monkeypatch):
    """codex-109 point 4: a subscription send or an ACK alone does not establish delivery."""
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)
    ack = {"ev": "status", "status": "success", "message": "subscribed to: T.AAPL"}
    first = FakeSocket([[AUTH], [ack], [trade(q=1), quote(q=1)]], drop=True)
    second = FakeSocket([[AUTH], [ack]])
    stream, stop, store, _ = make_stream([first, second])
    stream.add("AAPL")
    stream.run(stop)
    up = store.gaps["AAPL"][0].up_ns
    fill(store, "AAPL", START, END, up + 10)  # the whole date, complete
    assert store.snapshot(up + 10).refusal("AAPL") == "tick_coverage_unverified"
    # the first row delivered after the resubscription, inside that window: now verified
    streamed(store, "AAPL", up + 11, q=2, t=T0 // MS + 2)
    assert store.snapshot(up + 11).refusal("AAPL") is None


def test_lag_and_same_millisecond_counts_are_recorded():
    store = ticks.TickStore(DAY)
    store.add("trades", [ticks.stream_row("trades", trade(q=1))], T0 + 3 * MS, ticks.STREAM)
    store.add("trades", [ticks.stream_row("trades", trade(q=2))], T0 + 4 * MS, ticks.STREAM)
    store.add("quotes", [ticks.stream_row("quotes", quote(q=3))], T0 + 5 * MS, ticks.STREAM)
    assert store.counts["same_ms_trades"] == 1 and store.counts["same_ms_trade_quote"] == 1
    snap = store.snapshot(T0 + 4 * MS)
    assert snap.lag("AAPL") == (T0 + 4 * MS, 4 * MS) and snap.lag("MSFT") is None


def test_a_rate_limit_answer_is_counted(creds):
    back = ticks.TickBackfill(CFG, ticks.TickStore(DAY), Wire({"trades": [429]}), clock=lambda: 1)
    with pytest.raises(ModelRefused):
        back.backfill("AAPL", START, END)
    assert back.requests == 1 and back.rate_limited == 1


@pytest.mark.parametrize("answers,cause", [
    ({"trades": [500]}, "http_status"),
    ({"trades": [{"status": "ERROR", "results": []}]}, "backfill_unreadable"),
    ({"trades": [{"status": "OK", "results": [rest_trade(1, END)]}]}, "backfill_unreadable"),
    ({"trades": [{"status": "OK", "results": [{"sip_timestamp": T0, "sequence_number": 1}]}]},
     "tick_unreadable"),
    ({"trades": [{"status": "OK", "results": [],
                  "next_url": "https://elsewhere.example/v3/trades/AAPL"}]},
     "backfill_unreadable"),
    ({"trades": [{"status": "OK", "results": []}],
      "quotes": [{"status": "OK", "results": [rest_quote(1, T0)],
                  "next_url": NEXT.replace("trades", "quotes")}, 404]}, "not_found"),
])
def test_a_failed_backfill_refuses_once_and_keeps_nothing(creds, answers, cause):
    store = ticks.TickStore(DAY)
    wire = Wire(answers)
    with pytest.raises(ModelRefused) as exc:
        ticks.TickBackfill(CFG, store, wire, clock=lambda: 1).backfill("AAPL", START, END)
    assert exc.value.cause == cause
    assert frame(store, "trades").empty and frame(store, "quotes").empty
    assert all(not v for v in wire.answers.values())  # every request sent once, none retried
    assert [w.cause for w in store.windows] == [cause]  # recorded as an incomplete window


@pytest.mark.parametrize("pages", [
    # an empty page that promises another
    [{"status": "OK", "results": [], "next_url": NEXT}],
    # a page whose last SIP time does not pass the previous page's
    [{"status": "OK", "results": [rest_trade(1, T0 + 5)], "next_url": NEXT},
     {"status": "OK", "results": [rest_trade(2, T0 + 5)], "next_url": NEXT + "x"}],
    # a next_url fetched before
    [{"status": "OK", "results": [rest_trade(1, T0)], "next_url": NEXT},
     {"status": "OK", "results": [rest_trade(2, T0 + 1)], "next_url": NEXT}],
])
def test_stalled_paging_is_an_incomplete_window_not_a_complete_one(creds, pages):
    wire = Wire({"trades": list(pages), "quotes": [{"status": "OK", "results": []}]})
    store = ticks.TickStore(DAY)
    store.subscribe(["AAPL"], 1)
    windows = ticks.TickBackfill(CFG, store, wire, clock=lambda: 1).backfill("AAPL", START, END)
    assert windows["trades"].cause == "backfill_no_progress"
    assert windows["trades"].pages == len(pages) and windows["quotes"].cause is None
    assert len(wire.requests) == len(pages) + 1  # nothing asked again
    snap = store.snapshot(1)
    assert snap.refusal("AAPL") == "tick_backfill_incomplete"
    assert len(snap.frame("trades")) == sum(len(p["results"]) for p in pages)  # rows received


@pytest.mark.parametrize("args", [("AAPL", END, START), ("", START, END),
                                  ("AAPL", True, END)])
def test_a_bad_range_is_refused_before_any_request(creds, args):
    wire = Wire({})
    with pytest.raises(ModelRefused) as exc:
        ticks.TickBackfill(CFG, ticks.TickStore(DAY), wire).backfill(*args)
    assert exc.value.cause == "backfill_range" and wire.requests == []


def test_the_day_bounds_are_new_york_midnights():
    start, end = ticks.day_bounds_ns("2026-03-08")  # the spring change: a 23-hour day
    assert (end - start) == 23 * 3600 * 10**9


def test_unknown_subscription_mode_is_refused_by_the_config(tmp_path):
    text = (ROOT / "config" / "bot.toml").read_text("utf-8").replace(
        'tick_subscription = "symbols"', 'tick_subscription = "some"')
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bot.toml").write_text(text, "utf-8")
    with pytest.raises(config.ConfigError):
        config.load(tmp_path / "config" / "bot.toml")
