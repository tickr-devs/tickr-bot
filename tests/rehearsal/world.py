"""The rehearsal tests' view of `tickr.demo.world`: a regular New York session from the
repo calendar, a one-cent half spread and 500-share quotes, as the scenarios were written."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from tickr.calendar import SessionCalendar, ny_date_of
from tickr.demo import world as demo_world
from tickr.demo.world import CENT, Exchange, Feed, ny  # noqa: F401 - re-exported
from tickr.types import Bar

CALENDAR = SessionCalendar.load(Path(__file__).resolve().parents[2] / "config" / "calendar")
QUOTE_SIZE = Decimal(500)
WICK = Decimal("0.05")
# The rehearsal account's day buying power, USD: the Sandbox account's 4x on 1,000,000.
BUYING_POWER = Decimal(4_000_000)


def flat_session(symbol: str, day: date, price: Decimal, volume: Decimal) -> list[Bar]:
    return demo_world.flat_bars(symbol, CALENDAR.day(day), price, volume)


def build_day(symbol: str, day: date, base: Decimal, volume: Decimal,
              legs: Sequence[tuple[str, str, Decimal, Decimal]],
              wick: Decimal = WICK) -> list[Bar]:
    return demo_world.day_bars(symbol, CALENDAR.day(day), base, volume, legs, wick)


class World(demo_world.World):
    def __init__(self, start: datetime, half_spread: Decimal = CENT) -> None:
        super().__init__(start, CALENDAR.day(ny_date_of(start)), half_spread, QUOTE_SIZE,
                         "rehearsal", demo_world.cash_account(BUYING_POWER))
