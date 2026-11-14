"""Builders shared by tests/research (named apart from tests/replay/conftest.py)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aperture.replay.clock import fixed_clock
from aperture.replay.settings import ReplaySettings, load_settings
from aperture.research.records import Admission, DecisionRecord
from aperture.research.settings import ResearchSettings, load_research_settings
from aperture.strategy.base import Check, Decision, Role, SymbolContext, Verdict
from aperture.types import Bar

HERE = Path(__file__).parent
NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 10)
LAST_ENTRY = time(15, 45)
FLATTEN = time(15, 55)
CLOCK = fixed_clock(NY, time(9, 30), LAST_ENTRY, FLATTEN)
M1 = timedelta(minutes=1)


def at(clock: str, day: date = DAY) -> datetime:
    """UTC start of the bar that opens at `clock` New York time on `day`."""
    return datetime.combine(day, time.fromisoformat(clock), NY).astimezone(UTC)


def series(start: str, rows: Sequence[tuple[str, str, str, str]], symbol: str = "ABC",
           day: date = DAY, skip: Sequence[int] = ()) -> tuple[Bar, ...]:
    """M1 bars from `start`, one per row (open, high, low, close); indices in `skip` are
    left out (a minute without trades) while later bars keep their clock."""
    first = at(start, day)
    return tuple(
        Bar(symbol, first + i * M1, *(Decimal(v) for v in row), Decimal(1000))
        for i, row in enumerate(rows)
        if i not in skip
    )


def flat(price: str) -> tuple[str, str, str, str]:
    return (price, price, price, price)


def research_settings() -> ResearchSettings:
    return load_research_settings(HERE / "research_settings.toml")


def replay_settings() -> ReplaySettings:
    return load_settings(HERE / "replay_settings.toml")


def d(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def record(
    clock: str,
    verdict: Verdict = Verdict.ENTER,
    *,
    symbol: str = "ABC",
    day: date = DAY,
    admission: Admission | None = None,
    cause: str | None = None,
    role: Role = Role.ACTIVE,
    strategy: str = "s",
    version: str = "1",
    low: str | None = None,
    high: str | None = None,
    stop: str | None = None,
    quantity: str | None = None,
    setup: str | None = None,
    checks: tuple[Check, ...] = (),
) -> DecisionRecord:
    if admission is None:
        if verdict is Verdict.ENTER:
            admission = Admission.REFUSED
            cause = cause or "daily loss ceiling"
        else:
            admission = Admission.NOT_APPLICABLE
    start = at(clock, day)
    decision = Decision(
        strategy, version, symbol, verdict, checks, entry_low=d(low), entry_high=d(high),
        stop=d(stop), quantity=d(quantity), setup=setup,
    )
    return DecisionRecord(start + M1, start, role, decision, admission, cause)


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any] | str]) -> Path:
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
    return path


def bar_json(bar: Bar) -> dict[str, str]:
    return {
        "symbol": bar.symbol, "start": bar.start.isoformat(), "open": str(bar.open),
        "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
        "volume": str(bar.volume),
    }


class Breakout:
    """Tiny test strategy: ENTER when the close breaks the previous bar's high by more than
    `edge`; window [high, high + 0.10], stop = previous low. Not a trading strategy."""

    def __init__(self, version: str = "1", edge: str = "0") -> None:
        self.name = "breakout-test"
        self.version = version
        self.edge = Decimal(edge)

    def evaluate(self, context: SymbolContext) -> Decision:
        bars = context.bars
        if context.position is not None:
            return Decision(self.name, self.version, context.symbol, Verdict.HOLD, ())
        if len(bars) < 2 or bars[-1].close <= bars[-2].high + self.edge:
            check = Check("close_above_high", bars[-1].close, ">",
                          bars[-2].high + self.edge if len(bars) > 1 else None, False)
            return Decision(self.name, self.version, context.symbol, Verdict.IGNORE, (check,))
        high = bars[-2].high
        return Decision(
            self.name, self.version, context.symbol, Verdict.ENTER,
            (Check("close_above_high", bars[-1].close, ">", high + self.edge, True),),
            entry_low=high, entry_high=high + Decimal("0.10"), stop=bars[-2].low,
            setup="breakout", quantity=Decimal(10),
        )


def create(values: Mapping[str, Any]) -> Breakout:
    """Module factory used by the CLI test (`--strategy research_helpers`)."""
    if set(values) != {"strategy"} or set(values["strategy"]) != {"version", "edge"}:
        raise ValueError(f"keys mismatch: {values}")
    return Breakout(values["strategy"]["version"], values["strategy"]["edge"])
