"""Per-day, per-setup refusal table: which check refused first, which platform causes refused
ENTERs, and how ENTERs ended, counted from recorded decisions (docs/research.md).

One row per (session date, strategy, version, role, setup, kind, name) with its count:

- kind `first_failed_check`: IGNORE/WATCH decisions whose first failed check is `name`
  (the same count the advisor evidence uses);
- kind `platform_refusal`: ENTERs refused at admission with `refusal_cause` = `name`;
- kind `enter`: ENTERs by admission `name` (`taken`, `refused`, `not_evaluated`).

`setup` is empty when the decision named none (for example no setup armed).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from tickr.research.records import Admission, DecisionRecord
from tickr.strategy.base import Verdict

KINDS = ("first_failed_check", "platform_refusal", "enter")


@dataclass(frozen=True)
class RefusalRow:
    day: date
    strategy: str
    version: str
    role: str
    setup: str | None
    kind: str
    name: str
    count: int


def _entries(record: DecisionRecord) -> list[tuple[str, str]]:
    d = record.decision
    if d.verdict is Verdict.ENTER:
        out = [("enter", record.admission.value)]
        if record.admission is Admission.REFUSED and record.refusal_cause is not None:
            out.append(("platform_refusal", record.refusal_cause))
        return out
    if d.verdict in (Verdict.IGNORE, Verdict.WATCH) and d.decisive is not None:
        return [("first_failed_check", d.decisive.rule)]
    return []


def table(records: Sequence[DecisionRecord], zone: ZoneInfo) -> tuple[RefusalRow, ...]:
    counts: Counter[tuple[date, str, str, str, str | None, str, str]] = Counter()
    for r in records:
        d = r.decision
        day = r.bar_start.astimezone(zone).date()
        for kind, name in _entries(r):
            counts[(day, d.strategy, d.version, r.role.value, d.setup, kind, name)] += 1

    def order(item: tuple[tuple, int]) -> tuple:
        (day, strategy, version, role, setup, kind, name), count = item
        return (day, strategy, version, role, setup or "", KINDS.index(kind), -count, name)

    return tuple(RefusalRow(*key, count) for key, count in sorted(counts.items(), key=order))
