from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tickr.replay.clock import fixed_clock
from tickr.replay.settings import ReplaySettings, load_settings
from tickr.strategy.base import Decision, SymbolContext, Verdict
from tickr.types import Bar

HERE = Path(__file__).parent
NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 10)
LAST_ENTRY = time(15, 45)
FLATTEN = time(15, 55)
REGULAR_OPEN = time(9, 30)
CLOCK = fixed_clock(NY, REGULAR_OPEN, LAST_ENTRY, FLATTEN)
BOT_TOML = HERE.parent.parent / "config" / "bot.toml"
FLAT = ("10.00", "10.05", "9.95", "10.00")


def at(clock: str, day: date = DAY) -> datetime:
    """UTC start of the bar that opens at `clock` New York time on `day`."""
    local = datetime.combine(day, time.fromisoformat(clock), NY)
    return local.astimezone(UTC)


def bar(clock: str, o: str, h: str, low: str, c: str, symbol: str = "ABC", day: date = DAY):
    return Bar(
        symbol, at(clock, day), Decimal(o), Decimal(h), Decimal(low), Decimal(c), Decimal(1000)
    )


def series(start: str, rows: list[tuple[str, str, str, str]], symbol: str = "ABC", day=DAY):
    """Consecutive M1 bars from `start`; each row is (open, high, low, close)."""
    first = at(start, day)
    out = []
    for i, (o, h, low, c) in enumerate(rows):
        clock = (first + timedelta(minutes=i)).astimezone(NY).strftime("%H:%M")
        out.append(bar(clock, o, h, low, c, symbol, day))
    return tuple(out)


class Scripted:
    """Strategy returning scripted decisions by bar index within the session.

    `script[i]` is a dict of Decision keyword arguments plus `verdict`. Records every context.
    """

    name = "scripted"
    version = "1"

    def __init__(self, script: Mapping[int, Mapping[str, Any]]):
        self.script = script
        self.contexts: list[SymbolContext] = []

    def evaluate(self, context: SymbolContext) -> Decision:
        self.contexts.append(context)
        spec = dict(self.script.get(len(context.bars) - 1, {"verdict": Verdict.IGNORE}))
        verdict = spec.pop("verdict")
        return Decision(self.name, self.version, context.symbol, verdict, (), **spec)


def enter(low: str | None, high: str | None, stop: str | None) -> dict[str, Any]:
    def d(v: str | None) -> Decimal | None:
        return None if v is None else Decimal(v)

    return {"verdict": Verdict.ENTER, "entry_low": d(low), "entry_high": d(high), "stop": d(stop)}


@pytest.fixture
def settings() -> ReplaySettings:
    return load_settings(HERE / "replay_settings.toml")


@pytest.fixture
def scripted() -> Callable[[Mapping[int, Mapping[str, Any]]], Scripted]:
    return Scripted
