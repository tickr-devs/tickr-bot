"""Probe K (engine/tick_probe.py) against a fake stream. No Webull, no SDK."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tickr.broker.webull import tick_probe_event
from tickr.data.base import StreamAnswer
from tickr.engine import tick_probe as tp

T0 = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
TOO_MANY = StreamAnswer(False, 417, "TOO_MANY_SYMBOLS_SUBSCRIPTION", "ServerException")
SETTINGS = tp.TickSettings(30, 60, (2, 4, 5, 8), 10, 3)


class Clock:
    def __init__(self) -> None:
        self.at = T0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)


class FakeStream:
    """Holds a TICK and a QUOTE set; refuses a TICK request past `tick_cap` (or every TICK
    request when `tick_refused`; with `quote_cap_shared` any request past it counting (type,
    symbol) pairs); a message per held symbol is delivered on each probe sleep."""

    def __init__(self, on_event, *, connects=True, tick_cap=100, tick_refused=False,
                 ticks=True, quotes=True, quote_cap_shared=False) -> None:
        self.on_event, self.connects = on_event, connects
        self.tick_cap, self.tick_refused = tick_cap, tick_refused
        self.ticks, self.quotes, self.shared = ticks, quotes, quote_cap_shared
        self.held: dict[str, set[str]] = {tp.TICK: set(), tp.QUOTE: set()}
        self.requests: list[tuple[str, list[str]]] = []
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def wait_connected(self, seconds: float) -> bool:
        return self.connects

    def subscribe(self, symbols, sub_type) -> StreamAnswer:
        self.requests.append((sub_type, list(symbols)))
        held = self.held[sub_type]
        if sub_type == tp.TICK and self.tick_refused:
            return TOO_MANY
        count = len(held | set(symbols))
        if self.shared:  # the cap counts (type, symbol) pairs across both types
            count = len({(t, s) for t, ss in self.held.items() for s in ss}
                        | {(sub_type, s) for s in symbols})
        if sub_type == tp.TICK and count > self.tick_cap or self.shared and count > self.tick_cap:
            return TOO_MANY
        held.update(symbols)
        return StreamAnswer(True)

    def unsubscribe_all(self) -> StreamAnswer:
        self.requests.append(("ALL", []))
        for held in self.held.values():
            held.clear()
        return StreamAnswer(True)

    def deliver(self) -> None:
        for symbol in sorted(self.held[tp.TICK]) if self.ticks else []:
            self.on_event({"kind": "tick", "symbol": symbol, "price": "10.01", "volume": "100",
                           "side": "BUY", "time": "1"})
        for symbol in sorted(self.held[tp.QUOTE]) if self.quotes else []:
            self.on_event({"kind": "quote", "symbol": symbol})

    def stop(self) -> None:
        self.stopped = True


def probe(tmp_path, universe=("A", "B", "C", "D", "E", "F", "G", "H", "I"), **fake):
    clock, lines, made = Clock(), [], []

    def make(on_event):
        made.append(FakeStream(on_event, **fake))
        return made[0]

    def sleep(seconds):
        clock.sleep(seconds)
        made[0].deliver()

    code = tp.run(make, ["A", "B"], list(universe), SETTINGS, tmp_path, now=clock.now,
                  sleep=sleep, out=lines.append, environment="sandbox-test")
    summary = json.loads((tmp_path / tp.SUMMARY_FILE).read_text())
    return code, summary, made[0], lines


def test_ticks_stream_and_the_cap_is_found(tmp_path):
    code, summary, stream, lines = probe(tmp_path, tick_cap=4)
    assert code == tp.EXIT_ANSWERED and stream.stopped
    q1 = summary["q1"]
    assert q1["verdict"] == tp.STREAMS and q1["symbols_with_ticks"] == ["A", "B"]
    assert q1["first_tick"]["A"]["price"] == "10.01"
    q2 = summary["q2"]
    assert q2["verdict"] == tp.CAP_REFUSED and q2["held"] == 4
    assert q2["refused_answer"]["error_code"] == "TOO_MANY_SYMBOLS_SUBSCRIPTION"
    assert q2["quote_at_tick_cap"]["accepted"]  # the cap counts TICK symbols only
    # Each step sends only new symbols, at most request_max_symbols per request, never twice.
    ticks = [s for kind, s in stream.requests if kind == tp.TICK]
    assert ticks == [["A", "B"], ["A", "B"], ["C", "D"], ["E"]]
    assert stream.requests[-1] == ("ALL", [])
    assert any(line.startswith("q2_tick_cap: 4 symbol(s) held") for line in lines)
    events = [json.loads(line) for line in (tmp_path / tp.EVENTS_FILE).read_text().splitlines()]
    assert any(e.get("answer") == {"accepted": False, "http_status": 417,
                                   "error_code": "TOO_MANY_SYMBOLS_SUBSCRIPTION",
                                   "error_type": "ServerException"} for e in events)


def test_a_cap_shared_with_quotes_shows_in_the_quote_request(tmp_path):
    _, summary, _, _ = probe(tmp_path, tick_cap=4, quote_cap_shared=True)
    assert summary["q2"]["held"] == 4 and not summary["q2"]["quote_at_tick_cap"]["accepted"]


def test_quotes_without_ticks_answer_no(tmp_path):
    code, summary, _, _ = probe(tmp_path, ticks=False)
    assert summary["q1"]["verdict"] == tp.NO_TICKS and summary["q1"]["ticks"] == 0
    assert summary["q2"]["verdict"] == tp.CAP_NOT_REACHED and summary["q2"]["held"] == 8
    assert code == tp.EXIT_ANSWERED


def test_a_refused_tick_subscription_answers_q1(tmp_path):
    code, summary, stream, _ = probe(tmp_path, tick_refused=True)
    assert summary["q1"]["verdict"] == tp.REFUSED
    assert summary["q1"]["tick_answer"]["http_status"] == 417
    # The ramp's first request is refused too: nothing held, no QUOTE check.
    assert summary["q2"]["held"] == 0 and "quote_at_tick_cap" not in summary["q2"]
    assert code == tp.EXIT_ANSWERED


def test_no_messages_at_all_prove_nothing(tmp_path):
    code, summary, _, _ = probe(tmp_path, ticks=False, quotes=False)
    assert summary["q1"]["verdict"] == tp.SILENT and code == tp.EXIT_NO_ANSWER


def test_too_few_symbols_for_the_ladder_prove_nothing(tmp_path):
    code, summary, _, _ = probe(tmp_path, universe=("A", "B", "C"))
    assert summary["q2"]["verdict"] == tp.UNIVERSE_SHORT and code == tp.EXIT_NO_ANSWER


def test_no_connection_sends_nothing(tmp_path):
    code, summary, stream, _ = probe(tmp_path, connects=False)
    assert summary["q1"]["verdict"] == tp.NOT_CONNECTED and code == tp.EXIT_NO_ANSWER
    assert stream.requests == [] and stream.stopped


def test_stream_messages_become_records():
    basic = SimpleNamespace(symbol="SOFI", timestamp=1763391600000, trading_session="CORE")
    tick = SimpleNamespace(basic=basic, time=1763391600123, price="14.41", volume=100,
                           side="BUY")
    assert tick_probe_event("tick", tick) == {
        "kind": "tick", "symbol": "SOFI", "timestamp": "1763391600000", "session": "CORE",
        "time": "1763391600123", "price": "14.41", "volume": "100", "side": "BUY"}
    assert tick_probe_event("quote", SimpleNamespace(basic=basic)) == {
        "kind": "quote", "symbol": "SOFI"}
    assert tick_probe_event("notice", {"type": "1001", "drop": 0}) == {
        "kind": "notice", "fields": {"type": "1001", "drop": "0"}}
    assert tick_probe_event("snapshot", object()) == {"kind": "other", "topic": "snapshot"}
