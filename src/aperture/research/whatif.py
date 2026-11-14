""""What if we had entered": simulate ENTER decisions that were not taken. SIMULATED.

Each ENTER record whose admission is not `taken` (refused by the platform, or never evaluated:
observer and replay decisions) is replayed alone through the replay engine of
`aperture.replay` with a scripted strategy that returns exactly that decision on its bar and
IGNORE everywhere else. The fill and exit rules are therefore the replay's rules
(docs/replay.md), with `config/replay.toml` and each day's session clock (bot.toml
`[session]` plus the calendar, as live):

- entry at the TOP of the price window plus slippage, only if a following bar trades inside the
  window within `entry_wait_bars` (otherwise `missed`, never chased);
- exit at the stop (or the open of a bar that gaps through it), or at the open of the first bar
  at or after the flatten time, or at the last bar's close; every exit pays slippage;
- conservative: if the fill bar also reaches the stop, the stop is taken in that bar;
- an ENTER that live admission would refuse on time alone (before the open, completing at or
  after the last entry time, a disabled half day) is `rejected` with the replay's cause.

The result is in R (per share) and in USD for a quantity: the `quantity` argument when given,
otherwise the quantity the strategy recorded. Without either the record is `not_simulated`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from aperture.replay.clock import ClockFor
from aperture.replay.engine import SignalOutcome, SimulatedTrade, replay, split_sessions
from aperture.replay.settings import ReplaySettings
from aperture.research.errors import ResearchError
from aperture.research.records import Admission, DecisionRecord
from aperture.strategy.base import Decision, SymbolContext, Verdict
from aperture.types import Bar

LABEL = "simulated"


class WhatIfOutcome(StrEnum):
    FILLED = "filled"
    MISSED = "missed"
    REJECTED = "rejected"
    NOT_SIMULATED = "not_simulated"


@dataclass(frozen=True)
class WhatIf:
    record: DecisionRecord
    quantity: Decimal | None
    outcome: WhatIfOutcome
    cause: str
    trade: SimulatedTrade | None

    @property
    def r_multiple(self) -> Decimal | None:
        return None if self.trade is None else self.trade.r_multiple

    @property
    def result_usd(self) -> Decimal | None:
        if self.trade is None or self.quantity is None:
            return None
        return (self.trade.exit_price - self.trade.entry_price) * self.quantity

    @property
    def risk_usd(self) -> Decimal | None:
        """Planned loss at the stop from the simulated entry price, for the quantity."""
        if self.trade is None or self.quantity is None:
            return None
        return self.trade.risk_per_share * self.quantity


class _OneDecision:
    """Scripted strategy: the recorded decision on its bar, IGNORE on every other bar."""

    def __init__(self, record: DecisionRecord) -> None:
        self.record = record
        self.name = record.decision.strategy
        self.version = record.decision.version

    def evaluate(self, context: SymbolContext) -> Decision:
        if context.bars[-1].start == self.record.bar_start:
            return self.record.decision
        return Decision(self.name, self.version, context.symbol, Verdict.IGNORE, ())


def candidates(records: Sequence[DecisionRecord]) -> tuple[DecisionRecord, ...]:
    """ENTER records that did not become a position."""
    return tuple(
        r
        for r in records
        if r.decision.verdict is Verdict.ENTER and r.admission is not Admission.TAKEN
    )


def _quantity(record: DecisionRecord, override: Decimal | None) -> tuple[Decimal | None, str]:
    quantity = override if override is not None else record.decision.quantity
    if quantity is None:
        return None, "no quantity: none recorded and none given"
    if quantity < 1 or quantity != quantity.to_integral_value():
        return None, f"quantity {quantity} is not a whole number of shares >= 1"
    return quantity, ""


def simulate_one(
    record: DecisionRecord,
    bars: Sequence[Bar],
    settings: ReplaySettings,
    clock_for: ClockFor,
    quantity: Decimal | None = None,
) -> WhatIf:
    def skip(cause: str, qty: Decimal | None = None) -> WhatIf:
        return WhatIf(record, qty, WhatIfOutcome.NOT_SIMULATED, cause, None)

    qty, cause = _quantity(record, quantity)
    if qty is None:
        return skip(cause)
    session = next(
        (day_bars for _, day_bars in split_sessions(bars)
         if any(b.start == record.bar_start for b in day_bars)),
        None,
    )
    if session is None:
        return skip("decision bar not in bars", qty)
    result = replay(
        lambda: _OneDecision(record), {record.symbol: session}, settings, clock_for
    )
    if len(result.signals) != 1 or len(result.trades) > 1:
        raise ResearchError(
            f"{record.symbol} {record.bar_start.isoformat()}: replay produced "
            f"{len(result.signals)} signals and {len(result.trades)} trades for one decision"
        )
    (signal,) = result.signals
    trade = result.trades[0] if result.trades else None
    if signal.outcome is SignalOutcome.FILLED and trade is not None:
        return WhatIf(record, qty, WhatIfOutcome.FILLED, "", trade)
    if signal.outcome is SignalOutcome.MISSED:
        return WhatIf(record, qty, WhatIfOutcome.MISSED, signal.cause, None)
    if signal.outcome is SignalOutcome.REJECTED:
        return WhatIf(record, qty, WhatIfOutcome.REJECTED, signal.cause, None)
    raise ResearchError(
        f"{record.symbol} {record.bar_start.isoformat()}: unexpected replay outcome "
        f"{signal.outcome.value} ({signal.cause})"
    )


def what_if(
    records: Sequence[DecisionRecord],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    settings: ReplaySettings,
    clock_for: ClockFor,
    quantity: Decimal | None = None,
) -> tuple[WhatIf, ...]:
    """Simulate every ENTER that was not taken, independently of the others."""
    if quantity is not None and (quantity < 1 or quantity != quantity.to_integral_value()):
        raise ResearchError(f"quantity {quantity} is not a whole number of shares >= 1")
    out = []
    for record in candidates(records):
        bars = bars_by_symbol.get(record.symbol)
        if not bars:
            out.append(WhatIf(record, None, WhatIfOutcome.NOT_SIMULATED, "no bars for the symbol",
                              None))
            continue
        out.append(simulate_one(record, bars, settings, clock_for, quantity))
    return tuple(out)


@dataclass(frozen=True)
class WhatIfSummary:
    decisions: int
    by_outcome: dict[str, int]
    by_admission: dict[str, int]
    trades: int
    wins: int
    losses: int
    total_r: Decimal
    average_r: Decimal | None
    total_usd: Decimal
    exits_by_reason: dict[str, int]


def summarize(rows: Sequence[WhatIf]) -> WhatIfSummary:
    by_outcome = {o.value: 0 for o in WhatIfOutcome}
    by_admission: dict[str, int] = {}
    reasons: dict[str, int] = {}
    r_values: list[Decimal] = []
    usd = Decimal(0)
    for row in rows:
        by_outcome[row.outcome.value] += 1
        key = row.record.admission.value
        by_admission[key] = by_admission.get(key, 0) + 1
        if row.trade is not None:
            r_values.append(row.trade.r_multiple)
            reason = row.trade.exit_reason.value
            reasons[reason] = reasons.get(reason, 0) + 1
            if row.result_usd is not None:
                usd += row.result_usd
    total_r = sum(r_values, Decimal(0))
    return WhatIfSummary(
        decisions=len(rows),
        by_outcome=by_outcome,
        by_admission=dict(sorted(by_admission.items())),
        trades=len(r_values),
        wins=sum(1 for r in r_values if r > 0),
        losses=sum(1 for r in r_values if r < 0),
        total_r=total_r,
        average_r=total_r / len(r_values) if r_values else None,
        total_usd=usd,
        exits_by_reason=dict(sorted(reasons.items())),
    )
