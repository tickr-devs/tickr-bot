"""Probe M3 (engine/trade_events_probe.py, `tickr probe-trade-events`) against a fake event
source and fake order reads. No Webull, no network, no order."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tickr import cli
from tickr import config as config_module
from tickr.broker.base import BrokerError, OrderNotFound
from tickr.broker.webull import (
    AdapterConfigError,
    SdkTradeEventsSource,
    TradeEventsSettings,
    trade_event_record,
)
from tickr.engine import trade_events_probe as tep
from tickr.engine.core import ORDER_OBSERVED
from tickr.store import Store
from tickr.types import OrderDetail, OrderState, OrderType, Side

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
SETTINGS = tep.ProbeSettings(seconds=30, reconnect_seconds=0, poll_seconds=3,
                             detail_reads_per_poll=1)
SUBSCRIBED = {"kind": "event", "event_type": 0, "event_name": "SubscribeSuccess",
              "subscribe_type": 7, "content_type": "text/plain", "payload": "",
              "request_id": "r0", "timestamp": 1}


def order_event(cid: str, status: str) -> dict:
    payload = json.dumps({"data": {"client_order_id": cid, "order_status": status}})
    return {"kind": "event", "event_type": 1024, "event_name": None, "subscribe_type": 1,
            "content_type": tep.JSON_CONTENT, "payload": payload, "request_id": "r1",
            "timestamp": 2}


def ended(code: str, resubscribe: bool) -> dict:
    return {"kind": "ended", "code": code, "detail": "", "resubscribe": resubscribe}


def detail(cid: str, status: str, state: OrderState, filled: str) -> OrderDetail:
    return OrderDetail(cid, "SOFI", Side.BUY, OrderType.LIMIT, Decimal(1), Decimal(filled),
                       None, state, status)


class Clock:
    """Time moves only when the probe sleeps; with `gate`, the first sleep waits for it, so
    the listener thread has done its part before the run's time passes."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.at = T0
        self.lock = threading.Lock()
        self.gate = gate

    def now(self) -> datetime:
        with self.lock:
            return self.at

    def sleep(self, seconds: float) -> None:
        if self.gate is not None:
            self.gate.wait(5)
        with self.lock:
            self.at += timedelta(seconds=seconds)


class Source:
    """Yields one scripted connection per `connect`; the first waits until the first poll
    began, so an event-named order is tracked before the polls that follow."""

    def __init__(self, connections: list[list[dict]], start: threading.Event | None = None):
        self.connections = connections
        self.start = start
        self.asked: list[float] = []
        self.closed = False
        self.done = threading.Event()  # set once every scripted connection was delivered

    def connect(self, seconds: float):
        self.asked.append(seconds)
        if self.start is not None:
            self.start.wait(5)
        yield from (self.connections.pop(0) if self.connections else [ended("X", False)])
        if not self.connections:
            self.done.set()

    def close(self) -> None:
        self.closed = True


class Reads:
    """Open orders list ap-1 working on the first poll only; Detail says ap-1 filled and the
    event-named manual-2 cancelled."""

    def __init__(self, polled: threading.Event, delivered: threading.Event) -> None:
        self.polled, self.delivered = polled, delivered
        self.calls: list[str] = []

    def open_orders(self) -> list[OrderDetail]:
        self.calls.append("open")
        if not self.polled.is_set():
            self.polled.set()
            self.delivered.wait(5)
            return [detail("ap-1", "SUBMITTED", OrderState.ACCEPTED, "0")]
        return []

    def order_detail(self, cid: str) -> OrderDetail:
        self.calls.append(cid)
        if cid == "ap-1":
            return detail(cid, "FILLED", OrderState.FILLED, "1")
        if cid == "manual-2":
            return detail(cid, "CANCELLED", OrderState.CANCELLED, "0")
        raise OrderNotFound(cid)


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_poll_mode_records_events_and_changes_and_compares_them(tmp_path):
    clock, polled, delivered = Clock(), threading.Event(), threading.Event()

    class Delivering(Source):
        def connect(self, seconds):
            for item in super().connect(seconds):
                yield item
                if item.get("event_type") == 1024:
                    delivered.set()

    source = Delivering([[SUBSCRIBED, order_event("ap-1", "FILLED"),
                          order_event("manual-2", "CANCELLED"), ended("DEADLINE_EXCEEDED",
                                                                      False)]], polled)
    reads = Reads(polled, delivered)
    out: list[str] = []
    code = tep.run(source, reads, None, SETTINGS, tmp_path, now=clock.now, sleep=clock.sleep,
                   out=out.append, environment="sandbox")
    assert code == tep.EXIT_ANSWERED
    assert source.asked == [30.0] and source.closed
    events = rows(tmp_path / tep.EVENTS_FILE)
    assert [e["kind"] for e in events] == ["connect", "event", "event", "event", "ended"]
    assert all("received_at" in e for e in events)
    assert json.loads(events[2]["payload"])["data"]["client_order_id"] == "ap-1"  # raw
    polls = rows(tmp_path / tep.POLLS_FILE)
    seen = {(p["source"], p["client_order_id"], p["raw_status"]) for p in polls}
    assert ("open-orders", "ap-1", "SUBMITTED") in seen
    assert ("open-orders", "ap-1", "unlisted") in seen
    assert ("detail", "ap-1", "FILLED") in seen
    assert ("detail", "manual-2", "CANCELLED") in seen  # tracked because an event named it
    # Detail stops once it shows the order finished and the open orders no longer list it.
    assert reads.calls.count("ap-1") == 2 and reads.calls.count("manual-2") == 1
    summary = json.loads((tmp_path / tep.SUMMARY_FILE).read_text("utf-8"))
    assert summary["mode"] == tep.POLL_MODE and summary["subscribed"] == 1
    assert summary["events_by_type"] == {"1024": 2, "SubscribeSuccess": 1}
    assert summary["stream_ends"][0]["code"] == "DEADLINE_EXCEEDED"
    assert summary["orders"]["ap-1"]["events"] == 1
    assert set(summary["orders"]["ap-1"]["lead_seconds"]) == {"detail", "open-orders"}
    assert any(line.startswith("m3_order ap-1") for line in out)


def test_a_stream_the_sdk_would_resubscribe_is_subscribed_again(tmp_path):
    source = Source([[SUBSCRIBED, ended("UNAVAILABLE", True)],
                     [SUBSCRIBED, ended("PERMISSION_DENIED", False)]])
    clock = Clock(source.done)
    tep.run(source, None, lambda since: [], SETTINGS, tmp_path, now=clock.now,
            sleep=clock.sleep, out=lambda _: None, environment="sandbox")
    events = rows(tmp_path / tep.EVENTS_FILE)
    assert [e["connection"] for e in events if e["kind"] == "connect"] == [1, 2]
    summary = json.loads((tmp_path / tep.SUMMARY_FILE).read_text("utf-8"))
    assert [e["code"] for e in summary["stream_ends"]] == ["UNAVAILABLE", "PERMISSION_DENIED"]


def test_no_accepted_subscription_proves_nothing(tmp_path):
    refused = {**SUBSCRIBED, "event_type": 2, "event_name": "AuthError"}
    source = Source([[refused, ended("STREAM_CLOSED", False)]])
    clock = Clock(source.done)
    code = tep.run(source, None, lambda since: [], SETTINGS, tmp_path, now=clock.now,
                   sleep=clock.sleep, out=lambda _: None, environment="sandbox")
    assert code == tep.EXIT_NO_ANSWER


def test_journal_mode_sends_no_read_and_compares_with_the_bots_rows(tmp_path):
    source = Source([[SUBSCRIBED, order_event("ap-9", "FILLED"), ended("X", False)]])
    clock = Clock(source.done)
    later = (T0 + timedelta(seconds=4)).isoformat()
    journal = [{"at": later, "source": "bot-journal", "client_order_id": "ap-9",
                "raw_status": "FILLED", "filled_quantity": "1"}]
    tep.run(source, None, lambda since: journal if since == T0 else [], SETTINGS, tmp_path,
            now=clock.now, sleep=clock.sleep, out=lambda _: None, environment="sandbox")
    summary = json.loads((tmp_path / tep.SUMMARY_FILE).read_text("utf-8"))
    assert summary["mode"] == tep.JOURNAL_MODE
    lead = summary["orders"]["ap-9"]["lead_seconds"]["bot-journal"]
    assert len(lead) == 1 and lead[0] is not None and lead[0] >= 0


def test_a_failed_poll_is_recorded_not_raised(tmp_path):
    class Failing:
        def open_orders(self):
            raise BrokerError("read failed")

        def order_detail(self, cid):  # pragma: no cover - nothing is tracked
            raise AssertionError(cid)

    clock = Clock()
    tep.run(Source([[ended("X", False)]]), Failing(), None, SETTINGS, tmp_path, now=clock.now,
            sleep=clock.sleep, out=lambda _: None, environment="sandbox")
    polls = rows(tmp_path / tep.POLLS_FILE)
    assert polls and all(p["error"] == "BrokerError" for p in polls)


def test_order_ids_are_found_anywhere_in_a_json_payload_only():
    payload = json.dumps({"a": [{"client_order_id": "x"}, {"b": {"client_order_id": "y"}}]})
    assert tep.order_ids(payload, tep.JSON_CONTENT) == ["x", "y"]
    assert tep.order_ids(payload, "text/plain") == []
    assert tep.order_ids("{not json", tep.JSON_CONTENT) == []


def test_lead_is_positive_when_the_event_came_first_and_negative_when_later():
    t = [T0 + timedelta(seconds=s) for s in (0, 10, 20)]
    assert tep.lead_seconds([t[1]], [t[0]]) == [10.0]
    assert tep.lead_seconds([t[0]], [t[1]]) == [-10.0]
    assert tep.lead_seconds([t[0], t[2]], [t[1]]) == [None, 10.0]
    assert tep.lead_seconds([t[1]], []) == [None]


# --- Webull side, without the network -------------------------------------------------------

def test_a_response_becomes_a_raw_record_without_the_account_id():
    pb = pytest.importorskip("webull.trade.events.events_pb2")
    response = pb.SubscribeResponse(eventType=0, subscribeType=7, contentType="text/plain",
                                    payload="ok for ACC123", requestId="r", timestamp=5)
    record = trade_event_record(response, {0: "SubscribeSuccess"},
                                lambda text: text.replace("ACC123", "<account>"))
    assert record == {"kind": "event", "event_type": 0, "event_name": "SubscribeSuccess",
                      "subscribe_type": 7, "content_type": "text/plain",
                      "payload": "ok for <account>", "request_id": "r", "timestamp": 5}


def test_the_source_refuses_a_host_off_the_allowlist_before_reading_credentials():
    settings = TradeEventsSettings("evil.example", 443, frozenset({"events-api.webull.com"}),
                                   "NO_SUCH_KEY", "NO_SUCH_SECRET", "NO_SUCH_ACCOUNT")
    with pytest.raises(AdapterConfigError, match="allowlist"):
        SdkTradeEventsSource(settings)


# --- Command line ---------------------------------------------------------------------------

def config_copy(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    bot = tmp_path / "config" / "bot.toml"
    bot.write_text((ROOT / "config" / "bot.toml").read_text("utf-8"), "utf-8")
    return bot


def test_cli_polls_with_the_bot_stopped(tmp_path, monkeypatch, capsys):
    bot, clock = config_copy(tmp_path), Clock()
    adapters: list[object] = []

    class NoOrders:
        def open_orders(self):
            return []

        def order_detail(self, cid):  # pragma: no cover
            raise AssertionError(cid)

    monkeypatch.setattr(cli, "TRADE_EVENTS_SOURCE", lambda cfg: Source([[SUBSCRIBED]]))
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", lambda cfg: adapters.append(cfg) or
                        (NoOrders(), None))
    monkeypatch.setattr(cli, "NOW", clock.now)
    monkeypatch.setattr(cli, "SLEEP", clock.sleep)
    assert cli.main(["--config", str(bot), "probe-trade-events", "--minutes", "1"]) == 0
    assert len(adapters) == 1
    assert "bot stopped" in capsys.readouterr().out
    assert cli.main(["--config", str(bot), "probe-trade-events", "--minutes", "0"]) == 2


def test_cli_beside_a_running_bot_reads_its_journal_and_sends_nothing(tmp_path, monkeypatch,
                                                                    capsys):
    bot, clock = config_copy(tmp_path), Clock()
    cfg = config_module.load(bot)
    database, lock = cli.data_paths(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    store = Store.open(database, busy_timeout_seconds=1, now=lambda: T0 + timedelta(seconds=5))
    with store.transaction():
        store.append(ORDER_OBSERVED, {"state": "FILLED", "raw_status": "FILLED",
                                      "filled_quantity": "1"}, key="ap-3")
    store.close()

    def no_adapters(cfg):  # pragma: no cover - must not be called
        raise AssertionError("no HTTP client beside the bot")

    monkeypatch.setattr(cli, "TRADE_EVENTS_SOURCE",
                        lambda cfg: Source([[SUBSCRIBED, order_event("ap-3", "FILLED")]]))
    monkeypatch.setattr(cli, "PROBE_ADAPTERS", no_adapters)
    monkeypatch.setattr(cli, "NOW", clock.now)
    monkeypatch.setattr(cli, "SLEEP", clock.sleep)
    assert cli.main(["--config", str(bot), "probe-trade-events"]) == 0
    printed = capsys.readouterr().out
    assert "its journal is the polling side" in printed and "m3_order ap-3" in printed


def test_cli_refuses_when_the_source_cannot_be_built(tmp_path, monkeypatch, capsys):
    bot = config_copy(tmp_path)

    def missing(cfg):
        raise AdapterConfigError("credential variable WEBULL_SANDBOX_APP_KEY is not set")

    monkeypatch.setattr(cli, "TRADE_EVENTS_SOURCE", missing)
    assert cli.main(["--config", str(bot), "probe-trade-events"]) == 3
    assert "WEBULL_SANDBOX_APP_KEY is not set" in capsys.readouterr().err
