""""What if another strategy version": record a version's decisions over captured bars and diff
them against recorded decisions.

`decide` runs any strategy through the replay engine of `tickr.replay` (same history,
synthetic quote and no-look-ahead rules as a replay; docs/replay.md) and records EVERY decision
it returns as a `DecisionRecord` with role `observer` and admission `not_evaluated` for ENTER.
While the replay holds a simulated position the strategy is asked with that position, as a
replay would, so its HOLD/EXIT decisions are recorded too.

`diff` compares two record sets bar by bar. The key is (symbol, decision bar). When a side has
several records for one key (a live bot may decide more than once per bar), the LAST one in
file order is compared and the count is reported. Only (symbol, session date) pairs present in
the baseline are compared, so history days loaded for warm-up do not show up as differences.
A pair where both sides are IGNORE or absent is not a row; everything else is classified:

- `same`: identical verdict, setup and levels (trigger, invalidation, window, stop, quantity);
- `verdict_changed`: different verdict (a missing side counts as `absent`);
- `levels_changed`: same verdict, different setup or levels.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from tickr.replay.clock import ClockFor
from tickr.replay.engine import M1, replay
from tickr.replay.settings import ReplaySettings
from tickr.research.errors import ResearchError
from tickr.research.records import Admission, DecisionRecord
from tickr.strategy.base import (
    Decision,
    Role,
    SizingLimits,
    Strategy,
    SymbolContext,
    Verdict,
)
from tickr.types import Bar

LEVELS = ("setup", "trigger", "invalidation", "entry_low", "entry_high", "stop", "quantity")
ABSENT = "absent"


class _Recorder:
    def __init__(self, inner: Strategy, sink: list[DecisionRecord]) -> None:
        self.inner = inner
        self.sink = sink
        self.name = inner.name
        self.version = inner.version

    def evaluate(self, context: SymbolContext) -> Decision:
        decision = self.inner.evaluate(context)
        if isinstance(decision, Decision) and isinstance(decision.verdict, Verdict):
            start = context.bars[-1].start
            enter = decision.verdict is Verdict.ENTER
            self.sink.append(
                DecisionRecord(
                    time=start + M1,
                    bar_start=start,
                    role=Role.OBSERVER,
                    decision=decision,
                    admission=Admission.NOT_EVALUATED if enter else Admission.NOT_APPLICABLE,
                )
            )
        return decision  # the replay engine validates the contract and refuses violations


def decide(
    create_strategy: Callable[[], Strategy],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    settings: ReplaySettings,
    clock_for: ClockFor,
    sizing: SizingLimits,
) -> tuple[DecisionRecord, ...]:
    """Every decision of the strategy over the bars, in replay order (symbol, then time),
    sized on the platform's limits (`sizing`)."""
    sink: list[DecisionRecord] = []
    replay(lambda: _Recorder(create_strategy(), sink), bars_by_symbol, settings, clock_for,
           sizing)
    return tuple(sink)


class DiffKind(StrEnum):
    SAME = "same"
    VERDICT_CHANGED = "verdict_changed"
    LEVELS_CHANGED = "levels_changed"


@dataclass(frozen=True)
class DiffRow:
    symbol: str
    bar_start: datetime
    kind: DiffKind
    baseline: DecisionRecord | None
    candidate: DecisionRecord | None
    baseline_count: int
    candidate_count: int
    changed: tuple[str, ...]
    """Names of the fields that differ (`verdict` or LEVELS entries)."""


@dataclass(frozen=True)
class DiffResult:
    baseline: str
    candidate: str
    rows: tuple[DiffRow, ...]
    counts: dict[str, int]


def identity(records: Sequence[DecisionRecord], side: str) -> str:
    """`name vversion` of the single strategy in the records; mixed sets are refused."""
    names = sorted({f"{r.decision.strategy} v{r.decision.version}" for r in records})
    if len(names) != 1:
        raise ResearchError(
            f"{side} must hold decisions of exactly one strategy version, found: "
            f"{', '.join(names) or 'none'} (filter with --{side}-strategy)"
        )
    return names[0]


def select(records: Sequence[DecisionRecord], name: str, version: str | None):
    return tuple(
        r for r in records
        if r.decision.strategy == name and (version is None or r.decision.version == version)
    )


def _last_by_key(records: Sequence[DecisionRecord]):
    last: dict[tuple[str, datetime], DecisionRecord] = {}
    count: dict[tuple[str, datetime], int] = {}
    for r in records:
        key = (r.symbol, r.bar_start)
        last[key] = r
        count[key] = count.get(key, 0) + 1
    return last, count


def _verdict(record: DecisionRecord | None) -> str:
    return ABSENT if record is None else record.decision.verdict.value


def diff(
    baseline: Sequence[DecisionRecord],
    candidate: Sequence[DecisionRecord],
    zone,
) -> DiffResult:
    """Compare per (symbol, decision bar) inside the baseline's (symbol, session date) pairs.

    `zone` is the exchange time zone that defines session dates.
    """
    base_name = identity(baseline, "baseline")
    cand_name = identity(candidate, "candidate")

    def day(r: DecisionRecord) -> tuple[str, date]:
        return (r.symbol, r.bar_start.astimezone(zone).date())

    scope = {day(r) for r in baseline}
    base, base_count = _last_by_key(baseline)
    cand_all = [r for r in candidate if day(r) in scope]
    cand, cand_count = _last_by_key(cand_all)
    rows: list[DiffRow] = []
    counts = {k.value: 0 for k in DiffKind}
    for key in sorted(set(base) | set(cand)):
        b, c = base.get(key), cand.get(key)
        vb, vc = _verdict(b), _verdict(c)
        if {vb, vc} <= {ABSENT, Verdict.IGNORE.value}:
            continue
        if vb != vc:
            kind, changed = DiffKind.VERDICT_CHANGED, ("verdict",)
        else:
            assert b is not None and c is not None
            changed = tuple(
                f for f in LEVELS if getattr(b.decision, f) != getattr(c.decision, f)
            )
            kind = DiffKind.LEVELS_CHANGED if changed else DiffKind.SAME
        counts[kind.value] += 1
        rows.append(
            DiffRow(
                symbol=key[0],
                bar_start=key[1],
                kind=kind,
                baseline=b,
                candidate=c,
                baseline_count=base_count.get(key, 0),
                candidate_count=cand_count.get(key, 0),
                changed=changed,
            )
        )
    return DiffResult(base_name, cand_name, tuple(rows), counts)
