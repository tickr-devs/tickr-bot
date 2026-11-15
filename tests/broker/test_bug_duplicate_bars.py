"""BUG reproduction (fails until fixed): a bars body with the same minute twice for one symbol
is returned as two bars, even when they disagree (hard rule 7: refuse, never guess).

`read_bars` (src/aperture/broker/webull.py:871, also read_history_bars :900 and read_daily_bars
:1215) appends every row and sorts; nothing checks (symbol, start) is unique, within a series or
across two series of one symbol. `data/history.py` guards this itself (HistoryCause.CONFLICT),
but discovery (`session_dollar_volume`, data/discovery.py) and runtime/evaluation.py use
`MarketData.bars` directly: a repeated minute doubles that minute's dollar volume and gives the
strategy two different bars for one minute. Whether Webull ever repeats a minute is unproven.
"""

from __future__ import annotations

import pytest

from aperture.broker.webull import MalformedResponse, read_bars


def bar(close: str) -> dict:
    return {"time": "2026-11-13T20:59:00.000+0000", "open": "10", "high": "11", "low": "9",
            "close": close, "volume": "1000", "trading_session": "RTH"}


def series(*rows: dict) -> dict:
    return {"delay_minutes": 0, "instrument_id": "913256135", "symbol": "AAPL",
            "result": list(rows)}


@pytest.mark.xfail(strict=True, reason=(
    "bug: one minute returned twice as two bars"))
@pytest.mark.parametrize("body", [
    {"result": [series(bar("10"), bar("10.5"))]},  # one series, conflicting minute
    {"result": [series(bar("10"), bar("10"))]},  # one series, repeated minute
    {"result": [series(bar("10")), series(bar("10.5"))]},  # one symbol in two series
])
def test_bars_never_return_one_minute_twice(body):
    try:
        bars = read_bars(body)
    except MalformedResponse:
        return  # refusing is a fix
    keys = [(b.symbol, b.start) for b in bars]
    assert len(keys) == len(set(keys)), bars
