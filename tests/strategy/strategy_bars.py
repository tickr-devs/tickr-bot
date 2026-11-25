"""Bar generators shared by the strategy tests."""

from __future__ import annotations

import tomllib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tickr import config
from tickr.strategy.base import SizingLimits
from tickr.types import Bar, Quote

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
SYMBOL = "ABCD"
ROOT = Path(__file__).resolve().parents[2]
TOML = ROOT / "strategies" / "price-action-v1" / "strategy.toml"
# The platform's sizing limits as shipped (config/bot.toml [risk]), given with every context.
SIZING = SizingLimits.of(config.load(ROOT / "config" / "bot.toml").risk)
# Many sizing tests were written for a 2500 USD notional cap (the shipped one is 1000 since
# 2026-11-17, owner): pinned so their numbers still test the loss and the cap sizing.
SIZING_2500 = SizingLimits(SIZING.planned_loss_usd, Decimal(2500))


def strategy_values() -> dict:
    return tomllib.loads(TOML.read_text())


def d(x) -> Decimal:
    return Decimal(str(x))


def minute(day: date, index: int) -> datetime:
    """Start of the index-th regular-session minute of `day`, in UTC."""
    open_ = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
    return (open_ + timedelta(minutes=index)).astimezone(UTC)


def bar(day, index, o, h, low, c, v, symbol=SYMBOL) -> Bar:
    return Bar(symbol, minute(day, index), d(o), d(h), d(low), d(c), d(v))


def rising_session(day: date, count: int, start=20.0, step=0.10, wick=0.02, volume=10_000):
    """Monotone uptrend: each bar opens at the previous close and closes `step` higher."""
    bars = []
    price = d(start)
    for i in range(count):
        close = price + d(step)
        bars.append(
            Bar(SYMBOL, minute(day, i), price, close + d(wick), price - d(wick), close, d(volume))
        )
        price = close
    return bars


def flat_session(day: date, count: int, price=20.0, volume=3_000):
    p = d(price)
    return [Bar(SYMBOL, minute(day, i), p, p, p, p, d(volume)) for i in range(count)]


def quote(bid, ask, symbol=SYMBOL) -> Quote:
    t = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
    return Quote(symbol, d(bid), d(ask), d(100), d(100), t, t)


TODAY = date(2026, 11, 13)
PRIOR_DAYS = [date(2026, 11, day) for day in (6, 9, 10, 11, 12)]


def prior_sessions(count: int = 5, volume=3_000, minutes: int = 390):
    return [flat_session(day, minutes, volume=volume) for day in PRIOR_DAYS[-count:]]
