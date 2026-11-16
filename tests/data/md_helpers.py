"""Builders for market-data tests. Times are UTC; 2026-11-13 is a regular trading Friday."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tickr.data.base import ScreenerKind, ScreenerRow
from tickr.data.discovery import DiscoverySettings
from tickr.strategy.base import Check
from tickr.strategy.price_action_v1 import dollar_volume_check, spread_check
from tickr.types import Bar, Quote

OPEN = datetime(2026, 11, 13, 14, 30, tzinfo=UTC)  # 09:30 New York
NOW = OPEN + timedelta(minutes=30, seconds=10)


def row(kind: ScreenerKind, rank: int, symbol: str, iid: str | None = None, *,
        exchange: str = "NSQ", currency: str = "USD",
        security_type: str | None = "COMMON_STOCK") -> ScreenerRow:
    return ScreenerRow(kind, rank, iid or f"id-{symbol}", symbol, exchange, currency,
                       security_type, {"symbol": symbol, "close": "10.00"})


def quote(symbol: str, bid: str = "10.00", ask: str = "10.02", *,
          at: datetime = NOW, age_seconds: int = 1) -> Quote:
    return Quote(symbol, Decimal(bid), Decimal(ask), Decimal(100), Decimal(200),
                 at - timedelta(seconds=age_seconds), at)


def bars(symbol: str, minutes: int, *, close: str = "10", volume: str = "20000",
         start: datetime = OPEN) -> list[Bar]:
    c = Decimal(close)
    return [Bar(symbol, start + timedelta(minutes=i), c, c, c, c, Decimal(volume))
            for i in range(minutes)]


class Screen:
    """A strategy's liquidity rules (strategy.base.Liquidity) with the shipped values."""

    name, version = "screen", "1"

    def __init__(self, max_spread_ratio: str = "0.015",
                 min_dollar_volume_usd: str = "5000000") -> None:
        self.max_spread_ratio = Decimal(max_spread_ratio)
        self.min_dollar_volume_usd = Decimal(min_dollar_volume_usd)

    def quote_liquidity(self, quote: Quote) -> tuple[Check, ...]:
        return (spread_check(quote, self.max_spread_ratio),)

    def session_liquidity(self, session_bars: list[Bar]) -> tuple[Check, ...]:
        return (dollar_volume_check(session_bars, self.min_dollar_volume_usd),)


def settings(**overrides: object) -> DiscoverySettings:
    values: dict[str, object] = dict(
        allowed_exchanges=frozenset({"NMS", "NAS", "NSQ", "NYS", "ASE"}),
        allowed_currencies=frozenset({"USD"}),
        allowed_security_types=frozenset({"COMMON_STOCK"}),
        max_quote_age=timedelta(seconds=5),
        max_clock_skew=timedelta(seconds=1),
        snapshot_symbols_per_request=100,
        bars_symbols_per_request=20,
        bars_per_symbol=390,
        refused_symbol_recheck=timedelta(seconds=900),
        bars_budget_reserve=3,
    )
    values.update(overrides)
    return DiscoverySettings(**values)  # type: ignore[arg-type]
