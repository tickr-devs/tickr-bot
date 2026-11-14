"""Evidence bundle for the advisor: what the recorded days say about one strategy version.

Everything is computed by the research tools from recorded decisions and captured bars:
- verdict counts and, for non-entries, which check failed first (the binding constraint);
- why admission refused ENTERs;
- price moves after WATCH and ENTER (observed, research.outcomes summary);
- ENTERs that were not taken, simulated (research.whatif summary, R and gross USD);
- observer versions against the active one (research.observer_report).
Only aggregates go in: no raw rows, so the bundle stays small and nothing is cut.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any

from aperture.advisor.errors import AdvisorError
from aperture.replay.clock import ClockFor
from aperture.replay.settings import ReplaySettings
from aperture.research import observer_report
from aperture.research.outcomes import CloseFor, outcomes
from aperture.research.outcomes import summarize as summarize_outcomes
from aperture.research.records import Admission, DecisionRecord
from aperture.research.settings import ResearchSettings
from aperture.research.whatif import summarize as summarize_whatif
from aperture.research.whatif import what_if
from aperture.strategy.base import Role, Verdict
from aperture.types import Bar


def _plain(value: Any, places: int) -> Any:
    if isinstance(value, Decimal):
        return str(round(value, places))
    if isinstance(value, dict):
        return {k: _plain(v, places) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v, places) for v in value]
    return value


def build_evidence(
    *,
    days: Sequence[date],
    records: Sequence[DecisionRecord],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    strategy: str,
    version: str,
    research: ResearchSettings,
    close_for: CloseFor,
    replay: ReplaySettings,
    clock_for: ClockFor,
) -> dict[str, Any]:
    active = [
        r for r in records
        if r.role is Role.ACTIVE
        and (r.decision.strategy, r.decision.version) == (strategy, version)
    ]
    if not active:
        raise AdvisorError(f"no recorded active decisions of {strategy} v{version} in these days")
    verdicts = Counter(r.decision.verdict.value for r in active)
    first_failed = Counter(
        r.decision.decisive.rule
        for r in active
        if r.decision.verdict in (Verdict.IGNORE, Verdict.WATCH) and r.decision.decisive
    )
    refusals = Counter(r.refusal_cause for r in active if r.admission is Admission.REFUSED)
    places = research.output_decimal_places
    observed = summarize_outcomes(outcomes(active, bars_by_symbol, research, close_for))
    simulated = summarize_whatif(what_if(active, bars_by_symbol, replay, clock_for))
    versions = observer_report.report(records, bars_by_symbol, replay, clock_for)
    bundle = {
        "strategy": f"{strategy} v{version}",
        "days": [d.isoformat() for d in days],
        "decisions": len(active),
        "symbols": len({r.symbol for r in active}),
        "verdicts": dict(sorted(verdicts.items())),
        "first_failed_check_on_ignore_and_watch": dict(first_failed.most_common()),
        "enter_refusal_causes": dict(refusals.most_common()),
        "price_moves_after_decisions_observed": [asdict(s) for s in observed],
        "not_taken_enters_simulated": asdict(simulated),
        "versions_compared_simulated": [asdict(r) for r in versions.identities],
        "notes": [
            "price moves are observed captured prices, never profit",
            "simulated results use the replay fill model (window top + slippage, stop first, "
            "gross USD without commissions) and prove nothing about real fills",
        ],
    }
    return _plain(bundle, places)
