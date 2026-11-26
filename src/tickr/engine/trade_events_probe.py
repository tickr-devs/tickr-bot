"""Probe M3, read only: the account's gRPC trade events next to the polling the bot relies on.

Backlog M3 (gRPC trade events instead of polling) waits for these answers
(docs/webull-facts.md, section M3 trade events):

1. Latency: how much earlier than a Detail / open-orders poll does an event say that an order
   changed (accepted, filled, cancelled)?
2. Missed events: does every change polling sees also arrive as an event?
3. Reconnect: how often does the stream end, with which gRPC code, and does subscribing again
   work?
4. Ordering: do an order's events arrive in the order its changes happened?

For the whole run (`--minutes`, else `[probe] trade_events_minutes`) it holds one
subscription and records every message raw (the account id replaced by `<account>`) with the
time it was received, in `events.jsonl`. When the stream ends with a code the SDK itself
would subscribe again on, it subscribes again after `[probe] trade_events_reconnect_seconds`.

Polling, into `polls.jsonl`, one row whenever an order's status or filled quantity changes
for a source:
- bot stopped (`poll` mode): the probe holds the lock and reads open orders once per
  `[orders] detail_poll_seconds` and Detail for up to `[orders] detail_reads_per_step` of the
  orders it has seen (listed, dropped from the list, or named by an event's
  `client_order_id`) until Detail shows them finished: the bot's own cadence. The owner places
  a manual order meanwhile.
- bot running (`bot-journal` mode): the probe sends no HTTP read at all (the route limits are
  per App Key, C2, and the bot's reads must keep their budget) and, at the end, copies the
  bot's own `order-observed` journal rows of the run (the time its polling saw each change).

It never sends an order. `summary.json` ends the run: events per type, every stream end, and
per order a timeline of events naming it and polled changes, with each change's lead (seconds
the first event naming the order came before the poll that saw the change; negative when the
event came after it).
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tickr.broker.base import BrokerError
from tickr.types import OrderDetail

EVENTS_FILE = "events.jsonl"
POLLS_FILE = "polls.jsonl"
SUMMARY_FILE = "summary.json"

POLL_MODE = "poll"
JOURNAL_MODE = "bot-journal"
OPEN_ORDERS = "open-orders"
DETAIL = "detail"
EVENT = "event"
ENDED = "ended"
SUBSCRIBED = "SubscribeSuccess"  # SDK 3.0.3 EventType name of an accepted subscription
JSON_CONTENT = "application/json"  # SDK 3.0.3 contentType of a JSON payload
ORDER_ID_FIELD = "client_order_id"

EXIT_ANSWERED = 0
EXIT_NO_ANSWER = 4

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]
Out = Callable[[str], None]


class TradeEventSource(Protocol):
    def connect(self, seconds: float) -> Iterable[dict[str, Any]]: ...

    def close(self) -> None: ...


class OrderReads(Protocol):
    def open_orders(self) -> list[OrderDetail]: ...

    def order_detail(self, client_order_id: str) -> OrderDetail: ...


@dataclass(frozen=True)
class ProbeSettings:
    seconds: float
    reconnect_seconds: float
    poll_seconds: float
    detail_reads_per_poll: int


class Recorder:
    """Appends events and polled changes as JSON lines; the listener thread and the poller
    write at once, so both go under one lock."""

    def __init__(self, folder: Path) -> None:
        self._events = folder / EVENTS_FILE
        self._polls = folder / POLLS_FILE
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.polls: list[dict[str, Any]] = []

    def event(self, row: dict[str, Any]) -> None:
        with self._lock:
            row = {"seq": len(self.events), **row}
            self.events.append(row)
            _append(self._events, row)

    def poll(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.polls.append(row)
            _append(self._polls, row)


def _append(path: Path, row: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def order_ids(payload: str, content_type: str) -> list[str]:
    """Every `client_order_id` value anywhere in a JSON payload (the field name of every order
    row Webull answers); nothing for any other payload. The payload's shape is not assumed."""
    if content_type != JSON_CONTENT:
        return []
    try:
        body = json.loads(payload)
    except ValueError:
        return []
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == ORDER_ID_FIELD and isinstance(value, str) and value:
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return found


class Poller:
    """The bot's polling, read only: open orders each poll, then Detail for up to
    `detail_reads` tracked orders in turn. A row goes to `polls.jsonl` when an order's status
    or filled quantity changes for that source (and when it drops out of the open orders)."""

    def __init__(self, reads: OrderReads, recorder: Recorder, detail_reads: int,
                 now: Clock) -> None:
        self._reads = reads
        self._recorder = recorder
        self._detail_reads = detail_reads
        self._now = now
        self._lock = threading.Lock()
        self._tracked: list[str] = []
        self._listed: set[str] = set()
        self._last: dict[tuple[str, str], tuple[str, str]] = {}

    def track(self, client_order_id: str) -> None:
        with self._lock:
            if client_order_id not in self._tracked:
                self._tracked.append(client_order_id)

    def _seen(self, source: str, order: OrderDetail, at: datetime) -> None:
        state = (order.raw_status, str(order.filled_quantity))
        if self._last.get((source, order.client_order_id)) != state:
            self._last[(source, order.client_order_id)] = state
            self._recorder.poll({"at": at.isoformat(), "source": source,
                                 "client_order_id": order.client_order_id,
                                 "symbol": order.symbol, "raw_status": order.raw_status,
                                 "state": str(order.state),
                                 "filled_quantity": str(order.filled_quantity)})

    def poll(self) -> None:
        at = self._now()
        try:
            listed = self._reads.open_orders()
        except BrokerError as exc:
            self._recorder.poll({"at": at.isoformat(), "source": OPEN_ORDERS,
                                 "error": type(exc).__name__})
            listed = None
        if listed is not None:
            ids = {order.client_order_id for order in listed}
            for order in listed:
                self.track(order.client_order_id)
                self._seen(OPEN_ORDERS, order, at)
            for gone in sorted(self._listed - ids):
                self._last.pop((OPEN_ORDERS, gone), None)
                self._recorder.poll({"at": at.isoformat(), "source": OPEN_ORDERS,
                                     "client_order_id": gone, "raw_status": "unlisted"})
            self._listed = ids
        with self._lock:
            due = self._tracked[:self._detail_reads]
            self._tracked = self._tracked[self._detail_reads:] + due
        for cid in due:
            at = self._now()
            try:
                detail = self._reads.order_detail(cid)
            except BrokerError as exc:
                self._recorder.poll({"at": at.isoformat(), "source": DETAIL,
                                     "client_order_id": cid, "error": type(exc).__name__})
                continue
            self._seen(DETAIL, detail, at)
            if detail.state.terminal and cid not in self._listed:
                with self._lock:
                    self._tracked.remove(cid)


def _listen(source: TradeEventSource, settings: ProbeSettings, end: datetime, now: Clock,
            recorder: Recorder, stop: threading.Event,
            on_order: Callable[[str], None]) -> None:
    connection = 0
    while not stop.is_set():
        left = (end - now()).total_seconds()
        if left <= 0:
            return
        connection += 1
        recorder.event({"kind": "connect", "connection": connection,
                        "received_at": now().isoformat()})
        ended: Mapping[str, Any] | None = None
        for item in source.connect(left):
            received = now()  # stamped before anything else is done with the message
            recorder.event({**item, "connection": connection,
                            "received_at": received.isoformat()})
            if item.get("kind") == ENDED:
                ended = item
            else:
                for cid in order_ids(str(item.get("payload") or ""),
                                     str(item.get("content_type") or "")):
                    on_order(cid)
        if ended is None or not ended.get("resubscribe"):
            return
        if stop.wait(settings.reconnect_seconds):
            return


def lead_seconds(changes: Sequence[datetime], events: Sequence[datetime]) -> list[float | None]:
    """For each polled change of one order and source (oldest first): seconds from the first
    event naming the order after the previous change to this change (positive: the event came
    first); else, from the events no change took that way, from this change to the first one
    before the next change (negative: the event came later); None when no event is left
    between the neighbouring changes. Each event counts for one change at most."""
    leads: list[float | None] = [None] * len(changes)
    used: set[int] = set()
    for i, at in enumerate(changes):
        before = changes[i - 1] if i else None
        early = [j for j, e in enumerate(events) if (before is None or e > before) and e <= at]
        if early:
            first = min(early, key=lambda j: events[j])
            used.add(first)
            leads[i] = (at - events[first]).total_seconds()
    for i, at in enumerate(changes):
        if leads[i] is not None:
            continue
        after = changes[i + 1] if i + 1 < len(changes) else None
        late = [j for j, e in enumerate(events)
                if j not in used and e > at and (after is None or e <= after)]
        if late:
            first = min(late, key=lambda j: events[j])
            used.add(first)
            leads[i] = -(events[first] - at).total_seconds()
    return leads


def summarize(events: Sequence[Mapping[str, Any]],
              polls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Events per type, every stream end, and per order seen by polling: its timeline
    (events naming it by `client_order_id` or by its id anywhere in the payload) and the lead
    of every polled change, per source."""
    by_type = Counter(str(e.get("event_name") or e.get("event_type"))
                      for e in events if e.get("kind") == EVENT)
    ends = [{k: e.get(k) for k in ("connection", "received_at", "code", "detail",
                                     "resubscribe")}
            for e in events if e.get("kind") == ENDED]
    orders: dict[str, Any] = {}
    for cid in sorted({str(p["client_order_id"]) for p in polls if "client_order_id" in p}):
        named = [e for e in events if e.get("kind") == EVENT
                 and cid in str(e.get("payload") or "")]
        event_times = [datetime.fromisoformat(str(e["received_at"])) for e in named]
        timeline = [{"at": e["received_at"], "source": EVENT, "seq": e.get("seq"),
                     "event_type": e.get("event_type")} for e in named]
        leads: dict[str, list[float | None]] = {}
        for source in sorted({str(p["source"]) for p in polls
                              if p.get("client_order_id") == cid and "error" not in p}):
            rows = [p for p in polls if p.get("client_order_id") == cid
                    and p.get("source") == source and "error" not in p]
            leads[source] = lead_seconds(
                [datetime.fromisoformat(str(p["at"])) for p in rows], event_times)
            timeline += [{"at": p["at"], "source": source, "raw_status": p.get("raw_status"),
                          "filled_quantity": p.get("filled_quantity")} for p in rows]
        orders[cid] = {"events": len(named), "lead_seconds": leads,
                       "timeline": sorted(timeline, key=lambda r: str(r["at"]))}
    return {"events_by_type": dict(sorted(by_type.items())), "stream_ends": ends,
            "subscribed": by_type.get(SUBSCRIBED, 0), "orders": orders}


def run(source: TradeEventSource, reads: OrderReads | None,
        journal: Callable[[datetime], list[dict[str, Any]]] | None, settings: ProbeSettings,
        folder: Path, *, now: Clock, sleep: Sleep, out: Out, environment: str) -> int:
    """Listen for `settings.seconds`, polling with `reads` (bot stopped) or, without it,
    copying the bot's journal rows of the run at the end. Exit 0 when a subscription was
    accepted at least once, 4 when none was (nothing proven)."""
    started = now()
    end = started + timedelta(seconds=settings.seconds)
    recorder = Recorder(folder)
    poller = None if reads is None else Poller(reads, recorder, settings.detail_reads_per_poll,
                                               now)
    stop = threading.Event()
    listener = threading.Thread(
        target=_listen, name="tickr-trade-events-probe", daemon=True,
        args=(source, settings, end, now, recorder, stop,
              poller.track if poller is not None else (lambda _cid: None)))
    listener.start()
    try:
        while now() < end:
            if poller is not None:
                poller.poll()
            sleep(min(settings.poll_seconds, max((end - now()).total_seconds(), 0)))
    finally:
        stop.set()
        source.close()
        listener.join()
    if journal is not None:
        for row in journal(started):
            recorder.poll(row)
    summary = {"probe": "trade-events", "environment": environment,
               "mode": POLL_MODE if reads is not None else JOURNAL_MODE,
               "started_at": started.isoformat(), "ended_at": now().isoformat(),
               "seconds": settings.seconds,
               **summarize(recorder.events, recorder.polls)}
    (folder / SUMMARY_FILE).write_text(json.dumps(summary, indent=1, sort_keys=True,
                                                  default=str) + "\n", "utf-8")
    out(f"m3_events: {summary['events_by_type']}; stream ends "
        f"{[(e['code'], e['resubscribe']) for e in summary['stream_ends']]}")
    for cid, order in summary["orders"].items():
        out(f"m3_order {cid}: {order['events']} event(s); lead seconds by source "
            f"{order['lead_seconds']}")
    return EXIT_ANSWERED if summary["subscribed"] else EXIT_NO_ANSWER
