"""Shared fakes for backtest tests: a scripted history source and bar builders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tickr import config
from tickr.broker.webull import DailyBar, InstrumentPage, InstrumentRow
from tickr.calendar import SessionCalendar
from tickr.data.base import SymbolsRefused
from tickr.types import Bar

ROOT = Path(__file__).resolve().parents[2]
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def cfg():
    return config.load(ROOT / "config" / "bot.toml")


def calendar() -> SessionCalendar:
    c = cfg()
    return SessionCalendar.load((c.root / c.session.calendar_file).parent)


def row(symbol: str, sub="COMMON_STOCK", exchange="NSQ", currency="USD", iid=None,
        status="OC"):
    return InstrumentRow(iid or f"id-{symbol}", symbol, exchange, currency, sub, status)


def minute(day: date, index: int) -> datetime:
    return (datetime.combine(day, time(9, 30), NY) + timedelta(minutes=index)).astimezone(UTC)


def flat_bars(symbol: str, day: date, price: str = "10", volume: str = "1000",
              count: int = 390) -> list[Bar]:
    p = Decimal(price)
    return [Bar(symbol, minute(day, i), p, p, p, p, Decimal(volume)) for i in range(count)]


@dataclass
class FakeHistory:
    pages: dict
    daily: dict
    minutes: dict
    requests: int = 0
    invalid: frozenset = frozenset()

    def __post_init__(self):
        self.calls: list[tuple] = []

    def instruments(self, sub_category: str, key: str | None) -> InstrumentPage:
        self.requests += 1
        self.calls.append(("instruments", sub_category, key))
        return self.pages[key]

    def daily_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[DailyBar]:
        self.requests += 1
        self.calls.append(("daily", tuple(symbols), end, count))
        if any(s in self.invalid for s in symbols):
            raise SymbolsRefused("BARS", 417, "INVALID_SYMBOL")
        out = []
        for s in symbols:
            out.extend([b for b in self.daily.get(s, []) if b.time < end][-count:])
        return out

    def minute_bars(self, symbols: Sequence[str], end: datetime, count: int) -> list[Bar]:
        self.requests += 1
        self.calls.append(("minutes", tuple(symbols), end, count))
        if any(s in self.invalid for s in symbols):
            raise SymbolsRefused("BARS", 417, "INVALID_SYMBOL")
        out = []
        for s in symbols:
            out.extend(sorted((b for b in self.minutes.get(s, []) if b.start < end),
                              key=lambda b: b.start)[-count:])
        return out


def daily_bar(symbol: str, day: date, o: str, h: str, low: str, c: str, v: str) -> DailyBar:
    label = datetime.combine(day, time(0), NY).astimezone(UTC)
    return DailyBar(symbol, label, Decimal(o), Decimal(h), Decimal(low), Decimal(c), Decimal(v))
