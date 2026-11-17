"""Probe K, read only: trade-by-trade (TICK) data on the L1 stream.

Two open questions (docs/webull-facts.md, section K):

1. Does the stream deliver TICK messages (price, volume, time, side of each trade) on our
   OpenAPI market-data entitlement? TICK and QUOTE are subscribed together for the given
   symbols and their messages counted for `[probe] tick_flow_wait_seconds`. Quotes without
   ticks say TICK is not delivered while the stream works; no messages at all say nothing (a
   quiet or closed market).
2. How many symbols may one connection hold for TICK (QUOTE allows 100, probe M3)? A fresh
   set of symbols is subscribed for TICK step by step (`[probe] tick_cap_steps`, cumulative,
   at most `[market_data] stream_max_symbols` per request) until a request is refused. While
   TICK is at its cap, one QUOTE request for an already subscribed symbol tells whether the
   cap counts symbols or symbol and type.

Every subscribe request is sent once and never repeated; nothing is ordered. Each message and
answer goes to `events.jsonl` of the evidence folder and a `summary.json` ends the run.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from tickr.data.base import StreamAnswer

TICK = "TICK"  # SDK sub-type names (protocol constants)
QUOTE = "QUOTE"
EVENTS_FILE = "events.jsonl"
SUMMARY_FILE = "summary.json"

STREAMS = "streams"
NO_TICKS = "no-ticks-while-quotes-flow"
REFUSED = "refused"
SILENT = "no-messages"  # neither ticks nor quotes: nothing proven
NOT_CONNECTED = "not-connected"
CAP_REFUSED = "refused-past"
CAP_NOT_REACHED = "not-reached"
UNIVERSE_SHORT = "universe-too-small"

EXIT_ANSWERED = 0
EXIT_NO_ANSWER = 4

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]
Out = Callable[[str], None]


class TickStream(Protocol):
    def start(self) -> None: ...

    def wait_connected(self, seconds: float) -> bool: ...

    def subscribe(self, symbols: Sequence[str], sub_type: str) -> StreamAnswer: ...

    def unsubscribe_all(self) -> StreamAnswer: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class TickSettings:
    connect_wait_seconds: float
    flow_wait_seconds: float
    cap_steps: tuple[int, ...]
    step_wait_seconds: float
    request_max_symbols: int


class Recorder:
    """Writes every event and answer as one JSON line and counts messages per kind and symbol.
    Stream events arrive on the SDK's thread, so everything is under one lock."""

    def __init__(self, path: Path, now: Clock) -> None:
        self._file = path.open("a", encoding="utf-8")
        self._now = now
        self._lock = threading.Lock()
        self.counts: Counter[tuple[str, str]] = Counter()
        self.first_tick: dict[str, dict[str, Any]] = {}
        self._closed = False

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self._closed:  # a message after stop(), on the SDK's thread: the run is over
                return
            line = {"at": self._now().isoformat(), **record}
            self._file.write(json.dumps(line, default=str, sort_keys=True) + "\n")
            self._file.flush()

    def on_event(self, event: dict[str, Any]) -> None:
        kind, symbol = str(event.get("kind")), str(event.get("symbol"))
        with self._lock:
            self.counts[(kind, symbol)] += 1
            if kind == TICK.lower():
                self.first_tick.setdefault(symbol, event)
        self.write({"event": event})

    def answer(self, step: str, sub_type: str, symbols: Sequence[str],
               answer: StreamAnswer) -> StreamAnswer:
        self.write({"step": step, "sub_type": sub_type, "symbols": list(symbols),
                    "answer": asdict(answer)})
        return answer

    def symbols_with(self, kind: str) -> set[str]:
        with self._lock:
            return {symbol for (k, symbol), n in self.counts.items() if k == kind and n}

    def total(self, kind: str) -> int:
        with self._lock:
            return sum(n for (k, _), n in self.counts.items() if k == kind)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._file.close()


def describe(answer: StreamAnswer) -> str:
    if answer.accepted:
        return "accepted"
    return f"refused (HTTP {answer.http_status} {answer.error_code} {answer.error_type})"


def flow(stream: TickStream, rec: Recorder, symbols: Sequence[str], settings: TickSettings,
         sleep: Sleep, out: Out) -> dict[str, Any]:
    """Question 1: subscribe TICK and QUOTE for `symbols`, wait, count, unsubscribe all."""
    tick = rec.answer("flow", TICK, symbols, stream.subscribe(symbols, TICK))
    quote = rec.answer("flow", QUOTE, symbols, stream.subscribe(symbols, QUOTE))
    out(f"q1 subscribe TICK {describe(tick)}, QUOTE {describe(quote)}; "
        f"counting {settings.flow_wait_seconds:g} s")
    sleep(settings.flow_wait_seconds)
    ticks, quotes = rec.total("tick"), rec.total("quote")
    if not tick.accepted:
        verdict = REFUSED
    elif ticks:
        verdict = STREAMS
    elif quotes:
        verdict = NO_TICKS
    else:
        verdict = SILENT
    rec.answer("flow-end", "ALL", [], stream.unsubscribe_all())
    result = {"verdict": verdict, "tick_answer": asdict(tick), "quote_answer": asdict(quote),
              "ticks": ticks, "quotes": quotes,
              "symbols_with_ticks": sorted(rec.symbols_with("tick")),
              "first_tick": rec.first_tick.copy()}
    out(f"q1_tick_streams: {verdict}: {ticks} tick(s) of {len(result['symbols_with_ticks'])} "
        f"symbol(s), {quotes} quote(s)")
    for symbol, event in sorted(result["first_tick"].items()):
        out(f"  {symbol}: price {event.get('price')} volume {event.get('volume')} "
            f"side {event.get('side')} time {event.get('time')}")
    return result


def cap(stream: TickStream, rec: Recorder, universe: Sequence[str], settings: TickSettings,
        sleep: Sleep, out: Out) -> dict[str, Any]:
    """Question 2: cumulative TICK subscribes up the ladder until a request is refused."""
    pool = list(dict.fromkeys(universe))
    held: list[str] = []
    verdict, refused_answer = CAP_NOT_REACHED, None
    for step in settings.cap_steps:
        target = min(step, len(pool))  # too few symbols: the last step takes them all
        if target <= len(held):
            verdict = UNIVERSE_SHORT
            break
        new = pool[len(held):target]
        for start in range(0, len(new), settings.request_max_symbols):
            chunk = new[start:start + settings.request_max_symbols]
            answer = rec.answer(f"cap-{target}", TICK, chunk, stream.subscribe(chunk, TICK))
            if not answer.accepted:
                verdict, refused_answer = CAP_REFUSED, answer
                break
            held.extend(chunk)
        out(f"q2 TICK {len(held)} symbol(s) held after step {target}: "
            f"{'refused' if refused_answer else 'accepted'}")
        if refused_answer is not None:
            break
        if target < step:
            verdict = UNIVERSE_SHORT
            break
        sleep(settings.step_wait_seconds)
    result: dict[str, Any] = {"verdict": verdict, "held": len(held), "universe": len(pool),
                              "refused_answer": None if refused_answer is None
                              else asdict(refused_answer)}
    if refused_answer is not None and held:
        quote = rec.answer("cap-quote", QUOTE, held[:1], stream.subscribe(held[:1], QUOTE))
        result["quote_at_tick_cap"] = asdict(quote)
    result["symbols_with_ticks"] = len(rec.symbols_with("tick"))
    rec.answer("cap-end", "ALL", [], stream.unsubscribe_all())
    if verdict == CAP_REFUSED:
        out(f"q2_tick_cap: {len(held)} symbol(s) held, the next request {describe(refused_answer)}"
            f"; QUOTE at the cap {describe(StreamAnswer(**result['quote_at_tick_cap']))}"
            if held else f"q2_tick_cap: the first request {describe(refused_answer)}")
    else:
        out(f"q2_tick_cap: {verdict}: {len(held)} symbol(s) held of {len(pool)} available")
    return result


def run(make_stream: Callable[[Callable[[dict[str, Any]], None]], TickStream],
        symbols: Sequence[str], universe: Sequence[str], settings: TickSettings, folder: Path,
        *, now: Clock, sleep: Sleep, out: Out, environment: str) -> int:
    """Both questions on one connection; 0 when both are answered, 4 otherwise."""
    rec = Recorder(folder / EVENTS_FILE, now)
    summary: dict[str, Any] = {"probe": "tick", "environment": environment,
                               "started_at": now().isoformat(), "symbols": list(symbols)}
    stream = make_stream(rec.on_event)
    try:
        stream.start()
        if not stream.wait_connected(settings.connect_wait_seconds):
            summary["q1"] = summary["q2"] = {"verdict": NOT_CONNECTED}
            out(f"not connected within {settings.connect_wait_seconds:g} s")
        else:
            summary["q1"] = flow(stream, rec, symbols, settings, sleep, out)
            summary["q2"] = cap(stream, rec, universe, settings, sleep, out)
    finally:
        stream.stop()
        summary["ended_at"] = now().isoformat()
        (folder / SUMMARY_FILE).write_text(
            json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n", "utf-8")
        rec.close()
    out(f"summary: {folder / SUMMARY_FILE}")
    answered = (summary["q1"]["verdict"] in (STREAMS, NO_TICKS, REFUSED)
                and summary["q2"]["verdict"] in (CAP_REFUSED, CAP_NOT_REACHED))
    return EXIT_ANSWERED if answered else EXIT_NO_ANSWER
