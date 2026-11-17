"""Strategy interface.

A strategy is a pure analysis module: it receives completed bars and quotes for one symbol
and returns a Decision with the checks that produced it. It never sends orders, never reads
the clock, and never loosens platform risk. Several instances (one `active`, any number of
`observer`) may run on the same data; only the active one reaches the safety layer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tickr.types import Bar, Quote

if TYPE_CHECKING:
    from tickr.data.movers import Mover


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
    its own limits and refuses on breach. On EXIT: None closes the whole position; a whole
    number below the position sells only that many at market (a scale-out, once per trade)
    and keeps the rest under its stop."""
    scale_out: Decimal | None = None
    """ENTER only, optional: the whole shares (below `quantity`) the strategy plans to sell
    first, by a later EXIT with `quantity`. The platform places stops so that exactly these
    shares can be released while the rest stays protected."""
    rest_bars: int | None = None
    """ENTER only. None: a marketable entry, admitted only while the ask is inside
    entry_low..entry_high and cancelled as soon as it leaves. A whole number N >= 1: a resting
    limit buy at entry_high (a pullback entry), which may be below the ask; it works for the
    N bars after the decision bar and is cancelled then, at the last entry time, or once the
    ask falls below entry_low. The platform refuses N above `[risk] max_entry_rest_bars`."""

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
    entry_quantity: Decimal | None = None
    """Shares bought in all (None: not known, e.g. replay); below it, a part was sold."""
    filled_at: datetime | None = None
    """When the first fill of the entry was seen (None: not known): bars from then on are
    the position's own, e.g. for the highest high since entry."""


@dataclass(frozen=True)
class SymbolContext:
    """Inputs for one evaluation. `bars` are completed M1 bars of the current NY session
    plus history, oldest first; the forming bar is never included."""

    symbol: str
    bars: Sequence[Bar]
    prior_sessions: Sequence[Sequence[Bar]] = field(default_factory=tuple)
    quote: Quote | None = None
    position: PositionView | None = None
    loss_budget_usd: Decimal | None = None
    """Most a new trade may plan to lose today: the room the platform's daily loss ceiling
    leaves (owner decision 2026-11-14: near the ceiling, size down to fit). None = no
    platform cap (replay, research). A strategy sizes on the smaller of this and its own
    planned loss; the platform still checks the ceiling on admission."""
    session_open: datetime | None = None
    """The regular open of the current session when the platform proved that `bars` hold every
    bar of the session from it (its read reached back that far); None = not proven. Minutes
    without trades have no bar, so the first session bar may start after the open."""


class Liquidity(Protocol):
    """A strategy's liquidity rules, which discovery applies to the active strategy's
    candidates (owner decision 2026-11-15: the strategy owns every liquidity threshold; the
    platform keeps only the universe and quote validity). Each method returns its checks in
    order, and discovery refuses a symbol on the first failed one, with that check as the
    cause. `evaluate` applies the same rules again, so a strategy run as an observer, in
    replay or in research keeps them too."""

    name: str
    version: str

    def quote_liquidity(self, quote: Quote) -> tuple[Check, ...]:
        """Rules on one valid snapshot quote (0 < bid <= ask, fresh), such as the spread."""
        ...

    def session_liquidity(self, session_bars: Sequence[Bar]) -> tuple[Check, ...]:
        """Rules on the current regular session's completed M1 bars, oldest first (at least
        one), such as the session dollar volume."""
        ...


@runtime_checkable
class FastMovers(Protocol):
    """A strategy that defines fast movers (`tickr.data.movers` with its own thresholds).
    Discovery records the active strategy's fast movers each pass for the apps; a strategy
    without this has none, and the list says so."""

    def fast_mover(self, symbol: str, session_bars: Sequence[Bar]) -> Mover | None:
        """The fast mover of the current session's completed bars (oldest first), or None."""
        ...


class Strategy(Liquidity, Protocol):
    prior_sessions_needed: int
    """How many completed prior sessions of M1 bars `evaluate` needs in
    `SymbolContext.prior_sessions`. The platform refuses to run a strategy that needs more
    than `[market_data] history_sessions` provides; 0 = no history needed."""

    def evaluate(self, context: SymbolContext) -> Decision: ...


StrategyFactory = Callable[[Mapping[str, Any]], Strategy]
"""Each strategy module exposes `create(values) -> Strategy`, where `values` is the parsed
`strategies/<name>/strategy.toml`. Unknown or missing keys must raise ValueError."""


def usable_quote(quote: Quote | None, at: datetime, max_age: timedelta,
                  max_skew: timedelta) -> Quote | None:
    """The quote when entry admission would accept its times at `at`: aware provider and
    receipt times, received no later than `at`, provider time at most `max_skew` ahead and at
    most `max_age` old. Otherwise None. The platform passes only such a quote ([risk]
    max_quote_age_seconds and max_clock_skew_seconds) to observers and to the active strategy
    deciding a held position, so no exit rests on an old quote."""
    if quote is None:
        return None
    times = (quote.provider_time, quote.receipt_time)
    if any(t.tzinfo is None or t.utcoffset() is None for t in times):
        return None
    if quote.receipt_time > at:
        return None
    age = at - quote.provider_time
    return quote if -max_skew <= age <= max_age else None
