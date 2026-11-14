from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from aperture import config
from aperture.api import contract as api
from aperture.broker.fake import FakeBroker
from aperture.broker.webull import MalformedResponse
from aperture.calendar import NEW_YORK, SessionCalendar
from aperture.config import GateResult
from aperture.data.base import FakeMarketData
from aperture.data.health import StreamHealth
from aperture.engine.core import Engine
from aperture.runtime.market import (
    DISCOVERY,
    DISCOVERY_FAILED,
    STREAM_ERROR,
    STREAM_LOST_EXIT,
    STREAM_TRANSPORT,
    MarketRuntime,
    StreamInbox,
)
from aperture.store import Store
from aperture.types import Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY_11 = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)
SATURDAY = datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK)
INTERVAL = float(CFG.market_data.discovery_interval_seconds)
LOST_AFTER = timedelta(seconds=float(CFG.market_data.stream_lost_exit_seconds))


class Rig:
    def __init__(self, tmp_path: Path, now: datetime = MONDAY_11,
                 source: FakeMarketData | None = None) -> None:
        self.broker = FakeBroker()
        self.store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
        self.engine = Engine(self.store, self.broker, CFG, "mkt00001",
                             lambda: GateResult(True, "t"))
        self.source = source or FakeMarketData()
        self.now = now
        self.clock = 0.0
        self.inbox = StreamInbox()
        self.market = MarketRuntime(self.engine, CFG, self.source,
                                    health=StreamHealth.from_config(CFG), inbox=self.inbox,
                                    capture=None, calendar=CALENDAR, now=lambda: self.now,
                                    monotonic=lambda: self.clock)

    def advance(self, seconds: float) -> None:
        self.clock += seconds
        self.now += timedelta(seconds=seconds)

    def position(self) -> str:
        t = self.engine.open_trade("ABC", D(2), D(10), D("9.80"), D(1))
        self.broker.fill(self.engine.orders(t)[0].client_order_id, D(2), D(10))
        for _ in range(3):
            self.engine.step()
        return t


def quote(at: datetime) -> Quote:
    return Quote("ABC", D("10.00"), D("10.01"), D(1), D(1), at, at)


def test_position_symbol_is_subscribed_and_unsubscribed_after_close(tmp_path):
    rig = Rig(tmp_path, now=SATURDAY)
    rig.market.tick()
    assert rig.source.subscribed == set()
    t = rig.position()
    rig.market.tick()
    assert rig.source.subscribed == {"ABC"}
    assert rig.market.freshness(None).stream is api.StreamHealth.LOST  # never connected
    rig.source.transport(True, rig.now, "up")
    rig.source.push_quote(quote(rig.now))
    fresh = rig.market.freshness(None)
    assert (fresh.stream, fresh.last_quote_at) == (api.StreamHealth.FRESH, rig.now)
    rig.engine.request_exit(t, "test")
    for _ in range(4):
        rig.engine.step()
        for o in rig.broker.open_orders():
            if o.order_type == "MARKET":
                rig.broker.fill(o.client_order_id, D(2), D(10))
    rig.market.tick()
    assert rig.source.subscribed == set()
    assert rig.market.freshness(None).stream is api.StreamHealth.OFF


def test_stream_events_are_journaled_on_the_loop_thread(tmp_path):
    rig = Rig(tmp_path, now=SATURDAY)
    rig.inbox.on_transport(False, rig.now, "disconnected (rc=7)")
    rig.inbox.on_error("subscribe", RuntimeError("POST https://host/path?token=secret"))
    rig.market.tick()
    (transport,) = rig.store.journal(STREAM_TRANSPORT)
    assert transport.body["detail"] == "disconnected (rc=7)"
    (error,) = rig.store.journal(STREAM_ERROR)
    assert error.body == {"where": "subscribe", "error": "RuntimeError"}  # no message kept
    rig.market.tick()
    assert len(rig.store.journal(STREAM_TRANSPORT)) == 1


def test_stream_lost_with_a_position_exits_after_the_grace(tmp_path):
    rig = Rig(tmp_path)
    t = rig.position()
    rig.market.tick()  # subscribes; transport never came up
    assert not rig.engine.trade(t).exit_requested
    rig.source.transport(True, rig.now, "up")
    rig.advance(LOST_AFTER.total_seconds() * 2)
    rig.market.tick()
    assert not rig.engine.trade(t).exit_requested  # up: nothing happens
    rig.source.transport(False, rig.now, "down")
    rig.advance(LOST_AFTER.total_seconds() - 1)
    rig.market.tick()
    assert not rig.engine.trade(t).exit_requested
    rig.advance(1)
    rig.market.tick()
    assert rig.engine.trade(t).exit_requested == STREAM_LOST_EXIT


def test_stream_loss_outside_the_session_does_not_exit(tmp_path):
    rig = Rig(tmp_path, now=SATURDAY)
    t = rig.position()
    rig.market.tick()
    rig.advance(LOST_AFTER.total_seconds() * 10)
    rig.market.tick()
    assert not rig.engine.trade(t).exit_requested


def test_discovery_runs_on_its_interval_in_the_session(tmp_path):
    rig = Rig(tmp_path)
    rig.market.tick()
    calls = len(rig.source.calls)
    assert calls > 0 and len(rig.store.journal(DISCOVERY)) == 1
    rig.advance(INTERVAL - 1)
    rig.market.tick()
    assert len(rig.source.calls) == calls
    rig.advance(1)
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 2
    assert rig.market.discovery is not None


def test_discovery_waits_while_protection_needs_the_loop(tmp_path):
    rig = Rig(tmp_path)
    rig.market.tick(discover=False)
    assert rig.source.calls == [] and rig.store.journal(DISCOVERY) == []
    rig.market.tick()  # due at once once allowed again
    assert len(rig.store.journal(DISCOVERY)) == 1


def test_discovery_never_runs_outside_the_session(tmp_path):
    rig = Rig(tmp_path, now=SATURDAY)
    rig.market.tick()
    assert rig.source.calls == [] and rig.market.discovery is None


class Malformed(FakeMarketData):
    failures = 1

    def screener(self, kind):
        if self.failures:
            self.failures -= 1
            raise MalformedResponse("screener: missing field")
        return super().screener(kind)


def test_discovery_failure_is_journaled_and_retried_next_interval(tmp_path):
    rig = Rig(tmp_path, source=Malformed())
    rig.market.tick()
    (failed,) = rig.store.journal(DISCOVERY_FAILED)
    assert failed.body == {"error": "MalformedResponse"}
    rig.advance(INTERVAL)
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 1


def test_bot_adapters_share_one_transport_and_budget(monkeypatch):
    from aperture.broker import webull

    class Dummy:
        def __init__(self, *args):
            self.args = args

    monkeypatch.setattr(webull, "SdkTransport", Dummy)
    monkeypatch.setattr(webull, "SdkQuoteStream", Dummy)
    broker, market, stream = webull.bot_adapters(CFG, lambda where, exc: None)
    assert broker._transport is market._transport
    assert broker._budget is market._budget
    session = stream.args[1]
    assert session._budget is broker._budget
    assert market._stream is stream


class SessionSource(FakeMarketData):
    """FakeMarketData whose stream is the real SDK-free StreamSession."""

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    def subscribe(self, symbols, listener):
        self.session.subscribe(symbols, listener)

    def unsubscribe(self, symbols):
        self.session.unsubscribe(symbols)


class Budget:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail

    def take(self, path):
        if self.fail is not None:
            raise self.fail


def real_session(budget):
    from aperture.broker.webull import StreamSession
    session = StreamSession(budget, lambda: MONDAY_11, lambda where, exc: None, max_symbols=100)
    session.bind(lambda symbols: None, lambda symbols: None, lambda: None)
    return session


def test_connect_before_the_first_subscription_is_seen_as_up(tmp_path):
    session = real_session(Budget())
    rig = Rig(tmp_path, source=SessionSource(session))
    session.on_connected()  # the stream connects at startup, before anything is subscribed
    rig.market.tick()
    rig.advance(LOST_AFTER.total_seconds() * 4)
    t = rig.position()
    rig.market.tick()  # first subscription on a live connection
    assert session.up and rig.market.health.transport.up
    rig.advance(LOST_AFTER.total_seconds() * 2)
    rig.market.tick()
    assert rig.engine.trade(t).exit_requested is None


def test_bind_before_start_sees_the_first_connect(tmp_path):
    session = real_session(Budget())
    rig = Rig(tmp_path, source=SessionSource(session))
    rig.market.bind()
    session.on_connected()
    assert rig.market.health.transport.up


def test_a_failing_subscribe_is_journaled_never_raised(tmp_path):
    from aperture.broker.webull import WebullMarketData

    session = real_session(Budget(fail=RuntimeError("bare SDK exception")))
    adapter = WebullMarketData.__new__(WebullMarketData)
    adapter._stream = session
    source = SessionSource(session)
    source.subscribe = adapter.subscribe
    source.unsubscribe = adapter.unsubscribe
    rig = Rig(tmp_path, source=source)
    session.on_connected()
    rig.position()
    rig.market.tick()  # must not raise into the service loop
    assert rig.market.subscribed == set()  # tried again next pass
    rig.market.tick()
    errors = rig.store.journal(STREAM_ERROR)
    assert {"where": "subscribe", "error": "MarketDataError"} in [e.body for e in errors]


def test_best_candidates_are_subscribed_up_to_the_configured_count(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from aperture.runtime.market import DiscoveryState

    rig = Rig(tmp_path, now=SATURDAY)
    limit = 2
    rig.market.config = replace(CFG, market_data=replace(CFG.market_data,
                                                         candidate_subscriptions=limit))
    best_first = [SimpleNamespace(instrument=SimpleNamespace(symbol=s))
                  for s in ("AAA", "BBB", "CCC")]
    rig.market.discovery = DiscoveryState(rig.now, "x", tuple(best_first))  # type: ignore[arg-type]
    rig.market.watch = {"WAT"}
    rig.market.tick()
    assert rig.source.subscribed == {"WAT"}  # transport not up: no candidates
    rig.source.transport(True, rig.now, "up")
    rig.market.tick()
    assert rig.source.subscribed == {"AAA", "BBB", "WAT"}
    rig.market.discovery = DiscoveryState(rig.now, "x", ())
    rig.market.tick()
    assert rig.source.subscribed == {"WAT"}


def test_refused_candidates_never_take_the_position_off_the_stream(tmp_path):
    """Real StreamSession whose broker refuses more than K symbols per connection."""
    from dataclasses import replace
    from types import SimpleNamespace

    from aperture.broker.webull import StreamSession
    from aperture.runtime.market import DiscoveryState

    limit = 2
    sent: set[str] = set()

    def send_subscribe(symbols):
        if len(sent | set(symbols)) > limit:
            raise RuntimeError("symbol limit")
        sent.update(symbols)

    session = StreamSession(Budget(), lambda: MONDAY_11, lambda where, exc: None, max_symbols=100)
    session.bind(send_subscribe, lambda symbols: sent.difference_update(symbols),
                 lambda: (sent.clear(), session.on_connected()))
    rig = Rig(tmp_path, source=SessionSource(session))
    rig.market.config = replace(CFG, market_data=replace(CFG.market_data,
                                                         candidate_subscriptions=3))
    rig.market.bind()
    session.on_connected()
    t = rig.position()
    rig.market.discovery = DiscoveryState(rig.now, "x", tuple(
        SimpleNamespace(instrument=SimpleNamespace(symbol=s)) for s in ("AAA", "BBB", "CCC")))
    rig.market.tick()
    assert rig.market.subscribed == {"ABC"}  # candidates refused as one request
    assert session.symbols == {"ABC"} and session.up  # reconnected without them
    assert rig.market.health.transport.up
    rig.advance(LOST_AFTER.total_seconds() * 2)
    rig.market.tick()  # candidates are off until candidate_retry_seconds
    assert rig.engine.trade(t).exit_requested is None
    assert rig.market.subscribed == {"ABC"}
    rig.advance(float(CFG.market_data.candidate_retry_seconds))
    rig.market.tick()
    assert rig.market.subscribed == {"ABC"} and session.up  # refused again, still up


def test_candidates_are_dropped_while_the_transport_is_down(tmp_path):
    """Review probe #43: candidates queued while down must not loop the reconnect."""
    from dataclasses import replace
    from types import SimpleNamespace

    from aperture.broker.webull import StreamSession
    from aperture.runtime.market import DiscoveryState

    limit, sent = 2, set()
    reconnects = []

    def send_subscribe(symbols):
        if len(sent | set(symbols)) > limit:
            raise RuntimeError("symbol limit")
        sent.update(symbols)

    clock = [MONDAY_11]
    session = StreamSession(Budget(), lambda: clock[0], lambda where, exc: None, max_symbols=100)
    session.bind(send_subscribe, lambda symbols: sent.difference_update(symbols),
                 lambda: reconnects.append(1))  # the SDK reconnects later
    rig = Rig(tmp_path, source=SessionSource(session))
    clock[0] = rig.now
    rig.market.config = replace(CFG, market_data=replace(CFG.market_data,
                                                         candidate_subscriptions=3))
    rig.market.bind()
    session.on_connected()
    t = rig.position()
    rig.market.tick()  # ABC subscribed while up
    session.on_disconnected("network blip")
    rig.market.discovery = DiscoveryState(rig.now, "x", tuple(
        SimpleNamespace(instrument=SimpleNamespace(symbol=s)) for s in ("AAA", "BBB", "CCC")))
    rig.market.tick()  # down: no candidates
    assert rig.market.subscribed == {"ABC"} and session.symbols == {"ABC"}
    for _ in range(5):  # the SDK reconnects every 10 s
        sent.clear()
        session.on_connected()
    assert session.up and rig.market.health.transport.up
    rig.advance(LOST_AFTER.total_seconds() * 2)
    clock[0] = rig.now
    rig.market.tick()  # up again: candidates tried in their own request, refused, rolled back
    assert rig.engine.trade(t).exit_requested is None
    assert rig.market.subscribed == {"ABC"} and session.symbols == {"ABC"}
    sent.clear()
    session.on_connected()  # the SDK reconnect sends only the position
    assert session.up
    rig.advance(LOST_AFTER.total_seconds() * 2)
    clock[0] = rig.now
    rig.market.tick()  # candidates are in their retry pause
    assert rig.engine.trade(t).exit_requested is None
