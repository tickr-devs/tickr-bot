"""L1 stream: StreamSession (SDK-free) and SdkQuoteStream wiring.

Frames are shaped like the SDK 3.0.3 QuoteResult (basic{symbol, timestamp}, bids[], asks[] with
Decimal price and string size); no real frame has been recorded yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from aperture.broker.base import RateLimited
from aperture.broker.budget import RequestBudget, RouteLimit
from aperture.broker.webull import (
    STREAM_SUBSCRIBE_PATH,
    AdapterConfigError,
    MalformedResponse,
    StreamSession,
    StreamSettings,
    WebullMarketData,
    quote_from_stream,
)
from aperture.data.base import MarketDataError
from aperture.data.health import StreamHealth, StreamState

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
MS = int(NOW.timestamp() * 1000)


def frame(symbol="AAA", bid="10.00", ask="10.02", ts=MS, bids=None, asks=None):
    side = lambda price, size: SimpleNamespace(price=None if price is None else D(price),  # noqa: E731
                                               size=size)
    return SimpleNamespace(
        basic=SimpleNamespace(symbol=symbol, instrument_id="1", timestamp=ts,
                              trading_session="CORE"),
        bids=[side(bid, "300")] if bids is None else bids,
        asks=[side(ask, "200")] if asks is None else asks,
    )


class Recorder:
    def __init__(self) -> None:
        self.quotes: list = []
        self.transport: list = []

    def on_quote(self, q):
        self.quotes.append(q)

    def on_transport(self, up, at, detail):
        self.transport.append((up, detail))


def session(limit=30):
    errors: list = []
    budget = RequestBudget(RouteLimit(limit, 60, 60), clock=lambda: 0.0)
    s = StreamSession(budget, lambda: NOW, lambda where, exc: errors.append((where, exc)))
    sent: list = []
    s.bind(lambda syms: sent.append(("sub", syms)), lambda syms: sent.append(("unsub", syms)),
           lambda: sent.append(("reconnect", [])))
    return s, sent, errors, budget


def test_quote_from_stream():
    q = quote_from_stream(frame(), NOW)
    assert (q.symbol, q.bid, q.ask, q.bid_size, q.ask_size) == ("AAA", D("10.00"), D("10.02"),
                                                                 D(300), D(200))
    assert q.provider_time == NOW and q.receipt_time == NOW
    assert quote_from_stream(frame(bids=[]), NOW) is None
    assert quote_from_stream(frame(ask=None), NOW) is None
    with pytest.raises(MalformedResponse):
        quote_from_stream(frame(symbol=""), NOW)
    with pytest.raises(MalformedResponse):
        quote_from_stream(frame(ts=0), NOW)


def test_every_connect_resubscribes_the_full_set_before_up():
    s, sent, _, _ = session()
    rec = Recorder()
    s.subscribe(["BBB", "AAA"], rec)  # not connected: nothing sent yet
    assert sent == [] and not s.up
    s.on_connected()
    assert sent == [("sub", ["AAA", "BBB"])] and s.up
    s.subscribe(["CCC", "AAA"], rec)  # connected: only the new symbol
    s.on_disconnected("disconnected (rc=7)")
    assert not s.up
    s.on_connected()
    assert sent[-1] == ("sub", ["AAA", "BBB", "CCC"])
    assert rec.transport == [(True, "connected, 2 symbol(s) subscribed"),
                             (False, "disconnected (rc=7)"),
                             (True, "connected, 3 symbol(s) subscribed")]
    assert sent[1] == ("sub", ["CCC"])


def test_unsubscribe_while_connected():
    s, sent, _, _ = session()
    s.subscribe(["AAA", "BBB"], Recorder())
    s.on_connected()
    s.unsubscribe(["BBB", "ZZZ"])
    assert sent[-1] == ("unsub", ["BBB"]) and s.symbols == {"AAA"}


def test_resubscribe_failure_reports_down_and_reconnects_without_raising():
    s, sent, errors, budget = session(limit=1)
    rec = Recorder()
    s.subscribe(["AAA"], rec)
    budget.take(STREAM_SUBSCRIBE_PATH)
    s.on_connected()  # must not raise into the provider thread
    assert sent == [("reconnect", [])] and not s.up
    assert rec.transport == [(False, "resubscribe failed (BudgetExhausted); reconnecting")]
    assert [(w, type(e).__name__) for w, e in errors] == [("subscribe", "BudgetExhausted")]
    assert isinstance(errors[0][1], RateLimited)


def test_sdk_http_failure_while_resubscribing_is_caught():
    s, _, errors, _ = session()

    def boom(symbols):
        raise RuntimeError("HTTP 500")

    s.bind(boom, lambda syms: None, lambda: errors.append(("reconnect", None)))
    s.subscribe(["AAA"], Recorder())
    s.on_connected()
    assert not s.up and [w for w, _ in errors] == ["subscribe", "reconnect"]


def test_subscribe_racing_with_connect_is_sent_before_up():
    s, sent, _, _ = session()
    rec = Recorder()
    s.subscribe(["AAA"], rec)
    raced = {"done": False}

    def send(symbols):
        sent.append(("sub", symbols))
        if not raced["done"]:  # the bot subscribes while the connect is resubscribing
            raced["done"] = True
            s.subscribe(["BBB"], rec)
            s.unsubscribe(["AAA"])

    s.bind(send, lambda syms: sent.append(("unsub", syms)), lambda: None)
    s.on_connected()
    assert sent == [("sub", ["AAA"]), ("sub", ["BBB"]), ("unsub", ["AAA"])]
    assert s.up and rec.transport == [(True, "connected, 1 symbol(s) subscribed")]


def test_messages_reach_the_listener_only_for_wanted_symbols_and_notices():
    s, _, errors, _ = session()
    rec = Recorder()
    s.subscribe(["AAA"], rec)
    s.on_connected()
    s.on_message("quote", frame("AAA"))
    s.on_message("quote", frame("ZZZ"))
    s.on_message("quote", frame("AAA", bids=[]))
    s.on_message("notice", {"type": "1001", "rtt": "5"})
    s.on_message("snapshot", object())
    assert [q.symbol for q in rec.quotes] == ["AAA"]
    s.on_message("notice", {"type": "1002", "content": "preempted"})
    assert rec.transport[-1] == (False, "entitlement taken by another device (notice 1002)")
    assert not s.up and errors == []


def test_bad_frames_and_listener_errors_never_raise_into_the_provider_thread():
    s, _, errors, _ = session()

    class Broken(Recorder):
        def on_quote(self, q):
            raise OSError("disk full")

    s.subscribe(["AAA"], Broken())
    s.on_connected()
    s.on_message("quote", frame("AAA"))
    s.on_message("quote", frame(""))
    assert [w for w, _ in errors] == ["quote", "quote"]


def test_one_listener_per_session():
    s, _, _, _ = session()
    s.subscribe(["A"], Recorder())
    with pytest.raises(MarketDataError, match="FanOut"):
        s.subscribe(["B"], Recorder())


def test_drives_stream_health_end_to_end():
    s, _, _, _ = session()
    health = StreamHealth(quiet_after=timedelta(seconds=5))
    health.track(["AAA"])
    s.subscribe(["AAA"], health)
    assert health.state("AAA", NOW) is StreamState.LOST
    s.on_connected()
    assert health.state("AAA", NOW) is StreamState.QUIET
    s.on_message("quote", frame("AAA"))
    assert health.state("AAA", NOW) is StreamState.FRESH
    s.on_message("notice", {"type": "1002"})
    assert health.state("AAA", NOW) is StreamState.LOST


def test_market_data_delegates_to_stream_or_refuses():
    budget = RequestBudget(RouteLimit(30, 60, 60), clock=lambda: 0.0)
    plain = WebullMarketData(None, budget, "US_STOCK", "DAY_1", "VOLUME", 100,  # type: ignore
                             utc_now=lambda: NOW)
    with pytest.raises(MarketDataError):
        plain.subscribe(["A"], Recorder())
    s, _, _, _ = session()
    md = WebullMarketData(None, budget, "US_STOCK", "DAY_1", "VOLUME", 100,  # type: ignore
                          utc_now=lambda: NOW, stream=s)
    md.subscribe(["A"], Recorder())
    md.unsubscribe(["A"])
    assert s.symbols == frozenset()


# ---------------------------------------------------------------- SDK wiring (optional dep)


def _settings(tmp_path, mqtt="data-api.sandbox.webull.com"):
    allowed = frozenset({"api.sandbox.webull.com", "data-api.sandbox.webull.com"})
    return StreamSettings(mqtt_host=mqtt, mqtt_port=1883, http_host="api.sandbox.webull.com",
                          allowed_hosts=allowed, region_id="us",
                          token_dir=str(tmp_path / "token"), app_key_env="T_APP_KEY",
                          app_secret_env="T_APP_SECRET", category="US_STOCK",
                          reconnect_delay_seconds=0.0)


def test_sdk_stream_refuses_host_outside_allowlist(tmp_path):
    from aperture.broker.webull import SdkQuoteStream
    s, _, _, _ = session()
    with pytest.raises(AdapterConfigError, match="allowlist"):
        SdkQuoteStream(_settings(tmp_path, mqtt="evil.example.com"), s)


def test_sdk_stream_wires_callbacks(tmp_path, monkeypatch, capsys):
    pytest.importorskip("webull")
    from aperture.broker.webull import SdkQuoteStream
    monkeypatch.setenv("T_APP_KEY", "CANARY-KEY")
    monkeypatch.setenv("T_APP_SECRET", "CANARY-SECRET")
    monkeypatch.chdir(tmp_path)
    s = StreamSession(RequestBudget(RouteLimit(30, 60, 60), clock=lambda: 0.0), lambda: NOW,
                      lambda w, e: None)
    stream = SdkQuoteStream(_settings(tmp_path), s)
    client = stream._client
    calls: list = []
    monkeypatch.setattr(client, "subscribe",
                        lambda symbols, category, types: calls.append((symbols, category, types)))
    rec = Recorder()
    stream.subscribe(["AAA"], rec)
    client.on_connect_success(client, None, "sid")
    assert calls == [(["AAA"], "US_STOCK", ["QUOTE"])] and s.up
    client.on_quotes_message(client, "quote", frame("AAA"))
    client.on_disconnect(client, None, 7)
    assert [q.symbol for q in rec.quotes] == ["AAA"]
    assert rec.transport[-1] == (False, "disconnected (rc=7)")
    out = capsys.readouterr()
    assert "CANARY" not in out.out + out.err
    assert not list(tmp_path.glob("*.log"))


def test_sdk_stream_reconnects_after_the_loop_ends(tmp_path, monkeypatch):
    pytest.importorskip("webull")
    from aperture.broker.webull import SdkQuoteStream
    monkeypatch.setenv("T_APP_KEY", "k")
    monkeypatch.setenv("T_APP_SECRET", "s")
    monkeypatch.chdir(tmp_path)
    s = StreamSession(RequestBudget(RouteLimit(30, 60, 60), clock=lambda: 0.0), lambda: NOW,
                      lambda w, e: None)
    rec = Recorder()
    s.subscribe(["AAA"], rec)
    stream = SdkQuoteStream(_settings(tmp_path), s)
    runs: list = []

    def loop(logger_enable=True):
        runs.append(logger_enable)
        if len(runs) == 1:
            raise RuntimeError("gave up")
        stream._stopping.set()  # second run: stop() from the bot

    monkeypatch.setattr(stream._client, "connect_and_loop_forever", loop)
    stream._run()
    assert runs == [False, False]
    assert [d for _, d in rec.transport] == ["stream loop stopped (RuntimeError)",
                                             "stream loop exited"]


def test_subscribe_failure_while_up_marks_down_reconnects_and_raises():
    s, sent, errors, budget = session(limit=1)
    rec = Recorder()
    s.subscribe(["AAA"], rec)
    s.on_connected()  # uses the only subscribe slot
    with pytest.raises(RateLimited):
        s.subscribe(["BBB"], rec)
    assert not s.up and sent[-1] == ("reconnect", [])
    assert rec.transport[-1] == (False, "resubscribe failed (BudgetExhausted); reconnecting")
    assert [w for w, _ in errors] == ["subscribe"]
    # Rolled back: the next connect resubscribes only what was in sync, so a refused symbol
    # can never keep the stream down; the caller (runtime.market) asks for BBB again.
    assert s.symbols == {"AAA"}
