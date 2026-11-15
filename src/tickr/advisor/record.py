"""advisor-run-v1 `run.json`: the outcome of one advisor run for the apps (GET /advisor).

Names and codes are in tickr.advisor_files, which the API reads without importing this
package.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Any

from tickr.advisor.proposal import Proposal
from tickr.advisor_files import RUN_SCHEMA


def literal(value: Any) -> str:
    """A strategy.toml value as the text the owner reads (JSON spelling of the TOML value)."""
    if isinstance(value, date | time | datetime):  # TOML dates and times
        return value.isoformat()
    return json.dumps(value)


def run_record(*, status: str, cause: str, proposal: Proposal | None, new_identity: str | None,
               model: str, base: str, folder: str, tuning: Any, now: datetime) -> dict[str, Any]:
    """`tuning` is advisor.run.Tuning (not imported: run imports this module)."""
    idea = proposal.idea if proposal is not None else None
    return {
        "schema": RUN_SCHEMA,
        "at": now.isoformat(),
        "mode": "backtest" if tuning.backtest else "live",
        "status": status,
        "base": base,
        "base_folder": folder,
        "proposed": new_identity,
        "model": model,
        "evidence": tuning.label,
        "tuned_on": tuning.tuned_on,
        "judge_on": tuning.judge_on,
        "summary": proposal.summary if proposal is not None else "",
        "changes": [
            {"section": c.section, "key": c.key, "now": literal(c.old), "proposed": c.new_literal,
             "reason": c.reason, "evidence": c.evidence}
            for c in (proposal.changes if proposal is not None else ())
        ],
        "expected_effect": proposal.expected_effect if proposal is not None else "",
        "risks": list(proposal.risks) if proposal is not None else [],
        "no_change_reason": proposal.no_change_reason if proposal is not None else "",
        "cause": cause,
        "idea": None if idea is None else {
            "name": idea.name, "hypothesis": idea.hypothesis, "entry_rule": idea.entry_rule,
            "exit_rule": idea.exit_rule, "filters": idea.filters, "evidence": idea.evidence,
            "test_plan": idea.test_plan,
        },
    }
