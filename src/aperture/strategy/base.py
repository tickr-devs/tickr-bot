"""Strategy interface.

A strategy is a pure analysis module: it receives completed bars and quotes for one symbol
and returns a Decision with the checks that produced it. It never sends orders, never reads
the clock, and never loosens platform risk. Several instances (one `active`, any number of
`observer`) may run on the same data; only the active one reaches the safety layer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from aperture.types import Bar, Quote


class Verdict(StrEnum):
    IGNORE = "IGNORE"
    WATCH = "WATCH"
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"


class Role(StrEnum):
    ACTIVE = "active"
    OBSERVER = "observer"


@dataclass(frozen=True)
class Check:
    """One evaluated rule: what was measured against which threshold."""

    rule: str
    measured: Decimal | None
    operator: str
    threshold: Decimal | None
    passed: bool
    note: str = ""


@dataclass(frozen=True)
class Decision:
    strategy: str
    version: str
    symbol: str
    verdict: Verdict
    checks: tuple[Check, ...]
    trigger: Decimal | None = None
    invalidation: Decimal | None = None
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    stop: Decimal | None = None
    setup: str | None = None
    quantity: Decimal | None = None
    """Whole shares the strategy sized for (set on ENTER); the platform checks it against
    its own limits and refuses on breach."""

    @property
    def decisive(self) -> Check | None:
        """The first failed check, or None if all passed."""
        return next((c for c in self.checks if not c.passed), None)


@dataclass(frozen=True)
class PositionView:
    """What a strategy may know about an open position in the symbol."""

    entry_price: Decimal
    quantity: Decimal
    stop: Decimal


@dataclass(frozen=True)
class SymbolContext:
    """Inputs for one evaluation. `bars` are completed M1 bars of the current NY session
    plus history, oldest first; the forming bar is never included."""

    symbol: str
    bars: Sequence[Bar]
    prior_sessions: Sequence[Sequence[Bar]] = field(default_factory=tuple)
    quote: Quote | None = None
    position: PositionView | None = None


class Strategy(Protocol):
    name: str
    version: str
    prior_sessions_needed: int
    """How many completed prior sessions of M1 bars `evaluate` needs in
    `SymbolContext.prior_sessions`. The platform refuses to run a strategy that needs more
    than `[market_data] history_sessions` provides; 0 = no history needed."""

    def evaluate(self, context: SymbolContext) -> Decision: ...


StrategyFactory = Callable[[Mapping[str, Any]], Strategy]
"""Each strategy module exposes `create(values) -> Strategy`, where `values` is the parsed
`strategies/<name>/strategy.toml`. Unknown or missing keys must raise ValueError."""
