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
    session = StreamSession(budget, lambda: MONDAY_11, lambda where, exc: None)
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
