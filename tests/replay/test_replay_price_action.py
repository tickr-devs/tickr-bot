"""End-to-end replay of the real price-action-v1 strategy over generated M1 bars."""

from __future__ import annotations

import tomllib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tickr.replay.__main__ import platform_sizing, session_clock
from tickr.replay.engine import ExitReason, SignalOutcome, replay
from tickr.replay.settings import load_settings
from tickr.strategy import price_action_v1 as pa
from tickr.types import Bar

ROOT = Path(__file__).resolve().parents[2]
NY = ZoneInfo("America/New_York")
SYMBOL = "ABCD"
PRIOR_DAYS = [date(2026, 11, n) for n in (6, 9, 10, 11, 12)]
TODAY = date(2026, 11, 13)


def minute(day: date, index: int) -> datetime:
    return (
        datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY) + timedelta(minutes=index)
    ).astimezone(ZoneInfo("UTC"))


def flat_day(day: date) -> list[Bar]:
    p = Decimal(20)
    return [Bar(SYMBOL, minute(day, i), p, p, p, p, Decimal(3000)) for i in range(390)]


def today() -> list[Bar]:
    """Steady 30-minute uptrend on high volume, then a collapse through any stop."""
    bars, price, step, wick = [], Decimal(20), Decimal("0.30"), Decimal("0.05")
    for i in range(30):
        close = price + step
        bars.append(
            Bar(SYMBOL, minute(TODAY, i), price, close + wick, price - wick, close, Decimal(30000))
        )
        price = close
    for i in range(30, 35):
        bars.append(
            Bar(SYMBOL, minute(TODAY, i), price, price, Decimal(15), Decimal(15), Decimal(30000))
        )
        price = Decimal(15)
    return bars


def test_real_strategy_enters_and_trades():
    values = tomllib.loads((ROOT / "strategies" / "price-action-v1" / "strategy.toml").read_text())
    settings = load_settings(ROOT / "config" / "replay.toml")
    bars = [b for day in PRIOR_DAYS for b in flat_day(day)] + today()
    result = replay(
        lambda: pa.create(values),
        {SYMBOL: bars},
        settings,
        session_clock(ROOT / "config" / "bot.toml"),
        platform_sizing(ROOT / "config" / "bot.toml"),
    )
    assert (result.strategy, result.version) == ("price-action-v1", "1")
    assert result.verdict_counts.get("ENTER", 0) >= 1
    filled = [s for s in result.signals if s.outcome is SignalOutcome.FILLED]
    assert filled and result.trades
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.STOP
    assert trade.r_multiple < -1  # conservative fills: a stop-out costs more than 1 R
