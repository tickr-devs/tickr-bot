"""BUG reproduction (fails until fixed): an out-of-range provider time escapes as a bare
exception instead of MalformedResponse (hard rule 7, webull.py module docstring).

`epoch_ms_to_utc` (src/aperture/broker/webull.py:740) checks only that the value is a positive
whole number, then calls `datetime.fromtimestamp`, which raises OverflowError, ValueError or
OSError past year 9999. `_bar_time` (webull.py:753) converts with `astimezone(UTC)`, which raises
OverflowError at the edges of the datetime range. Snapshot `quote_time` and the stream
`timestamp` share the first path, every bar reader the second. JSON numbers with an exponent
reach the reader as Decimal (`parse_json`), so `1e30` on the wire is enough.

Impact: runtime/evaluation.py catches (MarketDataError, BrokerError) around `bars`, so an
OverflowError from a bar time passes through it; snapshot callers get a ValueError or
OverflowError where a typed refusal is promised. (The stream path is wrapped by a catch-all in
StreamSession.on_message.) Realistic only for a garbage body: low likelihood.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aperture.broker.webull import (
    MalformedResponse,
    parse_json,
    quote_from_stream,
    read_bars,
    read_snapshot,
)

RECEIPT = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


def snapshot_body(quote_time_json: str):
    return parse_json('[{"symbol": "AAPL", "bid": "10.00", "ask": "10.02", "bid_size": "300", '
                      f'"ask_size": "200", "quote_time": {quote_time_json}}}]')


@pytest.mark.xfail(strict=True, reason=(
    "bug: out-of-range times raise bare errors instead of MalformedResponse"))
@pytest.mark.parametrize("quote_time", ["1e30", "253402300800000", "100000000000000000000"])
def test_snapshot_quote_time_out_of_range_is_malformed(quote_time):
    with pytest.raises(MalformedResponse):
        read_snapshot(snapshot_body(quote_time), RECEIPT)


@pytest.mark.xfail(strict=True, reason=(
    "bug: out-of-range times raise bare errors instead of MalformedResponse"))
def test_stream_timestamp_out_of_range_is_malformed():
    level = SimpleNamespace(price="10.00", size="100")
    frame = SimpleNamespace(basic=SimpleNamespace(symbol="AAPL", timestamp="253402300800000"),
                            bids=[level], asks=[level])
    with pytest.raises(MalformedResponse):
        quote_from_stream(frame, RECEIPT)


@pytest.mark.xfail(strict=True, reason=(
    "bug: out-of-range times raise bare errors instead of MalformedResponse"))
@pytest.mark.parametrize("time", ["0001-01-01T00:00:00.000+0100",
                                  "9999-12-31T23:59:00.000-0100"])
def test_bar_time_out_of_range_is_malformed(time):
    body = {"result": [{"delay_minutes": 0, "symbol": "AAPL", "result": [
        {"time": time, "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1",
         "trading_session": "RTH"}]}]}
    with pytest.raises(MalformedResponse):
        read_bars(body)
