from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from md_helpers import NOW, quote

from aperture import config
from aperture.data.base import FakeMarketData
from aperture.data.health import FanOut, StreamHealth, StreamState, UntrackedSymbol

QUIET = timedelta(seconds=5)
ROOT = Path(__file__).resolve().parents[2]


def health(*symbols: str) -> StreamHealth:
    h = StreamHealth(QUIET)
    h.track(symbols)
    return h


def test_lost_before_transport_ever_comes_up():
    h = health("AAA")
    h.on_quote(quote("AAA"))
    assert h.state("AAA", NOW) is StreamState.LOST
    assert h.transport.detail == "never connected"


def test_quiet_until_a_quote_then_fresh_then_quiet_again():
    h = health("AAA", "BBB")
    h.on_transport(True, NOW, "connected")
    assert h.state("AAA", NOW) is StreamState.QUIET
    h.on_quote(quote("AAA", at=NOW))
    assert h.state("AAA", NOW + QUIET) is StreamState.FRESH
    assert h.state("AAA", NOW + QUIET + timedelta(seconds=1)) is StreamState.QUIET
    assert h.states(NOW) == {"AAA": StreamState.FRESH, "BBB": StreamState.QUIET}


def test_transport_loss_marks_every_symbol_lost_and_old_quotes_do_not_count_after():
    h = health("AAA")
    h.on_transport(True, NOW, "connected")
    h.on_quote(quote("AAA", at=NOW))
    h.on_transport(False, NOW, "socket closed")
    assert h.state("AAA", NOW) is StreamState.LOST
    h.on_transport(True, NOW, "reconnected")
    assert h.state("AAA", NOW) is StreamState.QUIET


def test_older_quote_does_not_move_last_receipt_back():
    h = health("AAA")
    h.on_transport(True, NOW, "connected")
    h.on_quote(quote("AAA", at=NOW))
    h.on_quote(quote("AAA", at=NOW - timedelta(seconds=10)))
    assert h.state("AAA", NOW + QUIET) is StreamState.FRESH


def test_untracked_symbol_is_refused():
    h = health("AAA")
    with pytest.raises(UntrackedSymbol):
        h.state("ZZZ", NOW)
    h.on_transport(True, NOW, "up")
    h.on_quote(quote("ZZZ"))
    h.untrack(["AAA"])
    assert h.states(NOW) == {}


def test_fan_out_through_fake_stream():
    md = FakeMarketData()
    h = health("AAA")
    seen: list = []

    class Recorder:
        def on_quote(self, q):
            seen.append(q.symbol)

        def on_transport(self, up, at, detail):
            seen.append(detail)

    md.subscribe(["AAA"], FanOut(h, Recorder(), on_error=lambda *a: None))
    md.transport(True, NOW, "up")
    md.push_quote(quote("AAA", at=NOW))
    md.push_quote(quote("NOT", at=NOW))
    assert seen == ["up", "AAA"]
    assert h.state("AAA", NOW) is StreamState.FRESH


def test_from_config():
    cfg = config.load(ROOT / "config" / "bot.toml")
    h = StreamHealth.from_config(cfg)
    h.track(["AAA"])
    h.on_transport(True, NOW, "up")
    h.on_quote(quote("AAA", at=NOW))
    assert h.state("AAA", NOW + timedelta(seconds=float(cfg.market_data.quiet_after_seconds))) \
        is StreamState.FRESH


def test_fan_out_isolates_a_failing_listener():
    h = health("AAA")
    errors: list = []

    class Broken:
        def on_quote(self, q):
            raise OSError("disk full")

        def on_transport(self, up, at, detail):
            raise OSError("disk full")

    broken = Broken()
    fan = FanOut(broken, h, on_error=lambda listener, event, exc: errors.append(
        (listener, event, str(exc))))
    fan.on_transport(True, NOW, "up")
    fan.on_quote(quote("AAA", at=NOW))
    assert h.state("AAA", NOW) is StreamState.FRESH
    assert errors == [(broken, "transport", "disk full"), (broken, "quote", "disk full")]
