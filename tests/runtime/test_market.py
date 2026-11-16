from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from tickr import config
from tickr.api import contract as api
from tickr.broker.fake import FakeBroker
from tickr.broker.webull import MalformedResponse
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.data.base import FakeMarketData, ScreenerKind, ScreenerRow, ScreenerSession
from tickr.data.capture import DISCOVERY_FILE, CaptureWriter
from tickr.data.discovery import NoStrategy
from tickr.data.health import StreamHealth
from tickr.data.todaybars import TodayBars
from tickr.engine.core import Engine
from tickr.observers.loader import load_active
from tickr.runtime.market import (
    DISCOVERY,
    DISCOVERY_FAILED,
    REFUSED_SYMBOLS_UNREAD,
    STREAM_ERROR,
    STREAM_LOST_EXIT,
    STREAM_SUBSCRIPTIONS,
    STREAM_TRANSPORT,
    MarketRuntime,
    StreamInbox,
)
from tickr.store import Store
from tickr.types import Quote

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load(ROOT / "config" / "bot.toml")
CALENDAR = SessionCalendar.load((ROOT / CFG.session.calendar_file).parent)
MONDAY_11 = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)
SATURDAY = datetime(2026, 11, 14, 11, 0, tzinfo=NEW_YORK)
INTERVAL = float(CFG.market_data.discovery_interval_seconds)
LOST_AFTER = timedelta(seconds=float(CFG.market_data.stream_lost_exit_seconds))


def in_place(run):
    """Run a discovery pass in place, so `tick` journals it in the same pass."""
    run()


class Rig:
    def __init__(self, tmp_path: Path, now: datetime = MONDAY_11,
                 source: FakeMarketData | None = None, spawn=in_place,
                 capture=None) -> None:
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
                                    capture=capture, calendar=CALENDAR,
                                    today_bars=TodayBars(), now=lambda: self.now,
                                    monotonic=lambda: self.clock, spawn=spawn)
        self.market.screen = load_active(CFG).strategy  # set by the caller once loaded

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


def test_discovery_starts_only_in_a_minutes_first_pass_after_its_interval(tmp_path):
    rig = Rig(tmp_path, now=MONDAY_11 + timedelta(seconds=25))  # startup mid-minute
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 1
    rig.advance(INTERVAL - 25)  # 11:01:00: first pass of a minute, but 35 s after the start
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 1
    rig.advance(30)  # 11:01:30: interval over, but not a minute's first pass
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 1
    rig.advance(30.4)  # 11:02:00.4: first pass of the minute, 95 s after the start
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 2
    rig.advance(59.8)  # 11:03:00.2: 59.8 s after that start, counted from its minute
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 3
    rig.advance(1)
    rig.market.tick()  # the same minute again: never twice
    assert len(rig.store.journal(DISCOVERY)) == 3


def test_discovery_waits_while_protection_needs_the_loop(tmp_path):
    rig = Rig(tmp_path)
    rig.market.tick(discover=False)
    assert rig.source.calls == [] and rig.store.journal(DISCOVERY) == []
    rig.advance(1)
    rig.market.tick()  # allowed again mid-minute: waits for the next minute's first pass
    assert rig.store.journal(DISCOVERY) == []
    rig.advance(INTERVAL - 1)
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 1


class PassAll:
    """Liquidity rules that refuse nothing."""

    name, version = "all", "1"

    def quote_liquidity(self, quote):
        return ()

    def session_liquidity(self, session_bars):
        return ()


def test_a_pass_stopped_by_the_bars_budget_keeps_the_candidates_it_did_not_reach(tmp_path):
    from tickr.types import Bar

    rig = Rig(tmp_path)
    rig.market.screen = PassAll()
    rows = [ScreenerRow(ScreenerKind.GAINERS, n, f"id-{s}", s, "NSQ", "USD", "COMMON_STOCK", {})
            for n, s in enumerate(["ABC", "DEF"], start=1)]
    rig.source.set_screener(ScreenerKind.GAINERS, rows)
    for symbol in ("ABC", "DEF"):
        rig.source.set_quote(Quote(symbol, D("10.00"), D("10.01"), D(1), D(1), rig.now, rig.now))
        rig.source.add_bars([Bar(symbol, rig.now - timedelta(minutes=2), D(10), D(10), D(10),
                                 D(10), D(100))])
    rig.market.tick()
    assert rig.market.candidates() == ["ABC", "DEF"]
    rig.source.limit("bars", 1)  # the next pass is refused its bars read
    rig.advance(INTERVAL)
    for symbol in ("ABC", "DEF"):
        rig.source.set_quote(Quote(symbol, D("10.00"), D("10.01"), D(1), D(1), rig.now, rig.now))
    rig.market.tick()
    assert rig.market.candidates() == ["ABC", "DEF"]  # not reached: kept, not dropped
    row = rig.store.journal(DISCOVERY)[-1]
    assert row.body["kept"] == ["ABC", "DEF"] and row.body["candidates"] == ["ABC", "DEF"]
    assert "bars budget" in row.body["summary"] and "kept 2 earlier" in row.body["summary"]
    rig.source.set_screener(ScreenerKind.GAINERS, rows[:1])  # DEF left the screener
    rig.source.limit("bars", 100)
    rig.advance(INTERVAL)
    for symbol in ("ABC", "DEF"):
        rig.source.set_quote(Quote(symbol, D("10.00"), D("10.01"), D(1), D(1), rig.now, rig.now))
    rig.market.tick()
    assert rig.market.candidates() == ["ABC"]  # a complete pass replaces the list


def test_without_a_strategy_discovery_captures_but_keeps_no_candidate(tmp_path):
    rig = Rig(tmp_path)
    rig.market.screen = NoStrategy()  # the active strategy did not load: no entries
    rig.source.set_screener(ScreenerKind.GAINERS, [ScreenerRow(
        ScreenerKind.GAINERS, 1, "id-ABC", "ABC", "NSQ", "USD", "COMMON_STOCK", {})])
    rig.source.set_quote(quote(rig.now))
    rig.market.tick()
    assert any(c[0] == "screener" for c in rig.source.calls)
    assert rig.market.discovery is not None and rig.market.discovery.candidates == ()
    assert not any(c[0] == "bars" for c in rig.source.calls)


def test_discovery_never_runs_outside_the_session(tmp_path):
    rig = Rig(tmp_path, now=SATURDAY)
    rig.market.tick()
    assert rig.source.calls == [] and rig.market.discovery is None


class Malformed(FakeMarketData):
    failures = 1

    def screener(self, kind, session=ScreenerSession.REGULAR):
        if self.failures:
            self.failures -= 1
            raise MalformedResponse("screener: missing field")
        return super().screener(kind, session)


def test_discovery_failure_is_journaled_and_retried_next_interval(tmp_path):
    rig = Rig(tmp_path, source=Malformed())
    rig.market.tick()
    (failed,) = rig.store.journal(DISCOVERY_FAILED)
    assert failed.body == {"error": "MalformedResponse", "detail": "screener: missing field"}
    rig.advance(INTERVAL)
    rig.market.tick()
    assert len(rig.store.journal(DISCOVERY)) == 1


class Wordy(FakeMarketData):
    def screener(self, kind, session=ScreenerSession.REGULAR):
        raise MalformedResponse("screener: " + "x" * 5000)


def test_discovery_failure_detail_is_capped(tmp_path):
    rig = Rig(tmp_path, source=Wordy())
    rig.market.tick()
    (failed,) = rig.store.journal(DISCOVERY_FAILED)
    cap = CFG.market_data.failure_detail_max_chars
    assert len(failed.body["detail"]) == cap
    assert failed.body["detail"].startswith("screener: x")
    assert failed.body["detail_length"] == len("screener: ") + 5000


def test_bot_adapters_share_one_transport_and_budget(monkeypatch):
    from tickr.broker import webull

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

    def subscribe_optional(self, symbols, listener, budget_reserve):
        self.session.subscribe_optional(symbols, listener, budget_reserve)

    def unsubscribe_optional(self, symbols, budget_reserve):
        self.session.unsubscribe_optional(symbols, budget_reserve)


class Budget:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail

    def take(self, path):
        if self.fail is not None:
            raise self.fail

    def can_take(self, route, count=1):
        return self.fail is None


def real_session(budget):
    from tickr.broker.webull import StreamSession
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
    from tickr.broker.webull import WebullMarketData

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

    from tickr.runtime.market import DiscoveryState

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

    from tickr.broker.webull import StreamSession
    from tickr.runtime.market import DiscoveryState

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

    from tickr.broker.webull import StreamSession
    from tickr.runtime.market import DiscoveryState

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


RESYNC = float(CFG.market_data.observer_resync_seconds)


def candidates(*symbols):
    from types import SimpleNamespace

    return tuple(SimpleNamespace(instrument=SimpleNamespace(symbol=s)) for s in symbols)


def tiered(tmp_path, source=None, **market_data):
    from dataclasses import replace

    from tickr.runtime.market import DiscoveryState

    rig = Rig(tmp_path, source=source) if source is not None else Rig(tmp_path, now=SATURDAY)
    rig.market.config = replace(CFG, market_data=replace(CFG.market_data, **market_data))
    rig.market.discovery = DiscoveryState(rig.now, "x", candidates("AAA"))  # type: ignore[arg-type]
    return rig


def test_observer_symbols_are_a_lowest_tier_of_the_stream(tmp_path):
    rig = tiered(tmp_path, candidate_subscriptions=1, observer_subscriptions=2)
    rig.market.watch = {"WAT"}
    rig.market.observer_watch = {"WAT", "AAA", "OB1", "OB2", "OB3"}
    rig.market.tick()
    assert rig.source.subscribed == {"WAT"}  # transport not up: no candidates, no observers
    rig.source.transport(True, rig.now, "up")
    rig.advance(RESYNC)
    rig.market.tick()
    # the active strategy's symbols are not counted against the observer slots
    assert rig.source.subscribed == {"WAT", "AAA", "OB1", "OB2"}
    rig.market.observer_watch = {"OB0", "OB2", "OB3"}
    rig.market.tick()  # the observer tier changes at most once per observer_resync_seconds
    assert rig.source.subscribed == {"WAT", "AAA", "OB1", "OB2"}
    rig.advance(RESYNC)
    rig.market.tick()  # an already subscribed symbol keeps its slot (no churn)
    assert rig.source.subscribed == {"WAT", "AAA", "OB0", "OB2"}
    rig.market.observer_watch = set()
    rig.advance(RESYNC)
    rig.market.tick()
    assert rig.source.subscribed == {"WAT", "AAA"}


def test_lower_tiers_give_their_slots_back_to_the_active_symbols(tmp_path):
    rig = tiered(tmp_path, candidate_subscriptions=1, observer_subscriptions=2,
                 stream_max_symbols=7, stream_reserved_symbols=2)
    rig.market.bind()
    rig.source.transport(True, rig.now, "up")
    rig.market.watch = {"W1", "W2"}
    rig.market.observer_watch = {"OB1", "OB2"}
    rig.advance(RESYNC)
    rig.market.tick()
    assert rig.source.subscribed == {"W1", "W2", "AAA", "OB1", "OB2"}
    rig.market.watch = {"W1", "W2", "W3", "W4"}  # 7 - 2 reserved - 5 taken: no observer slot
    rig.market.tick()  # at once, not at the next resync
    assert rig.source.subscribed == {"W1", "W2", "W3", "W4", "AAA"}
    rig.market.watch = {f"W{i}" for i in range(7)}  # the whole connection: no candidate
    rig.market.tick()
    assert rig.source.subscribed == rig.market.watch


def test_the_observer_tier_is_frozen_while_a_position_is_open(tmp_path):
    rig = tiered(tmp_path, candidate_subscriptions=1, observer_subscriptions=2)
    rig.market.bind()
    rig.source.transport(True, rig.now, "up")
    rig.market.observer_watch = {"OB1"}
    rig.advance(RESYNC)
    rig.market.tick(discover=False)
    assert "OB1" in rig.source.subscribed
    rig.position()
    rig.market.observer_watch = {"OB2"}
    rig.advance(RESYNC)
    rig.market.tick(discover=False)
    assert "OB1" in rig.source.subscribed and "OB2" not in rig.source.subscribed


def stream_rig(tmp_path, send_subscribe, budget=None, **market_data):
    from tickr.broker.webull import StreamSession

    sent: set[str] = set()
    reconnects: list[int] = []

    def subscribe(symbols):
        send_subscribe(symbols)
        sent.update(symbols)

    session = StreamSession(budget or Budget(), lambda: MONDAY_11, lambda where, exc: None,
                            max_symbols=100)
    session.bind(subscribe, lambda symbols: sent.difference_update(symbols),
                 lambda: reconnects.append(1))
    rig = tiered(tmp_path, source=SessionSource(session), **market_data)
    rig.market.bind()
    session.on_connected()
    return rig, session, sent, reconnects


def test_a_failed_observer_subscribe_never_reconnects_or_exits_a_position(tmp_path):
    def send_subscribe(symbols):
        if any(s.startswith("OB") for s in symbols):
            raise RuntimeError("provider refused")

    rig, session, sent, reconnects = stream_rig(
        tmp_path, send_subscribe, candidate_subscriptions=1, observer_subscriptions=3)
    rig.market.observer_watch = {"OB1", "OB2"}
    rig.advance(RESYNC)
    rig.market.tick(discover=False)
    assert rig.market.subscribed == {"AAA"} and session.up and reconnects == []
    rig.market.tick(discover=False)  # errors are journaled on the next pass
    errors = [e.body for e in rig.store.journal(STREAM_ERROR)]
    assert {"where": "subscribe-observers", "error": "MarketDataError"} in errors
    t = rig.position()
    rig.advance(LOST_AFTER.total_seconds() * 2)
    rig.market.tick(discover=False)
    assert rig.engine.trade(t).exit_requested is None and session.up and reconnects == []
    assert rig.market.subscribed == {"ABC", "AAA"}


def test_the_observer_tier_leaves_the_subscribe_budget_reserve_free(tmp_path):
    class Tight(Budget):
        def can_take(self, route, count=1):
            return count <= CFG.market_data.stream_reserved_subscribe_requests

    rig, session, sent, reconnects = stream_rig(
        tmp_path, lambda symbols: None, budget=Tight(), candidate_subscriptions=1,
        observer_subscriptions=3)
    rig.market.observer_watch = {"OB1"}
    rig.advance(RESYNC)
    rig.market.tick(discover=False)
    # candidates and observer symbols both wait for the reserve; the stream stays up
    assert sent == set() and session.up and reconnects == []


def test_observer_symbols_are_unsubscribed_without_a_reconnect(tmp_path):
    rig, session, sent, reconnects = stream_rig(
        tmp_path, lambda symbols: None, candidate_subscriptions=1, observer_subscriptions=3)
    rig.market.observer_watch = {"OB1"}
    rig.advance(RESYNC)
    rig.market.tick(discover=False)
    assert sent == {"AAA", "OB1"}

    def broken(symbols):
        raise RuntimeError("unsubscribe failed")

    session._send_unsubscribe = broken  # the provider fails the unsubscribe POST
    rig.market.observer_watch = set()
    rig.advance(RESYNC)
    rig.market.tick(discover=False)
    assert rig.market.subscribed == {"AAA"} and session.symbols == {"AAA"}
    assert session.up and reconnects == []


def test_a_dropped_candidate_an_observer_watches_stays_on_the_optional_path(tmp_path):
    from tickr.runtime.market import DiscoveryState

    rig = tiered(tmp_path, candidate_subscriptions=1, observer_subscriptions=2)
    rig.market.bind()
    rig.source.transport(True, rig.now, "up")
    rig.market.observer_watch = {"AAA"}
    rig.advance(RESYNC)
    rig.market.tick()
    assert rig.source.subscribed == {"AAA"} and rig.market._optional == {"AAA"}  # candidate
    rig.market.discovery = DiscoveryState(rig.now, "x", ())  # type: ignore[arg-type]
    rig.advance(RESYNC)
    rig.market.tick()  # no longer a candidate: the observer tier keeps it, no churn
    assert rig.source.subscribed == {"AAA"} and rig.market._optional == {"AAA"}


def test_a_failed_candidate_subscribe_never_reconnects_or_exits_a_position(tmp_path):
    def send_subscribe(symbols):
        if "AAA" in symbols:
            raise RuntimeError("provider refused")

    rig, session, sent, reconnects = stream_rig(
        tmp_path, send_subscribe, candidate_subscriptions=1, observer_subscriptions=0)
    t = rig.position()
    rig.market.tick(discover=False)
    assert rig.market.subscribed == {"ABC"} and session.up and reconnects == []
    rig.advance(LOST_AFTER.total_seconds() * 2)
    rig.market.tick(discover=False)
    assert rig.engine.trade(t).exit_requested is None and session.up and reconnects == []


def test_no_observer_symbol_is_added_while_trading_even_after_a_failed_pass(tmp_path):
    rig = tiered(tmp_path, candidate_subscriptions=1, observer_subscriptions=2)
    rig.market.bind()
    rig.source.transport(True, rig.now, "up")
    rig.market.observer_watch = {"OB1"}
    calls = {"n": 0}
    original = rig.source.subscribe

    def failing_once(symbols, listener):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("candidate request failed")
        original(symbols, listener)

    rig.source.subscribe = failing_once  # type: ignore[method-assign]
    rig.advance(RESYNC)
    rig.market.tick(discover=False)  # resync chose OB1, but the pass stopped at candidates
    assert "OB1" not in rig.source.subscribed
    rig.position()
    rig.market.tick(discover=False)
    assert "OB1" not in rig.source.subscribed


def test_no_unsubscribe_failure_reconnects_with_a_position_open(tmp_path):
    from tickr.runtime.market import DiscoveryState

    rig, session, sent, reconnects = stream_rig(
        tmp_path, lambda symbols: None, candidate_subscriptions=1, observer_subscriptions=0)
    t = rig.position()
    rig.market.watch = {"WAT"}
    rig.market.tick(discover=False)
    assert rig.market.subscribed == {"ABC", "AAA", "WAT"}

    def refuse(symbols):
        raise RuntimeError("HTTP 429")

    session._send_unsubscribe = refuse
    # a candidate rotates out
    rig.market.discovery = DiscoveryState(rig.now, "x", candidates("WAT"))  # type: ignore[arg-type]
    rig.market.tick(discover=False)
    # WATCH -> still a candidate -> dropped
    rig.market.watch = set()
    rig.market.tick(discover=False)
    rig.market.discovery = DiscoveryState(rig.now, "x", ())  # type: ignore[arg-type]
    rig.market.tick(discover=False)
    assert rig.market.subscribed == {"ABC"} and session.up and reconnects == []
    rig.advance(LOST_AFTER.total_seconds() * 2)
    rig.market.tick(discover=False)
    assert rig.engine.trade(t).exit_requested is None and reconnects == []


def test_every_change_of_the_subscribed_set_is_journaled_once(tmp_path):
    rig = tiered(tmp_path, candidate_subscriptions=1, observer_subscriptions=1)
    rig.market.watch = {"WAT"}
    rig.market.observer_watch = {"OB1"}
    rig.market.tick()
    rig.market.tick()  # nothing changed: no second record
    (first,) = rig.store.journal(STREAM_SUBSCRIPTIONS)
    assert first.body == {"added": ["WAT"], "removed": [], "subscribed": ["WAT"],
                          "optional": []}
    assert first.run_id == rig.engine.run_id
    rig.source.transport(True, rig.now, "up")
    rig.advance(RESYNC)
    rig.market.tick()
    assert rig.store.journal(STREAM_SUBSCRIPTIONS)[-1].body == {
        "added": ["AAA", "OB1"], "removed": [], "subscribed": ["AAA", "OB1", "WAT"],
        "optional": ["AAA", "OB1"]}
    rig.market.watch = set()
    rig.market.discovery = None
    rig.market.tick()
    assert rig.store.journal(STREAM_SUBSCRIPTIONS)[-1].body == {
        "added": [], "removed": ["AAA", "WAT"], "subscribed": ["OB1"], "optional": ["OB1"]}
    assert len(rig.store.journal(STREAM_SUBSCRIPTIONS)) == 3


def test_a_refused_subscribe_is_not_journaled_as_subscribed(tmp_path):
    class Refusing(FakeMarketData):
        def subscribe(self, symbols, listener):
            if symbols:
                raise RuntimeError("refused")
            super().subscribe(symbols, listener)

    rig = Rig(tmp_path, now=SATURDAY, source=Refusing())
    rig.market.watch = {"WAT"}
    rig.market.tick()
    assert rig.market.subscribed == set()
    assert rig.store.journal(STREAM_SUBSCRIPTIONS) == []


def test_each_discovery_pass_is_appended_to_the_day_capture(tmp_path):
    rig = Rig(tmp_path)
    rig.market.capture = CaptureWriter(tmp_path / "capture", "fake")
    rig.market.tick()
    rig.advance(INTERVAL)
    rig.market.tick()
    rig.market.capture.close()
    lines = (tmp_path / "capture" / "2026-11-16" / DISCOVERY_FILE).read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 2 and all(r["provider"] == "fake" for r in records)
    assert records[0]["stopped"] is None and records[0]["not_reached"] == []


def _discovery_file(tmp_path, text):
    from tickr.calendar import ny_date_of
    from tickr.data.capture import DISCOVERY_FILE, CaptureWriter

    writer = CaptureWriter(tmp_path / "capture", "webull")
    path = writer.path(ny_date_of(MONDAY_11), DISCOVERY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return writer


def test_a_restart_seeds_refused_symbols_from_the_days_discovery_record(tmp_path):
    import json

    from tickr.data.discovery import Stage

    session = MONDAY_11.replace(hour=9, minute=30).astimezone(UTC)
    at = (MONDAY_11 - timedelta(minutes=1)).astimezone(UTC)
    record = {"session_open": session.isoformat(), "refused_symbols": [
        {"symbol": "DJT", "stage": "snapshot", "refused_at": at.isoformat()}]}
    rig = Rig(tmp_path, capture=_discovery_file(tmp_path, json.dumps(record) + "\n"))
    active = rig.market._refused_symbols.active(session, MONDAY_11, timedelta(seconds=900))
    assert active == {"DJT": Stage.SNAPSHOT}
    assert rig.store.journal(REFUSED_SYMBOLS_UNREAD) == []


def test_an_unreadable_discovery_record_is_journaled_and_the_memory_starts_empty(tmp_path):
    rig = Rig(tmp_path, capture=_discovery_file(tmp_path, "{broken\n"))
    (row,) = rig.store.journal(REFUSED_SYMBOLS_UNREAD)
    assert "discovery.jsonl:1" in row.body["detail"]
    assert rig.market._refused_symbols.remembered() == ()


def test_a_truncated_archived_discovery_record_never_stops_the_start(tmp_path):
    import gzip

    from tickr.calendar import ny_date_of
    from tickr.data.capture import DISCOVERY_FILE, CaptureWriter

    writer = CaptureWriter(tmp_path / "capture", "webull")
    plain = writer.path(ny_date_of(MONDAY_11), DISCOVERY_FILE)
    plain.parent.mkdir(parents=True, exist_ok=True)
    whole = gzip.compress(b'{"session_open": "x"}\n' * 50)
    plain.with_name(plain.name + ".gz").write_bytes(whole[: len(whole) // 2])  # truncated
    rig = Rig(tmp_path, capture=writer)
    (row,) = rig.store.journal(REFUSED_SYMBOLS_UNREAD)
    assert row.body["detail"]
    assert rig.market._refused_symbols.remembered() == ()


def test_a_recorder_capture_not_written_is_journaled_and_the_round_not_used(tmp_path):
    from tickr.data.capture import CaptureError, CaptureWriter
    from tickr.runtime.market import CAPTURE_FAILED

    class Broken(CaptureWriter):
        def screener(self, *args, **kwargs):
            raise CaptureError("screener-recorded.jsonl: disk full")

        def screener_skipped(self, *args, **kwargs):
            raise CaptureError("events.jsonl: disk full")

    source = FakeMarketData()
    rig = Rig(tmp_path, source=source, capture=Broken(tmp_path / "capture", source.provider))
    source.set_screener(ScreenerKind.GAINERS, [
        ScreenerRow(ScreenerKind.GAINERS, 1, "id-1", "AAA", "NSQ", "USD", None, {})])
    day = CALENDAR.day(MONDAY_11.date())
    assert rig.market.record_screeners(day, day.regular_close) is None
    (row,) = rig.store.journal(CAPTURE_FAILED)
    assert row.body == {"what": "screener-recorder", "error": "CaptureError",
                        "detail": "screener-recorded.jsonl: disk full"}
