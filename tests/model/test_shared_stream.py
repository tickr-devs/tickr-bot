"""Minute bars and trades/quotes on one Massive connection (codex-108 point 10): a stand-in
socket that answers each subscribe parameter like Massive, no network."""

from __future__ import annotations

import dataclasses
import itertools
import json
import threading
import time

import pytest
from test_tick_stream import AUTH, CFG, DAY, END, KEY, MS, START, T0, quote, trade

from tickr.model import ticks
from tickr.model.errors import ModelRefused
from tickr.model.massive import BarRepair, DayBars
from tickr.model.stream import SharedStream

ALL = 10**30  # a cutoff after every receipt of a test
WAIT = 5.0  # seconds a test waits for a stream thread before it fails (test-side only)
CFG0 = dataclasses.replace(CFG, massive=dataclasses.replace(
    CFG.massive, stream_release_pause_seconds=0, tick_subscription="symbols"))


def bar(sym="AAPL", s=T0 // MS):
    return {"ev": "AM", "sym": sym, "v": 100, "o": 1.0, "c": 1.1, "h": 1.2, "l": 0.9, "s": s}


def until(check):
    deadline = time.monotonic() + WAIT
    while not check():
        assert time.monotonic() < deadline, "stream thread did not get there"
        time.sleep(0.001)


class Server:
    """Frames to send, an acknowledgment per subscribed parameter (except `unacked`, and
    `unacked_once` the first time), `after` frames once the first subscribe was acknowledged,
    `idle` hooks run one per empty read, and at the end either a drop or the stop flag."""

    def __init__(self, frames, drop=False, unacked=(), unacked_once=(), after=(), idle=()):
        self.incoming = [json.dumps(f) for f in frames]
        self.sent = []
        self.drop = drop
        self.unacked = set(unacked)
        self.unacked_once = set(unacked_once)
        self.after = [json.dumps(f) for f in after]
        self.idle = list(idle)
        self.stream = None
        self.stop = None
        self.at_end = None  # runs once when the frames are all read and routed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["action"] == "subscribe":
            params = msg["params"].split(",")
            self.incoming.append(json.dumps([
                {"ev": "status", "status": "success", "message": f"subscribed to: {p}"}
                for p in params if p not in self.unacked and p not in self.unacked_once]))
            self.unacked_once -= set(params)
            self.incoming.extend(self.after)
            self.after = []

    def recv(self, timeout):
        if not self.incoming:
            until(lambda: self.stream.router.caught_up(ALL))
        if self.incoming:
            return self.incoming.pop(0)
        if self.idle:
            self.idle.pop(0)()
            raise TimeoutError
        if self.at_end is not None:
            hook, self.at_end = self.at_end, None
            hook()
            raise TimeoutError
        until(lambda: all(lane.caught_up(ALL) or lane.failed
                          for lane in self.stream.lanes.values()))
        if self.drop:
            raise ConnectionError("closed")
        self.stop.set()
        raise TimeoutError


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setenv(CFG.massive.api_key_variable, KEY)

    def build(servers, cfg=CFG0, on_connect=None, clock=None, offset=lambda: None):
        stop = threading.Event()
        bars, store = DayBars(DAY), ticks.TickStore(DAY)
        count = itertools.count(START + 10**12)
        made = []

        def connect(url, max_size):
            assert url == f"wss://{cfg.massive.stream_host}{cfg.massive.stream_path}"
            if on_connect is not None:
                on_connect(len(made))
            server = servers.pop(0)
            server.stream, server.stop = stream, stop
            made.append(server)
            return server

        stream = SharedStream(cfg, bars, store, connect, clock=clock or (lambda: next(count)),
                              mono=MONO, offset=offset)
        return stream, stop, bars, store, made
    return build


class Mono:
    """A monotonic clock (ns) that ticks by one per read and jumps when a test says so."""

    def __init__(self):
        self.now = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.now += 1
            return self.now

    def jump(self, ns):
        with self._lock:
            self.now += ns


MONO = Mono()


def complete(store, symbol, upto=ALL):
    for kind in ticks.KINDS:
        store.window(ticks.Window(symbol, kind, store.start_ns, upto, 0, 1, None, 0))


def test_one_connection_subscribes_both_channel_sets_and_records_each_acknowledgment(make):
    stream, stop, bars, store, made = make([Server([[AUTH]], after=[[bar(), trade(), quote()]])])
    stream.add("AAPL")
    stream.run(stop)
    assert len(made) == 1
    assert made[0].sent == [{"action": "auth", "params": KEY},
                            {"action": "subscribe", "params": "AM.*,T.AAPL,Q.AAPL"}]
    [(conn, sent, params, what)] = stream.requests
    assert conn == 1 and params == ("AM.*", "T.AAPL", "Q.AAPL") and what == "subscribe"
    for param in params:
        [entry] = stream.acks[param]
        assert entry.sent_ns == sent and entry.acked_ns > sent and entry.dropped_ns is None
        assert entry.conn == 1 and entry.wait_ns > 0 and entry.resent_ns is None
    assert store.subscribed == {"AAPL": stream.acks["Q.AAPL"][0].acked_ns}  # when both took
    assert len(bars.rows) == 1 and len(store.versions["trades"]) == 1
    assert stream.bars_refusal(ALL) is None
    complete(store, "AAPL")
    assert stream.tick_refusal(stream.tick_snapshot(ALL), "AAPL") is None
    assert stream.stats()["AM"]["handed"] == 1 and stream.stats()["T"]["max_lag_ns"] > 0


def test_a_symbol_joining_later_is_its_own_acknowledged_request(make):
    server = Server([[AUTH]])
    stream, stop, _, store, _ = make([server])
    server.at_end = lambda: stream.add("MSFT")
    stream.run(stop)
    assert server.sent[1:] == [{"action": "subscribe", "params": "AM.*"},
                               {"action": "subscribe", "params": "T.MSFT,Q.MSFT"}]
    assert all(stream.acks[p][0].acked_ns is not None for p in ("T.MSFT", "Q.MSFT"))
    assert "MSFT" in store.subscribed


@pytest.mark.parametrize("unacked", ["AM.*", "Q.AAPL"])
def test_a_channel_never_acknowledged_is_refused(make, unacked):
    server = Server([[AUTH]], unacked=[unacked], after=[[trade(), quote()]],
                    idle=[lambda: MONO.jump(10**15)])  # long past any timeout: resend is off
    stream, stop, _, store, _ = make([server])
    stream.add("AAPL")
    stream.run(stop)
    assert stream.acks[unacked][0].acked_ns is None
    assert [r[3] for r in stream.requests] == ["subscribe"] and len(server.sent) == 2
    complete(store, "AAPL")
    bars_cause = stream.bars_refusal(ALL)
    tick_cause = stream.tick_refusal(stream.tick_snapshot(ALL), "AAPL")
    if unacked == "AM.*":
        assert bars_cause == "stream_not_acknowledged" and tick_cause is None
    else:
        assert tick_cause == "stream_not_acknowledged" and bars_cause is None
        assert "AAPL" not in store.subscribed


def test_an_acknowledgment_arriving_after_the_cutoff_does_not_count(make):
    stream, stop, _, _, _ = make([Server([[AUTH]])])
    stream.run(stop)
    acked = stream.acks["AM.*"][0].acked_ns
    assert stream.bars_refusal(acked - 1) == "stream_not_acknowledged"
    assert stream.bars_refusal(acked) is None


def test_a_slow_tick_consumer_never_delays_the_bars_and_is_visible(make, monkeypatch):
    release = threading.Event()
    server = Server([[AUTH], [trade()], [bar()]])
    stream, stop, bars, store, _ = make([server])
    real_add = store.add

    def slow_add(*args, **kwargs):
        assert release.wait(WAIT)
        return real_add(*args, **kwargs)

    monkeypatch.setattr(store, "add", slow_add)
    stream.add("AAPL")
    seen = {}

    def at_end():
        until(lambda: len(bars.rows) == 1)  # the bar went through while the trade waits
        seen["T"] = stream.stats()["T"]
        seen["bars"] = stream.bars_refusal(ALL)
        with pytest.raises(ModelRefused) as exc:
            stream.tick_snapshot(ALL)
        seen["ticks"] = exc.value.cause
        release.set()
        until(lambda: stream.lanes["T"].caught_up(ALL))

    server.at_end = at_end
    stream.run(stop)
    assert seen["T"]["depth"] == 1 and seen["T"]["oldest_waiting_ns"] is not None
    assert seen["bars"] is None and seen["ticks"] == "stream_backlog"
    assert stream.tick_snapshot(ALL).rows["trades"]  # handed once the consumer caught up


def test_a_failed_consumer_refuses_its_own_channel_only(make):
    bad = {"ev": "T", "sym": "AAPL"}  # no price, size, time or sequence
    stream, stop, _, store, _ = make([Server([[AUTH], [bad], [bar()]])])
    stream.add("AAPL")
    stream.run(stop)
    with pytest.raises(ModelRefused) as exc:
        stream.tick_snapshot(ALL)
    assert exc.value.cause == "stream_consumer_failed"
    assert stream.stats()["T"]["failed"] == "tick_unreadable"
    assert stream.bars_refusal(ALL) is None


MINUTE_MS = 60_000  # test data: one minute in ms
SECOND = 10**9  # test data: one second in ns


def fill(store, frm, to, recv, cause=None, symbol="AAPL"):
    for kind in ticks.KINDS:
        store.window(ticks.Window(symbol, kind, frm, to, recv, 1, cause, 0))


def test_a_drop_gaps_bars_and_ticks_at_once_and_the_reconnect_resubscribes(make):
    """codex-109 point 6: the gap of both channels is bounded by event times (the last verified
    event before the drop, the first after the reconnect), never by the receipt times."""
    open_state = {}
    holder = {}
    t1 = T0 + 2 * SECOND  # SIP of the last tick before the drop
    t2 = T0 + 9 * SECOND  # SIP of the first tick after the reconnect
    m0, m1 = T0 // MS, T0 // MS + 5 * MINUTE_MS  # the last bar minute before, the first after

    def on_connect(n):
        if n == 1:  # between the drop and the second connection
            s = holder["stream"]
            fill(s.ticks, START, T0 + MS, ALL)  # the initial state, past the first event's ms
            snap = s.tick_snapshot(ALL)
            open_state.update(bars=s.bars_refusal(ALL), ticks=s.tick_refusal(snap, "AAPL"),
                              open_bars=s.bars.open_gap, open_ticks=snap.gaps["AAPL"])

    first = Server([[AUTH]], drop=True, after=[
        [bar(s=m0), trade(q=100, t=T0 // MS), quote(q=200, t=T0 // MS)],
        [trade(q=101, t=t1 // MS), quote(q=201, t=t1 // MS)]])
    second = Server([[AUTH]], after=[
        [bar(s=m1), trade(q=102, t=t2 // MS), quote(q=202, t=t2 // MS)]])
    stream, stop, bars, store, made = make([first, second], on_connect=on_connect)
    holder["stream"] = stream
    stream.add("AAPL")
    stream.run(stop)
    assert open_state["bars"] == "bars_gap" and open_state["ticks"] == "tick_gap"
    down, cause = open_state["open_bars"]
    assert cause == "ConnectionError"
    assert [(g.down_ns, g.up_ns, g.cause) for g in open_state["open_ticks"]] == [
        (down, None, "ConnectionError")]
    # the new connection authenticates and sends every subscription again
    assert second.sent == [{"action": "auth", "params": KEY},
                           {"action": "subscribe", "params": "AM.*,T.AAPL,Q.AAPL"}]
    for param in ("AM.*", "T.AAPL", "Q.AAPL"):
        old, new = stream.acks[param]
        assert old.dropped_ns == down and old.conn == 1 and new.conn == 2
        assert new.acked_ns is not None
    am_up = stream.acks["AM.*"][1].acked_ns
    tick_up = stream.acks["Q.AAPL"][1].acked_ns
    assert bars.gaps == [(down, am_up, "ConnectionError")] and bars.open_gap is None
    [gap] = store.gaps["AAPL"]
    assert (gap.down_ns, gap.up_ns, gap.cause) == (down, tick_up, "ConnectionError")
    assert gap.duration_ns > 0  # monotonic, apart from the wall-clock receipts
    assert stream.take_backfill() == [("AM", "*", down, am_up),
                                      ("ticks", "AAPL", down, tick_up)]
    assert stream.take_backfill() == []
    # what to recover, in event time
    snap = stream.tick_snapshot(ALL)
    assert snap.repair_from("AAPL") == t1
    assert bars.needed(ALL) == [(m0, m1)]
    assert stream.bars_refusal(ALL) == "bars_gap"
    assert stream.tick_refusal(snap, "AAPL") == "tick_gap"
    # the receipt times of the drop taken as SIP times fill nothing
    fill(store, down, tick_up, ALL)
    assert stream.tick_refusal(stream.tick_snapshot(ALL), "AAPL") == "tick_gap"
    fill(store, t1, t2 + 1, ALL)  # ends inside the first verified row's millisecond
    assert stream.tick_refusal(stream.tick_snapshot(ALL), "AAPL") == "tick_gap"
    fill(store, t1, t2 + MS, ALL)  # overlaps both verified rows' milliseconds
    assert stream.tick_refusal(stream.tick_snapshot(ALL), "AAPL") is None
    assert len(store.versions["trades"]) == 3


def test_a_bar_gap_is_repaired_only_by_a_complete_market_repair_of_its_whole_range(make):
    """codex-109 point 6: a cohort-only backfill cannot restore market-wide ranks, breadth or
    membership; until a complete-market repair covers the gap the bars stay unavailable."""
    m0, m1 = T0 // MS, T0 // MS + 5 * MINUTE_MS
    first = Server([[AUTH]], drop=True, after=[[bar(s=m0)]])
    second = Server([[AUTH]], after=[[bar(s=m1)]])
    stream, stop, bars, _, _ = make([first, second])
    stream.run(stop)
    assert bars.needed(ALL) == [(m0, m1)] and stream.bars_refusal(ALL) == "bars_gap"
    cohort = BarRepair(m0, m1 + MINUTE_MS, 1, 1, market=False, cause=None)
    with pytest.raises(ModelRefused) as exc:
        bars.repair(cohort, [bar(sym="AAPL", s=m0 + MINUTE_MS)])
    assert exc.value.cause == "bars_repair_not_market"
    assert bars.refused_repairs == [cohort] and ("AAPL", m0 + MINUTE_MS) not in bars.rows
    for repair in (BarRepair(m0 + MINUTE_MS, m1 + MINUTE_MS, 1, 1, True, None),  # misses m0
                   BarRepair(m0, m1, 1, 1, True, None),  # stops before m1
                   BarRepair(m0, m1 + MINUTE_MS, 1, 3, True, "backfill_no_progress")):  # stalled
        bars.repair(repair, [])
        assert stream.bars_refusal(ALL) == "bars_gap"
    late = ALL - 1
    bars.repair(BarRepair(m0, m1 + MINUTE_MS, late, 2, True, None), [bar(sym="ZZZ", s=m0)])
    assert stream.bars_refusal(late - 1) == "bars_gap"  # the repair arrived after this cutoff
    assert stream.bars_refusal(ALL) is None and bars.needed(ALL) == []
    with pytest.raises(ModelRefused) as exc:
        bars.repair(BarRepair(m0, m1, 1, 1, True, None), [bar(s=m1)])  # a bar outside its range
    assert exc.value.cause == "bars_repair_unreadable"


def test_a_reset_after_midnight_recovers_both_channels_from_day_start(make):
    """A reset shortly after New York midnight is a real gap: nothing of the new day was verified
    before it, so the recovery of both channels starts at 00:00 and runs through the first
    verified event after the reconnect (events before 04:00 count); stalled paging does not."""
    from test_tick_stream import Wire, rest_quote, rest_trade

    early = START + (17 * 60 + 5) * SECOND  # 00:17:05 New York: before the 04:00 session
    minute = (START + 17 * 60 * SECOND) // MS
    yesterday = START // MS - MINUTE_MS
    first = Server([[AUTH]], drop=True, after=[  # the previous day's last prints, then the reset
        [bar(s=yesterday), trade(q=1, t=START // MS - 1), quote(q=2, t=START // MS - 1)]])
    second = Server([[AUTH]], after=[
        [bar(s=minute), trade(q=7, t=early // MS), quote(q=8, t=early // MS)]])
    stream, stop, bars, store, _ = make([first, second])
    stream.add("AAPL")
    stream.run(stop)
    assert store.counts["other_day"] == 2 and bars.other_day == 1
    snap = stream.tick_snapshot(ALL)
    assert snap.repair_from("AAPL") == START
    # nothing of the new date was proven before the reset: the day's own state is missing
    assert stream.tick_refusal(snap, "AAPL") == "tick_no_initial_state"
    assert bars.needed(ALL) == [(START // MS, minute)]
    stalled = {"status": "OK", "results": [rest_trade(3, START + SECOND)],
               "next_url": f"https://{CFG.massive.api_host}/v3/trades/AAPL?cursor=a"}
    wire = Wire({"trades": [stalled, dict(stalled)], "quotes": [{"status": "OK", "results": []}]})
    back = ticks.TickBackfill(CFG0, store, wire, clock=lambda: ALL - 1)
    assert back.backfill("AAPL", START, early + MS)["trades"].cause == "backfill_no_progress"
    assert stream.tick_refusal(stream.tick_snapshot(ALL), "AAPL") == "tick_backfill_incomplete"
    wire = Wire({"trades": [{"status": "OK", "results": [rest_trade(3, START + SECOND)]}],
                 "quotes": [{"status": "OK", "results": [rest_quote(4, START + SECOND)]}]})
    windows = ticks.TickBackfill(CFG0, store, wire, clock=lambda: ALL - 1).backfill(
        "AAPL", START, early + MS)
    assert all(w.cause is None for w in windows.values())  # the last page had no next_url
    snap = stream.tick_snapshot(ALL)
    assert stream.tick_refusal(snap, "AAPL") is None
    assert snap.frame("quotes", "AAPL").sip_timestamp.tolist() == [START + SECOND, early]
    assert stream.bars_refusal(ALL) == "bars_gap"  # no market repair yet
    bars.repair(BarRepair(START // MS, minute + MINUTE_MS, 1, 1, True, None), [])
    assert stream.bars_refusal(ALL) is None


def test_readiness_needs_acknowledgment_drained_messages_and_verified_coverage(make):
    """codex-109 point 6: each missing piece alone keeps the channel not ready (the missing
    acknowledgment and the backlog are also shown above, each on its own)."""
    stream, stop, bars, store, _ = make([Server([[AUTH]], after=[[bar()]])])
    stream.add("AAPL")
    stream.run(stop)
    acked = stream.acks["Q.AAPL"][0].acked_ns
    fill(store, START, END, ALL)
    # acknowledged and drained, but no stream event proves the live edge yet
    assert stream.tick_refusal(stream.tick_snapshot(ALL), "AAPL") == "tick_coverage_unverified"
    # acknowledged and drained, no initial state at all
    assert stream.tick_refusal(stream.tick_snapshot(acked), "AAPL") == "tick_no_initial_state"
    # not acknowledged by the cutoff
    assert stream.tick_refusal(stream.tick_snapshot(acked - 1), "AAPL") == (
        "stream_not_acknowledged")
    assert stream.bars_refusal(ALL) is None
    # a bars gap with no repair: acknowledged and drained, coverage not verified
    bars.down(ALL - 1, "ConnectionError")
    assert stream.bars_refusal(ALL) == "bars_gap"
    # drained: a message received by the cutoff still waiting in a lane refuses
    stream.lanes["Q"].put(quote(), ALL - 2, 0)
    with pytest.raises(ModelRefused) as exc:
        stream.tick_snapshot(ALL)
    assert exc.value.cause == "stream_backlog"


def ack_cfg(seconds):
    return dataclasses.replace(CFG0, massive=dataclasses.replace(
        CFG0.massive, stream_ack_timeout_seconds=seconds))


def test_a_missing_acknowledgment_is_re_sent_once_on_the_same_connection(make, caplog):
    jump = lambda: MONO.jump(2 * SECOND)  # noqa: E731 - past the 1 s test timeout
    server = Server([[AUTH]], unacked_once=["Q.AAPL"], idle=[jump],
                    after=[[trade(), quote()]])
    stream, stop, _, store, made = make([server], ack_cfg(1))
    stream.add("AAPL")
    with caplog.at_level("WARNING", logger="tickr.model.stream"):
        stream.run(stop)
    assert len(made) == 1
    assert server.sent[1:] == [{"action": "subscribe", "params": "AM.*,T.AAPL,Q.AAPL"},
                               {"action": "subscribe", "params": "Q.AAPL"}]
    assert [r[3] for r in stream.requests] == ["subscribe", "resend"]
    [entry] = stream.acks["Q.AAPL"]
    assert entry.resent_ns is not None and entry.acked_ns is not None
    assert stream.counts["ack_resends"] == 1
    assert "re-sending once" in caplog.text and "Q.AAPL" in caplog.text
    waits = {param: (wait, resent) for param, _, wait, resent in stream.ack_waits()}
    assert waits["Q.AAPL"][0] >= 2 * SECOND and waits["Q.AAPL"][1] is True
    assert waits["AM.*"][0] < SECOND and waits["AM.*"][1] is False
    assert "AAPL" in store.subscribed


def test_still_unacknowledged_after_the_resend_reconnects_and_resubscribes(make, caplog):
    jump = lambda: MONO.jump(2 * SECOND)  # noqa: E731
    first = Server([[AUTH]], unacked=["Q.AAPL"], idle=[jump, jump])
    second = Server([[AUTH]])
    stream, stop, bars, store, made = make([first, second], ack_cfg(1))
    stream.add("AAPL")
    with caplog.at_level("WARNING", logger="tickr.model.stream"):
        stream.run(stop)
    assert len(made) == 2
    assert [r[3] for r in stream.requests] == ["subscribe", "resend", "subscribe"]
    assert first.sent[2] == {"action": "subscribe", "params": "Q.AAPL"}  # the one resend
    assert second.sent[1] == {"action": "subscribe", "params": "AM.*,T.AAPL,Q.AAPL"}
    assert [link.cause for link in stream.links] == ["ack_timeout", "stopped"]
    assert "reconnecting" in caplog.text
    assert [g.cause for g in store.gaps["AAPL"]] == ["ack_timeout"]
    assert [g[2] for g in bars.gaps] == ["ack_timeout"]
    assert stream.acks["Q.AAPL"][1].acked_ns is not None


def test_receipt_offset_and_monotonic_durations_are_recorded_apart(make):
    """codex-109 point 6 (5): the wall clock stands still here; every duration still comes from
    the monotonic clock, and the offset is recorded as given (None when unknown)."""
    wall = START + 10**12
    stream, stop, _, _, _ = make([Server([[AUTH]], after=[[bar()]])], clock=lambda: wall,
                                 offset=lambda: -2 * 10**6)
    stream.run(stop)
    [link] = stream.links
    assert link.opened_ns == wall and link.offset_ns == -2 * 10**6
    assert link.closed_mono > link.opened_mono and link.closed_ns == wall
    entry = stream.acks["AM.*"][0]
    assert entry.acked_ns == wall and entry.offset_ns == -2 * 10**6 and entry.wait_ns > 0
    assert stream.stats()["AM"]["max_lag_ns"] > 0  # monotonic lag, the wall clock did not move
    unknown, stop, _, _, _ = make([Server([[AUTH]])])
    unknown.run(stop)
    assert unknown.links[0].offset_ns is None and unknown.acks["AM.*"][0].offset_ns is None


def test_the_reconnect_waits_the_documented_release_pause(make, monkeypatch):
    waits = []
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: waits.append(t) or True)
    cfg = dataclasses.replace(CFG0, massive=dataclasses.replace(
        CFG0.massive, stream_release_pause_seconds=CFG.massive.stream_release_pause_seconds))
    stream, stop, _, _, _ = make([Server([[AUTH]], drop=True), Server([[AUTH]])], cfg)
    stream.run(stop)
    assert [w for w in waits if w is not None] == [float(CFG.massive.stream_release_pause_seconds)]


def test_the_full_market_path_shares_the_connection(make):
    cfg = dataclasses.replace(CFG0, massive=dataclasses.replace(
        CFG0.massive, tick_subscription="market"))
    server = Server([[AUTH], [trade(sym="ZZZ")]], drop=True)
    stream, stop, _, store, _ = make([server, Server([[AUTH]])], cfg)
    stream.run(stop)
    assert server.sent[1] == {"action": "subscribe", "params": "AM.*,T.*,Q.*"}
    assert "*" in store.subscribed and [g.cause for g in store.gaps["*"]] == ["ConnectionError"]
    assert ("ticks", "*") in [r[:2] for r in stream.take_backfill()]


@pytest.mark.parametrize("status", ["auth_failed", "max_connections", "error"])
def test_a_stream_level_refusal_stops_the_stream(make, status):
    stream, stop, _, _, _ = make([Server([[{"ev": "status", "status": status}]])])
    with pytest.raises(ModelRefused) as exc:
        stream.run(stop)
    assert exc.value.cause == "stream_refused" and exc.value.detail == status


def test_events_before_login_are_counted_not_kept(make):
    stream, stop, bars, _, _ = make([Server([[bar()], [AUTH]])])
    stream.run(stop)
    assert not bars.rows and stream.counts["stale_or_early_events"] == 1

